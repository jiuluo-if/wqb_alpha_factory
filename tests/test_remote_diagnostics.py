import json
import os
import tempfile
import unittest

from wqb_agent.audit import audit_state
from wqb_agent.doctor import run_doctor
from wqb_agent.preflight import build_agent_context, run_takeover_preflight
from wqb_agent.simulation_gateway import ExecutionGuard


class TestRemoteDiagnostics(unittest.TestCase):
    def config(self, state_dir):
        return {"simulation": {}, "agent": {"state_dir": state_dir}}

    def test_doctor_reports_only_guard_and_rebuildable_cache(self):
        with tempfile.TemporaryDirectory() as state_dir:
            result = run_doctor(self.config(state_dir), offline=True)
        self.assertTrue(result["config_valid"])
        self.assertFalse(result["network_write"])
        self.assertEqual(result["execution_guard"]["active_count"], 0)
        self.assertEqual(result["cache"]["freshness"], "UNKNOWN")
        self.assertNotIn("trajectory", result)
        self.assertNotIn("trial_ledger", result)

    def test_legacy_result_files_are_ignored_not_replayed(self):
        with tempfile.TemporaryDirectory() as state_dir:
            with open(os.path.join(state_dir, "trajectory.jsonl"), "w", encoding="utf-8") as handle:
                handle.write('{"alpha_id":"synthetic"}\n')
            result = run_doctor(self.config(state_dir))
        self.assertEqual(result["legacy_artifacts_ignored"], ["trajectory.jsonl"])
        self.assertEqual(result["execution_guard"]["active_count"], 0)
        self.assertIn("LEGACY_LOCAL_STATE_IGNORED", {
            item["code"] for item in result["diagnostics"]
        })

    def test_submit_unknown_blocks_preflight_without_network_write(self):
        with tempfile.TemporaryDirectory() as state_dir:
            guard = ExecutionGuard(state_dir)
            guard.register("fp", status="SUBMIT_UNKNOWN")
            result = run_takeover_preflight(self.config(state_dir))
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("SUBMIT_UNKNOWN_REQUIRES_RECONCILIATION", result["blocking"])
        self.assertFalse(result["network_write"])

    def test_known_running_execution_is_reported_without_replaying_result(self):
        with tempfile.TemporaryDirectory() as state_dir:
            guard = ExecutionGuard(state_dir)
            guard.register("fp", progress_url="https://brain.example/progress/1", status="RUNNING")
            result = audit_state(state_dir)
        self.assertTrue(result["ok"])
        self.assertEqual(result["execution_guard"]["statuses"], ["RUNNING"])
        self.assertFalse(result["network_write"])

    def test_malformed_guard_is_fail_closed(self):
        with tempfile.TemporaryDirectory() as state_dir:
            path = os.path.join(state_dir, "execution_guard.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"entries": [{"execution_fingerprint": "fp", "status": "DONE"}]}, handle)
            result = audit_state(state_dir)
        self.assertFalse(result["ok"])
        self.assertIn("EXECUTION_GUARD_STATUS_INVALID", result["errors"])

    def test_context_routes_to_remote_tools(self):
        with tempfile.TemporaryDirectory() as state_dir:
            context = build_agent_context(self.config(state_dir), task="execution")
        self.assertEqual(context["task"]["name"], "execution")
        self.assertIn("simulation_gateway.py", " ".join(context["task"]["files"]))
        self.assertNotIn("trajectory", context["evidence"])


if __name__ == "__main__":
    unittest.main()
