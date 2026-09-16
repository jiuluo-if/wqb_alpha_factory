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
        return command

    def test_top_level_research_commands(self):
        self.assert_command(
            ["suggest"], domain="research", action="suggest"
        )

    def test_state_context_and_smoke_commands(self):
        for action in ("doctor", "audit", "preflight"):
            self.assert_command(
                ["state", action, "--offline"],
                domain="state",
                action=action,
                offline=True,
            )
        self.assert_command(
            ["context", "--compact", "--json", "--task", "state-recovery"],
            domain="context",
            action="show",
            compact=True,
            json_output=True,
            task="state-recovery",
        )
        self.assert_command(["smoke"], domain="smoke", action="readonly")

    def test_alpha_commands(self):
        self.assert_command(
            ["alpha", "sync-colors", "--dry-run"],
            domain="alpha",
            action="sync-colors",
            dry_run=True,
        )
        self.assert_command(
            ["alpha", "sync-feed"], domain="alpha", action="sync-feed"
        )

    def test_global_options_are_canonical_fields(self):
        command = self.assert_command(
            [
                "--config",
                "custom.json",
                "--state-dir",
                "state",
                "state",
                "doctor",
            ],
            domain="state",
            action="doctor",
            config="custom.json",
            state_dir="state",
        )
        self.assertFalse(command.legacy)

    def test_command_specific_options_are_rejected_elsewhere(self):
        invalid = (
            ["factory", "stop", "--hours", "1"],
            ["recovery", "skip-stale", "1", "x"],
            ["suggest", "--force-new-round"],
            ["context", "--hours", "1"],
            ["alpha", "sync-feed", "--dry-run"],
        )
        for argv in invalid:
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    parse_cli(argv)
            self.assertEqual(raised.exception.code, 2)

    def test_offline_is_only_a_readonly_compatibility_option(self):
        for argv in (
            ["state", "doctor", "--offline"],
            ["state", "audit", "--offline"],
            ["state", "preflight", "--offline"],
            ["context", "--offline"],
        ):
            self.assertTrue(parse_cli(argv).offline)
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                parse_cli(["--factory-run", "--offline"])
        self.assertEqual(raised.exception.code, 2)


class TestLegacyCliCanonicalization(unittest.TestCase):
    def test_representative_legacy_commands_map_to_canonical_commands(self):
        cases = (
            (["--suggest"], ("research", "suggest"), {}),
            (
                ["--doctor", "--offline"],
                ("state", "doctor"),
                {"offline": True},
            ),
            (
                ["--agent-context", "--compact", "--json"],
                ("context", "show"),
                {"compact": True, "json_output": True},
            ),
            (
                ["--sync-alpha-colors", "--dry-run"],
                ("alpha", "sync-colors"),
                {"dry_run": True},
            ),
        )
        for argv, key, expected in cases:
            with self.subTest(argv=argv), patch("sys.stderr", new_callable=io.StringIO):
                command = parse_cli(argv)
            self.assertEqual((command.domain, command.action), key)
            self.assertTrue(command.legacy)
            for name, value in expected.items():
                self.assertEqual(getattr(command, name), value)

    def test_invalid_legacy_combinations_remain_rejected(self):
        invalid = (
            ["--offline", "--factory-run"],
            ["--dry-run", "--sync-alpha-feed"],
            ["--factory-run"],
            ["--compact"],
        )
        for argv in invalid:
            with self.subTest(argv=argv), patch("sys.stderr", new_callable=io.StringIO):
                with self.assertRaises(SystemExit) as raised:
                    parse_cli(argv)
            self.assertEqual(raised.exception.code, 2)


