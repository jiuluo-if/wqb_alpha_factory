"""Tests for the client refactor + efficiency optimizations.

Covers what the git_selfbqr comparison contributed:
- classified client exceptions (WQBRejectedError / WQBRateLimitError /
  WQBNotFoundError / WQBTimeoutError) and their failure-kind mapping
- thread-local sessions (concurrent-safe authentication)
- FieldDiscovery disk cache (cross-run, TTL-bounded)
- classify_experiment recognizes the new exception names
"""

import json
import os
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wqb_agent.client import (
    WQBAuthError,
    WQBClient,
    WQBNotFoundError,
    WQBQueryTooBroadError,
    WQBRateLimitError,
    WQBRejectedError,
    WQBSimulationError,
    WQBSubmitUnknownError,
    WQBTimeoutError,
)
from wqb_agent.discovery import FieldDiscovery
from wqb_agent.failures import FailureKind


class TestProgressUrlSafety(unittest.TestCase):
    def setUp(self):
        self.client = WQBClient.__new__(WQBClient)
        self.client.base_url = "https://api.example.test"

    def test_relative_and_same_origin_urls_normalize(self):
        self.assertEqual(self.client._normalize_progress_url("/simulations/1"), "https://api.example.test/simulations/1")
        self.assertEqual(self.client._normalize_progress_url("https://api.example.test/simulations/2"), "https://api.example.test/simulations/2")

    def test_cross_origin_downgrade_port_and_fragment_are_rejected(self):
        for url in ("https://evil.example/simulations/1", "http://api.example.test/simulations/1", "https://api.example.test:444/simulations/1", "https://api.example.test/simulations/1#fragment"):
            with self.assertRaises(WQBSimulationError):
                self.client._normalize_progress_url(url)

    def test_accepted_invalid_location_is_submit_unknown(self):
        response = mock.Mock(headers={"Location": "https://evil.example/job"})
        self.client._wait_submission_slot = mock.Mock()
        self.client._request = mock.Mock(return_value=response)
        with self.assertRaises(WQBSubmitUnknownError):
            self.client.submit_simulation("rank(x)", {})
        self.client._request.assert_called_once()

    def test_invalid_persisted_url_makes_no_get(self):
        self.client._session = mock.Mock()
        with self.assertRaises(WQBSimulationError):
            self.client.get_progress_snapshot("https://evil.example/job")
        self.client._session.assert_not_called()


class FakeResponse:
    def __init__(self, status_code=200, text="", headers=None, payload=None):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}
        self._payload = payload or {}

    def json(self):
        return self._payload


class FakeSession:
    """Minimal requests.Session stub: returns queued responses for GET/POST."""

    def __init__(self, responses):
        self.responses = list(responses)

    def get(self, url, **kwargs):
        return self.responses.pop(0)

    def post(self, url, **kwargs):
        return self.responses.pop(0)

    def request(self, method, url, **kwargs):
        return self.responses.pop(0)


def make_client():
    c = WQBClient.__new__(WQBClient)
    c._local = threading.local()
    c._local.authenticated = True  # skip auth handshake
    c.max_retries = 2
    c.base_url = "https://api.worldquantbrain.com"
    return c


