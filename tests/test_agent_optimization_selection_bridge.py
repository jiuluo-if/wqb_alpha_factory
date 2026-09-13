import unittest
from unittest.mock import Mock

from wqb_agent.agent import Agent
from wqb_agent.optimization_decision import (
    OptimizationDecision,
    optimization_decision_identity,
)


class TestAgentOptimizationSelectionBridge(unittest.TestCase):
    def test_bridge_accounts_finalized_decisions_once(self):
        agent = object.__new__(Agent)
        agent.trial_ledger = Mock()
        agent.memory = Mock()
        agent.optimizer_workflow = Mock()
        decisions = [
            OptimizationDecision(parent_id="p-child", decision="CHILD"),
            OptimizationDecision(parent_id="p-stop", decision="STOP"),
            OptimizationDecision(parent_id="p-pruned", decision="VALIDATE"),
        ]
        agent.optimizer_workflow.generate_from_decisions.return_value = {
            "accepted": [{"parent_id": "p-child"}],
            "rejected": [
                {"parent_id": "p-stop", "reasons": ["NOT_A_CHILD_DECISION"]},
                {"parent_id": "p-pruned", "reasons": ["PRUNED_BY_GATE"]},
            ],
            "decision_results": [
                {"parent_id": "p-child", "decision": "CHILD", "outcome": "GENERATED",
                 "decision_id": optimization_decision_identity(decisions[0])},
                {"parent_id": "p-stop", "decision": "STOP", "outcome": "STOP",
                 "decision_id": optimization_decision_identity(decisions[1])},
                {"parent_id": "p-pruned", "decision": "VALIDATE", "outcome": "PRUNED",
                 "decision_id": optimization_decision_identity(decisions[2])},
            ],
        }

        Agent.propose_optimization(agent, decisions)

        outcomes = [call.kwargs["outcome"] for call in agent.trial_ledger.record_optimization_selection.call_args_list]
        self.assertEqual(outcomes, ["GENERATED", "STOP", "PRUNED"])

    def test_bridge_uses_generated_validate_result_instead_of_accepted_list(self):
        agent = object.__new__(Agent)
        agent.trial_ledger = Mock()
        agent.memory = Mock()
        agent.optimizer_workflow = Mock()
        decision = OptimizationDecision(parent_id="p-validate", decision="VALIDATE",
                                        validation_variable="decay", expected_effect="stable",
                                        falsification="fails", reason="check")
        agent.optimizer_workflow.generate_from_decisions.return_value = {
            "accepted": [], "rejected": [],
            "decision_results": [{
                "parent_id": "p-validate", "decision": "VALIDATE", "outcome": "GENERATED",
                "decision_id": optimization_decision_identity(decision),
            }],
        }

        Agent.propose_optimization(agent, [decision])

        self.assertEqual(
            agent.trial_ledger.record_optimization_selection.call_args.kwargs["outcome"],
            "GENERATED",
        )

    def test_memory_projection_failure_does_not_erase_ledger_fact(self):
        agent = object.__new__(Agent)
        agent.trial_ledger = Mock()
        agent.memory = Mock()
        agent.memory.add_short_term.side_effect = RuntimeError("projection unavailable")
        agent.optimizer_workflow = Mock()
        agent.optimizer_workflow.generate_from_decisions.return_value = {
            "accepted": [], "rejected": [{"parent_id": "p1", "reasons": ["NO_CANDIDATE"]}],
        }

        Agent.propose_optimization(agent, [OptimizationDecision(parent_id="p1", decision="STOP")])

        agent.trial_ledger.record_optimization_selection.assert_called_once()

    def test_generator_input_is_materialized_once_for_workflow_and_accounting(self):
        agent = object.__new__(Agent)
        agent.trial_ledger = Mock()
        agent.memory = Mock()
        agent.optimizer_workflow = Mock()

        def consume(items, max_candidates=4):
            self.assertEqual(len(list(items)), 1)
            # The real Agent passes a materialized list to both consumers.
            return {"accepted": [], "rejected": [], "decision_results": [{
                "decision_id": optimization_decision_identity(decision), "parent_id": "p1",
                "decision": "STOP", "outcome": "STOP",
            }]}

        agent.optimizer_workflow.generate_from_decisions.side_effect = consume
        decision = OptimizationDecision(parent_id="p1", decision="STOP")
        Agent.propose_optimization(agent, (item for item in [decision]))
        agent.trial_ledger.record_optimization_selection.assert_called_once()

    def test_decision_result_identity_mismatch_fails_closed(self):
        agent = object.__new__(Agent)
        agent.trial_ledger = Mock()
        agent.memory = Mock()
        agent.optimizer_workflow = Mock()
        agent.optimizer_workflow.generate_from_decisions.return_value = {
            "accepted": [], "rejected": [], "decision_results": [{
                "decision_id": "wrong", "parent_id": "p1",
                "decision": "STOP", "outcome": "STOP",
            }],
        }
        decision = OptimizationDecision(parent_id="p1", decision="STOP")
        with self.assertRaises(ValueError):
            Agent.propose_optimization(agent, [decision])
        agent.trial_ledger.record_optimization_selection.assert_not_called()

    def test_semantic_replay_does_not_project_memory_twice(self):
        from wqb_agent.trial_ledger import TrialLedger

        agent = object.__new__(Agent)
        agent.trial_ledger = TrialLedger(None, persist=False)
        agent.memory = Mock()
        agent.optimizer_workflow = Mock()
        decision = OptimizationDecision(parent_id="p1", decision="STOP")
        result = {"accepted": [], "rejected": [], "decision_results": [{
            "decision_id": optimization_decision_identity(decision),
            "parent_id": "p1", "decision": "STOP", "outcome": "STOP",
        }]}
        agent.optimizer_workflow.generate_from_decisions.return_value = result

        Agent.propose_optimization(agent, [decision])
        Agent.propose_optimization(agent, [decision])

        self.assertEqual(agent.trial_ledger.summarize()["selection_trial_count"], 1)
        agent.memory.add_short_term.assert_not_called()
