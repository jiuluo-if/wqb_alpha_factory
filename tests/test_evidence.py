"""证据缓存侧车（evidence_cache.json）的回归测试。"""
import os
import sys
import tempfile
import unittest
from unittest import mock

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wqb_agent.evidence import (
    _bounded_cache,
    load_evidence_cache,
    overlay_cached_checks,
    refresh_self_correlation_cache,
    save_evidence_cache,
)
from wqb_agent.state import Experiment


def _metrics_with_pending_self_corr(sharpe=2.0, fitness=1.5, turnover=0.18):
    return {
        "sharpe": sharpe,
        "fitness": fitness,
        "turnover": turnover,
        "returns": 0.07,
        "drawdown": 0.04,
        "margin": 0.0007,
        "checks": [
            {"name": "LOW_SHARPE", "pass": True, "result": "PASS",
             "value": sharpe, "limit": 1.25},
            {"name": "SELF_CORRELATION", "pass": None, "result": "PENDING",
             "value": None, "limit": None},
        ],
        "passed": None,
    }


def _cached_entry(self_corr_pass=True):
    return {
        "checks": [
            {"name": "LOW_SHARPE", "pass": True, "result": "PASS",
             "value": 2.0, "limit": 1.25},
            {"name": "SELF_CORRELATION", "pass": self_corr_pass,
             "result": "PASS" if self_corr_pass else "FAIL", "value": 0.4,
             "limit": 0.5},
        ],
        "passed": self_corr_pass,
        "updated_at": "2026-08-26T10:00:00",
    }


class TestOverlay(unittest.TestCase):
    def test_overlay_replaces_only_pending_checks(self):
        metrics = _metrics_with_pending_self_corr()
        view = overlay_cached_checks(metrics, _cached_entry(True))
        self.assertIsNot(view, metrics)
        sc = next(c for c in view["checks"] if c["name"] == "SELF_CORRELATION")
        self.assertIs(sc["pass"], True)
        # 原始存储指标不被修改
        orig = next(c for c in metrics["checks"] if c["name"] == "SELF_CORRELATION")
        self.assertIsNone(orig["pass"])
        self.assertIs(view["passed"], True)

    def test_overlay_keeps_tri_state_when_cache_missing_check(self):
        metrics = _metrics_with_pending_self_corr()
        cached = {"checks": [{"name": "LOW_SHARPE", "pass": True}]}
        view = overlay_cached_checks(metrics, cached)
        self.assertIs(view, metrics)  # 无可叠加项 → 原样返回

    def test_overlay_fail_result_propagates(self):
        metrics = _metrics_with_pending_self_corr()
        view = overlay_cached_checks(metrics, _cached_entry(False))
        sc = next(c for c in view["checks"] if c["name"] == "SELF_CORRELATION")
        self.assertIs(sc["pass"], False)
        self.assertIs(view["passed"], False)

    def test_overlay_noop_without_cache(self):
        metrics = _metrics_with_pending_self_corr()
        self.assertIs(overlay_cached_checks(metrics, None), metrics)

    def test_overlay_reapplies_configured_correlation_limit(self):
        metrics = _metrics_with_pending_self_corr()
        view = overlay_cached_checks(metrics, _cached_entry(True), 0.3)
        self_corr = next(
            check for check in view["checks"]
            if check["name"] == "SELF_CORRELATION"
        )
        self.assertIs(self_corr["pass"], False)
        self.assertEqual(self_corr["limit"], 0.3)

    def test_overlay_accepts_result_only_cached_check(self):
        metrics = _metrics_with_pending_self_corr()
        cached = _cached_entry(True)
        cached["checks"][1].pop("pass")
        cached["checks"][1]["result"] = "PASS"
        view = overlay_cached_checks(metrics, cached)
        self.assertIs(
            next(c for c in view["checks"] if c["name"] == "SELF_CORRELATION")["pass"],
            True,
        )
        self.assertIs(view["passed"], True)


