import contextlib
import io
import json
import pathlib
import subprocess
import sys
import unittest
from unittest.mock import patch

import main as main_entry
from wqb_agent.cli import CLICommand, parse_cli


class TestCanonicalCliGrammar(unittest.TestCase):
    def assert_command(self, argv, *, domain, action, **expected):
        command = parse_cli(argv)
        self.assertIsInstance(command, CLICommand)
        self.assertEqual((command.domain, command.action), (domain, action))
        for name, value in expected.items():
            self.assertEqual(getattr(command, name), value, name)

    def test_public_commands(self):
        self.assert_command(["suggest"], domain="research", action="suggest")
        for action in ("doctor", "audit"):
            self.assert_command(["diagnostics", action, "--offline"],
                                domain="diagnostics", action=action, offline=True)
        self.assert_command(["smoke"], domain="smoke", action="readonly")
        self.assert_command(["alpha", "sync-colors", "--dry-run"],
                            domain="alpha", action="sync-colors", dry_run=True)
        self.assert_command(["alpha", "sync-feed"], domain="alpha", action="sync-feed")

    def test_global_options_are_canonical_fields(self):
        command = parse_cli(["--config", "custom.json", "--state-dir", "state",
                             "diagnostics", "doctor"])
        self.assertEqual(command.config, "custom.json")
        self.assertEqual(command.state_dir, "state")

    def test_removed_commands_and_options_are_rejected(self):
        for argv in (["state", "doctor"], ["context"], ["--doctor"],
                     ["--agent-context"], ["factory", "run"]):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    parse_cli(argv)
            self.assertEqual(raised.exception.code, 2)

    def test_command_specific_options_are_rejected_elsewhere(self):
        for argv in (["suggest", "--force-new-round"], ["alpha", "sync-feed", "--dry-run"]):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    parse_cli(argv)
            self.assertEqual(raised.exception.code, 2)


class TestCliRuntimeSafety(unittest.TestCase):
    def test_sync_colors_is_independent_remote_metadata_write(self):
        changes = [{"alpha_id": "a1", "action": "DRY_RUN_PATCH"}]
        with patch.object(main_entry, "acquire_single_instance_lock", return_value="lock") as acquire, \
                patch.object(main_entry, "release_single_instance_lock") as release, \
                patch("wqb_agent.WQBClient"), \
                patch("wqb_agent.research_api.refresh_remote_alphas", return_value={}), \
                patch("wqb_agent.research_api.list_remote_alphas", return_value=[{"alpha_id": "a1"}]), \
                patch("wqb_agent.research_api.sync_alpha_colors", return_value=changes) as sync:
            with contextlib.redirect_stdout(io.StringIO()) as output:
                main_entry.main(["--config", "config.example.json", "--state-dir", "tests/fixtures",
                                 "alpha", "sync-colors", "--dry-run"])
        acquire.assert_called_once_with("tests/fixtures", operation="sync-alpha-colors")
        release.assert_called_once_with("lock")
        sync.assert_called_once()
        payload = json.loads(output.getvalue())
        self.assertTrue(payload["dry_run"])
        self.assertFalse(payload["network_write"])

    def test_local_diagnostics_do_not_construct_client(self):
        for action in ("doctor", "audit"):
            with self.subTest(action=action), patch(
                    "wqb_agent.WQBClient", side_effect=AssertionError("client constructed")):
                with contextlib.redirect_stdout(io.StringIO()):
                    main_entry.main(["--config", "config.example.json", "diagnostics", action])

    def test_suggest_does_not_acquire_simulation_owner_lock(self):
        with patch("main.acquire_single_instance_lock") as acquire, \
                patch("wqb_agent.WQBClient"), \
                patch("wqb_agent.research_api.discover_fields", return_value={"fields": []}) as discover, \
                patch.object(main_entry, "load_config", return_value={"simulation": {}, "agent": {}}):
            with contextlib.redirect_stdout(io.StringIO()):
                main_entry.main(["suggest"])
        acquire.assert_not_called()
        discover.assert_called_once()

    def test_audit_failure_exits_two(self):
        with patch.object(main_entry, "load_config", return_value={"simulation": {}, "agent": {}}), \
                patch("wqb_agent.audit.audit_execution_surface",
                      return_value={"ok": False, "errors": ["blocked"]}):
            with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as raised:
                main_entry.main(["diagnostics", "audit"])
        self.assertEqual(raised.exception.code, 2)

    def test_smoke_runtime_failure_is_unavailable(self):
        with patch.object(main_entry, "load_config", return_value={"simulation": {}, "agent": {}}), \
                patch("wqb_agent.WQBClient", side_effect=RuntimeError("credentials unavailable")):
            with contextlib.redirect_stdout(io.StringIO()) as output, self.assertRaises(SystemExit) as raised:
                main_entry.main(["smoke"])
        self.assertEqual(raised.exception.code, 1)
        self.assertEqual(json.loads(output.getvalue())["status"], "UNAVAILABLE")


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


if __name__ == "__main__":
    unittest.main()
