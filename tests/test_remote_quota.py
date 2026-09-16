import tempfile
import unittest

from wqb_agent import research_api
from wqb_agent.remote_quota import SimulationQuota
from wqb_agent.simulation_gateway import ExecutionGuard


class _Repository:
    def __init__(self, rows):
        self.rows = rows

    def list_remote_alphas(self):
        return list(self.rows)


class TestRemoteSimulationQuota(unittest.TestCase):
    def test_quota_projects_remote_rows_and_active_guards_without_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            guard = ExecutionGuard(tmp)
            guard.register("active")
            quota = SimulationQuota(
                _Repository([
                    {"alpha_id": "today", "local_date": "2026-09-16"},
                    {"alpha_id": "older", "local_date": "2026-09-10"},
                ]), guard, daily_cap=3, rolling_cap=5,
                local_date=lambda: "2026-09-16",
            )

            snapshot = quota.snapshot()

            self.assertEqual(snapshot["today_used"], 2)
            self.assertEqual(snapshot["rolling_used"], 3)
            self.assertEqual(snapshot["today_remaining"], 1)
            self.assertEqual(snapshot["rolling_remaining"], 2)
            self.assertEqual(snapshot["active_guard_count"], 1)
            self.assertFalse(snapshot["persisted_quota_state"])

    def test_public_quota_api_reads_cache_without_constructing_a_client(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = research_api.simulation_quota(state_dir=tmp)
            self.assertEqual(result["source"], "REMOTE_ALPHA_REPOSITORY+EXECUTION_GUARD")
            self.assertEqual(result["today_used"], 0)


if __name__ == "__main__":
    unittest.main()
