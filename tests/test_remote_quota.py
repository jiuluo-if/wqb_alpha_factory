import tempfile
import unittest
from datetime import UTC, datetime

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
            self.assertEqual(snapshot["evidence_status"], "APPROXIMATE")
            self.assertTrue(snapshot["estimate"]["approximate"])
            self.assertEqual(snapshot["official"]["status"], "UNKNOWN")

    def test_unique_alpha_rows_are_only_an_approximate_simulation_estimate(self):
        with tempfile.TemporaryDirectory() as tmp:
            quota = SimulationQuota(
                _Repository([{"alpha_id": "already-existing", "local_date": "2026-09-16"}]),
                ExecutionGuard(tmp), daily_cap=1600, rolling_cap=11200,
                local_date=lambda: "2026-09-16",
            )

            snapshot = quota.snapshot()

            self.assertEqual(snapshot["estimate"]["today_used"], 1)
            self.assertEqual(snapshot["estimate"]["source"],
                             "ESTIMATE_REMOTE_ALPHA_REPOSITORY+EXECUTION_GUARD")
            self.assertEqual(snapshot["official"]["status"], "UNKNOWN")
            self.assertIsNone(snapshot["official"]["remaining"])

    def test_official_headers_are_primary_without_overwriting_legacy_estimate_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            quota = SimulationQuota(
                _Repository([{"alpha_id": "one", "local_date": "2026-09-16"}]),
                ExecutionGuard(tmp), daily_cap=3, rolling_cap=5,
                local_date=lambda: "2026-09-16",
                official_observation={
                    "status": "AVAILABLE",
                    "evidence_status": "AVAILABLE",
                    "source": "BRAIN_SIMULATION_HEADERS",
                    "limit": 1600,
                    "remaining": 987,
                    "reset_seconds": 12345,
                },
            )

            snapshot = quota.snapshot()

            self.assertEqual(snapshot["source"], "BRAIN_SIMULATION_HEADERS")
            self.assertEqual(snapshot["official"]["remaining"], 987)
            self.assertEqual(snapshot["today_remaining"], 2)
            self.assertEqual(snapshot["estimate"]["today_remaining"], 2)
            self.assertEqual(snapshot["evidence_status"], "AVAILABLE")
            self.assertTrue(snapshot["legacy_fields_are_estimate"])

    def test_fallback_date_uses_new_york_at_fixed_utc_boundary(self):
        fixed = datetime(2026, 9, 17, 3, 30, tzinfo=UTC)
        self.assertEqual(SimulationQuota._today(fixed), "2026-09-16")

    def test_public_quota_api_reads_cache_without_constructing_a_client(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = research_api.simulation_quota(state_dir=tmp)
            self.assertEqual(result["source"],
                             "ESTIMATE_REMOTE_ALPHA_REPOSITORY+EXECUTION_GUARD")
            self.assertEqual(result["today_used"], 0)
            self.assertEqual(result["official"]["status"], "UNKNOWN")

    def test_public_quota_api_uses_client_observation_as_official_primary(self):
        class _Client:
            def get_all_user_alphas(self, **_kwargs):
                return []

            def get_simulation_quota_observation(self):
                return {
                    "status": "AVAILABLE",
                    "evidence_status": "AVAILABLE",
                    "source": "BRAIN_SIMULATION_HEADERS",
                    "limit": 1600,
                    "remaining": 987,
                    "reset_seconds": 12345,
                }

        with tempfile.TemporaryDirectory() as tmp:
            result = research_api.simulation_quota(client=_Client(), state_dir=tmp)

        self.assertEqual(result["official"]["remaining"], 987)
        self.assertEqual(result["source"], "BRAIN_SIMULATION_HEADERS")
        self.assertEqual(result["estimate"]["source"],
                         "ESTIMATE_REMOTE_ALPHA_REPOSITORY+EXECUTION_GUARD")


if __name__ == "__main__":
    unittest.main()
