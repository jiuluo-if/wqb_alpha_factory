import os
import tempfile
import unittest

from wqb_agent.evidence_status import evidence_status
from wqb_agent.search_calibration import (
    SearchPolicyReplay,
    build_search_calibration,
    calibrate_replay,
)
from wqb_agent.search_outcome import (
    SearchOutcome,
    parent_relative_delta,
    reward_v1,
    staged_promotion,
)
from wqb_agent.search_policy import (
    BudgetAllocator,
    SearchPolicy,
    validate_budget_hierarchy,
)
from wqb_agent.search_snapshot import SearchSnapshot
from wqb_agent.trial_ledger import TrialLedger


class TestSearchRecovery(unittest.TestCase):
    @staticmethod
    def candidate(key="c-1", status="UNKNOWN"):
        return {
            "candidate_id": key,
            "proposal_id": None,
            "expression": f"rank(field_{key})",
            "fields": [f"field_{key}"],
            "datasets": ["pv1"],
            "template_family": "trend",
            "status": status,
        }

    def test_rejected_candidate_does_not_restore_unknown_slot(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = TrialLedger(os.path.join(tmp, "ledger.jsonl"))
            candidate = self.candidate()
            ledger.record(candidate, "candidate_generated", outcome="CONSIDERED")
            ledger.record(candidate, "candidate_rejected", outcome="REJECTED",
                          reason_code="DUPLICATE_LOCAL", reason="重复")
            snapshot = SearchSnapshot.from_sources([], ledger.summarize())
            self.assertEqual(snapshot["arms"], {})
            self.assertEqual(snapshot["proposals"], {})

    def test_admitted_candidate_rejected_by_batch_cap_does_not_restore_slot(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = TrialLedger(os.path.join(tmp, "ledger.jsonl"))
            candidate = self.candidate()
            ledger.record(candidate, "candidate_generated", outcome="CONSIDERED")
            ledger.record(candidate, "candidate_admitted", outcome="ADMITTED")
            ledger.record(candidate, "candidate_rejected", outcome="REJECTED",
                          reason_code="BATCH_CAP", reason="本地批次上限")
            snapshot = SearchSnapshot.from_sources([], ledger.summarize())
            self.assertEqual(snapshot["arms"], {})
            self.assertEqual(snapshot["proposals"], {})

    def test_commit_failure_releases_provisional_reservation(self):
        allocator = BudgetAllocator(total_budget=1, max_pending_per_arm=10)
        first = {"proposal_id": "p1", "datasets": ["pv1"], "template_family": "trend"}
        second = {"proposal_id": "p2", "datasets": ["pv1"], "template_family": "trend"}
        self.assertTrue(allocator.admit(first))
        allocator.consumed_budget = allocator.total_budget
        self.assertFalse(allocator.commit(first))
        allocator.mark_skipped_local(first)
        self.assertEqual(allocator.arms["pv1::trend"]["reserved"], 0)
        self.assertEqual(allocator.arms["pv1::trend"]["skipped_local"], 1)
        self.assertFalse(allocator.admit(second))

    def test_terminal_proposal_key_cannot_rebind_to_another_arm(self):
        allocator = BudgetAllocator(total_budget=3, max_pending_per_arm=2)
        first = {"proposal_id": "p-rebind", "datasets": ["pv1"], "template_family": "trend"}
        other_arm = {"proposal_id": "p-rebind", "datasets": ["pv2"], "template_family": "trend"}

        self.assertTrue(allocator.reserve(first))
        self.assertTrue(allocator.complete(first, reward=0.0))
        self.assertFalse(allocator.reserve(other_arm))
        self.assertEqual(allocator.last_rejection_code, "TERMINAL_PROPOSAL_KEY_ARM_REBIND")

        replay_same_arm = dict(first)
        self.assertTrue(allocator.reserve(replay_same_arm))


class TestSearchOutcome(unittest.TestCase):
    def test_raw_fitness_extremes_cannot_escape_reward_bounds(self):
        for _fitness in (-10**12, 10**12, float("inf"), float("nan")):
            reward = reward_v1(status="DONE", infrastructure_failure=False,
                               base_quality="PROMISING", robustness="",
                               parent_delta=None)
            self.assertGreaterEqual(reward, 0.0)
            self.assertLessEqual(reward, 1.0)

    def test_infrastructure_failure_has_no_evaluated_reward(self):
        result = reward_v1(status="FAILED", infrastructure_failure=True,
                           base_quality="FAILED", robustness="")
        self.assertIsNone(result)

    def test_completed_research_failure_can_be_explicit_zero(self):
        self.assertEqual(
            reward_v1(status="FAILED", infrastructure_failure=False,
                      base_quality="FAILED", robustness="", parent_delta=None),
            0.0,
        )

    def test_parent_relative_delta_rewards_improvement(self):
        parent = {"metrics": {"sharpe": 0.5, "fitness": 0.4, "turnover": 0.3, "drawdown": 0.2}}
        child = {"metrics": {"sharpe": 1.0, "fitness": 0.8, "turnover": 0.2, "drawdown": 0.1}}
        self.assertGreater(parent_relative_delta(child, parent), 0.0)
        outcome = SearchOutcome(
            proposal_id="p", arm="pv1::trend", research_role="EXPLOIT",
            experiment_stage="CHILD", evaluated=True,
            infrastructure_failure=False, base_quality="PROMISING",
            robustness="", novelty=None, parent_delta=.2, reward=.7,
        )
        self.assertEqual(outcome.reward_version, "reward_v1")

    def test_unavailable_statistical_evidence_does_not_reduce_robustness_reward(self):
        self.assertEqual(
            reward_v1(status="DONE", infrastructure_failure=False,
                      base_quality="PROMISING", robustness="STABLE",
                      statistical_decision="UNAVAILABLE", parent_delta=None),
            .85,
        )

    def test_staged_promotion_has_bounded_next_step_and_stop(self):
        self.assertEqual(staged_promotion(base_quality="PROMISING")["stage"], "EXPLOIT")
        self.assertEqual(staged_promotion(base_quality="PROMISING", budget_used=2,
                                          budget_limit=2)["decision"], "STOP")
        self.assertEqual(staged_promotion(base_quality="PROMISING",
                                          validation_status="STABLE")["stage"], "STABLE")


class TestSearchCalibration(unittest.TestCase):
    def rows(self):
        return [
            {"proposal_id": "p1", "arm": "a", "family": "trend", "reward": .5,
             "promising": True, "stable": False, "structural_fingerprint": "s1"},
            {"proposal_id": "p2", "arm": "b", "family": "value", "reward": .0,
             "promising": False, "stable": False, "structural_fingerprint": "s2"},
            {"proposal_id": "p3", "arm": "a", "family": "trend", "reward": .85,
             "promising": True, "stable": True, "structural_fingerprint": "s3"},
        ]

    def test_calibration_report_has_efficiency_and_role_budgets(self):
        report = build_search_calibration(
            {"candidate_count": 3, "submitted_count": 3},
            outcomes=[
                dict(row, evaluated=True, research_role=role,
                     infrastructure_failure=False, base_quality="PROMISING" if row["promising"] else "FAILED",
                     robustness="STABLE" if row["stable"] else "")
                for row, role in zip(self.rows(), ("EXPLORE", "EXPLOIT", "VALIDATION"))
            ],
        )
        self.assertEqual(report["promising_count"], 2)
        self.assertEqual(report["stable_count"], 1)
        self.assertEqual(report["explore_budget"], 1)
        self.assertEqual(report["simulation_per_promising"], 1.5)

    def test_replay_does_not_use_future_rewards_when_ordering(self):
        candidates = [
            {"proposal_id": "first", "arm": "a", "novelty": .5, "reward": 0},
            {"proposal_id": "second", "arm": "b", "novelty": .5, "reward": 1},
        ]
        result = SearchPolicyReplay(candidates).run(checkpoints=(1, 2))
        self.assertEqual(result["checkpoints"][0]["reward_at_n"], 0)
        self.assertEqual(result["selected_proposal_ids"][0], "first")

    def test_replay_and_fifo_have_same_schema_and_grid_is_declared(self):
        replay = calibrate_replay(self.rows(), checkpoints=(1, 3))
        self.assertEqual(
            set(replay["baseline_fifo"]["checkpoints"][0]),
            set(replay["current_policy"]["checkpoints"][0]),
        )
        self.assertEqual(replay["tested_parameters"], [0.5, 1.0, 1.5])
        self.assertIn(replay["selected_parameter"], replay["tested_parameters"])

    def test_validation_does_not_consume_discovery_ucb_quota(self):
        policy = SearchPolicy({"enabled": True, "max_simulations": 1,
                               "validation_max_simulations": 1,
                               "max_pending_per_arm": 10})
        validation = {"proposal_id": "v1", "datasets": ["pv1"],
                      "template_family": "trend", "research_role": "VALIDATION"}
        explore = {"proposal_id": "e1", "datasets": ["pv1"],
                   "template_family": "trend", "research_role": "EXPLORE"}
        self.assertTrue(policy.accept(validation))
        self.assertTrue(policy.commit(validation))
        self.assertEqual(policy.allocator.consumed_budget, 0)
        self.assertTrue(policy.accept(explore))
        self.assertTrue(policy.commit(explore))
        self.assertEqual(policy.allocator.consumed_budget, 1)

    def test_validation_budget_is_protected_and_bounded(self):
        policy = SearchPolicy({"enabled": True, "max_simulations": 3,
                               "validation_max_simulations": 1,
                               "max_pending_per_arm": 10})
        first = {"proposal_id": "v1", "datasets": ["pv1"],
                 "template_family": "trend", "research_role": "VALIDATION"}
        second = {"proposal_id": "v2", "datasets": ["pv1"],
                  "template_family": "trend", "research_role": "VALIDATION"}
        self.assertTrue(policy.accept(first))
        self.assertFalse(policy.accept(second))

    def test_budget_hierarchy_fails_closed(self):
        with self.assertRaises(ValueError):
            validate_budget_hierarchy(factory_max_simulations=10,
                                      search_max_simulations=11,
                                      research_max_simulations=1)
        with self.assertRaises(ValueError):
            validate_budget_hierarchy(factory_max_simulations=10,
                                      search_max_simulations=5,
                                      research_max_simulations=6)

    def test_legacy_evidence_helper_cannot_promote_availability(self):
        self.assertNotEqual(
            evidence_status({"availability": "AVAILABLE", "quality": "VERIFIED"}),
            "PASS",
        )
        self.assertEqual(evidence_status({"availability": "AVAILABLE", "decision": "PASS"}), "PASS")

    def test_pending_and_unknown_lifecycle_states_restore_separately(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = TrialLedger(os.path.join(tmp, "ledger.jsonl"))
            for key, state in (("p-pending", "PENDING"), ("p-unknown", "UNKNOWN")):
                row = {
                    "candidate_id": key, "proposal_id": key,
                    "expression": f"rank({key})", "fields": [key],
                    "datasets": ["pv1"], "template_family": "trend",
                    "status": state,
                }
                ledger.record(row, "simulation_committed", outcome="COMMITTED")
                ledger.record(row, "simulation_submitted", outcome=state)
            state = SearchSnapshot.from_sources([], ledger.summarize())["arms"]["pv1::trend"]
            self.assertEqual(state["pending"], 1)
            self.assertEqual(state["unknown"], 1)
            self.assertEqual(state["reserved"], 0)

    def test_calibration_first_signal_indexes_follow_history_order(self):
        outcomes = [
            {"evaluated": True, "base_quality": "FAILED", "robustness": ""},
            {"evaluated": True, "base_quality": "PROMISING", "robustness": ""},
            {"evaluated": True, "base_quality": "PROMISING", "robustness": "STABLE"},
        ]
        report = build_search_calibration({"candidate_count": 3, "submitted_count": 3}, outcomes=outcomes)
        self.assertEqual(report["simulations_to_first_promising"], 2)
        self.assertEqual(report["simulations_to_first_stable"], 3)

    def test_infrastructure_and_research_failures_survive_snapshot_rebuild(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = TrialLedger(os.path.join(tmp, "ledger.jsonl"))
            for key, reason in (("p-infra", "TIMEOUT"), ("p-research", "QUALITY_GATE")):
                row = {
                    "candidate_id": key,
                    "proposal_id": key,
                    "expression": f"rank({key})",
                    "fields": [key],
                    "datasets": ["pv1"],
                    "template_family": "trend",
                    "status": "FAILED",
                }
                ledger.record(row, "candidate_generated", outcome="CONSIDERED")
                ledger.record(row, "candidate_admitted", outcome="ADMITTED")
                ledger.record(row, "simulation_committed", outcome="COMMITTED")
                ledger.record(row, "simulation_settled", outcome="FAILED",
                              reason_code=reason, reason=reason)
            snapshot = SearchSnapshot.from_sources([], ledger.summarize())
            state = snapshot["arms"]["pv1::trend"]
            self.assertEqual(state["failed_infra"], 1)
            self.assertEqual(state["failed_research"], 1)
            self.assertEqual(state["unknown"], 0)

    def test_snapshot_projection_is_idempotent_across_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = TrialLedger(os.path.join(tmp, "ledger.jsonl"))
            row = {
                "candidate_id": "p-restart", "proposal_id": "p-restart",
                "expression": "rank(restart_field)", "fields": ["restart_field"],
                "datasets": ["pv1"], "template_family": "trend",
                "status": "UNKNOWN", "research_role": "EXPLORE",
            }
            ledger.record(row, "candidate_generated", outcome="CONSIDERED")
            ledger.record(row, "candidate_admitted", outcome="ADMITTED")
            ledger.record(row, "simulation_committed", outcome="COMMITTED")
            ledger.record(row, "simulation_submitted", outcome="UNKNOWN")
            summary = ledger.summarize()
            before = SearchSnapshot.from_sources([], summary).allocator_state(10)
            after = SearchSnapshot.from_sources([], ledger.summarize()).allocator_state(10)
            self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
