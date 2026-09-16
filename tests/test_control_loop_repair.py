"""Phase VI 控制链修复的 offline 验收测试（无真实 Simulation、无状态写入、correlation/decision 反馈闭环）。"""

import dataclasses
import json
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
    _child,
    _correlation_parent,
    _CorrelationClient,
    _Factory,
    _parent,
    _workflow,
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
from wqb_agent import research_api
from wqb_agent.alpha_factory import AlphaFactory
from wqb_agent.checkpoints import CheckpointStore
from wqb_agent.factory_runner import AIFactoryRunner
from wqb_agent.optimizer_workflow import OptimizerHooks, OptimizerWorkflow
from wqb_agent.pre_correlation import (
    metric_optimization_context,
    pre_self_correlation_eligibility,
)
from wqb_agent.proposal_contract import (
    TARGETED_BATCH_TYPE,
    targeted_batch_state,
    validate_proposal,
)
from wqb_agent.state import Experiment, Trajectory


class TestGenerationBoundUsesRealChildHistory(unittest.TestCase):
    """P0-B：多代边界必须来自 canonical trajectory 的真实 CHILD 证据。"""

    def _restarted(self, path):
        trajectory = Trajectory(path=path, max_len=64, persist=True)
        trajectory.load()
        return trajectory

    def test_child_done_without_incremental_evidence_blocks_the_next_generation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "trajectory.jsonl")
            first = Trajectory(path=path, max_len=64, persist=True)
            first.add(_parent())
            first.add(_child(decision="UNAVAILABLE"))
            bound = _workflow(self._restarted(path)).optimizer_context()[
                "generation_bound"
            ]
            self.assertEqual(bound["children_generated"], 1)
            self.assertEqual(bound["children_done"], 1)
            self.assertFalse(bound["allowed"])
            self.assertEqual(bound["stop_reason"], "NO_INCREMENTAL_CHILD_EVIDENCE")

    def test_incremental_pass_allows_the_next_generation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "trajectory.jsonl")
            first = Trajectory(path=path, max_len=64, persist=True)
            first.add(_child(decision="PASS"))
            bound = _workflow(self._restarted(path)).optimizer_context()[
                "generation_bound"
            ]
            self.assertEqual(bound["incremental_pass"], 1)
            self.assertTrue(bound["allowed"])
            self.assertIsNone(bound["stop_reason"])

    def test_robustness_records_are_not_a_new_child_generation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "trajectory.jsonl")
            first = Trajectory(path=path, max_len=64, persist=True)
            first.add(_child(stage="ROBUSTNESS", decision="UNAVAILABLE"))
            bound = _workflow(self._restarted(path)).optimizer_context()[
                "generation_bound"
            ]
            self.assertEqual(bound["children_generated"], 0)
            self.assertTrue(bound["allowed"])

    def test_blocked_generation_turns_structural_repair_into_stop(self):
        """generation bound 不允许时，结构修复只能 STOP，不得再提议新 CHILD。"""
        blocked = dataclasses.replace(
            _parent(),
            health={"ok": False, "reasons": ["CONCENTRATED_WEIGHT=FAIL v=0.9"]},
            metrics=synthetic_metrics(checks=[
                {"name": "CONCENTRATED_WEIGHT", "pass": False, "result": "FAIL"},
                {"name": "SELF_CORRELATION", "pass": None, "result": "PENDING"},
            ]),
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "trajectory.jsonl")
            first = Trajectory(path=path, max_len=64, persist=True)
            first.add(blocked)
            first.add(_child(decision="UNAVAILABLE"))
            context = _workflow(self._restarted(path)).optimizer_context()
            self.assertFalse(context["generation_bound"]["allowed"])
            summary = context["eligible_parents"][0]
            self.assertEqual(
                summary["metric_optimization_context"]["readiness"],
                "STRUCTURAL_REPAIR_REQUIRED",
            )
            self.assertEqual(summary["next_action"], "STOP")


