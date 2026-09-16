import json
import os
import tempfile
import threading
import time
import unittest

from wqb_agent import research_api
from wqb_agent.client import WQBSubmitUnknownError
from wqb_agent.remote_evidence import RemoteAlphaEvidenceProvider
from wqb_agent.simulation_gateway import (
    ExecutionGuard,
    SimulationGateway,
    SimulationSpec,
)


class FakeGatewayClient:
    def __init__(self, *, unknown=False):
        self.unknown = unknown
        self.submissions = []

    def submit_simulation(self, expression, settings, **kwargs):
        self.submissions.append((expression, settings, kwargs))
        if self.unknown:
            raise WQBSubmitUnknownError("response ambiguous")
        return "progress-1"

    def poll_progress(self, progress_url, **kwargs):
        self.assert_progress_url = progress_url
        return "alpha-1"

    def get_alpha(self, alpha_id):
        return {"id": alpha_id, "regular": "rank(close)", "is": {"sharpe": 1.0}}

    def get_aggregates(self, alpha_id):
        return {"alpha_id": alpha_id, "years": []}

    def get_pnl(self, alpha_id):
        return {"alpha_id": alpha_id, "records": []}

    def get_self_correlation(self, alpha_id):
        return {"alpha_id": alpha_id, "status": "AVAILABLE", "value": 0.1}


class CapabilityGatewayClient(FakeGatewayClient):
    def __init__(self):
        super().__init__()
        self.capability_calls = 0

    def get_operator_capability(self):
        self.capability_calls += 1
        return {"valid": True, "availability": "AVAILABLE", "operators": ["rank"]}


class UnavailableCapabilityGatewayClient(FakeGatewayClient):
    def get_operator_capability(self):
        return None


class RemoteHistoryGatewayClient(FakeGatewayClient):
    def __init__(self):
        super().__init__()
        self.history_calls = 0

    def get_all_user_alphas(self, **_kwargs):
        self.history_calls += 1
        return [{
            "id": "alpha-existing", "regular": "rank(close)",
            "settings": {"delay": 1}, "status": "UNSUBMITTED",
        }]


class ConcurrentGatewayClient(FakeGatewayClient):
    def __init__(self):
        super().__init__()
        self._active = 0
        self._active_lock = threading.Lock()
        self.max_active = 0

    def submit_simulation(self, expression, settings, **kwargs):
        with self._active_lock:
            self._active += 1
            self.max_active = max(self.max_active, self._active)
        try:
            time.sleep(0.03)
            return super().submit_simulation(expression, settings, **kwargs)
        finally:
            with self._active_lock:
                self._active -= 1


class MultiGatewayClient(FakeGatewayClient):
    def __init__(self):
        super().__init__()
        self.multi_submissions = []
        self._active_multi = 0
        self._active_lock = threading.Lock()
        self.max_active_multi = 0
        self._multi_sizes = {}

    def submit_multi_simulation(self, payloads, **kwargs):
        with self._active_lock:
            self._active_multi += 1
            self.max_active_multi = max(self.max_active_multi, self._active_multi)
        try:
            time.sleep(0.03)
            self.multi_submissions.append((payloads, kwargs))
            progress_url = f"multi-progress-{len(self.multi_submissions)}"
            self._multi_sizes[progress_url] = len(payloads)
            return progress_url
        finally:
            with self._active_lock:
                self._active_multi -= 1

    def poll_multi_progress(self, progress_url, **kwargs):
        return [
            f"multi-alpha-{progress_url}-{index}"
            for index in range(self._multi_sizes[progress_url])
        ]


class UnknownMultiGatewayClient(MultiGatewayClient):
    def submit_multi_simulation(self, payloads, **kwargs):
        self.multi_submissions.append((payloads, kwargs))
        raise WQBSubmitUnknownError("multi response ambiguous")


