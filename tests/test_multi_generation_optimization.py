"""Phase III acceptance test: real P0 -> C1 -> C2 multi-generation restart.

This is the phase's most important acceptance test.  It exercises the real
production settlement hook (``Agent._settle_research_outcome`` -> canonical
``Trajectory`` revision), a real restart, and the formal Agent
``OptimizationDecision`` path, instead of hand-writing three Experiments into
one trajectory.
"""

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from wqb_agent.agent import Agent
from wqb_agent.optimization_decision import OptimizationDecision
from wqb_agent.optimizer_workflow import OptimizerHooks, OptimizerWorkflow
from wqb_agent.state import RESEARCH_SETTLED_REVISION, Experiment

MECHANISM = "质量变化驱动的相对定价差异"
IMPACT = {
    "expected_effect": "LOWER",
    "basis": "子域中性化降低与现有族的相关暴露",
    "rationale": "同一字段、同一机制，仅改变可比组",
    "admission": "ALLOW",
}


def _report():
    return {
        "status": "PASS",
        "dimensions": {
            "platform_quality": {"status": "PASS"},
            "robustness": {"status": "PASS"},
        },
        "statistical_evidence": {},
    }


class FakeCache:
    def load(self):
        return {}


class RecordingFactory:
    def __init__(self):
        self.optimize_calls = []

    def screen_optimization_parents(self, parents, **kwargs):
        return list(parents)

    def optimize_signal_proposals(self, parents, operator_reference, **kwargs):
        self.optimize_calls.append(list(parents))
        return ["factory-result"]


