import json
import os
import pathlib
import tempfile
import time
import unittest

from wqb_agent import research_api
from wqb_agent.alpha_feed_cache import TEMP_RESOURCE_TTL_SEC, RemoteAlphaCache
from wqb_agent.audit import audit_execution_surface
from wqb_agent.config import parse_config
from wqb_agent.doctor import run_doctor
from wqb_agent.simulation_gateway import ExecutionGuard


class TestRemoteDiagnostics(unittest.TestCase):
    def config(self, state_dir):
        return {"simulation": {}, "runtime": {"state_dir": state_dir}}

    def test_doctor_reports_only_guard_and_rebuildable_cache(self):
        with tempfile.TemporaryDirectory() as state_dir:
            result = run_doctor(self.config(state_dir), offline=True)
        self.assertTrue(result["config_valid"])
        self.assertFalse(result["network_write"])
        self.assertEqual(result["execution_guard"]["active_count"], 0)
        self.assertEqual(result["cache"]["freshness"], "UNKNOWN")
        self.assertNotIn("trajectory", result)
        self.assertNotIn("trial_ledger", result)

    def test_quota_ignores_legacy_rolling_days_without_typed_field(self):
        parsed = parse_config({
            "simulation": {},
            "quota": {"daily": 3, "rolling_days": 14, "rolling_limit": 5},
        })

        self.assertEqual(parsed.quota.daily, 3)
        self.assertFalse(hasattr(parsed.quota, "rolling_days"))
        self.assertFalse(hasattr(parsed.quota, "rolling_limit"))

    def test_datafield_page_budget_has_hard_ceiling(self):
        with self.assertRaisesRegex(ValueError, "max_pagination_pages"):
            parse_config({
                "simulation": {},
                "runtime": {"max_pagination_pages": 101},
            })

    def test_quota_ignores_legacy_rolling_limit_without_typed_field(self):
        parsed = parse_config({
            "simulation": {},
            "quota": {"daily": 5000, "rolling_limit": 11200},
        })

        self.assertEqual(parsed.quota.daily, 5000)
        self.assertFalse(hasattr(parsed.quota, "rolling_limit"))

    def test_consultant_daily_policy_defaults_to_5000(self):
        parsed = parse_config({"simulation": {}})

        self.assertEqual(parsed.quota.daily, 5000)
        self.assertFalse(hasattr(parsed.quota, "rolling_limit"))

    def test_consultant_daily_policy_can_be_lowered(self):
        parsed = parse_config({
            "simulation": {},
            "quota": {"daily": 4000},
        })

        self.assertEqual(parsed.quota.daily, 4000)

    def test_consultant_daily_policy_ceiling_is_5000(self):
        self.assertEqual(
            parse_config({
                "simulation": {},
                "quota": {"daily": 5000},
            }).quota.daily,
            5000,
        )
        self.assertEqual(
            parse_config({
                "simulation": {},
                "quota": {"daily": 0},
            }).quota.daily,
            0,
        )
        for value in (5001, 100000):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "小于或等于 5000"):
                    parse_config({
                        "simulation": {},
                        "quota": {"daily": value},
                    })

    def test_consultant_daily_policy_rejects_bool_and_non_integral_float(self):
        for value in (True, 5000.5):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    parse_config({
                        "simulation": {},
                        "quota": {"daily": value},
                    })

        self.assertEqual(
            parse_config({
                "simulation": {},
                "quota": {"daily": 5000.0},
            }).quota.daily,
            5000,
        )

    def test_config_example_does_not_publish_rolling_days(self):
        example = pathlib.Path(__file__).resolve().parents[1] / "config.example.json"
        payload = json.loads(example.read_text(encoding="utf-8"))

        self.assertNotIn("rolling_days", payload["quota"])
        self.assertEqual(payload["quota"], {"daily": 5000})

    def test_known_running_execution_is_reported_without_replaying_result(self):
        with tempfile.TemporaryDirectory() as state_dir:
            guard = ExecutionGuard(state_dir)
            guard.register("fp", progress_url="https://brain.example/progress/1", status="RUNNING")
            result = audit_execution_surface(state_dir)
        self.assertTrue(result["ok"])
        self.assertEqual(result["execution_guard"]["statuses"], ["RUNNING"])
        self.assertFalse(result["network_write"])

    def test_old_guard_remains_visible_outside_quota_window(self):
        with tempfile.TemporaryDirectory() as state_dir:
            path = os.path.join(state_dir, "execution_guard.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"entries": [{
                    "execution_fingerprint": "old-guard",
                    "status": "SUBMIT_UNKNOWN",
                    "created_at": 0,
                    "updated_at": 0,
                    "simulation_count": 10,
                }]}, handle)

            audit = audit_execution_surface(state_dir)
            pending = research_api.get_pending_executions(state_dir=state_dir)

        self.assertEqual(audit["execution_guard"]["active_count"], 1)
        self.assertEqual(len(pending["entries"]), 1)
        self.assertEqual(pending["entries"][0]["simulation_count"], 10)

    def test_malformed_guard_is_fail_closed(self):
        with tempfile.TemporaryDirectory() as state_dir:
            path = os.path.join(state_dir, "execution_guard.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"entries": [{"execution_fingerprint": "fp", "status": "DONE"}]}, handle)
            result = audit_execution_surface(state_dir)
        self.assertFalse(result["ok"])
        self.assertIn("EXECUTION_GUARD_STATUS_INVALID", result["errors"])

    def test_audit_does_not_delete_valid_non_default_retention_cache(self):
        with tempfile.TemporaryDirectory() as state_dir:
            cache_path = os.path.join(state_dir, ".alpha_feed_cache", "remote.json")
            cache = RemoteAlphaCache(cache_path, retention_days=14)
            cache.refresh({
                cache.local_date: {
                    "simulations": [{"alpha_id": "synthetic-simulation"}],
                    "submitted_alphas": [],
                }
            })
            with open(cache_path, "rb") as handle:
                before = handle.read()

            audit_execution_surface(state_dir)

            self.assertTrue(os.path.exists(cache_path))
            with open(cache_path, "rb") as handle:
                self.assertEqual(handle.read(), before)

    def test_doctor_does_not_remove_stale_temp_resources(self):
        with tempfile.TemporaryDirectory() as state_dir:
            cache_path = os.path.join(state_dir, ".alpha_feed_cache", "remote.json")
            cache = RemoteAlphaCache(cache_path)
            cache.refresh({cache.local_date: {"simulations": [], "submitted_alphas": []}})
            stale_temp = cache_path + ".tmp.stale"
            with open(stale_temp, "w", encoding="utf-8") as handle:
                handle.write("synthetic temp")
            stale_at = time.time() - TEMP_RESOURCE_TTL_SEC - 1
            os.utime(stale_temp, (stale_at, stale_at))

            run_doctor(self.config(state_dir), offline=True)

            self.assertTrue(os.path.exists(stale_temp))


if __name__ == "__main__":
    unittest.main()