class TestPollProgressRejectsErrorStatus(unittest.TestCase):
    """A terminal ERROR payload from the platform (syntax/settings rejection)
    must raise WQBRejectedError carrying the real platform sim id — not
    WQBSimulationError/"without alpha id" that used to misclassify it as
    UNKNOWN and pause the whole dispatch."""

    def test_error_status_raises_rejected_with_real_id(self):
        c = make_client()
        c._local.session = FakeSession([
            FakeResponse(
                status_code=200,
                headers={},
                payload={
                    "id": "2OyvSjcle4UH8LG1cegtJaNS",
                    "type": "REGULAR",
                    "status": "ERROR",
                    "message": 'Required attribute "lookback" must have a value.',
                },
            )
        ])
        with self.assertRaises(WQBRejectedError) as ctx:
            c.poll_progress("/simulations/abc")
        msg = str(ctx.exception)
        self.assertIn("2OyvSjcle4UH8LG1cegtJaNS", msg)
        self.assertIn("lookback", msg)

    def test_failed_status_aliases_raise_rejected(self):
        for status, sim_id, message in (
            ("FAILED", "sim-1", "boom"),
            ("FAIL", "sim-2", ""),
        ):
            with self.subTest(status=status):
                c = make_client()
                c._local.session = FakeSession([
                    FakeResponse(
                        status_code=200,
                        headers={},
                        payload={"id": sim_id, "type": "REGULAR", "status": status,
                                 "message": message},
                    )
                ])
                with self.assertRaises(WQBRejectedError) as ctx:
                    c.poll_progress("/simulations/abc")
                self.assertIn(sim_id, str(ctx.exception))
                if status == "FAIL":
                    self.assertIn("status=FAIL", str(ctx.exception))

    def test_complete_returns_alpha_id(self):
        c = make_client()
        c._local.session = FakeSession([
            FakeResponse(
                status_code=200,
                headers={},
                payload={"id": "sim-1", "type": "REGULAR", "status": "COMPLETE",
                         "alpha": "KP73prPz"},
            )
        ])
        self.assertEqual(c.poll_progress("/simulations/abc"), "KP73prPz")

    def test_non_object_progress_payload_fails_closed(self):
        c = make_client()
        c._local.session = FakeSession([
            FakeResponse(status_code=200, headers={}, payload=["unexpected"]),
        ])
        with self.assertRaises(WQBSimulationError):
            c.poll_progress("/simulations/abc")

    def test_repeated_progress_401_is_bounded(self):
        c = make_client()
        c._local.session = FakeSession([
            FakeResponse(status_code=401),
            FakeResponse(status_code=401),
        ])
        with mock.patch.object(c, "_ensure_auth"), \
             mock.patch.object(c, "_set_authenticated"):
            with self.assertRaises(WQBAuthError):
                c.poll_progress("/simulations/abc", timeout_sec=60)


class TestOperatorCapabilityClient(unittest.TestCase):
    def test_operator_capability_uses_get_request_and_live_truth(self):
        c = make_client()
        c._local.session = FakeSession([
            FakeResponse(200, headers={}, payload={"operators": [{"name": "rank"}]})
        ])
        result = c.get_operator_capability()
        self.assertEqual(result["status"], "LIVE_VERIFIED")
        self.assertEqual(result["availability"], "AVAILABLE")

    def test_multi_submission_posts_an_array_with_one_idempotency_key(self):
        c = make_client()
        response = FakeResponse(201, headers={"Location": "/multi/1"})
        with mock.patch.object(c, "_request", return_value=response) as request:
            result = c.submit_multi_simulation([
                {"expression": "rank(a)", "settings": {"delay": 1}},
                {"expression": "rank(b)", "settings": {"delay": 1}},
            ], idempotency_key="multi-fingerprint")

        self.assertEqual(result, "https://api.worldquantbrain.com/multi/1")
        request.assert_called_once()
        self.assertEqual(request.call_args.args[:2], (
            "POST", "https://api.worldquantbrain.com/simulations",
        ))
        self.assertEqual(request.call_args.kwargs["json"][0]["regular"], "rank(a)")
        self.assertEqual(
            request.call_args.kwargs["headers"],
            {"X-Idempotency-Key": "multi-fingerprint"},
        )
        self.assertNotEqual(request.call_args.kwargs.get("retry_rate_limit"), False)

    def test_multi_progress_resolves_child_simulations_without_a_new_post(self):
        c = make_client()
        with mock.patch.object(c, "get_progress_snapshot", return_value={
            "status_code": 200,
            "headers": {},
            "payload": {"status": "COMPLETE", "children": ["sim-1", "sim-2"]},
            "retry_after_seconds": 1.0,
        }), mock.patch.object(
            c, "poll_progress", side_effect=["alpha-1", "alpha-2"]
        ) as poll:
            result = c.poll_multi_progress(f"{c.base_url}/multi/1")

        self.assertEqual(result, ["alpha-1", "alpha-2"])
        self.assertEqual(
            [call.args[0] for call in poll.call_args_list],
            [f"{c.base_url}/simulations/sim-1",
             f"{c.base_url}/simulations/sim-2"],
        )


