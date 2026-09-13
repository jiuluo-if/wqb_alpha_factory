import json
import os
import tempfile
import unittest

from wqb_agent.research_api import (
    ExperimentSpec,
    compare_experiments,
    discover_fields,
    get_experiment,
    get_operator_reference,
    inspect_optimizer_context,
    inspect_state,
    reconcile,
    run_experiment,
    search_history,
)
from wqb_agent.state import Experiment, Trajectory


class _Discovery:
    fields_per_discovery = 3

    def discover(self, query, target_count):
        return [{"id": "close", "type": "MATRIX"}][:target_count]

    def source_provenance(self):
        return {"kind": "brain_api", "snapshot_date": None}


class _FakeAgent:
    state_dir = None
    fields_per_discovery = 3
    discovery = _Discovery()

    def __init__(self, state_dir):
        self.state_dir = state_dir
        self.received = None
        self.optimizer_limit = None

    def next_round_no(self):
        return 7

    def run_proposals(self, path):
        with open(path, encoding="utf-8") as handle:
            self.received = json.load(handle)
        return {"accepted": 1, "status": "DONE"}

    def optimizer_context(self, *, limit=8):
        self.optimizer_limit = limit
        return {"limit": limit, "parents": []}


class _FakeClient:
    def __init__(self):
        self.urls = []

    def get_progress_snapshot(self, url, timeout=60):
        self.urls.append((url, timeout))
        return {"url": url, "status": "RUNNING"}


class TestResearchApi(unittest.TestCase):
    def test_experiment_spec_is_lightweight_and_adapts_to_existing_contract(self):
        spec = ExperimentSpec(
            hypothesis="short-term reversal",
            expression="rank(returns)",
            fields=("returns",),
            settings={"decay": 2},
            rationale="test a falsifiable reversal mechanism",
        )
        proposal = spec.to_proposal(round_no=4)
        self.assertEqual(proposal["round"], 4)
        self.assertEqual(proposal["fields"], ["returns"])
        self.assertEqual(proposal["experiment_stage"], "BASELINE")
        self.assertEqual(proposal["research_role"], "EXPLORE")
        self.assertNotIn("operator_mapping", proposal)
        self.assertNotIn("experiment_question", proposal)
        self.assertNotIn("expected_failure_modes", proposal)
        self.assertNotIn("tuning_risk", proposal)

    def test_legacy_spec_metadata_is_ignored(self):
        spec = ExperimentSpec.from_mapping({
            "hypothesis": "test", "expression": "rank(close)",
            "external_evidence_refs": ["legacy"],
        })
        self.assertFalse(hasattr(spec, "external_evidence_refs"))
        self.assertNotIn("external_evidence_refs", spec.to_proposal())

    def test_discovery_facade_uses_existing_discovery_component(self):
        result = discover_fields("price reversal", agent=_FakeAgent(tempfile.gettempdir()))
        self.assertEqual(result["fields"][0]["id"], "close")
        self.assertEqual(result["field_source"]["kind"], "brain_api")

    def test_run_experiment_delegates_to_guarded_agent_path(self):
        with tempfile.TemporaryDirectory() as directory:
            agent = _FakeAgent(directory)
            result = run_experiment(
                {"hypothesis": "test", "expression": "rank(close)", "fields": ["close"]},
                agent=agent,
            )
            self.assertEqual(result["status"], "DONE")
            self.assertEqual(agent.received["round_no"], 7)
            self.assertEqual(agent.received["proposals"][0]["expression"], "rank(close)")
            self.assertEqual(os.listdir(directory), [])

    def test_reconcile_only_polls_the_known_url(self):
        client = _FakeClient()
        result = reconcile("https://brain.example/progress/1", client=client, timeout=12)
        self.assertEqual(result["status"], "RUNNING")
        self.assertEqual(client.urls, [("https://brain.example/progress/1", 12)])

    def test_operator_reference_is_read_from_the_checked_in_table(self):
        reference = get_operator_reference()
        self.assertTrue(reference["sha256"])
        self.assertIn("rank", reference["operators"])

    def test_state_queries_are_small_and_composable(self):
        with tempfile.TemporaryDirectory() as directory:
            result = inspect_state(state_dir=directory, limit=2)
            self.assertEqual(result["experiment_count"], 0)
            self.assertEqual(result["recent_experiments"], [])
            self.assertEqual(compare_experiments(["missing"], state_dir=directory)["missing"], ["missing"])

    def test_optimizer_context_facade_is_bounded_and_needs_no_agent_object(self):
        agent = _FakeAgent(tempfile.gettempdir())
        self.assertEqual(inspect_optimizer_context(agent=agent, limit=32), {"limit": 8, "parents": []})
        self.assertEqual(agent.optimizer_limit, 8)
        self.assertEqual(inspect_optimizer_context(agent=agent, limit=3)["limit"], 3)
        self.assertEqual(agent.optimizer_limit, 3)

    def test_history_queries_read_experiment_evidence_by_id_and_expression(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "trajectory.jsonl")
            experiment = Experiment(1, "h1", "rank(close)", {}, ["close"], ["pv1"])
            experiment.proposal_id = "proposal-1"
            experiment.status = "DONE"
            Trajectory(path=path).add(experiment)

            by_id = get_experiment(experiment.id, state_dir=directory)
            self.assertEqual(by_id["proposal_id"], "proposal-1")
            self.assertEqual(get_experiment("proposal-1", state_dir=directory)["id"], experiment.id)
            compared = compare_experiments([experiment.id, "missing"], state_dir=directory)
            self.assertEqual(len(compared["experiments"]), 1)
            self.assertEqual(compared["missing"], ["missing"])
            self.assertEqual(search_history("rank(close)", state_dir=directory)[0]["id"], experiment.id)


if __name__ == "__main__":
    unittest.main()
