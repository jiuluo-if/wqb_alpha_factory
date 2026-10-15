import contextlib
import io
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import main as main_entry
from tests.test_targeted_ci import critical_contract
from wqb_agent.cli import CLICommand, parse_cli
from wqb_agent.config import normalize_config
from wqb_agent.smoke import run_readonly_smoke


class TestCanonicalCliGrammar(unittest.TestCase):
    def assert_command(self, argv, *, domain, action, **expected):
        command = parse_cli(argv)
        self.assertIsInstance(command, CLICommand)
        self.assertEqual((command.domain, command.action), (domain, action))
        for name, value in expected.items():
            self.assertEqual(getattr(command, name), value, name)

    def test_public_commands(self):
        self.assert_command(["datasets"], domain="research", action="list-datasets")
        for action in ("doctor", "audit"):
            self.assert_command(["diagnostics", action, "--offline"],
                                domain="diagnostics", action=action, offline=True)
        self.assert_command(["smoke"], domain="smoke", action="readonly")
        self.assert_command(["diagnostics", "platform"], domain="diagnostics", action="platform")
        self.assert_command(["alpha", "sync-colors", "--dry-run", "--plan", "plan.json"],
                            domain="alpha", action="sync-colors", dry_run=True,
                            color_plan="plan.json")
        self.assert_command(["alpha", "sync-feed"], domain="alpha", action="sync-feed")

    def test_global_options_are_canonical_fields(self):
        command = parse_cli(["--config", "custom.json", "--state-dir", "state",
                             "diagnostics", "doctor"])
        self.assertEqual(command.config, "custom.json")
        self.assertEqual(command.state_dir, "state")

    def test_removed_commands_and_options_are_rejected(self):
        for argv in (["state", "doctor"], ["context"], ["suggest"], ["--doctor"],
                     ["--agent-context"], ["factory", "run"]):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    parse_cli(argv)
            self.assertEqual(raised.exception.code, 2)

    def test_command_specific_options_are_rejected_elsewhere(self):
        for argv in (["datasets", "--force-new-round"], ["alpha", "sync-feed", "--dry-run"]):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    parse_cli(argv)
            self.assertEqual(raised.exception.code, 2)

    def test_color_sync_requires_reviewed_plan(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                parse_cli(["alpha", "sync-colors", "--dry-run"])
        self.assertEqual(raised.exception.code, 2)


class TestCliRuntimeSafety(unittest.TestCase):
    def test_sync_colors_is_independent_remote_metadata_write(self):
        changes = [{"alpha_id": "a1", "action": "DRY_RUN_PATCH"}]
        with patch.object(main_entry, "acquire_single_instance_lock", return_value="lock") as acquire, \
                patch.object(main_entry, "release_single_instance_lock") as release, \
                patch("wqb_agent.WQBClient"), \
                patch.object(main_entry, "load_color_plan", return_value=[{"alpha_id": "a1"}]), \
                patch("wqb_agent.research_api.sync_alpha_colors", return_value=changes) as sync:
            with contextlib.redirect_stdout(io.StringIO()) as output:
                main_entry.main(["--config", "config.example.json", "--state-dir", "tests/fixtures",
                                 "alpha", "sync-colors", "--dry-run", "--plan", "plan.json"])
        acquire.assert_called_once_with("tests/fixtures", operation="sync-alpha-colors")
        release.assert_called_once_with("lock")
        sync.assert_called_once()
        self.assertEqual(sync.call_args.kwargs["exact_plan"], [{"alpha_id": "a1"}])
        payload = json.loads(output.getvalue())
        self.assertTrue(payload["dry_run"])
        self.assertFalse(payload["network_write"])

    def test_local_diagnostics_do_not_construct_client(self):
        for action in ("doctor", "audit"):
            with self.subTest(action=action), tempfile.TemporaryDirectory() as state_dir:
                with patch(
                    "wqb_agent.WQBClient",
                    side_effect=AssertionError("client constructed"),
                ):
                    with contextlib.redirect_stdout(io.StringIO()):
                        main_entry.main([
                            "--config", "config.example.json",
                            "--state-dir", state_dir, "diagnostics", action,
                        ])

    def test_dataset_list_returns_raw_rows_without_selection_or_lock(self):
        with patch("main.acquire_single_instance_lock") as acquire, \
                patch("wqb_agent.WQBClient"), \
                patch("wqb_agent.research_api.list_datasets", return_value={
                    "datasets": [{"id": "synthetic-dataset"}],
                }) as list_datasets, \
                patch.object(main_entry, "load_config", return_value={"simulation": {}, "runtime": {}}):
            with contextlib.redirect_stdout(io.StringIO()):
                main_entry.main(["datasets"])
        acquire.assert_not_called()
        list_datasets.assert_called_once()

    def test_audit_failure_exits_two(self):
        with patch.object(main_entry, "load_config", return_value={"simulation": {}, "runtime": {}}), \
                patch("wqb_agent.audit.audit_execution_surface",
                      return_value={"ok": False, "errors": ["blocked"]}):
            with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as raised:
                main_entry.main(["diagnostics", "audit"])
        self.assertEqual(raised.exception.code, 2)

    def test_smoke_runtime_failure_is_unavailable(self):
        with patch.object(main_entry, "load_config", return_value={"simulation": {}, "runtime": {}}), \
                patch("wqb_agent.WQBClient", side_effect=RuntimeError("credentials unavailable")):
            with contextlib.redirect_stdout(io.StringIO()) as output, self.assertRaises(SystemExit) as raised:
                main_entry.main(["smoke"])
        self.assertEqual(raised.exception.code, 1)
        self.assertEqual(json.loads(output.getvalue())["status"], "UNAVAILABLE")

    def test_platform_diagnostics_is_read_only_preflight(self):
        with patch.object(main_entry, "load_config", return_value={"simulation": {}, "runtime": {}}), \
                patch("wqb_agent.WQBClient") as client_type, \
                patch("wqb_agent.research_api.get_live_preflight", return_value={
                    "network_write": False, "multi_child_range": "2..10",
                }) as preflight:
            with contextlib.redirect_stdout(io.StringIO()) as output:
                main_entry.main(["diagnostics", "platform"])
        preflight.assert_called_once()
        self.assertFalse(json.loads(output.getvalue())["network_write"])


class TestCliSubprocessIntegration(unittest.TestCase):
    root = pathlib.Path(__file__).resolve().parents[1]

    def run_cli(self, *argv):
        return subprocess.run([sys.executable, "main.py", *argv], cwd=self.root,
                              capture_output=True, text=True, check=False)

    def test_root_help_exposes_remote_first_commands(self):
        result = self.run_cli("--help")
        self.assertEqual(result.returncode, 0)
        self.assertIn("diagnostics", result.stdout)
        self.assertNotIn("factory", result.stdout)
        self.assertNotIn("\n    state ", result.stdout)

    def test_diagnostics_doctor_is_machine_readable(self):
        result = self.run_cli("--state-dir", "tests/fixtures", "diagnostics", "doctor")
        self.assertEqual(result.returncode, 0)
        self.assertTrue(json.loads(result.stdout)["config_valid"])
        self.assertEqual(result.stderr, "")

    def test_diagnostics_doctor_exits_nonzero_when_guard_is_unavailable(self):
        with tempfile.TemporaryDirectory() as state_dir:
            guard_path = pathlib.Path(state_dir) / "execution_guard.json"
            guard_path.write_text('{"entries": [', encoding="utf-8")

            result = self.run_cli(
                "--state-dir", state_dir, "diagnostics", "doctor",
            )

        payload = json.loads(result.stdout)
        self.assertEqual(result.returncode, 2)
        self.assertIn(
            "EXECUTION_GUARD_UNAVAILABLE",
            {row["code"] for row in payload["diagnostics"]},
        )


class ReadOnlyClient:
    def __init__(self):
        self.calls = []

    def get_datasets(self):
        self.calls.append("datasets")
        return [{"id": "fallback"}]

    def get_datafields(self, dataset_id, limit=50, offset=0, field_type=None):
        self.calls.append(("fields", dataset_id))
        return [{"id": "close"}], 1


class TestReadOnlySmoke(unittest.TestCase):
    @critical_contract("smoke.read_only_methods_only")
    def test_smoke_uses_only_read_methods(self):
        client = ReadOnlyClient()
        result = run_readonly_smoke(
            client,
            normalize_config({"simulation": {}, "runtime": {"smoke_dataset": "pv1"}}),
        )
        self.assertEqual(result["datasets"]["status"], "PASS")
        self.assertEqual(result["fields"]["status"], "PASS")
        self.assertEqual(client.calls, ["datasets", ("fields", "pv1")])

    @critical_contract("smoke.unavailable_read_capability_degrades")
    def test_smoke_degrades_without_read_capability(self):
        result = run_readonly_smoke(
            object(), normalize_config({"simulation": {}, "runtime": {}})
        )
        self.assertEqual(result["datasets"]["status"], "UNAVAILABLE")
        self.assertEqual(result["fields"]["status"], "UNAVAILABLE")

    @critical_contract("smoke.raw_config_rejected")
    def test_smoke_rejects_raw_config_input(self):
        client = ReadOnlyClient()
        with self.assertRaises(TypeError):
            run_readonly_smoke(client, {"runtime": {}})
        self.assertEqual(client.calls, [])



if __name__ == "__main__":
    unittest.main()
