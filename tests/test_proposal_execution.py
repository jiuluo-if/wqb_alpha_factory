"""Characterization and architecture guards for proposal execution extraction."""

import inspect
import json
import multiprocessing
import os
import tempfile
import threading
import unittest
from unittest.mock import Mock

from wqb_agent.agent import Agent
from wqb_agent.locking import single_instance_scope
from wqb_agent.proposal_admission import AdmissionFacts
from wqb_agent.proposal_execution import ProposalExecutionWorkflow
from wqb_agent.state import Experiment


class CountingClient:
    def __init__(self):
        self.sim_calls = []

    def submit_simulation(self, expression, settings, **kwargs):
        self.sim_calls.append((expression, settings))
        raise AssertionError("characterization path must not submit")


def _run_agent_in_process(state_dir, path, output):
    client = CountingClient()
    agent = _agent(state_dir, client)
    result = agent.run_proposals(path)
    output.put({
        "result": result,
        "status": agent.last_run_stats.get("status"),
        "sim_calls": list(client.sim_calls),
        "checkpoints": [
            name for name in os.listdir(state_dir)
            if name.endswith(".checkpoint.json")
        ],
        "ledger": os.path.exists(os.path.join(state_dir, "trial_ledger.jsonl")),
    })


def _agent(tmpdir, client=None):
    return Agent(
        client or CountingClient(),
        {"simulation": {}, "agent": {"state_dir": tmpdir}},
    )