class TestEvidenceCacheIO(unittest.TestCase):
    def test_cache_memory_view_is_bounded_and_drops_non_objects(self):
        cache = {
            "old": {"updated_at": "2026-01-01"},
            "new": {"updated_at": "2026-09-07"},
            "bad": None,
        }
        bounded = _bounded_cache(cache, max_entries=1)
        self.assertEqual(list(bounded), ["new"])

    def test_save_load_roundtrip_and_corrupt_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = {"abc123": _cached_entry(True)}
            save_evidence_cache(tmp, cache)
            loaded = load_evidence_cache(tmp)
            self.assertIn("abc123", loaded)
            self.assertIs(loaded["abc123"]["passed"], True)
            with open(os.path.join(tmp, "evidence_cache.json"), "w") as f:
                f.write("{corrupt")
            self.assertEqual(load_evidence_cache(tmp), {})

    def test_refresh_reads_settled_self_correlation_endpoint(self):
        class Response:
            status_code = 200
            headers = {}

            @staticmethod
            def json():
                return {
                    "schema": {"properties": [
                        {"name": "alpha"}, {"name": "correlation"}
                    ]},
                    "records": [["old-alpha", -0.31], ["other", 0.42]],
                }

        class Session:
            def get(self, url, timeout):
                self.url = url
                return Response()

        class Client:
            base_url = "https://brain.example"

            def __init__(self):
                self.session = Session()

            def get_self_correlation(self, alpha_id, timeout_sec=20):
                return self.session.get(
                    f"{self.base_url}/alphas/{alpha_id}/correlations/self",
                    timeout=60,
                ).json()

        with tempfile.TemporaryDirectory() as tmp:
            count = refresh_self_correlation_cache(Client(), tmp, ["new-alpha"])
            self.assertEqual(count, 1)
            cached = load_evidence_cache(tmp)["new-alpha"]
            check = cached["checks"][0]
            self.assertEqual(check["result"], "PASS")
            self.assertEqual(check["value"], 0.42)

    def test_refresh_reads_nested_is_data_correlation_shape(self):
        class Client:
            def get_self_correlation(self, alpha_id, timeout_sec=20):
                return {
                    "is": {
                        "data": [
                            {"correlation": -0.21},
                            {"correlation": 0.33},
                        ],
                    },
                }

        with tempfile.TemporaryDirectory() as tmp:
            count = refresh_self_correlation_cache(Client(), tmp, ["nested-alpha"])
            self.assertEqual(count, 1)
            check = load_evidence_cache(tmp)["nested-alpha"]["checks"][0]
            self.assertEqual(check["value"], 0.33)
            self.assertEqual(check["result"], "PASS")

    def test_refresh_uses_configured_correlation_limit(self):
        class Response:
            status_code = 200
            headers = {}

            @staticmethod
            def json():
                return {"max": 0.42, "records": []}

        class Session:
            def get(self, url, timeout):
                return Response()

        class Client:
            base_url = "https://brain.example"

            def __init__(self):
                self.session = Session()

            def get_self_correlation(self, alpha_id, timeout_sec=20):
                return self.session.get(
                    f"{self.base_url}/alphas/{alpha_id}/correlations/self",
                    timeout=60,
                ).json()

        with tempfile.TemporaryDirectory() as tmp:
            count = refresh_self_correlation_cache(
                Client(), tmp, ["strict-alpha"], correlation_limit=0.4
            )
            self.assertEqual(count, 1)
            check = load_evidence_cache(tmp)["strict-alpha"]["checks"][0]
            self.assertEqual(check["result"], "FAIL")
            self.assertEqual(check["limit"], 0.4)

    def test_refresh_batches_advisory_cache_writes(self):
        class Response:
            status_code = 200
            headers = {}

            @staticmethod
            def json():
                return {"max": 0.2, "records": []}

        class Session:
            def get(self, url, timeout):
                return Response()

        class Client:
            base_url = "https://brain.example"

            def __init__(self):
                self.session = Session()

            def get_self_correlation(self, alpha_id, timeout_sec=20):
                return self.session.get(
                    f"{self.base_url}/alphas/{alpha_id}/correlations/self",
                    timeout=60,
                ).json()

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("wqb_agent.evidence.save_evidence_cache") as saver:
                count = refresh_self_correlation_cache(
                    Client(), tmp, [f"alpha-{i}" for i in range(9)]
                )
            self.assertEqual(count, 9)
            self.assertEqual(saver.call_count, 3)

    def test_refresh_skips_resolved_cache_entries(self):
        class Session:
            calls = 0

            def get(self, url, timeout):
                self.calls += 1
                raise AssertionError("resolved evidence must not be fetched again")

        class Client:
            base_url = "https://brain.example"

            def __init__(self):
                self.session = Session()

            def get_self_correlation(self, alpha_id, timeout_sec=20):
                raise AssertionError("resolved evidence must not be fetched")

        with tempfile.TemporaryDirectory() as tmp:
            save_evidence_cache(tmp, {"alpha": _cached_entry(True)})
            count = refresh_self_correlation_cache(
                Client(), tmp, ["alpha", "alpha"]
            )
            self.assertEqual(count, 0)

    def test_refresh_ignores_malformed_correlation_values_fail_closed(self):
        class Response:
            status_code = 200
            headers = {}

            @staticmethod
            def json():
                return {"records": [["alpha", "not-a-number"]]}

        class Session:
            def get(self, url, timeout):
                return Response()

        class Client:
            base_url = "https://brain.example"
            session = Session()

            def get_self_correlation(self, alpha_id, timeout_sec=20):
                return self.session.get(
                    f"{self.base_url}/alphas/{alpha_id}/correlations/self",
                    timeout=60,
                ).json()

        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(refresh_self_correlation_cache(Client(), tmp, ["alpha"]), 0)
            self.assertFalse(os.path.exists(os.path.join(tmp, "evidence_cache.json")))

    def test_refresh_ignores_non_object_platform_payload(self):
        class Response:
            status_code = 200
            headers = {}

            @staticmethod
            def json():
                return ["unexpected", "payload"]

        class Session:
            def get(self, url, timeout):
                return Response()

        class Client:
            base_url = "https://brain.example"
            session = Session()

            def get_self_correlation(self, alpha_id, timeout_sec=20):
                return self.session.get(
                    f"{self.base_url}/alphas/{alpha_id}/correlations/self",
                    timeout=60,
                ).json()

        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(refresh_self_correlation_cache(Client(), tmp, ["alpha"]), 0)
            self.assertFalse(os.path.exists(os.path.join(tmp, "evidence_cache.json")))

    def test_refresh_network_error_stays_advisory_and_fail_closed(self):
        class Session:
            def get(self, url, timeout):
                raise requests.exceptions.ConnectionError("temporary network")

        class Client:
            base_url = "https://brain.example"
            session = Session()

            def get_self_correlation(self, alpha_id, timeout_sec=20):
                try:
                    return self.session.get(
                        f"{self.base_url}/alphas/{alpha_id}/correlations/self",
                        timeout=60,
                    ).json()
                except requests.exceptions.RequestException as exc:
                    from wqb_agent.client import WQBSimulationError
                    raise WQBSimulationError("temporary network") from exc

        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(refresh_self_correlation_cache(Client(), tmp, ["alpha"]), 0)
            self.assertFalse(os.path.exists(os.path.join(tmp, "evidence_cache.json")))

    def test_refresh_skips_unhashable_alpha_ids(self):
        class Client:
            def get_self_correlation(self, alpha_id, timeout_sec=20):
                raise AssertionError("malformed ids must be skipped")

        with tempfile.TemporaryDirectory() as tmp:
            count = refresh_self_correlation_cache(
                Client(), tmp, [["bad"], {"id": "bad"}, None]
            )
            self.assertEqual(count, 0)
            self.assertFalse(os.path.exists(os.path.join(tmp, "evidence_cache.json")))


if __name__ == "__main__":
    unittest.main()