class TestMultiGenerationOptimization(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wqb_multigen_")
        self.path = os.path.join(self.tmp, "trajectory.jsonl")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def agent(self):
        agent = Agent(object(), {"simulation": {}, "agent": {"state_dir": self.tmp}})
        agent._ensure_loaded()
        return agent

    def experiment(self, experiment_id, expression, **overrides):
        experiment = Experiment(
            1, "h-1", expression, {"decay": 4}, ["field_a"], ["fundamental6"],
            id=experiment_id,
        )
        experiment.status = "DONE"
        experiment.metrics = {
            "sharpe": 1.1, "fitness": 0.8, "turnover": 0.2,
            "checks": [{"name": "LOW_TURNOVER", "pass": True}],
        }
        experiment.provisional_outcome = {
            "reward": 1.0, "reward_quality": "PROVISIONAL_EVIDENCE",
            "outcome_kind": "PROVISIONAL",
        }
        experiment.search_outcome = experiment.provisional_outcome
        experiment.field_understanding = {"field_a": "已核验字段"}
        experiment.field_analysis = {"field_a": {"data_type": "MATRIX"}}
        experiment.field_source = {"kind": "brain_api"}
        experiment.field_hypothesis_basis = {"field_a": {"mechanism": MECHANISM}}
        experiment.economic_mechanism = MECHANISM
        for key, value in overrides.items():
            setattr(experiment, key, value)
        return experiment

    def workflow(self, trajectory, factory=None):
        return OptimizerWorkflow(
            trajectory=trajectory,
            alpha_feed_cache=FakeCache(),
            alpha_factory=factory or RecordingFactory(),
            quality_policy={},
            operator_reference={"operators": ["rank", "group_neutralize"]},
            hooks=OptimizerHooks(
                ensure_loaded=lambda: None,
                terminal_expressions=lambda: set(),
            ),
        )

    @staticmethod
    def decision(parent_id, expression):
        return OptimizationDecision(
            parent_id=parent_id,
            decision="CHILD",
            observed_evidence="父代子域内表现弱于整体",
            economic_mechanism=MECHANISM,
            change_type="neutralization",
            changed_variable="neut",
            expression=expression,
            expected_effect="lower sub-universe gap",
            falsification="若子域检查转差则机制不成立",
            direction="long",
            direction_transform={
                "applied": False,
                "reason": "沿用 parent 方向，不把方向翻转当作新机制",
            },
            self_correlation_impact=IMPACT,
            why_not_parameter_tuning="改变可比组而非窗口/权重",
        )

    def rows(self):
        return [
            json.loads(line)
            for line in Path(self.path).read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def test_p0_to_c1_to_c2_survives_two_real_restarts(self):
        # ---- generation 0: P0 executes, then settles later in the same process
        agent0 = self.agent()
        p0 = self.experiment("e0", "rank(field_a)")
        agent0.trajectory.add(p0)
        agent0._settle_research_outcome(p0, _report())
        self.assertIsNotNone(p0.final_outcome)

        # ---- restart 1: Agent sees P0 FINAL evidence and emits C1
        reader1 = self.agent()
        row_p0 = reader1.trajectory.find_row("e0")
        self.assertIsNotNone(row_p0)
        self.assertEqual(row_p0["final_outcome"], p0.final_outcome)
        self.assertEqual(row_p0["research_classification"], p0.research_classification)

        factory1 = RecordingFactory()
        flow1 = self.workflow(reader1.trajectory, factory1)
        summaries = flow1.inspect_optimizer_parents(limit=4)
        self.assertEqual([item["parent_id"] for item in summaries], ["e0"])
        decision_c1 = self.decision("e0", "group_neutralize(rank(field_a), SUBINDUSTRY)")
        result1 = flow1.generate_from_decisions([decision_c1], max_candidates=2)
        self.assertEqual(result1["proposals"], ["factory-result"])
        self.assertEqual(result1["decision_report"]["accepted"], 1)
        self.assertEqual(len(factory1.optimize_calls), 1)

        # ---- generation 1: C1 executes and settles
        agent1 = self.agent()
        c1 = self.experiment(
            "e1", decision_c1.expression,
            parent_expression=p0.expression, lineage_id="lin-1",
            change_type="neutralization",
            child_economic_hypothesis=decision_c1.to_child_hypothesis(),
        )
        agent1.trajectory.add(c1)
        self.assertIsNone(c1.final_outcome)
        agent1._settle_research_outcome(c1, _report())
        self.assertIsNotNone(c1.final_outcome)

        # the early DONE snapshot and the later settled revision are both kept
        rows = [row for row in self.rows() if row.get("id") == "e1"]
        revisions = [
            row for row in rows
            if row.get("trajectory_revision") == RESEARCH_SETTLED_REVISION
        ]
        self.assertEqual(len(revisions), 1)
        self.assertEqual(revisions[0]["final_outcome"], c1.final_outcome)

        # ---- restart 2: C1 is now a legal parent with FINAL settlement
        reader2 = self.agent()
        row_c1 = reader2.trajectory.find_row("e1")
        self.assertIsNotNone(row_c1)
        self.assertEqual(row_c1["final_outcome"], c1.final_outcome)
        self.assertEqual(row_c1["incremental_evidence"], c1.incremental_evidence)
        flow2 = self.workflow(reader2.trajectory)
        from wqb_agent.optimizer_selection import parent_rejections

        self.assertEqual(parent_rejections(row_c1), [])
        parent_ids = [item["parent_id"] for item in flow2.inspect_optimizer_parents()] 
        self.assertIn("e1", parent_ids)

        decision_c2 = self.decision("e1", "group_neutralize(rank(field_a), INDUSTRY)")
        result2 = flow2.generate_from_decisions([decision_c2], max_candidates=2)
        self.assertEqual(result2["proposals"], ["factory-result"])
        self.assertEqual(result2["decision_report"]["accepted"], 1)

    def test_execution_identity_is_immutable_across_generations(self):
        agent0 = self.agent()
        p0 = self.experiment("e0", "rank(field_a)")
        agent0.trajectory.add(p0)
        agent0._settle_research_outcome(p0, _report())

        # A settlement revision cannot be re-pointed at a different execution.
        tampered = self.experiment("e0", "rank(field_b)")
        with self.assertRaises(ValueError):
            agent0.trajectory.settle(tampered)

        reader = self.agent()
        self.assertEqual(reader.trajectory.find_row("e0")["expression"], "rank(field_a)")


if __name__ == "__main__":
    unittest.main()
