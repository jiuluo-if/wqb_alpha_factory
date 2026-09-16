"""Phase VI 控制链修复的 offline 验收测试（无真实 Simulation、无状态写入、correlation/decision 反馈闭环）。"""

import dataclasses
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from scripts.refresh_self_correlation import (
    load_trajectory_rows,
    pre_correlation_selection,
)
from tests.control_loop_helpers import (
    _Cache,
    _completed_parent,
    _CorrelationClient,
    _write_field_cache,
)
from tests.optimizer_helpers import (
    CHILD_EXPRESSION,
    PARENT_EXPRESSION,
    FakeTrajectory,
    child_decision,
    parent_record,
    validate_decision,
)
from tests.test_agent_flow import make_agent
from tests.test_pre_correlation import synthetic_metrics
from wqb_agent.alpha_factory import AlphaFactory
from wqb_agent.checkpoints import CheckpointStore
from wqb_agent.factory_runner import AIFactoryRunner
from wqb_agent.optimizer_workflow import OptimizerHooks, OptimizerWorkflow
from wqb_agent.pre_correlation import (
    metric_optimization_context,
    pre_self_correlation_eligibility,
)
from wqb_agent.proposal_contract import (
    validate_proposal,
)
from wqb_agent.state import Experiment, Trajectory


class TestStructuralRepairChainEndToEnd(unittest.TestCase):
    """Phase VI 端到端离线链：结构 blocker → CHILD → 相关性 FAIL → 下一代边界。"""

    def test_structural_blocker_repair_chain(self):
        field_profile = {
            "id": "field_a", "dataset": "fundamental6", "type": "MATRIX",
            "description": "已核验字段", "semantic_status": "KNOWN",
        }
        parent = parent_record(
            "p-chain",
            health={"ok": False, "reasons": ["CONCENTRATED_WEIGHT=FAIL v=0.9"]},
            metrics={
                "sharpe": 1.1, "fitness": 0.8, "turnover": 0.2,
                "returns": 0.03, "drawdown": 0.1,
                "checks": [
                    {"name": "CONCENTRATED_WEIGHT", "pass": False,
                     "result": "FAIL"},
                    {"name": "SELF_CORRELATION", "pass": None,
                     "result": "PENDING"},
                ],
            },
            field_analysis={"field_a": {
                "semantic": "已核验字段", "coverage": None,
                "frequency": None, "data_type": "MATRIX",
            }},
        )
        trajectory = FakeTrajectory([parent])
        flow = OptimizerWorkflow(
            trajectory=trajectory,
            alpha_feed_cache=_Cache(),
            alpha_factory=AlphaFactory(),
            quality_policy={},
            operator_reference={
                "operators": ["rank", "group_neutralize"], "sha256": "sha",
            },
            hooks=OptimizerHooks(
                ensure_loaded=lambda: None,
                terminal_expressions=lambda: set(),
            ),
        )
        # 1) Agent 视图：结构 blocker 必须可见，并指向 CHILD 修复。
        summary = flow.optimizer_context()["eligible_parents"][0]
        self.assertEqual(
            summary["metric_optimization_context"]["readiness"],
            "STRUCTURAL_REPAIR_REQUIRED",
        )
        self.assertEqual(summary["next_action"], "CONSIDER_CHILD")
        self.assertFalse(
            summary["metric_optimization_context"]["pre_correlation_eligible"]
        )
        # 即使 parent 历史 SELF_CORRELATION 已 PASS，结构 blocker 未修复时
        # 下一步仍由 CHILD 修复主导，不得推进。
        self.assertEqual(parent["self_correlation"]["status"], "PASS")
        # 2) Agent authored CHILD → production-valid proposal。
        repaired = flow.generate_from_decisions(
            [child_decision("p-chain")], max_candidates=1,
        )
        self.assertEqual(len(repaired["proposals"]), 1)
        ok, problems = validate_proposal(
            repaired["proposals"][0],
            discovered_fields=[field_profile],
            strict_experiment=True,
            operator_reference={
                "operators": ["rank", "group_neutralize"], "sha256": "sha",
            },
            require_economic_integrity=True,
        )
        self.assertEqual(problems, [])
        self.assertTrue(ok)

        with tempfile.TemporaryDirectory() as tmp:
            agent, _client = make_agent(tmp, rounds=1)
            # 3) synthetic C1 DONE：非相关性 checks PASS、metrics 过线、health ok。
            child = Experiment(
                round=2, hypothesis_id="h-child",
                expression=CHILD_EXPRESSION, settings={"delay": 1},
                fields_used=["field_a"], datasets=["fundamental6"],
                field_understanding={"field_a": "已核验字段"},
                field_analysis={"field_a": {"data_type": "MATRIX"}},
                field_source={"kind": "brain_api"},
                field_hypothesis_basis={"field_a": {"mechanism": "质量变化"}},
                economic_mechanism="质量变化驱动的相对定价差异",
                status="DONE", alpha_id="alpha-child",
                experiment_stage="CHILD", parent_expression=PARENT_EXPRESSION,
                metrics=synthetic_metrics(), health={"ok": True},
            )
            agent.trajectory.add(child)
            self.assertTrue(pre_self_correlation_eligibility(
                child.metrics, delay=1, quality_policy={}, health=child.health,
            )["eligible"])
            self.assertEqual(
                metric_optimization_context(
                    child.metrics, delay=1, quality_policy={},
                    health=child.health,
                )["readiness"],
                "PRE_CORRELATION_READY",
            )
            # 4) SELF_CORRELATION 只 GET 一次，结果立即进入下一步判断。
            agent.reflector.evidence_cache = {}
            client = _CorrelationClient({"correlation": 0.9})
            agent.client = client
            agent._refresh_self_correlation_evidence([child])
            agent._refresh_self_correlation_evidence([child])
            self.assertEqual(client.calls, ["alpha-child"])
            self.assertEqual(
                agent.resolved_self_correlation("alpha-child")["status"], "FAIL"
            )
            # 5) FAIL 立即进入 Agent 视图：opportunity 与 next_action 同步。
            context = agent.optimizer_context()
            summary = context["eligible_parents"][0]
            self.assertEqual(summary["self_correlation_status"], "FAIL")
            self.assertEqual(summary["next_action"], "CONSIDER_CORRELATION_REPAIR")
            self.assertEqual(context["next_action"], "CONSIDER_CORRELATION_REPAIR")
            self.assertIn(
                "SELF_CORRELATION_REPAIR",
                summary["metric_optimization_context"]["opportunities"],
            )
            # 6) generation bound 反映真实 C1 历史（无 incremental evidence）。
            trajectory.rows.append(child.to_dict())
            bound = flow.optimizer_context()["generation_bound"]
        self.assertEqual(bound["children_done"], 1)
        self.assertFalse(bound["allowed"])
        self.assertEqual(bound["stop_reason"], "NO_INCREMENTAL_CHILD_EVIDENCE")