class TestSimulationGateway(unittest.TestCase):
    def test_public_batch_uses_bounded_gateway_concurrency(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = ConcurrentGatewayClient()
            config = {
                "simulation": {},
                "runtime": {"max_concurrent_sims": 2},
            }
            specs = [
                SimulationSpec("rank(close)", {"delay": 1}),
                SimulationSpec("rank(open)", {"delay": 1}),
                SimulationSpec("rank(high)", {"delay": 1}),
            ]

            results = research_api.simulate_batch(
                specs, client=client, config=config, state_dir=tmp
            )

            self.assertEqual([item["status"] for item in results], ["DONE"] * 3)
            self.assertEqual(len(client.submissions), 3)
            self.assertEqual(client.max_active, 2)

    def test_single_batch_defaults_to_ten_concurrent_simulations(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = ConcurrentGatewayClient()
            gateway = SimulationGateway(client, state_dir=tmp)
            specs = [
                SimulationSpec(f"rank(field_{index})", {"delay": 1})
                for index in range(11)
            ]

            results = gateway.simulate_batch(specs)

            self.assertEqual([item["status"] for item in results], ["DONE"] * 11)
            self.assertEqual(client.max_active, 10)

    def test_multi_batch_groups_ten_children_and_bounds_eight_jobs(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = MultiGatewayClient()
            gateway = SimulationGateway(client, state_dir=tmp)
            specs = [
                SimulationSpec(f"rank(field_{index})", {"delay": 1})
                for index in range(21)
            ]

            results = gateway.simulate_multi_batch(
                specs, child_batch_size=10, max_concurrent_multi=2
            )

            self.assertEqual([item["status"] for item in results], ["DONE"] * 21)
            self.assertEqual(
                [len(payloads) for payloads, _kwargs in client.multi_submissions],
                [10, 10, 1],
            )
            self.assertTrue(all(
                payload["type"] == "REGULAR"
                and payload["regular"].startswith("rank(field_")
                for payloads, _kwargs in client.multi_submissions
                for payload in payloads
            ))
            self.assertEqual(client.max_active_multi, 2)
            self.assertEqual(gateway.guard.entries(), [])

    def test_multi_unknown_keeps_one_parent_guard_and_never_reposts(self):
        with tempfile.TemporaryDirectory() as tmp:
            specs = [
                SimulationSpec(f"rank(field_{index})", {"delay": 1})
                for index in range(2)
            ]
            first_client = UnknownMultiGatewayClient()
            first = SimulationGateway(first_client, state_dir=tmp)
            first_results = first.simulate_multi_batch(specs)

            self.assertEqual(
                [item["status"] for item in first_results],
                ["SUBMIT_UNKNOWN", "SUBMIT_UNKNOWN"],
            )
            self.assertEqual(len(first.guard.entries()), 1)

            second_client = UnknownMultiGatewayClient()
            second = SimulationGateway(second_client, state_dir=tmp)
            second_results = second.simulate_multi_batch(specs)

            self.assertEqual(
                [item["status"] for item in second_results],
                ["SUBMIT_UNKNOWN", "SUBMIT_UNKNOWN"],
            )
            self.assertEqual(second_client.multi_submissions, [])

    def test_batch_marks_unsubmitted_tail_without_creating_unknown_guard(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeGatewayClient(unknown=True)
            gateway = SimulationGateway(client, state_dir=tmp, max_concurrent=1)
            specs = [
                SimulationSpec("rank(close)", {"delay": 1}),
                SimulationSpec("rank(open)", {"delay": 1}),
            ]

            results = gateway.simulate_batch(specs)

            self.assertEqual(results[0]["status"], "SUBMIT_UNKNOWN")
            self.assertEqual(results[1]["status"], "NOT_DISPATCHED")
            self.assertEqual(
                [row["status"] for row in gateway.guard.entries()],
                ["SUBMIT_UNKNOWN"],
            )

    def test_batch_reuses_live_capability_and_history_reads(self):
        with tempfile.TemporaryDirectory() as tmp:
            capability_client = CapabilityGatewayClient()
            capability_gateway = SimulationGateway(
                capability_client, state_dir=tmp, max_concurrent=2
            )
            capability_gateway.simulate_batch([
                SimulationSpec("rank(close)", {"delay": 1}),
                SimulationSpec("rank(open)", {"delay": 1}),
            ])

            self.assertEqual(capability_client.capability_calls, 1)

        with tempfile.TemporaryDirectory() as tmp:
            history_client = RemoteHistoryGatewayClient()
            history_gateway = SimulationGateway(history_client, state_dir=tmp)
            history_gateway.simulate_batch([
                SimulationSpec("rank(close)", {"delay": 1}),
                SimulationSpec("rank(open)", {"delay": 1}),
            ])

            self.assertEqual(history_client.history_calls, 1)

    def test_batch_rejects_missing_capability_without_registering_prior_items(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = CapabilityGatewayClient()
            gateway = SimulationGateway(client, state_dir=tmp, max_concurrent=2)

            with self.assertRaisesRegex(ValueError, "OPERATOR_CAPABILITY_UNAVAILABLE"):
                gateway.simulate_batch([
                    SimulationSpec("rank(close)", {"delay": 1}),
                    SimulationSpec("ts_mean(close, 5)", {"delay": 1}),
                ])

            self.assertEqual(client.submissions, [])
            self.assertEqual(gateway.guard.entries(), [])

    def test_batch_treats_none_capability_as_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = UnavailableCapabilityGatewayClient()
            gateway = SimulationGateway(client, state_dir=tmp)

            with self.assertRaisesRegex(ValueError, "OPERATOR_CAPABILITY_UNAVAILABLE"):
                gateway.simulate_batch([
                    SimulationSpec("rank(close)", {"delay": 1}),
                ])

            self.assertEqual(client.submissions, [])

    def test_public_research_api_simulate_uses_gateway_without_agent_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeGatewayClient()
            result = research_api.simulate(
                {"expression": "rank(close)", "settings": {"delay": 1}},
                client=client, state_dir=tmp,
            )
            self.assertEqual(result["status"], "DONE")
            self.assertEqual(len(client.submissions), 1)
            self.assertEqual(research_api.get_pending_executions(state_dir=tmp), {"entries": []})

    def test_unverified_live_operator_is_rejected_before_post(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = CapabilityGatewayClient()
            with self.assertRaisesRegex(ValueError, "OPERATOR_CAPABILITY_UNAVAILABLE"):
                SimulationGateway(client, state_dir=tmp).simulate(
                    SimulationSpec("ts_mean(close, 5)", {"delay": 1})
                )
            self.assertEqual(client.submissions, [])

    def test_simulation_uses_spec_and_removes_guard_after_live_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeGatewayClient()
            gateway = SimulationGateway(client, state_dir=tmp, max_concurrent=1)

            result = gateway.simulate(SimulationSpec("rank(close)", {"delay": 1}))

            self.assertEqual(result["status"], "DONE")
            self.assertEqual(result["alpha_id"], "alpha-1")
            self.assertEqual(len(client.submissions), 1)
            self.assertFalse(os.path.exists(os.path.join(tmp, "trajectory.jsonl")))
            self.assertFalse(os.path.exists(os.path.join(tmp, "trial_ledger.jsonl")))
            self.assertEqual(ExecutionGuard(tmp).entries(), [])

    def test_active_exact_fingerprint_is_rejected_without_second_post(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeGatewayClient()
            gateway = SimulationGateway(client, state_dir=tmp, max_concurrent=1)
            spec = SimulationSpec("rank(close)", {"delay": 1})
            fingerprint = gateway.execution_fingerprint(spec)
            ExecutionGuard(tmp).register(fingerprint)

            result = gateway.simulate(spec)

            self.assertEqual(result["status"], "EXACT_DUPLICATE")
            self.assertEqual(result["fingerprint"], fingerprint)
            self.assertEqual(client.submissions, [])

    def test_recent_remote_exact_fingerprint_is_rejected_without_post(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = RemoteHistoryGatewayClient()
            result = SimulationGateway(client, state_dir=tmp).simulate(
                SimulationSpec("rank(close)", {"delay": 1})
            )
            self.assertEqual(result["status"], "EXACT_DUPLICATE")
            self.assertEqual(result["alpha_id"], "alpha-existing")
            self.assertEqual(client.submissions, [])

    def test_ambiguous_submit_is_persisted_and_restart_cannot_post_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = SimulationGateway(FakeGatewayClient(unknown=True), state_dir=tmp)
            spec = SimulationSpec("rank(close)", {"delay": 1})
            result = first.simulate(spec)
            self.assertEqual(result["status"], "SUBMIT_UNKNOWN")
            with open(os.path.join(tmp, "execution_guard.json"), encoding="utf-8") as handle:
                stored = json.load(handle)
            self.assertEqual(stored["entries"][0]["status"], "SUBMIT_UNKNOWN")

            second_client = FakeGatewayClient()
            second = SimulationGateway(second_client, state_dir=tmp)
            resumed = second.simulate(spec)

            self.assertEqual(resumed["status"], "SUBMIT_UNKNOWN")
            self.assertEqual(second_client.submissions, [])

    def test_restart_promotes_submitting_to_submit_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            spec = SimulationSpec("rank(close)", {"delay": 1})
            fingerprint = ExecutionGuard.fingerprint(spec.expression, spec.settings)
            ExecutionGuard(tmp).register(fingerprint, status="SUBMITTING")

            gateway = SimulationGateway(FakeGatewayClient(), state_dir=tmp)
            result = gateway.simulate(spec)

            self.assertEqual(result["status"], "SUBMIT_UNKNOWN")
            self.assertEqual(ExecutionGuard(tmp).find(fingerprint)["status"], "SUBMIT_UNKNOWN")

    def test_execution_guard_strips_research_result_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "execution_guard.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"entries": [{
                    "execution_fingerprint": "fp",
                    "status": "RUNNING",
                    "progress_url": "progress",
                    "created_at": 1,
                    "updated_at": 2,
                    "metrics": {"sharpe": 9},
                    "checks": ["PASS"],
                    "expression": "rank(close)",
                    "lineage_id": "private-lineage",
                }]}, handle)

            entry = ExecutionGuard(tmp).entries()[0]

            self.assertEqual(
                set(entry),
                {"execution_fingerprint", "status", "progress_url",
                 "created_at", "updated_at"},
            )
            self.assertNotIn("metrics", entry)

    def test_known_progress_url_recovery_only_polls_same_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            guard = ExecutionGuard(tmp)
            spec = SimulationSpec("rank(close)", {"delay": 1})
            fingerprint = guard.fingerprint(spec.expression, spec.settings)
            guard.register(fingerprint, progress_url="progress-known", status="RUNNING")
            client = FakeGatewayClient()
            gateway = SimulationGateway(client, state_dir=tmp)

            result = gateway.resume_execution(fingerprint)

            self.assertEqual(result["status"], "DONE")
            self.assertEqual(client.submissions, [])
            self.assertEqual(client.assert_progress_url, "progress-known")

    def test_public_remote_evidence_is_live_and_explicitly_sourced(self):
        evidence = research_api.get_alpha_evidence("alpha-1", client=FakeGatewayClient())
        self.assertEqual(evidence["source"], "LIVE")
        self.assertEqual(evidence["alpha_id"], "alpha-1")
        self.assertEqual(evidence["alpha"]["id"], "alpha-1")

    def test_remote_evidence_provider_exposes_named_read_operations(self):
        provider = RemoteAlphaEvidenceProvider(FakeGatewayClient())
        evidence = provider.get_alpha_evidence("alpha-1")
        self.assertEqual(evidence["source"], "LIVE")
        self.assertEqual(provider.get_alpha("alpha-1")["id"], "alpha-1")


if __name__ == "__main__":
    unittest.main()