class TestResolvedCorrelationFeedsTheNextDecision(unittest.TestCase):
    """P0-C：同进程只 GET 一次，且已结算结果进入 optimizer context。"""

    def test_resolved_failure_is_reused_and_visible(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent, _client = make_agent(tmp, rounds=1)
            parent = _correlation_parent()
            agent.trajectory.add(parent)
            agent.reflector.evidence_cache = {}
            client = _CorrelationClient({"correlation": 0.9})
            agent.client = client
            agent._refresh_self_correlation_evidence([parent])
            self.assertEqual(client.calls, ["alpha-corr"])
            agent._refresh_self_correlation_evidence([parent])
            self.assertEqual(client.calls, ["alpha-corr"])
            context = agent.optimizer_context()
            summary = context["eligible_parents"][0]
            self.assertEqual(summary["self_correlation_status"], "FAIL")
            self.assertEqual(summary["next_action"], "CONSIDER_CORRELATION_REPAIR")
            self.assertEqual(
                summary["metric_optimization_context"]["readiness"],
                "STRUCTURAL_REPAIR_REQUIRED",
            )
            self.assertFalse(
                summary["metric_optimization_context"]["pre_correlation_eligible"]
            )
            self.assertIn(
                "SELF_CORRELATION_REPAIR",
                summary["metric_optimization_context"]["opportunities"],
            )
            self.assertEqual(context["next_action"], "CONSIDER_CORRELATION_REPAIR")

    def test_resolved_pass_advances_instead_of_rechecking(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent, _client = make_agent(tmp, rounds=1)
            parent = _correlation_parent()
            agent.trajectory.add(parent)
            agent.reflector.evidence_cache = {}
            agent.client = _CorrelationClient({"correlation": 0.1})
            agent._refresh_self_correlation_evidence([parent])
            summary = agent.optimizer_context()["eligible_parents"][0]
            self.assertEqual(summary["self_correlation_status"], "PASS")
            self.assertEqual(summary["next_action"], "READY_TO_ADVANCE")


class TestBoundedValidationStaysBounded(unittest.TestCase):
    """P1-A：VALIDATE 只能取 Python 解析的有界池，旧值必须来自 parent 自身。"""

    def _parent(self, **overrides):
        overrides.setdefault(
            "metrics",
            {
                "sharpe": 1.1,
                "fitness": 0.8,
                "turnover": 0.2,
                "checks": [
                    {"name": "LOW_SUB_UNIVERSE_SHARPE", "pass": False,
                     "result": "FAIL"},
                    {"name": "SELF_CORRELATION", "pass": None,
                     "result": "PENDING"},
                ],
            },
        )
        overrides.setdefault(
            "settings",
            {"delay": 1, "decay": 4, "truncation": 0.08, "universe": "TOP3000"},
        )
        overrides.setdefault("self_correlation", {"status": "PENDING"})
        return parent_record("p-universe", **overrides)

    def _flow(self, parents, *, universes=None):
        return OptimizerWorkflow(
            trajectory=FakeTrajectory(parents),
            alpha_feed_cache=_Cache(),
            alpha_factory=AlphaFactory(),
            quality_policy={},
            operator_reference={"operators": ["rank", "group_neutralize"]},
            hooks=OptimizerHooks(
                ensure_loaded=lambda: None,
                terminal_expressions=lambda: set(),
                allowed_universes=(
                    (lambda: tuple(universes)) if universes is not None else None
                ),
            ),
        )

    def test_universe_validate_without_a_pool_is_denied(self):
        flow = self._flow([self._parent()])
        result = flow.generate_from_decisions([
            validate_decision(
                "p-universe", validation_variable="universe",
                old_value="TOP3000", new_value="TOP1234",
            )
        ])
        self.assertEqual(result["proposals"], [])
        self.assertIn(
            "VALIDATION_UNIVERSE_POOL_UNAVAILABLE",
            result["rejected"][0]["reasons"],
        )

    def test_universe_validate_only_accepts_pooled_values(self):
        flow = self._flow([self._parent()], universes=["TOP1000", "TOP500"])
        out_of_pool = flow.generate_from_decisions([
            validate_decision(
                "p-universe", validation_variable="universe",
                old_value="TOP3000", new_value="TOP1234",
            )
        ])
        self.assertEqual(out_of_pool["proposals"], [])
        self.assertIn(
            "VALIDATION_NEW_VALUE_OUT_OF_POOL",
            out_of_pool["rejected"][0]["reasons"],
        )
        pooled = flow.generate_from_decisions([
            validate_decision(
                "p-universe", validation_variable="universe",
                old_value="TOP3000", new_value="TOP1000",
            )
        ])
        self.assertEqual(len(pooled["proposals"]), 1)
        proposal = pooled["proposals"][0]
        self.assertEqual(proposal["settings"], {"universe": "TOP1000"})
        self.assertEqual(proposal["experiment_stage"], "ROBUSTNESS")
        self.assertEqual(
            proposal["settings_variant"]["parent_default_value"], "TOP3000"
        )

    def test_agent_reported_old_value_never_becomes_provenance(self):
        flow = self._flow([self._parent()])
        lied = flow.generate_from_decisions([
            validate_decision("p-universe", old_value=7, new_value=5),
        ])
        self.assertEqual(lied["proposals"], [])
        self.assertIn(
            "VALIDATION_OLD_VALUE_MISMATCH", lied["rejected"][0]["reasons"]
        )
        honest = flow.generate_from_decisions([
            validate_decision("p-universe", old_value=4, new_value=5),
        ])
        self.assertEqual(len(honest["proposals"]), 1)
        self.assertEqual(
            honest["proposals"][0]["settings_variant"]["parent_default_value"], 4
        )


class TestHistoricalCorrelationBackfill(unittest.TestCase):
    """P1-B：脚本选择必须覆盖 trajectory_window 之外的 canonical 历史。"""

    def test_selection_reaches_beyond_the_in_memory_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "trajectory.jsonl")
            trajectory = Trajectory(path=path, max_len=512, persist=True)
            base = 1_700_000_000.0
            oldest = None
            for index in range(600):
                experiment = Experiment(
                    round=1, hypothesis_id=f"h-{index}",
                    expression=f"rank(field_{index})",
                    settings={"delay": 1}, fields_used=["field_a"],
                    status="DONE", alpha_id=f"alpha-{index}",
                    created_at=base + index,
                    metrics=synthetic_metrics(), health={"ok": True},
                )
                if index == 0:
                    oldest = experiment
                trajectory.add(experiment)
            self.assertNotIn(oldest.id, {item.id for item in trajectory.experiments})
            trajectory.settle(dataclasses.replace(
                oldest, final_outcome={"evidence_quality": "FINAL"},
            ))
            rows = load_trajectory_rows(
                tmp, window=64, since=base, until=base + 0.5
            )
            self.assertEqual([row["id"] for row in rows], [oldest.id])
            self.assertEqual(
                rows[0]["final_outcome"], {"evidence_quality": "FINAL"}
            )
            selection = pre_correlation_selection(
                rows, base, base + 0.5, None, delay=1, quality_policy={},
            )
            self.assertEqual(selection["alpha_ids"], ["alpha-0"])