class TestTargetedBatchRunsOnTheSingleExecutionPath(unittest.TestCase):
    """P1-C：targeted batch 必须真的生成，并沿唯一 run-proposals 路径执行。"""

    def test_completed_parent_is_not_excluded_by_its_own_terminal_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent, _client = make_agent(tmp, rounds=1)
            parent = _completed_parent()
            agent.trajectory.add(parent)
            terminal = agent.optimizer_workflow.hooks.terminal_expressions()
            report = agent.propose_optimization(
                [child_decision(parent.id)], max_candidates=1
            )
        self.assertIn(PARENT_EXPRESSION, terminal)
        self.assertEqual(len(report["proposals"]), 1)
        self.assertEqual(report["decision_report"]["child_generated"], 1)

    def test_completed_parent_validate_decision_still_emits_robustness(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent, _client = make_agent(tmp, rounds=1)
            parent = _completed_parent()
            parent.settings = {"delay": 1, "decay": 4,
                               "truncation": 0.08, "universe": "TOP3000"}
            agent.trajectory.add(parent)
            report = agent.propose_optimization(
                [validate_decision(parent.id)], max_candidates=2
            )
        self.assertEqual(report["decision_report"]["validation_generated"], 1)
        proposal = report["proposals"][0]
        self.assertEqual(proposal["experiment_stage"], "ROBUSTNESS")
        self.assertEqual(proposal["changed_variable"], "decay")
        self.assertEqual(proposal["settings"], {"decay": 5})