class TestPublicReadAdapters(unittest.TestCase):
    def test_progress_snapshot_returns_transport_neutral_payload(self):
        c = make_client()
        c._local.session = FakeSession([
            FakeResponse(200, headers={"Retry-After": "1"},
                         text="pending", payload={"status": "RUNNING"}),
        ])
        snapshot = c.get_progress_snapshot("/simulations/s1")
        self.assertEqual(snapshot["status_code"], 200)
        self.assertEqual(snapshot["payload"]["status"], "RUNNING")
        self.assertEqual(snapshot["retry_after_seconds"], 1.0)

    def test_get_user_alphas_reads_a_bounded_page(self):
        c = make_client()
        c._local.session = FakeSession([
            FakeResponse(
                200,
                payload={
                    "count": 1,
                    "next": None,
                    "previous": None,
                    "results": [{
                        "id": "alpha-1",
                        "status": "UNSUBMITTED",
                        "dateCreated": "2026-09-09T08:00:00-04:00",
                    }],
                },
            ),
        ])
        page = c.get_user_alphas(status="UNSUBMITTED", limit=5, offset=10)
        self.assertEqual(page["count"], 1)
        self.assertEqual(page["results"][0]["id"], "alpha-1")

    def test_get_all_user_alphas_follows_pages_without_duplicate_ids(self):
        c = make_client()
        c._local.session = FakeSession([
            FakeResponse(200, payload={
                "count": 3,
                "next": "page-2",
                "previous": None,
                "results": [
                    {"id": "alpha-1", "status": "UNSUBMITTED"},
                    {"id": "alpha-2", "status": "UNSUBMITTED"},
                ],
            }),
            FakeResponse(200, payload={
                "count": 3,
                "next": None,
                "previous": "page-1",
                "results": [
                    {"id": "alpha-2", "status": "UNSUBMITTED"},
                    {"id": "alpha-3", "status": "UNSUBMITTED"},
                ],
            }),
        ])

        rows = c.get_all_user_alphas(status="UNSUBMITTED", limit=2)

        self.assertEqual([row["id"] for row in rows], [
            "alpha-1", "alpha-2", "alpha-3",
        ])

    def test_get_all_user_alphas_fails_closed_on_incomplete_last_page(self):
        c = make_client()
        c._local.session = FakeSession([
            FakeResponse(200, payload={
                "count": 3,
                "next": None,
                "previous": None,
                "results": [{"id": "alpha-1"}],
            }),
        ])

        with self.assertRaises(WQBSimulationError):
            c.get_all_user_alphas(limit=2)

    def test_get_all_user_alphas_rejects_a_window_over_platform_page_limit(self):
        c = make_client()
        with mock.patch.object(c, "get_user_alphas", return_value={
            "count": 1001, "next": "page-2", "results": [{"id": "alpha-1"}],
        }):
            with self.assertRaises(WQBQueryTooBroadError):
                c.get_all_user_alphas(limit=100, max_results=1000)


class TestAlphaRecordset(unittest.TestCase):
    """The PnL recordset body is optional: a freshly simulated Alpha whose
    PnL has not settled yet answers 200 with an empty body.  That must read
    as "no data" (None), not as a JSON parse error breaking the snapshot."""

    class EmptyBodyResponse:
        status_code = 200
        text = ""
        headers = {}

        def json(self):
            raise ValueError("Expecting value: line 1 column 1 (char 0)")

    def test_recordset_returns_payload_when_present(self):
        c = make_client()
        c._local.session = FakeSession([
            FakeResponse(200, payload={"records": [{"date": "2026-09-01"}]}),
        ])
        self.assertEqual(
            c.get_recordset("alpha-1", "pnl"),
            {"records": [{"date": "2026-09-01"}]},
        )

    def test_recordset_empty_body_reads_as_no_data(self):
        c = make_client()
        c._local.session = FakeSession([self.EmptyBodyResponse()])
        self.assertIsNone(c.get_recordset("alpha-1", "pnl"))

    def test_recordset_rejects_unknown_names(self):
        c = make_client()
        with self.assertRaises(ValueError):
            c.get_recordset("alpha-1", "evil")


