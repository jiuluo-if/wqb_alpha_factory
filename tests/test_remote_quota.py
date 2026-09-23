import tempfile
import unittest
from datetime import UTC, datetime

from wqb_agent import research_api
from wqb_agent.remote_quota import SimulationQuota
from wqb_agent.simulation_gateway import ExecutionGuard


class _Repository:
    def __init__(self, rows, retention_days=None):
        self.rows = rows
        if retention_days is not None:
            self.retention_days = retention_days

    def list_remote_alphas(self):
        return list(self.rows)


class TestRemoteSimulationQuota(unittest.TestCase):
    def test_default_daily_policy_projects_approximate_remaining(self):
        with tempfile.TemporaryDirectory() as tmp:
            guard = ExecutionGuard(tmp)
            guard.register("active-one")
            guard.register("active-two")
            rows = [
                {"alpha_id": f"synthetic-{index}", "local_date": "2026-09-16"}
                for index in range(100)
            ]
            snapshot = SimulationQuota(
                _Repository(rows, retention_days=7), guard,
                rolling_cap=11200, local_date=lambda: "2026-09-16",
            ).snapshot()

        self.assertEqual(snapshot["estimate"]["daily_cap"], 5000)
        self.assertEqual(snapshot["estimate"]["today_remaining"], 4898)
        self.assertEqual(snapshot["estimate"]["active_guard_count"], 2)
        self.assertEqual(snapshot["evidence_status"], "APPROXIMATE")

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
            self.assertIsNone(snapshot["estimate"]["window_days"])
            self.assertEqual(snapshot["estimate"]["window_source"], "UNKNOWN")
            self.assertEqual(snapshot["official"]["status"], "UNKNOWN")
            self.assertIsNone(snapshot["official"]["reset"])

    def test_estimate_window_comes_from_repository_retention(self):
        for retention_days in (3, 14):
            with self.subTest(retention_days=retention_days), tempfile.TemporaryDirectory() as tmp:
                snapshot = SimulationQuota(
                    _Repository([], retention_days=retention_days),
                    ExecutionGuard(tmp), daily_cap=3, rolling_cap=5,
                    local_date=lambda: "2026-09-16",
                ).snapshot()

            self.assertEqual(snapshot["estimate"]["window_days"], retention_days)
            self.assertEqual(
                snapshot["estimate"]["window_source"],
                "REMOTE_CACHE_RETENTION",
            )

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
            self.assertIsNone(snapshot["estimate"]["window_days"])
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
                    "reset": 12345,
                },
            )

            snapshot = quota.snapshot()

            self.assertEqual(snapshot["source"], "BRAIN_SIMULATION_HEADERS")
            self.assertEqual(snapshot["official"]["remaining"], 987)
            self.assertEqual(snapshot["official"]["reset"], 12345)
            self.assertNotIn("reset" + "_seconds", snapshot["official"])
            self.assertEqual(snapshot["today_remaining"], 2)
            self.assertEqual(snapshot["estimate"]["today_remaining"], 2)
            self.assertEqual(snapshot["estimate"]["window_days"], None)
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
            self.assertEqual(result["estimate"]["daily_cap"], 5000)
            self.assertIsNone(result["official"]["reset"])

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
                    "reset": 12345,
                }

        with tempfile.TemporaryDirectory() as tmp:
            result = research_api.simulation_quota(client=_Client(), state_dir=tmp)

            self.assertEqual(result["official"]["remaining"], 987)
            self.assertEqual(result["official"]["reset"], 12345)
            self.assertEqual(result["official"]["limit"], 1600)
            self.assertEqual(result["estimate"]["daily_cap"], 5000)
            self.assertNotIn("reset" + "_seconds", result["official"])
            self.assertEqual(result["source"], "BRAIN_SIMULATION_HEADERS")
            self.assertEqual(result["estimate"]["source"],
                             "ESTIMATE_REMOTE_ALPHA_REPOSITORY+EXECUTION_GUARD")

    def test_public_quota_api_falls_back_when_latest_observation_is_unknown(self):
        class _Client:
            def get_all_user_alphas(self, **_kwargs):
                return []

            def get_simulation_quota_observation(self):
                return {
                    "status": "UNKNOWN",
                    "evidence_status": "UNAVAILABLE",
                    "source": "BRAIN_SIMULATION_HEADERS",
                    "limit": None,
                    "remaining": None,
                    "reset": None,
                }

        with tempfile.TemporaryDirectory() as tmp:
            result = research_api.simulation_quota(client=_Client(), state_dir=tmp)

        self.assertEqual(result["official"]["status"], "UNKNOWN")
        self.assertEqual(
            result["source"], "ESTIMATE_REMOTE_ALPHA_REPOSITORY+EXECUTION_GUARD"
        )
        self.assertEqual(result["evidence_status"], "APPROXIMATE")

    def test_official_limit_above_local_policy_is_not_clamped(self):
        class _Client:
            def get_all_user_alphas(self, **_kwargs):
                return []

            def get_simulation_quota_observation(self):
                return {
                    "status": "AVAILABLE",
                    "evidence_status": "AVAILABLE",
                    "source": "BRAIN_SIMULATION_HEADERS",
                    "limit": 6000,
                    "remaining": 5000,
                    "reset": 12345,
                }

        with tempfile.TemporaryDirectory() as tmp:
            result = research_api.simulation_quota(client=_Client(), state_dir=tmp)

        self.assertEqual(result["source"], "BRAIN_SIMULATION_HEADERS")
        self.assertEqual(result["official"]["limit"], 6000)
        self.assertEqual(result["estimate"]["daily_cap"], 5000)

    def test_public_quota_estimate_window_uses_remote_cache_retention(self):
        class _Client:
            def get_all_user_alphas(self, **_kwargs):
                return []

            def get_simulation_quota_observation(self):
                return None

        for retention_days in (3, 14):
            with self.subTest(retention_days=retention_days), tempfile.TemporaryDirectory() as tmp:
                result = research_api.simulation_quota(
                    client=_Client(),
                    config={
                        "simulation": {},
                        "remote_cache": {"retention_days": retention_days},
                    },
                    state_dir=tmp,
                )

            self.assertEqual(result["official"]["status"], "UNKNOWN")
            self.assertEqual(result["estimate"]["window_days"], retention_days)
            self.assertEqual(
                result["estimate"]["window_source"],
                "REMOTE_CACHE_RETENTION",
            )


if __name__ == "__main__":
    unittest.main()
