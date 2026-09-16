import json
import os
import tempfile
import unittest

from wqb_agent import research_api
from wqb_agent.client import WQBSubmitUnknownError
from wqb_agent.optimization_interfaces import RemoteAlphaEvidenceProvider
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


class TestSimulationGateway(unittest.TestCase):
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