class TestClassifiedExceptions(unittest.TestCase):
    def setUp(self):
        self.client = WQBClient.__new__(WQBClient)
        self.client._local = threading.local()

    def test_classified_exception_contract(self):
        from wqb_agent.client import WQBError

        mappings = (
            (401, "auth", WQBAuthError, FailureKind.AUTH),
            (429, "slow", WQBRateLimitError, FailureKind.RATE_LIMIT),
            (422, "bad expr", WQBRejectedError, FailureKind.SYNTAX),
            (400, "bad settings", WQBRejectedError, FailureKind.SYNTAX),
            (404, "not found", WQBNotFoundError, FailureKind.DATA),
            (500, "boom", WQBSimulationError, None),
        )
        for status, text, expected_type, expected_kind in mappings:
            with self.subTest(status=status):
                error = self.client._classified_exception(status, text, "x")
                self.assertIsInstance(error, expected_type)
                self.assertIsInstance(error, WQBError)
                if expected_kind is not None:
                    self.assertEqual(expected_type.kind, expected_kind)

        for expected_type, expected_kind in (
            (WQBTimeoutError, FailureKind.TIMEOUT),
            (WQBAuthError, FailureKind.AUTH),
        ):
            with self.subTest(exception=expected_type.__name__):
                self.assertIsInstance(expected_type("test"), WQBError)
                self.assertEqual(expected_type.kind, expected_kind)

class TestSharedRateLimitGate(unittest.TestCase):
    def test_retry_after_longer_than_budget_fails_without_sleeping_full_delay(self):
        c = make_client()
        c._local.session = FakeSession([
            FakeResponse(429, headers={"Retry-After": "3600"}),
        ])
        with mock.patch.object(c, "_wait_rate_limit_gate"), \
             mock.patch.object(c, "_register_rate_limit") as register, \
             mock.patch("wqb_agent.client.time.sleep") as sleep:
            with self.assertRaises(WQBRateLimitError):
                c._request("GET", "/x", context="test", rate_limit_budget_sec=5)
        register.assert_called_once()
        sleep.assert_not_called()

    def test_repeated_429_does_not_consume_transport_retries(self):
        """Even more 429s than max_retries must still reach the success response."""
        c = make_client()
        c.max_retries = 1
        c._local.session = FakeSession([
            FakeResponse(429, headers={"Retry-After": "1"}) for _ in range(5)
        ] + [FakeResponse(200, payload={"ok": True})])
        with mock.patch.object(c, "_wait_rate_limit_gate"), \
             mock.patch.object(c, "_register_rate_limit"):
            response = c._request("GET", "/x", context="test", rate_limit_budget_sec=60)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])

    def test_retry_after_http_date_is_supported(self):
        response = FakeResponse(429, headers={
            "Retry-After": "Wed, 21 Oct 2099 07:28:00 GMT"
        })
        self.assertGreater(WQBClient._retry_after_seconds(response), 1.0)

    def test_simulation_submissions_are_staggered(self):
        c = make_client()
        # Leave enough headroom for busy Windows CI scheduling; 10 ms can
        # elapse in test/mocking overhead before the second call reaches the
        # gate, producing a false negative despite correct production logic.
        c.submit_spacing_sec = 0.05
        c._submit_lock = threading.Lock()
        c._next_submit_at = 0.0
        times = []

        def fake_request(*args, **kwargs):
            times.append(time.monotonic())
            return FakeResponse(201, headers={"Location": "/sim/1"})

        import time
        with mock.patch.object(c, "_request", side_effect=fake_request):
            c.submit_simulation("rank(a)", {})
            c.submit_simulation("rank(b)", {})
        self.assertGreaterEqual(times[1] - times[0], 0.04)

    def test_submission_slot_rechecks_gate_after_concurrent_429(self):
        """A 429 arriving while waiting for the slot must be observed."""
        c = make_client()
        c.submit_spacing_sec = 0.0
        c._rate_limit_lock = threading.Lock()
        c._rate_limit_until = 0.0
        c._submit_lock = threading.Lock()
        c._next_submit_at = 0.0
        c._submit_lock.acquire()
        first_gate_check = threading.Event()
        second_gate_check = threading.Event()
        gate_checks = []

        def wait_gate():
            gate_checks.append(c._rate_limit_until > time.monotonic())
            if len(gate_checks) == 1:
                first_gate_check.set()
            else:
                second_gate_check.set()
                # A real wait would return only after the gate opens.  Clearing
                # the synthetic gate lets the assertion inspect the POST edge.
                with c._rate_limit_lock:
                    c._rate_limit_until = 0.0
            return True

        post_gate_state = []

        def fake_request(*args, **kwargs):
            post_gate_state.append(c._rate_limit_until > time.monotonic())
            return FakeResponse(201, headers={"Location": "/sim/1"})

        import time
        with mock.patch.object(c, "_wait_rate_limit_gate", side_effect=wait_gate), \
             mock.patch.object(c, "_request", side_effect=fake_request):
            worker = threading.Thread(
                target=lambda: c.submit_simulation("rank(a)", {})
            )
            worker.start()
            self.assertTrue(first_gate_check.wait(1.0))
            c._register_rate_limit(FakeResponse(429, headers={"Retry-After": "60"}))
            c._submit_lock.release()
            worker.join(1.0)

        self.assertFalse(worker.is_alive())
        self.assertEqual(gate_checks, [False, True])
        self.assertEqual(post_gate_state, [False])
        self.assertTrue(second_gate_check.is_set())

    def test_request_rechecks_gate_after_auth_before_transport(self):
        c = make_client()
        c._local.session = FakeSession([
            FakeResponse(200, payload={"ok": True}),
        ])
        with mock.patch.object(c, "_wait_rate_limit_gate", side_effect=[True, False]), \
             mock.patch.object(c, "_ensure_auth"):
            with self.assertRaises(WQBRateLimitError):
                c._request("POST", "/simulations", context="submit", rate_limit_budget_sec=5)


