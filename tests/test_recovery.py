import json
import os
import tempfile
import unittest

from wqb_agent.agent import Agent
from wqb_agent.evidence_status import annotate_evidence
from wqb_agent.identity import candidate_identity
from wqb_agent.search_policy import BudgetAllocator, SearchPolicy
from wqb_agent.search_snapshot import SearchSnapshot
from wqb_agent.state import Experiment
from wqb_agent.trial_ledger import TrialLedger
from wqb_agent.validation_report import (
    build_validation_report,
    default_validation_plan,
    migrate_validation_plan,
    validate_plan,
)


class TestExperimentIdentity(unittest.TestCase):
    def test_candidate_identity_includes_settings(self):
        left = {"round": 1, "expression": "rank(close)", "fields": ["close"], "settings": {"decay": 2}}
        right = dict(left, settings={"decay": 4})
        self.assertNotEqual(candidate_identity(left), candidate_identity(right))

    def test_one_hundred_generated_candidates_are_counted(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = TrialLedger(os.path.join(tmp, "ledger.jsonl"))
            for index in range(100):
                candidate = {"round": 1, "expression": f"rank(field_{index})", "fields": [f"field_{index}"]}
                candidate["candidate_id"] = candidate_identity(candidate)
                ledger.record(candidate, "candidate_generated", outcome="CONSIDERED")
            summary = ledger.summarize()
            self.assertEqual(summary["candidate_generated_count"], 100)
            self.assertEqual(summary["candidate_count"], 100)
            self.assertEqual(summary["unique_candidate_count"], 100)

    def test_rejected_candidates_do_not_collide_without_proposal_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = TrialLedger(os.path.join(tmp, "ledger.jsonl"))
            for decay in (2, 4):
                candidate = {"round": 1, "expression": "rank(close)", "fields": ["close"], "settings": {"decay": decay}}
                candidate["candidate_id"] = candidate_identity(candidate)
                ledger.record(candidate, "candidate_generated", outcome="CONSIDERED")
                ledger.record(candidate, "candidate_rejected", outcome="REJECTED", reason_code="DUPLICATE_LOCAL", reason="duplicate")
            summary = ledger.summarize()
            self.assertEqual(summary["candidate_count"], 2)
            self.assertEqual(summary["candidate_rejected_count"], 2)


class TestBudgetAndRecovery(unittest.TestCase):
    def proposal(self, key="p1"):
        return {"proposal_id": key, "datasets": ["pv1"], "template_family": "trend"}

    def test_local_skip_does_not_consume_simulation_budget(self):
        policy = SearchPolicy({"enabled": True, "max_simulations": 2, "max_pending_per_arm": 10})
        first, second = self.proposal(), self.proposal("p2")
        self.assertTrue(policy.accept(first))
        self.assertTrue(policy.accept(second))
        self.assertTrue(policy.commit(first))
        policy.release(second, status="SKIPPED_LOCAL")
        self.assertEqual(policy.allocator.consumed_budget, 1)
        self.assertEqual(policy.allocator.arms["pv1::trend"]["skipped_local"], 1)

    def test_unknown_remains_committed_and_infra_failure_has_no_reward(self):
        allocator = BudgetAllocator(total_budget=3, max_pending_per_arm=10)
        unknown = self.proposal("unknown")
        self.assertTrue(allocator.reserve(unknown))
        allocator.mark_unknown(unknown)
        self.assertEqual(allocator.consumed_budget, 1)
        failed = self.proposal("infra")
        self.assertTrue(allocator.reserve(failed))
        allocator.mark_failed(failed, outcome="INFRA")
        state = allocator.arms["pv1::trend"]
        self.assertEqual(state["reward_count"], 0)
        self.assertEqual(state["failed_infra"], 1)
        self.assertEqual(allocator.arms["pv1::trend"]["reward_count"], 0)

    def test_restart_restores_family_penalty_and_committed_budget(self):
        policy = SearchPolicy({"enabled": True, "max_simulations": 5, "max_pending_per_arm": 10})
        proposal = self.proposal()
        self.assertTrue(policy.accept(proposal))
        self.assertTrue(policy.commit(proposal))
        snapshot = policy.snapshot()
        restored = SearchPolicy({"enabled": True, "max_simulations": 5, "max_pending_per_arm": 10})
        restored.restore(snapshot)
        self.assertEqual(restored.family_counts, policy.family_counts)
        self.assertEqual(restored.allocator.consumed_budget, 1)

    def test_snapshot_uses_ledger_candidate_count_and_deduplicates_rows(self):
        exp = Experiment(1, "h", "rank(close)", {}, ["close"], ["pv1"])
        exp.candidate_id = "c-1"
        exp.proposal_id = "p-1"
        exp.status = "DONE"
        exp.metrics = {"fitness": .2}
        summary = {"candidate_count": 7, "submitted_count": 1, "family_counts": {"trend": 1}}
        snapshot = SearchSnapshot.from_sources([exp], summary, [exp])
        self.assertEqual(snapshot["candidate_count"], 7)
        self.assertEqual(len(snapshot["proposals"]), 1)


class TestEvidenceAndValidationPlan(unittest.TestCase):
    def test_available_is_not_automatically_pass(self):
        result = annotate_evidence({"status": "AVAILABLE", "value": .62}, status="INCONCLUSIVE")
        self.assertEqual(result["availability"], "AVAILABLE")
        self.assertEqual(result["decision"], "INCONCLUSIVE")
        self.assertNotEqual(result["evidence_status"], "PASS")

    def test_statistical_policy_can_fail_available_evidence(self):
        parent = {"expression": "rank(close)", "status": "DONE", "metrics": {"checks": [{"pass": True}]}}
        plan = default_validation_plan(parent, statistical_policy={"mode": "required_when_available", "min_psr": .999, "min_dsr": .999})
        report = build_validation_report(parent, [], plan, return_series=[-.02, -.01, -.01, -.02, -.01, -.02], trial_summary={"candidate_count": 5, "trial_sharpes": [0.1, 0.2]})
        self.assertEqual(report["statistical_evidence"]["statistical_decision"], "FAIL")
        self.assertNotEqual(report["statistical_evidence"]["statistical_status"], "PASS")

    def test_calculation_without_policy_threshold_is_inconclusive(self):
        parent = {"expression": "rank(close)", "status": "DONE", "metrics": {"checks": [{"pass": True}]}}
        plan = default_validation_plan(parent)
        report = build_validation_report(parent, [], plan, return_series=[.01, .02, .01, .02, .01, .02], trial_summary={"candidate_count": 2, "trial_sharpes": [0.1, 0.2]})
        self.assertEqual(report["statistical_evidence"]["statistical_status"], "INCONCLUSIVE")

    def test_legacy_plan_migrates_to_v3_without_fake_dimension(self):
        legacy = default_validation_plan({"expression": "rank(close)", "settings": {}}, timestamp=1)
        legacy["schema_version"] = 2
        legacy["variables"].append({"variable": "decay_truncation", "reason": "legacy", "budget": 1,
                                     "falsification": "legacy", "stopping_rule": "legacy", "requirement": "REQUIRED"})
        migrated = migrate_validation_plan(legacy)
        self.assertEqual(migrated["schema_version"], 3)
        self.assertNotIn("decay_truncation", {item["variable"] for item in migrated["variables"]})
        self.assertTrue(validate_plan(migrated)[0])


class _NoSubmitClient:
    def submit_simulation(self, *args, **kwargs):
        raise AssertionError("settlement path must not submit simulations")


class TestCheckpointTerminalSettlement(unittest.TestCase):
    """Closed-checkpoint terminal states backfill into the trajectory.

    Guards the ``recovery settle-stale-trajectory`` path: local-only writes
    through the canonical ``Trajectory.settle`` channel, fail-closed evidence
    gates, no remote call and no re-POST.
    """

    SIM_URL = "https://api.worldquantbrain.com/simulations/simABC"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.agent = Agent(
            _NoSubmitClient(),
            {"simulation": {}, "agent": {"state_dir": self.tmp}},
        )

    def tearDown(self):
        self._tmp.cleanup()

    def make_experiment(self, exp_id="e1", proposal_id="p1", status="UNKNOWN",
                        progress_url=None, expression="rank(close)"):
        exp = Experiment(
            9, "h-test", expression, {"decay": 2}, ["close"], ["test"],
        )
        exp.id = exp_id
        exp.proposal_id = proposal_id
        exp.status = status
        exp.progress_url = progress_url
        return exp

    def write_checkpoint_row(self, exp, status):
        row = Experiment.from_dict(exp.to_dict())
        row.status = status
        self.agent.checkpoints.write(9, {"id": "h-test"}, [row], True)

    def write_reconcile_history(self, simulation_id, count=3, outcome="STALE"):
        path = os.path.join(self.tmp, "reconcile_history.jsonl")
        with open(path, "w", encoding="utf-8") as handle:
            for _ in range(count):
                handle.write(json.dumps({
                    "round": 9, "simulation_id": simulation_id,
                    "expression": "rank(close)", "old_status": "UNKNOWN",
                    "outcome": outcome, "reconciled_at": "2026-01-01T00:00:00",
                }) + "\n")

    def rejected_reasons(self, report):
        return [item.get("reason") for item in report["rejected"]]

    def test_settles_skipped_stale_with_sufficient_evidence(self):
        exp = self.make_experiment(progress_url=self.SIM_URL)
        self.agent.trajectory.add(exp)
        self.write_checkpoint_row(exp, "SKIPPED_STALE")
        self.write_reconcile_history("simABC", 3)

        report = self.agent.settle_stale_trajectory()
        self.assertEqual(len(report["settled"]), 1)
        self.assertTrue(report["settled"][0]["written"])
        self.assertEqual(report["rejected"], [])
        canon = self.agent.trajectory.find_row("e1")
        self.assertEqual(canon["status"], "SKIPPED_STALE")
        self.assertEqual(canon.get("trajectory_revision"), "RESEARCH_SETTLED")
        self.assertEqual(canon["metrics"], None)
        self.assertTrue(
            os.path.exists(os.path.join(self.tmp, "trajectory_settlement_log.jsonl"))
        )

        again = self.agent.settle_stale_trajectory()
        self.assertEqual(again["settled"], [])
        self.assertEqual(len(again["already_settled"]), 1)

    def test_skipped_stale_without_sufficient_evidence_is_rejected(self):
        exp = self.make_experiment(progress_url=self.SIM_URL)
        self.agent.trajectory.add(exp)
        self.write_checkpoint_row(exp, "SKIPPED_STALE")
        self.write_reconcile_history("simABC", 2)

        report = self.agent.settle_stale_trajectory()
        self.assertEqual(report["settled"], [])
        self.assertEqual(self.rejected_reasons(report), ["INSUFFICIENT_STALE_EVIDENCE"])
        self.assertEqual(self.agent.trajectory.find_row("e1")["status"], "UNKNOWN")
        self.assertFalse(
            os.path.exists(os.path.join(self.tmp, "trajectory_settlement_log.jsonl"))
        )

    def test_skipped_unknown_requires_missing_progress_url(self):
        missing = self.make_experiment(exp_id="e1", proposal_id="p1",
                                       status="SUBMIT_UNKNOWN", progress_url=None)
        with_url = self.make_experiment(
            exp_id="e2", proposal_id="p2",
            status="UNKNOWN", progress_url=self.SIM_URL,
            expression="rank(vwap)",
        )
        self.agent.trajectory.add(missing)
        self.agent.trajectory.add(with_url)
        missing_row = Experiment.from_dict(missing.to_dict())
        missing_row.status = "SKIPPED_UNKNOWN"
        with_url_row = Experiment.from_dict(with_url.to_dict())
        with_url_row.status = "SKIPPED_UNKNOWN"
        self.agent.checkpoints.write(
            9, {"id": "h-test"}, [missing_row, with_url_row], True,
        )

        report = self.agent.settle_stale_trajectory()
        settled_ids = [item["experiment_id"] for item in report["settled"]]
        self.assertEqual(settled_ids, ["e1"])
        self.assertEqual(
            [item["experiment_id"] for item in report["rejected"]], ["e2"],
        )
        self.assertEqual(
            self.rejected_reasons(report), ["SKIPPED_UNKNOWN_WITH_PROGRESS_URL"],
        )
        self.assertEqual(self.agent.trajectory.find_row("e1")["status"], "SKIPPED_UNKNOWN")
        self.assertEqual(self.agent.trajectory.find_row("e2")["status"], "UNKNOWN")

    def test_done_checkpoint_state_settles_without_inventing_metrics(self):
        exp = self.make_experiment(status="UNKNOWN", progress_url=self.SIM_URL)
        self.agent.trajectory.add(exp)
        self.write_checkpoint_row(exp, "DONE")

        report = self.agent.settle_stale_trajectory()
        self.assertEqual(len(report["settled"]), 1)
        canon = self.agent.trajectory.find_row("e1")
        self.assertEqual(canon["status"], "DONE")
        self.assertIsNone(canon["metrics"])
        self.assertIsNone(canon.get("alpha_id"))

    def test_identity_conflict_is_rejected(self):
        exp = self.make_experiment(expression="rank(close)")
        self.agent.trajectory.add(exp)
        conflicting = self.make_experiment(expression="rank(vwap)")
        self.write_checkpoint_row(conflicting, "SKIPPED_STALE")

        report = self.agent.settle_stale_trajectory()
        self.assertEqual(report["settled"], [])
        entry = report["rejected"][0]
        self.assertEqual(entry["reason"], "IDENTITY_CONFLICT")
        self.assertIn("expression", entry["fields"])
        self.assertEqual(self.agent.trajectory.find_row("e1")["status"], "UNKNOWN")

    def test_incomplete_checkpoint_is_rejected(self):
        exp = self.make_experiment(progress_url=self.SIM_URL)
        self.agent.trajectory.add(exp)
        row = Experiment.from_dict(exp.to_dict())
        row.status = "SKIPPED_STALE"
        self.agent.checkpoints.write(9, {"id": "h-test"}, [row], False)
        self.write_reconcile_history("simABC", 3)

        report = self.agent.settle_stale_trajectory()
        self.assertEqual(report["settled"], [])
        self.assertEqual(self.rejected_reasons(report), ["INCOMPLETE_CHECKPOINT"])
        self.assertEqual(self.agent.trajectory.find_row("e1")["status"], "UNKNOWN")

    def test_dry_run_writes_nothing(self):
        exp = self.make_experiment(progress_url=self.SIM_URL)
        self.agent.trajectory.add(exp)
        self.write_checkpoint_row(exp, "SKIPPED_STALE")
        self.write_reconcile_history("simABC", 3)

        report = self.agent.settle_stale_trajectory(dry_run=True)
        self.assertTrue(report["dry_run"])
        self.assertEqual(len(report["settled"]), 1)
        self.assertEqual(self.agent.trajectory.find_row("e1")["status"], "UNKNOWN")
        self.assertFalse(
            os.path.exists(os.path.join(self.tmp, "trajectory_settlement_log.jsonl"))
        )

    def test_active_canonical_row_is_not_settled(self):
        exp = self.make_experiment(status="RUNNING")
        self.agent.trajectory.add(exp)
        self.write_checkpoint_row(exp, "SKIPPED_STALE")

        report = self.agent.settle_stale_trajectory()
        self.assertEqual(report["settled"], [])
        entry = report["rejected"][0]
        self.assertEqual(entry["reason"], "CANONICAL_ROW_NOT_UNRESOLVED")
        self.assertEqual(entry["canonical_status"], "RUNNING")
        self.assertEqual(self.agent.trajectory.find_row("e1")["status"], "RUNNING")

    def test_round_scoped_settlement_only_touches_requested_round(self):
        other_exp = self.make_experiment(
            exp_id="e9", proposal_id="p9", progress_url=self.SIM_URL,
        )
        other_exp.round = 12
        self.agent.trajectory.add(other_exp)
        other_row = Experiment.from_dict(other_exp.to_dict())
        other_row.status = "SKIPPED_STALE"
        self.agent.checkpoints.write(12, {"id": "h-test"}, [other_row], True)

        exp = self.make_experiment(progress_url=self.SIM_URL)
        self.agent.trajectory.add(exp)
        self.write_checkpoint_row(exp, "SKIPPED_STALE")
        self.write_reconcile_history("simABC", 3)

        report = self.agent.settle_stale_trajectory(round_no=9)
        settled_ids = [item["experiment_id"] for item in report["settled"]]
        self.assertEqual(settled_ids, ["e1"])
        self.assertEqual(self.agent.trajectory.find_row("e9")["status"], "UNKNOWN")


if __name__ == "__main__":
    unittest.main()