class TestProposalExecutionCharacterization(unittest.TestCase):
    def test_admission_facts_are_frozen_request_scoped_values(self):
        facts = AdmissionFacts(
            research_seen=frozenset({"rank(x)"}),
            batch_execution_fingerprints=frozenset({"fp"}),
            durable_proposal_bindings={},
            unresolved_identities=frozenset(),
            discovered_profiles={},
            proposal_field_profiles=(),
            field_types={},
            parent_rows={},
            legacy_parent_candidates={},
        )
        with self.assertRaises(AttributeError):
            facts.research_seen = frozenset()

    def test_missing_proposals_file_returns_none_without_simulation_or_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = CountingClient()
            agent = _agent(tmp, client)

            self.assertIsNone(agent.run_proposals(os.path.join(tmp, "missing.json")))
            self.assertEqual(client.sim_calls, [])
            self.assertEqual(
                [name for name in os.listdir(tmp) if name.endswith(".checkpoint.json")],
                [],
            )

    def test_malformed_json_fails_closed_without_simulation_or_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "proposals.json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("{broken")
            client = CountingClient()
            agent = _agent(tmp, client)

            self.assertIsNone(agent.run_proposals(path))
            self.assertEqual(client.sim_calls, [])
            self.assertFalse(os.path.exists(os.path.join(tmp, "round_1.checkpoint.json")))

    def test_invalid_round_numbers_fail_closed_without_simulation(self):
        for raw_round in (True, 0, -1, "not-a-round"):
            with self.subTest(raw_round=raw_round), tempfile.TemporaryDirectory() as tmp:
                path = os.path.join(tmp, "proposals.json")
                with open(path, "w", encoding="utf-8") as handle:
                    json.dump({"round_no": raw_round, "proposals": [{"expression": "rank(x)"}]}, handle)
                client = CountingClient()
                agent = _agent(tmp, client)

                self.assertIsNone(agent.run_proposals(path))
                self.assertEqual(client.sim_calls, [])
                self.assertFalse(any(name.endswith(".checkpoint.json") for name in os.listdir(tmp)))

    def test_foreign_unfinished_checkpoint_blocks_new_round_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            checkpoint = {
                "round_no": 7,
                "complete": False,
                "hypothesis": {"id": "h-7"},
                "experiments": [],
            }
            with open(os.path.join(tmp, "round_7.checkpoint.json"), "w", encoding="utf-8") as handle:
                json.dump(checkpoint, handle)
            path = os.path.join(tmp, "proposals.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"round_no": 8, "proposals": []}, handle)
            client = CountingClient()
            agent = _agent(tmp, client)

            self.assertIsNone(agent.run_proposals(path))
            self.assertEqual(client.sim_calls, [])
            self.assertFalse(os.path.exists(os.path.join(tmp, "round_8.checkpoint.json")))

    def test_complete_checkpoint_returns_without_redispatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            experiment = Experiment(1, "h", "rank(x)", {}, ["x"])
            experiment.status = "DONE"
            agent = _agent(tmp)
            agent._write_proposal_checkpoint(1, {"id": "h"}, [experiment], complete=True)
            path = os.path.join(tmp, "proposals.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"round_no": 1, "proposals": [{"expression": "rank(x)"}]}, handle)

            self.assertIsNone(agent.run_proposals(path))
            self.assertEqual(agent.client.sim_calls, [])


class TestProposalExecutionBoundary(unittest.TestCase):
    def test_direct_agent_run_is_blocked_before_workflow_when_other_thread_owns_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = _agent(tmp)
            path = os.path.join(tmp, "proposals.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"round_no": 1, "proposals": []}, handle)
            called = []
            original = agent.proposal_execution.run
            agent.proposal_execution.run = lambda **kwargs: called.append(kwargs)
            with single_instance_scope(tmp, operation="outer"):
                thread = threading.Thread(
                    target=agent.run_proposals, args=(path,),
                )
                thread.start()
                thread.join()
            agent.proposal_execution.run = original

        self.assertEqual(called, [])
        self.assertEqual(agent.last_run_stats["status"], "LOCAL_OWNER_BUSY")

    def test_direct_agent_run_is_blocked_before_post_across_processes(self):
        context = multiprocessing.get_context("spawn")
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "proposals.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"round_no": 1, "proposals": []}, handle)
            output = context.Queue()
            with single_instance_scope(tmp, operation="outer"):
                process = context.Process(
                    target=_run_agent_in_process, args=(tmp, path, output)
                )
                process.start()
                process.join(15)

            self.assertFalse(process.is_alive())
            result = output.get(timeout=2)

        self.assertEqual(result["status"], "LOCAL_OWNER_BUSY")
        self.assertIsNone(result["result"])
        self.assertEqual(result["sim_calls"], [])
        self.assertEqual(result["checkpoints"], [])
        self.assertFalse(result["ledger"])

    def test_agent_run_proposals_delegates_to_workflow(self):
        agent = Agent.__new__(Agent)
        workflow = Mock()
        workflow.run.return_value = "result"
        workflow.last_run_stats = {"status": "DELEGATED"}
        agent.proposal_execution = workflow
        agent.last_run_stats = {"status": "NOT_STARTED"}
        agent.factory_batch_size = 100
        agent.min_factory_datasets = 1
        agent.min_cross_dataset_pairs = 0
        agent.candidates_per_round = 6
        agent.max_proposals_per_round = 18
        agent.research_allocation = {}
        agent.research_integrity = False
        agent.max_field_alpha_count = None
        agent.require_platform_alpha_count = False

        self.assertEqual(
            agent.run_proposals("proposals.json", allow_unresolved_checkpoint=True),
            "result",
        )
        workflow.run.assert_called_once_with(
            path="proposals.json", allow_unresolved_checkpoint=True
        )
        self.assertEqual(agent.last_run_stats, {"status": "DELEGATED"})

    def test_direct_workflow_and_facade_share_missing_file_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = _agent(tmp)
            path = os.path.join(tmp, "missing.json")

            direct = agent.proposal_execution.run(path)
            facade = agent.run_proposals(path)

            self.assertIsNone(direct)
            self.assertIsNone(facade)
            self.assertEqual(agent.last_run_stats, agent.proposal_execution.last_run_stats)

    def test_rejected_round_preserves_agent_iteration_state_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "proposals.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"round_no": 1, "proposals": [{"expression": "rank(x)"}]}, handle)
            agent = _agent(tmp)
            agent.memory.best_exhausted = True

            self.assertIsNone(agent.run_proposals(path))
            self.assertTrue(agent._last_round_skipped)
            self.assertTrue(agent.memory.best_exhausted)

    def test_direct_recovery_matches_facade_without_resubmitting_unknown_write(self):
        def prepare(tmp):
            agent = _agent(tmp)
            experiment = Experiment(1, "h", "rank(x)", {}, ["x"])
            experiment.status = "SUBMIT_UNKNOWN"
            experiment.proposal_id = "p-unknown"
            agent._write_proposal_checkpoint(
                1, {"id": "h", "_round": 1}, [experiment], complete=False
            )
            return agent

        with tempfile.TemporaryDirectory() as direct_tmp, tempfile.TemporaryDirectory() as facade_tmp:
            direct_agent = prepare(direct_tmp)
            facade_agent = prepare(facade_tmp)
            path = os.path.join(facade_tmp, "proposals.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"round_no": 1, "proposals": []}, handle)

            direct_result = direct_agent.proposal_execution.resume_checkpoint(
                direct_agent.checkpoints.load(1)
            )
            facade_result = facade_agent.run_proposals(path)

            self.assertIsNone(direct_result)
            self.assertIsNone(facade_result)
            self.assertEqual(direct_agent.client.sim_calls, [])
            self.assertEqual(facade_agent.client.sim_calls, [])
            self.assertFalse(
                direct_agent.checkpoints.load(1)["complete"]
            )
            self.assertFalse(
                facade_agent.checkpoints.load(1)["complete"]
            )

    def test_workflow_does_not_reference_agent_or_direct_client_submission(self):
        source = inspect.getsource(ProposalExecutionWorkflow)
        self.assertNotIn("self.agent", source)
        self.assertNotIn("submit_simulation(", source)