class TestDiscoveryDiskCache(unittest.TestCase):
    class FakeClient:
        def __init__(self):
            self.calls = []

        def get_datafields(self, dataset_id, limit=50, offset=0, field_type=None):
            # field_type 用于区分两轮拉取（MATRIX/VECTOR），生成不同 id，
            # 以便验证两类型字段都被发现且按 id 去重。
            prefix = (field_type or "MATRIX").lower()
            self.calls.append((dataset_id, offset, field_type))
            results = [
                {"id": f"{prefix}_{dataset_id}_f{offset + i}", "name": f"n{i}",
                 "description": "d", "dataset": {"id": dataset_id}}
                for i in range(limit)
            ]
            return results, 200

    def test_cache_hits_disk(self):
        tmp = tempfile.mkdtemp(prefix="wqb_test_disc_")
        cache_path = os.path.join(tmp, "fields_cache.json")
        client = self.FakeClient()
        d = FieldDiscovery(client, pagination_limit=50, max_pages=20,
                           cache_path=cache_path, cache_ttl_sec=3600)
        fields1 = d._fields_for("news18")
        # MATRIX + VECTOR 各 4 页（count=200），共 400 字段
        self.assertEqual(len(fields1), 400)
        self.assertTrue(os.path.exists(cache_path))
        # 两类型都被拉取（探索 Vector 字段族的前提）
        self.assertEqual({c[2] for c in client.calls}, {"MATRIX", "VECTOR"})
        # second instance should hit the disk cache: no API calls
        client2 = self.FakeClient()
        d2 = FieldDiscovery(client2, pagination_limit=50, max_pages=20,
                            cache_path=cache_path, cache_ttl_sec=3600)
        fields2 = d2._fields_for("news18")
        self.assertEqual(len(fields2), 400)
        self.assertEqual(client2.calls, [])

    def test_stale_cache_refetches(self):
        tmp = tempfile.mkdtemp(prefix="wqb_test_disc2_")
        cache_path = os.path.join(tmp, "fields_cache.json")
        client = self.FakeClient()
        d = FieldDiscovery(client, cache_path=cache_path, cache_ttl_sec=3600)
        d._fields_for("pv1")
        # rewrite saved_at to the past so the cache is stale
        with open(cache_path, encoding="utf-8") as f:
            data = json.load(f)
        data["saved_at"] = 0
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(data, f)
        client2 = self.FakeClient()
        d2 = FieldDiscovery(client2, cache_path=cache_path, cache_ttl_sec=3600)
        d2._fields_for("pv1")
        self.assertTrue(client2.calls)  # refetched from the API


if __name__ == "__main__":
    unittest.main()