class TestCliRuntimeSafety(unittest.TestCase):
    def test_sync_colors_dispatches_to_color_workflow_without_agent(self):
        changes = [{
            "alpha_id": "a1",
            "old_color": None,
            "new_color": "GREEN",
            "classification": "GREEN",
            "evidence": {},
            "action": "DRY_RUN_PATCH",
        }]
        with patch.object(
            main_entry, "acquire_single_instance_lock", return_value="lock"
        ) as acquire, patch.object(
            main_entry, "release_single_instance_lock"
        ) as release, patch(
            "wqb_agent.WQBClient"
        ) as client_class, patch(
            "wqb_agent.research_api.refresh_remote_alphas",
            return_value={"submitted_count": 1},
        ) as refresh, patch(
            "wqb_agent.research_api.list_remote_alphas",
            return_value=[{"alpha_id": "a1"}],
        ) as listed, patch(
            "wqb_agent.research_api.sync_alpha_colors",
            return_value=changes,
        ) as sync:
            with contextlib.redirect_stdout(io.StringIO()) as output:
                main_entry.main([
                    "--config", "config.example.json",
                    "--state-dir", "tests/fixtures",
                    "alpha", "sync-colors", "--dry-run",
                ])

        acquire.assert_called_once_with(
            "tests/fixtures", operation="sync-alpha-colors"
        )
        release.assert_called_once_with("lock")
        client = client_class.return_value
        refresh.assert_called_once()
        listed.assert_called_once()
        sync.assert_called_once()
        payload = json.loads(output.getvalue())
        self.assertTrue(payload["dry_run"])
        self.assertFalse(payload["network_write"])

    def test_sync_colors_failure_keeps_json_and_exit_contract(self):
        with patch.object(
            main_entry, "acquire_single_instance_lock", return_value="lock"
        ), patch.object(
            main_entry, "release_single_instance_lock"
        ) as release, patch(
            "wqb_agent.WQBClient"
        ), patch(
            "wqb_agent.research_api.refresh_remote_alphas",
            side_effect=ValueError(
                "color readback mismatch"
            ),
        ):
            with contextlib.redirect_stdout(io.StringIO()) as output:
                with self.assertRaises(SystemExit) as raised:
                    main_entry.main([
                        "--config", "config.example.json",
                        "alpha", "sync-colors",
                    ])

        self.assertEqual(raised.exception.code, 1)
        release.assert_called_once_with("lock")
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["status"], "FAILED")
        self.assertTrue(payload["network_write"])

    def test_local_readonly_commands_do_not_construct_client(self):
        for argv in (
            ["--config", "config.example.json", "state", "doctor"],
            ["--config", "config.example.json", "state", "audit"],
            ["--config", "config.example.json", "state", "preflight"],
            ["--config", "config.example.json", "context"],
        ):
            with self.subTest(argv=argv), patch(
                "wqb_agent.WQBClient",
                side_effect=AssertionError("read-only command built client"),
            ):
                with contextlib.redirect_stdout(io.StringIO()):
                    try:
                        main_entry.main(argv)
                    except SystemExit as exc:
                        self.assertIn(exc.code, (0, 2))

    def test_suggest_does_not_acquire_simulation_owner_lock(self):
        with patch("main.acquire_single_instance_lock") as acquire:
            with patch("wqb_agent.WQBClient"), patch(
                "wqb_agent.research_api.discover_fields",
                return_value={"fields": []},
            ) as discover:
                with patch.object(main_entry, "load_config", return_value={
                    "simulation": {}, "agent": {}
                }), patch("sys.argv", ["main.py", "suggest"]):
                    with contextlib.redirect_stdout(io.StringIO()):
                        try:
                            main_entry.main()
                        except SystemExit as exc:
                            self.assertNotEqual(exc.code, 2)
            acquire.assert_not_called()
            discover.assert_called_once()

    def test_audit_and_preflight_semantic_blocks_exit_two(self):
        with patch.object(
            main_entry, "load_config", return_value={"simulation": {}, "agent": {}}
        ), patch(
            "wqb_agent.audit.audit_state",
            return_value={"ok": False, "errors": ["blocked"]},
        ):
            with contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    main_entry.main(["state", "audit"])
            self.assertEqual(raised.exception.code, 2)

    def test_state_audit_uses_persistent_lifecycle_contract(self):
        with patch.object(
            main_entry, "load_config", return_value={"simulation": {}, "agent": {}}
        ), patch(
            "wqb_agent.audit.audit_state",
            return_value={"ok": True},
        ) as audit:
            main_entry.main(["state", "audit"])
        audit.assert_called_once_with(
            ".wqb_state", lifecycle_persistent=True
        )

        with patch.object(
            main_entry, "load_config", return_value={"simulation": {}, "agent": {}}
        ), patch(
            "wqb_agent.preflight.run_takeover_preflight",
            return_value={"status": "BLOCKED"},
        ):
            with contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    main_entry.main(["state", "preflight"])
            self.assertEqual(raised.exception.code, 2)

    def test_smoke_runtime_failure_exits_one_after_json_contract(self):
        with patch.object(
            main_entry, "load_config", return_value={"simulation": {}, "agent": {}}
        ), patch(
            "wqb_agent.WQBClient", side_effect=RuntimeError("credentials unavailable")
        ):
            with contextlib.redirect_stdout(io.StringIO()) as output:
                with self.assertRaises(SystemExit) as raised:
                    main_entry.main(["smoke"])
        self.assertEqual(raised.exception.code, 1)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["status"], "UNAVAILABLE")


class TestCliSubprocessIntegration(unittest.TestCase):
    root = pathlib.Path(__file__).resolve().parents[1]

    def run_cli(self, *argv):
        return subprocess.run(
            [sys.executable, "main.py", *argv],
            cwd=self.root,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_root_help_exposes_structured_commands(self):
        result = self.run_cli("--help")
        self.assertEqual(result.returncode, 0)
        self.assertNotIn("factory", result.stdout)
        self.assertIn("state", result.stdout)
        self.assertNotIn("--factory-run", result.stdout)

    def test_canonical_state_doctor_is_machine_readable(self):
        result = self.run_cli(
            "--state-dir", "tests/fixtures", "state", "doctor"
        )
        self.assertEqual(result.returncode, 0)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["config_valid"])
        self.assertEqual(result.stderr, "")


if __name__ == "__main__":
    unittest.main()
