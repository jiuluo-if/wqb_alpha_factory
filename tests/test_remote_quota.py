import json
import math
import tempfile
import unittest
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from wqb_agent import research_api
from wqb_agent.remote_quota import SimulationQuota
from wqb_agent.simulation_gateway import ExecutionGuard

NEW_YORK = ZoneInfo("America/New_York")


def _epoch(local_date):
    return datetime.fromisoformat(f"{local_date}T12:00:00").replace(
        tzinfo=NEW_YORK
    ).timestamp()


def _register_at(guard, fingerprint, local_date, *, simulation_count=1,
                 status="SUBMIT_UNKNOWN", updated_date=None):
    guard.register(
        fingerprint, status=status, simulation_count=simulation_count,
    )
    with open(guard.path, encoding="utf-8") as handle:
        payload = json.load(handle)
    row = payload["entries"][-1]
    row["created_at"] = _epoch(local_date)
    row["updated_at"] = _epoch(updated_date or local_date)
    with open(guard.path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)


class _Repository:
    def __init__(self, rows, retention_days=None):
        self.rows = rows
        if retention_days is not None:
            self.retention_days = retention_days

    def list_remote_alphas(self):
        return list(self.rows)


class TestSimulationQuota(unittest.TestCase):
    @staticmethod
    def _snapshot(rows, *, retention_days=7):
        with tempfile.TemporaryDirectory() as tmp:
            return SimulationQuota(
                _Repository(rows, retention_days=retention_days),
                ExecutionGuard(tmp),
                local_date=lambda: "2026-09-23",
            ).snapshot()

    def test_submitted_today_created_yesterday_counts_only_in_window_usage(self):
        snapshot = self._snapshot([{
            "alpha_id": "created-yesterday",
            "status": "SUBMITTED",
            "date_created": "2026-09-22T12:00:00Z",
            "date_submitted": "2026-09-23T12:00:00Z",
            "local_date": "2026-09-23",
            "simulation_local_date": "2026-09-22",
        }])

        self.assertEqual(snapshot["today_used"], 0)
        self.assertEqual(snapshot["window_used"], 1)
        self.assertEqual(snapshot["window_days"], 7)
        self.assertEqual(snapshot["window_source"], "REMOTE_CACHE_RETENTION")
        self.assertEqual(snapshot["estimate"]["today_remaining"], 5000)
        self.assertNotIn("rolling_cap", snapshot)
        self.assertNotIn("rolling_remaining", snapshot)
        self.assertNotIn("rolling_cap", snapshot["estimate"])
        self.assertNotIn("rolling_remaining", snapshot["estimate"])

    def test_submitted_today_created_outside_window_is_not_observed(self):
        snapshot = self._snapshot([{
            "alpha_id": "created-too-old",
            "status": "SUBMITTED",
            "date_created": "2026-09-10T12:00:00Z",
            "date_submitted": "2026-09-23T12:00:00Z",
            "local_date": "2026-09-23",
            "simulation_local_date": "2026-09-10",
        }])

        self.assertEqual(snapshot["today_used"], 0)
        self.assertEqual(snapshot["window_used"], 0)

    def test_created_today_counts_for_today_and_window_usage(self):
        snapshot = self._snapshot([{
            "alpha_id": "created-today",
            "status": "SUBMITTED",
            "date_created": "2026-09-23T12:00:00Z",
            "date_submitted": "2026-09-23T13:00:00Z",
            "local_date": "2026-09-23",
            "simulation_local_date": "2026-09-23",
        }])

        self.assertEqual(snapshot["today_used"], 1)
        self.assertEqual(snapshot["window_used"], 1)

    def test_unsubmitted_creation_date_remains_window_observation(self):
        snapshot = self._snapshot([{
            "alpha_id": "unsubmitted-created-today",
            "status": "UNSUBMITTED",
            "date_created": "2026-09-23T12:00:00Z",
            "local_date": "2026-09-23",
        }])

        self.assertEqual(snapshot["today_used"], 1)
        self.assertEqual(snapshot["window_used"], 1)

    def test_submitted_row_without_creation_date_fails_closed(self):
        snapshot = self._snapshot([{
            "alpha_id": "legacy-submitted",
            "status": "SUBMITTED",
            "date_submitted": "2026-09-23T12:00:00Z",
            "local_date": "2026-09-23",
        }])

        self.assertEqual(snapshot["today_used"], 0)
        self.assertEqual(snapshot["window_used"], 0)

    def test_default_daily_policy_projects_approximate_remaining(self):
        with tempfile.TemporaryDirectory() as tmp:
            guard = ExecutionGuard(tmp)
            _register_at(guard, "active-one", "2026-09-16")
            _register_at(guard, "active-two", "2026-09-16")
            rows = [
                {"alpha_id": f"synthetic-{index}", "local_date": "2026-09-16"}
                for index in range(100)
            ]
            snapshot = SimulationQuota(
                _Repository(rows, retention_days=7), guard,
                local_date=lambda: "2026-09-16",
            ).snapshot()

        self.assertEqual(snapshot["estimate"]["daily_cap"], 5000)
        self.assertEqual(snapshot["estimate"]["today_remaining"], 4898)
        self.assertEqual(snapshot["estimate"]["active_guard_count"], 2)
        self.assertEqual(snapshot["estimate"]["active_guard_simulation_count"], 2)
        self.assertEqual(snapshot["evidence_status"], "APPROXIMATE")

    def test_today_guard_contributes_to_today_and_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            guard = ExecutionGuard(tmp)
            _register_at(guard, "today", "2026-09-23", simulation_count=4)
            snapshot = SimulationQuota(
                _Repository([], retention_days=7), guard,
                local_date=lambda: "2026-09-23",
            ).snapshot()

        self.assertEqual(snapshot["active_guard_count"], 1)
        self.assertEqual(snapshot["active_guard_simulation_count"], 4)
        self.assertEqual(snapshot["today_guard_simulation_count"], 4)
        self.assertEqual(snapshot["window_guard_simulation_count"], 4)
        self.assertEqual(snapshot["today_used"], 4)
        self.assertEqual(snapshot["window_used"], 4)

    def test_yesterday_guard_stays_in_window_but_not_today(self):
        with tempfile.TemporaryDirectory() as tmp:
            guard = ExecutionGuard(tmp)
            _register_at(guard, "yesterday", "2026-09-22", simulation_count=4)
            snapshot = SimulationQuota(
                _Repository([], retention_days=7), guard,
                local_date=lambda: "2026-09-23",
            ).snapshot()

        self.assertEqual(snapshot["active_guard_simulation_count"], 4)
        self.assertEqual(snapshot["today_guard_simulation_count"], 0)
        self.assertEqual(snapshot["window_guard_simulation_count"], 4)
        self.assertEqual(snapshot["today_used"], 0)
        self.assertEqual(snapshot["today_remaining"], 5000)
        self.assertEqual(snapshot["window_used"], 4)

    def test_guard_outside_retention_stays_for_safety_but_not_window_usage(self):
        with tempfile.TemporaryDirectory() as tmp:
            guard = ExecutionGuard(tmp)
            _register_at(guard, "old", "2026-09-10", simulation_count=10)
            snapshot = SimulationQuota(
                _Repository([], retention_days=7), guard,
                local_date=lambda: "2026-09-23",
            ).snapshot()

            remaining_entries = len(guard.entries())

        self.assertEqual(snapshot["active_guard_count"], 1)
        self.assertEqual(remaining_entries, 1)
        self.assertEqual(snapshot["active_guard_simulation_count"], 10)
        self.assertEqual(snapshot["today_guard_simulation_count"], 0)
        self.assertEqual(snapshot["window_guard_simulation_count"], 0)
        self.assertEqual(snapshot["today_used"], 0)
        self.assertEqual(snapshot["window_used"], 0)

    def test_mixed_guard_dates_keep_all_active_and_filter_contributions(self):
        with tempfile.TemporaryDirectory() as tmp:
            guard = ExecutionGuard(tmp)
            _register_at(guard, "today", "2026-09-23", simulation_count=1)
            _register_at(guard, "yesterday", "2026-09-22", simulation_count=4)
            _register_at(guard, "old", "2026-09-10", simulation_count=10)
            snapshot = SimulationQuota(
                _Repository([], retention_days=7), guard,
                local_date=lambda: "2026-09-23",
            ).snapshot()

        self.assertEqual(snapshot["active_guard_count"], 3)
        self.assertEqual(snapshot["active_guard_simulation_count"], 15)
        self.assertEqual(snapshot["today_guard_simulation_count"], 1)
        self.assertEqual(snapshot["window_guard_simulation_count"], 5)
        self.assertEqual(snapshot["today_used"], 1)
        self.assertEqual(snapshot["window_used"], 5)

    def test_updated_at_does_not_redate_guard_after_reconcile(self):
        with tempfile.TemporaryDirectory() as tmp:
            guard = ExecutionGuard(tmp, reconcile=False)
            _register_at(
                guard, "interrupted", "2026-09-22", simulation_count=4,
                status="SUBMITTING", updated_date="2026-09-22",
            )
            guard.reconcile()
            entry = guard.find("interrupted")
            snapshot = SimulationQuota(
                _Repository([], retention_days=7), guard,
                local_date=lambda: "2026-09-23",
            ).snapshot()

        self.assertEqual(entry["status"], "SUBMIT_UNKNOWN")
        self.assertGreater(entry["updated_at"], entry["created_at"])
        self.assertEqual(snapshot["today_guard_simulation_count"], 0)
        self.assertEqual(snapshot["window_guard_simulation_count"], 4)

    def test_weighted_unresolved_guard_counts_sum_child_simulations(self):
        with tempfile.TemporaryDirectory() as tmp:
            guard = ExecutionGuard(tmp)
            _register_at(guard, "multi-10-a", "2026-09-23", simulation_count=10)
            _register_at(guard, "multi-10-b", "2026-09-23", simulation_count=10)
            _register_at(guard, "multi-4", "2026-09-23", simulation_count=4)
            snapshot = SimulationQuota(
                _Repository([], retention_days=7), guard,
                local_date=lambda: "2026-09-23",
            ).snapshot()

        self.assertEqual(snapshot["active_guard_count"], 3)
        self.assertEqual(snapshot["active_guard_simulation_count"], 24)
        self.assertEqual(snapshot["today_used"], 24)
        self.assertEqual(snapshot["window_used"], 24)
        self.assertEqual(snapshot["estimate"]["today_used"], 24)
        self.assertEqual(snapshot["estimate"]["active_guard_count"], 3)
        self.assertEqual(snapshot["estimate"]["active_guard_simulation_count"], 24)

    def test_quota_projects_remote_rows_and_active_guards_without_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            guard = ExecutionGuard(tmp)
            _register_at(guard, "active", "2026-09-16")
            quota = SimulationQuota(
                _Repository([
                    {"alpha_id": "today", "local_date": "2026-09-16"},
                    {"alpha_id": "older", "local_date": "2026-09-10"},
                ]), guard, daily_cap=3,
                local_date=lambda: "2026-09-16",
            )

            snapshot = quota.snapshot()

            self.assertEqual(snapshot["today_used"], 2)
            self.assertEqual(snapshot["window_used"], 3)
            self.assertEqual(snapshot["today_remaining"], 1)
            self.assertNotIn("rolling_cap", snapshot)
            self.assertNotIn("rolling_remaining", snapshot)
            self.assertNotIn("rolling_cap", snapshot["estimate"])
            self.assertNotIn("rolling_remaining", snapshot["estimate"])
            self.assertEqual(snapshot["active_guard_count"], 1)
            self.assertEqual(snapshot["today_guard_simulation_count"], 1)
            self.assertEqual(snapshot["window_guard_simulation_count"], 1)
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
                    ExecutionGuard(tmp), daily_cap=3,
                    local_date=lambda: "2026-09-16",
                ).snapshot()

            self.assertEqual(snapshot["estimate"]["window_used"], 0)
            self.assertEqual(snapshot["estimate"]["window_days"], retention_days)
            self.assertEqual(
                snapshot["estimate"]["window_source"],
                "REMOTE_CACHE_RETENTION",
            )

    def test_unknown_window_conservatively_includes_all_guard_contributions(self):
        with tempfile.TemporaryDirectory() as tmp:
            guard = ExecutionGuard(tmp)
            _register_at(guard, "old", "2026-09-10", simulation_count=4)
            _register_at(guard, "unknown-time", "2026-09-22", simulation_count=3)
            snapshot = SimulationQuota(
                _Repository([], retention_days=None), guard,
                local_date=lambda: "2026-09-23",
            ).snapshot()

        self.assertIsNone(snapshot["window_days"])
        self.assertEqual(snapshot["today_guard_simulation_count"], 0)
        self.assertEqual(snapshot["window_guard_simulation_count"], 7)
        self.assertEqual(snapshot["window_used"], 7)

    def test_legacy_missing_created_at_is_conservatively_included(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = f"{tmp}/execution_guard.json"
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"entries": [{
                    "execution_fingerprint": "legacy",
                    "status": "SUBMIT_UNKNOWN",
                    "simulation_count": 7,
                }]}, handle)
            guard = ExecutionGuard(tmp, reconcile=False)
            snapshot = SimulationQuota(
                _Repository([], retention_days=7), guard,
                local_date=lambda: "2026-09-23",
            ).snapshot()
            remaining_entries = len(guard.entries())

        self.assertEqual(remaining_entries, 1)
        self.assertEqual(snapshot["active_guard_simulation_count"], 7)
        self.assertEqual(snapshot["today_guard_simulation_count"], 7)
        self.assertEqual(snapshot["window_guard_simulation_count"], 7)

    def test_malformed_created_at_is_conservative_and_never_crashes(self):
        malformed = (None, True, "123", math.nan, math.inf)
        with tempfile.TemporaryDirectory() as tmp:
            path = f"{tmp}/execution_guard.json"
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"entries": [
                    {
                        "execution_fingerprint": f"bad-{index}",
                        "status": "SUBMIT_UNKNOWN",
                        "simulation_count": index + 1,
                        "created_at": value,
                        "updated_at": value,
                    }
                    for index, value in enumerate(malformed)
                ]}, handle, allow_nan=True)
            guard = ExecutionGuard(tmp, reconcile=False)
            snapshot = SimulationQuota(
                _Repository([], retention_days=7), guard,
                local_date=lambda: "2026-09-23",
            ).snapshot()
            remaining_entries = len(guard.entries())

        self.assertEqual(remaining_entries, 5)
        self.assertEqual(snapshot["active_guard_simulation_count"], 15)
        self.assertEqual(snapshot["today_guard_simulation_count"], 15)
        self.assertEqual(snapshot["window_guard_simulation_count"], 15)

    def test_guard_new_york_utc_boundary_uses_local_date(self):
        with tempfile.TemporaryDirectory() as tmp:
            guard = ExecutionGuard(tmp)
            _register_at(guard, "before-midnight", "2026-09-22")
            with open(guard.path, encoding="utf-8") as handle:
                payload = json.load(handle)
            payload["entries"][0]["created_at"] = datetime(
                2026, 9, 23, 3, 30, tzinfo=UTC
            ).timestamp()
            _register_at(guard, "after-midnight", "2026-09-23")
            with open(guard.path, encoding="utf-8") as handle:
                payload = json.load(handle)
            payload["entries"][0]["created_at"] = datetime(
                2026, 9, 23, 3, 30, tzinfo=UTC
            ).timestamp()
            payload["entries"][1]["created_at"] = datetime(
                2026, 9, 23, 4, 0, tzinfo=UTC
            ).timestamp()
            with open(guard.path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
            snapshot = SimulationQuota(
                _Repository([], retention_days=7), guard,
                local_date=lambda: "2026-09-23",
            ).snapshot()

        self.assertEqual(snapshot["today_guard_simulation_count"], 1)
        self.assertEqual(snapshot["window_guard_simulation_count"], 2)
        self.assertEqual(snapshot["today_used"], 1)
        self.assertEqual(snapshot["window_used"], 2)

    def test_unique_alpha_rows_are_only_an_approximate_simulation_estimate(self):
        with tempfile.TemporaryDirectory() as tmp:
            quota = SimulationQuota(
                _Repository([{"alpha_id": "already-existing", "local_date": "2026-09-16"}]),
                ExecutionGuard(tmp), daily_cap=1600,
                local_date=lambda: "2026-09-16",
            )

            snapshot = quota.snapshot()

            self.assertEqual(snapshot["estimate"]["today_used"], 1)
            self.assertEqual(snapshot["estimate"]["source"],
                             "ESTIMATE_REMOTE_ALPHA_REPOSITORY+EXECUTION_GUARD")
            self.assertEqual(snapshot["estimate"]["window_used"], 1)
            self.assertIsNone(snapshot["estimate"]["window_days"])
            self.assertNotIn("rolling_cap", snapshot)
            self.assertNotIn("rolling_remaining", snapshot)
            self.assertEqual(snapshot["official"]["status"], "UNKNOWN")
            self.assertIsNone(snapshot["official"]["remaining"])

    def test_official_headers_are_primary_without_overwriting_legacy_estimate_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            quota = SimulationQuota(
                _Repository([{"alpha_id": "one", "local_date": "2026-09-16"}]),
                ExecutionGuard(tmp), daily_cap=3,
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
            self.assertEqual(snapshot["estimate"]["window_used"], 1)
            self.assertEqual(snapshot["estimate"]["window_days"], None)
            self.assertNotIn("rolling_cap", snapshot)
            self.assertNotIn("rolling_remaining", snapshot)
            self.assertEqual(snapshot["evidence_status"], "AVAILABLE")
            self.assertTrue(snapshot["legacy_fields_are_estimate"])

    def test_official_headers_are_unchanged_by_weighted_guard_estimate(self):
        with tempfile.TemporaryDirectory() as tmp:
            guard = ExecutionGuard(tmp)
            _register_at(guard, "multi", "2026-09-23", simulation_count=10)
            snapshot = SimulationQuota(
                _Repository([], retention_days=7), guard,
                local_date=lambda: "2026-09-23",
                official_observation={
                    "status": "AVAILABLE",
                    "evidence_status": "AVAILABLE",
                    "source": "BRAIN_SIMULATION_HEADERS",
                    "limit": 6000,
                    "remaining": 5000,
                    "reset": 12345,
                },
            ).snapshot()

        self.assertEqual(snapshot["source"], "BRAIN_SIMULATION_HEADERS")
        self.assertEqual(snapshot["evidence_status"], "AVAILABLE")
        self.assertEqual(snapshot["official"], {
            "status": "AVAILABLE",
            "evidence_status": "AVAILABLE",
            "source": "BRAIN_SIMULATION_HEADERS",
            "limit": 6000,
            "remaining": 5000,
            "reset": 12345,
        })
        self.assertEqual(snapshot["estimate"]["active_guard_count"], 1)
        self.assertEqual(snapshot["estimate"]["active_guard_simulation_count"], 10)

    def test_official_headers_are_unchanged_when_creation_day_is_filtered(self):
        with tempfile.TemporaryDirectory() as tmp:
            quota = SimulationQuota(
                _Repository([{
                    "alpha_id": "created-too-old",
                    "status": "SUBMITTED",
                    "date_created": "2026-09-10T12:00:00Z",
                    "date_submitted": "2026-09-23T12:00:00Z",
                    "local_date": "2026-09-23",
                    "simulation_local_date": "2026-09-10",
                }], retention_days=7),
                ExecutionGuard(tmp),
                local_date=lambda: "2026-09-23",
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

        self.assertEqual(snapshot["today_used"], 0)
        self.assertEqual(snapshot["window_used"], 0)
        self.assertEqual(snapshot["source"], "BRAIN_SIMULATION_HEADERS")
        self.assertEqual(snapshot["official"]["limit"], 1600)
        self.assertEqual(snapshot["official"]["remaining"], 987)
        self.assertEqual(snapshot["official"]["reset"], 12345)

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
            self.assertEqual(result["estimate"]["window_used"], 0)
            self.assertNotIn("rolling_cap", result)
            self.assertNotIn("rolling_remaining", result)
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
        self.assertEqual(result["estimate"]["window_used"], 0)
        self.assertNotIn("rolling_cap", result)
        self.assertNotIn("rolling_remaining", result)

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
            self.assertEqual(result["estimate"]["window_used"], 0)
            self.assertEqual(result["estimate"]["window_days"], retention_days)
            self.assertEqual(
                result["estimate"]["window_source"],
                "REMOTE_CACHE_RETENTION",
            )


if __name__ == "__main__":
    unittest.main()
