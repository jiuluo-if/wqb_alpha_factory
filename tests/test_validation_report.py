import json
import os
import random
import statistics
import tempfile
import unittest

from wqb_agent.agent import Agent
from wqb_agent.pnl import PnlAdapter
from wqb_agent.state import Experiment, Trajectory
from wqb_agent.trial_ledger import TrialLedger
from wqb_agent.validation_report import (
    REQUIRED_VARIABLES,
    build_validation_report,
    default_validation_plan,
    deflated_sharpe_ratio,
    pbo_cscv,
    probabilistic_sharpe_ratio,
    validate_plan,
)


def _metrics(sharpe=1.2):
    return {
        "sharpe": sharpe, "fitness": 1.1, "turnover": 0.2,
        "returns": 0.1, "drawdown": 0.1, "margin": 0.01,
        "checks": [{"name": "ALL", "pass": True},
                    {"name": "SELF_CORRELATION", "pass": True, "value": 0.1}],
    }


class TestValidationReport(unittest.TestCase):
    def setUp(self):
        self.parent = {"expression": "rank(signal)", "status": "DONE", "metrics": _metrics(),
                       "health": {"ok": True},
                       "self_correlation": {"status": "PASS"},
                       "submission_fingerprint": "parent-fp", "yearly_evidence": {
                           "status": "VERIFIED", "stable": True,
                       }}
        self.plan = default_validation_plan(self.parent, timestamp=1.0)

    def test_plan_contains_preregistered_required_variables(self):
        valid, errors = validate_plan(self.plan)
        self.assertTrue(valid, errors)
        self.assertEqual({item["variable"] for item in self.plan["variables"]} & set(REQUIRED_VARIABLES), set(REQUIRED_VARIABLES))
        self.assertEqual(self.plan["preregistered_at"], 1.0)

    def test_one_robustness_success_cannot_be_stable(self):
        child = {"status": "DONE", "changed_variable": "window_locality", "metrics": _metrics()}
        report = build_validation_report(self.parent, [child], self.plan,
                                         yearly_evidence=self.parent["yearly_evidence"],
                                         platform_evidence={
                                             "parent": {"health": self.parent["health"], "correlation": self.parent["self_correlation"]},
                                             "children": [{"health": {"ok": True}, "correlation": {"status": "PASS"}}],
                                         })
        self.assertEqual(report["status"], "INCOMPLETE")
        self.assertFalse(report["stable"])

    def test_only_aggregate_report_promotes_parent(self):
        children = [
            {"status": "DONE", "changed_variable": variable, "metrics": _metrics(),
             "health": {"ok": True}, "self_correlation": {"status": "PASS"}}
            for variable in REQUIRED_VARIABLES if variable != "yearly_aggregates"
        ]
        report = build_validation_report(self.parent, children, self.plan,
                                         yearly_evidence=self.parent["yearly_evidence"],
                                         trial_summary={"selection_trial_count": 6, "candidate_count": 99},
                                         platform_evidence={
                                             "parent": {"health": self.parent["health"], "correlation": self.parent["self_correlation"]},
                                             "children": [{"health": child["health"], "correlation": child["self_correlation"]} for child in children],
                                         })
        self.assertEqual(report["status"], "PASS")
        self.assertEqual(report["candidate"], "parent")
        self.assertEqual(report["selection_adjustment"]["trial_events"], 6)

    def test_old_trial_summary_uses_backward_compatible_fallback(self):
        children = []
        report = build_validation_report(
            self.parent, children, self.plan,
            trial_summary={"generated_trials": 7},
        )
        self.assertEqual(report["selection_adjustment"]["trial_events"], 7)

    def test_missing_required_evidence_is_incomplete_and_nonterminal(self):
        report = build_validation_report(
            self.parent, [], self.plan,
            yearly_evidence=None,
        )

        self.assertEqual(report["status"], "INCOMPLETE")
        self.assertFalse(report["complete"])
        self.assertFalse(report["terminal"])
        self.assertIn("universe_robustness", report["missing_required_dimensions"])
        self.assertIn("yearly_aggregates", report["missing_required_dimensions"])
        self.assertEqual(report["failed_required_dimensions"], [])

    def test_infrastructure_failure_is_unavailable_not_research_fail(self):
        child = {
            "status": "FAILED",
            "changed_variable": "universe_robustness",
            "error": "HTTP timeout while polling BRAIN",
        }
        report = build_validation_report(
            self.parent, [child], self.plan,
            yearly_evidence=self.parent["yearly_evidence"],
        )

        self.assertEqual(
            report["dimensions"]["universe_robustness"]["status"],
            "UNAVAILABLE",
        )
        self.assertNotIn("universe_robustness", report["failed_required_dimensions"])
        self.assertEqual(report["status"], "INCOMPLETE")

    def test_dsr_uses_complete_selection_denominator(self):
        report = build_validation_report(
            self.parent, [], self.plan,
            trial_summary={"selection_trial_count": 6},
        )
        self.assertEqual(report["statistical_evidence"]["dsr"]["n_trials"], 6)

    def test_dsr_is_unavailable_when_selection_history_is_incomplete(self):
        report = build_validation_report(
            self.parent, [], self.plan,
            trial_summary={"selection_trial_count": 6, "history_completeness": "INCOMPLETE_LEGACY"},
        )
        self.assertEqual(report["statistical_evidence"]["dsr"]["status"], "UNAVAILABLE")
        self.assertEqual(report["statistical_evidence"]["dsr"]["reason_code"],
                         "INCOMPLETE_TRIAL_HISTORY")

    def test_more_searches_reduce_selection_adjusted_confidence(self):
        returns = [-0.01, 0.02, 0.01, 0.03, -0.02, 0.01, 0.015, -0.005, 0.01, 0.02]
        one = deflated_sharpe_ratio(returns, observed_sharpe=1.2, n_trials=1)
        many = deflated_sharpe_ratio(returns, observed_sharpe=1.2, n_trials=100)
        self.assertGreater(many["selection_threshold"], one["selection_threshold"])
        self.assertLess(many["psr"], one["psr"])
        observed = deflated_sharpe_ratio(
            returns, observed_sharpe=1.2, n_trials=10,
            trial_sharpes=[-0.2, 0.0, 0.3, 0.1],
        )
        self.assertTrue(observed["trial_sharpes_observed"])

    def test_synthetic_null_search_does_not_validate_raw_max_sharpe(self):
        rng = random.Random(20260907)
        candidates = [[rng.gauss(0.0, 1.0) for _ in range(80)] for _ in range(100)]
        sharpes = [statistics.fmean(row) / statistics.pstdev(row) for row in candidates]
        winner = candidates[max(range(len(candidates)), key=lambda index: sharpes[index])]
        raw = probabilistic_sharpe_ratio(winner, observed_sharpe=max(sharpes))
        adjusted = deflated_sharpe_ratio(winner, observed_sharpe=max(sharpes), n_trials=100)
        self.assertGreater(max(sharpes), 0.0)
        self.assertLess(adjusted["psr"], raw["psr"])
        self.assertGreater(adjusted["selection_threshold"], 0.0)

    def test_statistical_availability_is_fail_closed(self):
        self.assertEqual(probabilistic_sharpe_ratio([1.0, 1.0])["status"], "UNAVAILABLE")
        self.assertEqual(pbo_cscv([[1.0, 2.0, 3.0]])["status"], "UNAVAILABLE")
        self.assertEqual(pbo_cscv([[1, 2, 3, 4, 5, 6, 7, 8], [0, 1, 2, 3, 4, 5, 6, 7]])["status"], "AVAILABLE")

    def test_pnl_requires_verified_capability(self):
        self.assertEqual(PnlAdapter("COMMUNITY_OBSERVED").analyze([1, 2, 3])["status"], "UNAVAILABLE")
        result = PnlAdapter("LIVE_VERIFIED").analyze([0.01, 0.02, -0.01, 0.03] * 10, window=5)
        self.assertIn(result["status"], {"PASS", "FAIL"})
        self.assertIn("rolling_stability", result)
        self.assertIn("bootstrap", result)

    def test_agent_marks_parent_only_after_aggregate_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = Experiment(1, "h", "rank(signal)", {}, ["signal"])
            parent.status = "DONE"
            parent.metrics = _metrics()
            parent.health = {"ok": True}
            parent.self_correlation = {"status": "PASS"}
            parent.yearly_evidence = {"status": "VERIFIED", "stable": True}
            plan = default_validation_plan(parent, timestamp=1.0)
            children = []
            for variable in REQUIRED_VARIABLES[:-1]:
                child = Experiment(2, "h", f"rank({variable})", {}, ["signal"])
                child.status = "DONE"
                child.metrics = _metrics()
                child.health = {"ok": True}
                child.self_correlation = {"status": "PASS"}
                child.experiment_stage = "ROBUSTNESS"
                child.parent_expression = parent.expression
                child.changed_variable = variable
                child.validation_plan = plan
                children.append(child)
            agent = object.__new__(Agent)
            agent.trajectory = Trajectory()
            agent.trajectory.experiments = [parent, children[0]]
            agent.state_dir = tmp
            agent.trial_ledger = TrialLedger(os.path.join(tmp, "trial_ledger.jsonl"))
            agent.reflector = type("ReflectorStub", (), {"evidence_cache": {}})()
            agent._completed_parent = lambda expression: parent
            Agent._mark_robustness_stability(agent, [children[0]])
            self.assertNotEqual(parent.validation_status, "STABLE")
            agent.trajectory.experiments.extend(children[1:])
            Agent._mark_robustness_stability(agent, children)
            self.assertEqual(parent.validation_status, "STABLE")
            self.assertEqual(parent.validation_report["status"], "PASS")

    def test_agent_does_not_settle_incomplete_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = Experiment(1, "h", "rank(signal)", {}, ["signal"])
            parent.status = "DONE"
            parent.metrics = _metrics()
            parent.health = {"ok": True}
            parent.self_correlation = {"status": "PASS"}
            child = Experiment(2, "h", "rank(universe)", {}, ["signal"])
            child.status = "PENDING"
            child.experiment_stage = "ROBUSTNESS"
            child.parent_expression = parent.expression
            child.changed_variable = "universe_robustness"
            child.validation_plan = default_validation_plan(parent, timestamp=1.0)
            agent = object.__new__(Agent)
            agent.trajectory = Trajectory()
            agent.trajectory.experiments = [parent, child]
            agent.state_dir = tmp
            agent.trial_ledger = TrialLedger(os.path.join(tmp, "trial_ledger.jsonl"))
            agent.reflector = type("ReflectorStub", (), {"evidence_cache": {}})()
            agent._completed_parent = lambda expression: parent
            settled = []
            agent._settle_research_outcome = lambda experiment, report: settled.append(experiment)

            Agent._mark_robustness_stability(agent, [child])

            self.assertEqual(parent.validation_report["status"], "INCOMPLETE")
            self.assertFalse(parent.validation_report["terminal"])
            self.assertEqual(settled, [])
            self.assertIsNone(parent.final_outcome)

    def test_partial_validation_restart_then_completion_settles_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            trajectory_path = os.path.join(tmp, "trajectory.jsonl")
            parent = Experiment(1, "h", "rank(signal)", {}, ["signal"])
            parent.status = "DONE"
            parent.metrics = _metrics()
            parent.health = {"ok": True}
            parent.self_correlation = {"status": "PASS"}
            parent.yearly_evidence = {"status": "VERIFIED", "stable": True}
            parent.provisional_outcome = {"status": "PROVISIONAL", "reward": 0.4}
            plan = default_validation_plan(parent, timestamp=1.0)
            children = []
            for index, variable in enumerate(REQUIRED_VARIABLES[:-1], start=2):
                child = Experiment(index, "h", f"rank({variable})", {}, ["signal"])
                child.status = "DONE"
                child.metrics = _metrics()
                child.health = {"ok": True}
                child.self_correlation = {"status": "PASS"}
                child.experiment_stage = "ROBUSTNESS"
                child.parent_expression = parent.expression
                child.changed_variable = variable
                child.validation_plan = plan
                children.append(child)

            def make_agent(trajectory):
                agent = object.__new__(Agent)
                agent.trajectory = trajectory
                agent.state_dir = tmp
                agent.trial_ledger = TrialLedger(os.path.join(tmp, "trial_ledger.jsonl"))
                agent.reflector = type("ReflectorStub", (), {"evidence_cache": {}})()
                agent._settle_incremental_evidence = lambda experiment: {
                    "decision": "UNKNOWN"
                }
                agent.search_policy = type(
                    "SearchPolicyStub", (), {"replace_reward": lambda *args: None}
                )()
                agent._completed_parent = lambda expression: next(
                    (item for item in trajectory.experiments
                     if item.expression == expression and item.status == "DONE"),
                    None,
                )
                return agent

            first = Trajectory(path=trajectory_path)
            first.add_many([parent, children[0]])
            make_agent(first)._mark_robustness_stability([children[0]])
            self.assertIsNone(parent.final_outcome)

            restarted = Trajectory(path=trajectory_path).load()
            restored_parent = next(item for item in restarted.experiments if item.id == parent.id)
            restored_children = [
                item for item in restarted.experiments if item.experiment_stage == "ROBUSTNESS"
            ]
            restarted_agent = make_agent(restarted)
            restarted_agent._mark_robustness_stability(restored_children + children[1:])

            with open(os.path.join(tmp, "trial_ledger.jsonl"), encoding="utf-8") as handle:
                ledger_rows = [json.loads(line) for line in handle if line.strip()]
            final_rows = [
                row for row in ledger_rows
                if row.get("phase") == "research_outcome_settled"
            ]
            settled_rows = [
                row for row in restarted.iter_rows()
                if row.get("id") == parent.id
                and row.get("trajectory_revision") == "RESEARCH_SETTLED"
            ]
            self.assertEqual(len(final_rows), 1)
            self.assertEqual(len(settled_rows), 1)
            self.assertEqual(restored_parent.final_outcome.get("settlement_id"),
                             final_rows[0]["settlement"]["settlement_id"])


if __name__ == "__main__":
    unittest.main()
