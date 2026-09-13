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
    _factory_agent,
    _factory_proposals,
    _run_factory,
    _targeted_proposal,
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
from wqb_agent.optimization_decision import optimization_decision_identity
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


class TestFactoryNeverOverwritesTargetedBatch(unittest.TestCase):
    """P1-C：Agent targeted batch 存在时工厂不得静默覆盖为 exploration 100。"""

    def _write(self, tmp, *, created_at, expires_at):
        payload = {
            "batch_type": TARGETED_BATCH_TYPE,
            "source": "agent_optimizer",
            "round_no": 7,
            "created_at": created_at,
            "expires_at": expires_at,
            "proposals": [
                _targeted_proposal(0),
                _targeted_proposal(1),
                _targeted_proposal(2, stage="ROBUSTNESS"),
            ],
        }
        path = os.path.join(tmp, "proposals.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        return payload, path

    def test_pending_targeted_batch_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            now = [0.0]
            payload, path = self._write(tmp, created_at=0.0, expires_at=3600.0)
            agent = _factory_agent(tmp)
            result = _run_factory(agent, now)
            with open(path, encoding="utf-8") as handle:
                after = json.load(handle)

        self.assertEqual(after, payload)
        self.assertEqual(result["last_action"], "WAIT_AGENT_DECISION")
        self.assertEqual(
            result["last_result"]["status"], "TARGETED_OPTIMIZATION_PENDING"
        )
        self.assertEqual(result["simulations_reserved"], 0)
        agent.run_suggestion_round.assert_not_called()
        agent.run_proposals.assert_not_called()
        agent.alpha_factory.generate_factory_batch.assert_not_called()

    def test_invalid_targeted_envelope_still_blocks_the_inbox(self):
        with tempfile.TemporaryDirectory() as tmp:
            now = [0.0]
            payload, path = self._write(tmp, created_at=0.0, expires_at=3600.0)
            payload["proposals"] = [_targeted_proposal(index) for index in range(5)]
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
            agent = _factory_agent(tmp)
            result = _run_factory(agent, now)
            with open(path, encoding="utf-8") as handle:
                after = json.load(handle)

        self.assertEqual(after, payload)
        self.assertEqual(result["last_action"], "WAIT_AGENT_DECISION")
        self.assertEqual(result["last_result"]["status"], "TARGETED_BATCH_INVALID")
        self.assertTrue(result["last_result"]["errors"])
        agent.alpha_factory.generate_factory_batch.assert_not_called()

    def test_expired_targeted_batch_releases_the_inbox(self):
        with tempfile.TemporaryDirectory() as tmp:
            now = [10_000.0]
            _payload, path = self._write(tmp, created_at=0.0, expires_at=1.0)
            agent = _factory_agent(tmp)
            result = _run_factory(agent, now)
            with open(path, encoding="utf-8") as handle:
                after = json.load(handle)

        self.assertEqual(after["batch_type"], "factory_100")
        self.assertEqual(len(after["proposals"]), 100)
        self.assertEqual(result["last_action"], "WAIT_RUN_PROPOSALS")
        agent.alpha_factory.generate_factory_batch.assert_called_once()


class TestTargetedBatchMaterialization(unittest.TestCase):
    """P1-C：Agent 决策必须能落到唯一 proposals.json 的 targeted envelope。"""

    def _runtime(self, tmp, proposals, *, round_no=42):
        return SimpleNamespace(
            state_dir=tmp,
            next_round_no=lambda: round_no,
            propose_optimization=lambda decisions, max_candidates=4: {
                "proposals": list(proposals), "rejected": [],
            },
        )

    def test_materialize_writes_one_bounded_envelope(self):
        with tempfile.TemporaryDirectory() as tmp:
            proposals = (
                [_targeted_proposal(index) for index in range(4)]
                + [
                    _targeted_proposal(index + 10, stage="ROBUSTNESS")
                    for index in range(4)
                ]
            )
            result = research_api.materialize_targeted_batch(
                [], agent=self._runtime(tmp, proposals), state_dir=tmp,
            )
            with open(os.path.join(tmp, "proposals.json"), encoding="utf-8") as handle:
                payload = json.load(handle)
            state = targeted_batch_state(payload, now=payload["created_at"])

        self.assertTrue(result["written"])
        self.assertEqual(result["status"], "TARGETED_BATCH_WRITTEN")
        self.assertEqual(payload["batch_type"], TARGETED_BATCH_TYPE)
        self.assertEqual(payload["round_no"], 42)
        self.assertEqual(len(payload["proposals"]), 8)
        self.assertTrue(state["valid"])
        self.assertTrue(state["blocking"])
        self.assertGreater(payload["expires_at"], payload["created_at"])

    def test_out_of_contract_batch_is_refused_without_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            proposals = [_targeted_proposal(index) for index in range(5)]
            result = research_api.materialize_targeted_batch(
                [], agent=self._runtime(tmp, proposals), state_dir=tmp,
            )
            exists = os.path.exists(os.path.join(tmp, "proposals.json"))

        self.assertFalse(result["written"])
        self.assertEqual(result["status"], "TARGETED_BATCH_REJECTED")
        self.assertTrue(result["errors"])
        self.assertFalse(exists)

    def test_no_decision_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = research_api.materialize_targeted_batch(
                [], agent=self._runtime(tmp, []), state_dir=tmp,
            )
            exists = os.path.exists(os.path.join(tmp, "proposals.json"))

        self.assertFalse(result["written"])
        self.assertEqual(result["status"], "NO_TARGETED_PROPOSAL")
        self.assertFalse(exists)

    def test_identical_active_targeted_batch_is_a_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            decision = child_decision("p1")
            decision_ids = [optimization_decision_identity(decision)]
            fingerprint = research_api._targeted_batch_fingerprint(decision_ids)
            payload = {
                "batch_type": TARGETED_BATCH_TYPE, "source": "agent_optimizer",
                "round_no": 42, "created_at": 10.0, "expires_at": 4_000_000_000.0,
                "optimization_decision_ids": decision_ids,
                "decision_fingerprint": fingerprint,
                "proposals": [_targeted_proposal(0)],
            }
            path = os.path.join(tmp, "proposals.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
            runtime = self._runtime(tmp, [_targeted_proposal(9)], round_no=99)
            result = research_api.materialize_targeted_batch(
                [decision], agent=runtime, state_dir=tmp,
            )
            with open(path, encoding="utf-8") as handle:
                after = json.load(handle)

        self.assertEqual(result["status"], "TARGETED_BATCH_UNCHANGED")
        self.assertEqual(after, payload)

    def test_different_active_targeted_batch_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = child_decision("p1")
            second = child_decision("p2")
            payload = {
                "batch_type": TARGETED_BATCH_TYPE, "source": "agent_optimizer",
                "round_no": 42, "created_at": 10.0, "expires_at": 4_000_000_000.0,
                "optimization_decision_ids": [optimization_decision_identity(first)],
                "decision_fingerprint": research_api._targeted_batch_fingerprint(
                    [optimization_decision_identity(first)]
                ),
                "proposals": [_targeted_proposal(0)],
            }
            path = os.path.join(tmp, "proposals.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
            result = research_api.materialize_targeted_batch(
                [second], agent=self._runtime(tmp, [_targeted_proposal(9)]), state_dir=tmp,
            )
            with open(path, encoding="utf-8") as handle:
                after = json.load(handle)

        self.assertEqual(result["status"], "TARGETED_BATCH_CONFLICT")
        self.assertEqual(after, payload)

    def test_unfinished_checkpoint_blocks_materialization_before_selection(self):
        with tempfile.TemporaryDirectory() as tmp:
            original = {"batch_type": "existing", "proposals": [{"id": "original"}]}
            path = os.path.join(tmp, "proposals.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(original, handle)
            runtime = self._runtime(tmp, [_targeted_proposal(9)])
            runtime.checkpoints = SimpleNamespace(scan=lambda: [{
                "path": os.path.join(tmp, "round_1.checkpoint.json"),
                "round_no": 1,
                "malformed": False,
                "checkpoint": {"complete": False},
            }])
            result = research_api.materialize_targeted_batch(
                [child_decision("p1")], agent=runtime, state_dir=tmp,
            )
            with open(path, encoding="utf-8") as handle:
                after = json.load(handle)

        self.assertEqual(result["status"], "TARGETED_BATCH_RECOVERY_BLOCKED")
        self.assertEqual(after, original)

    def test_malformed_checkpoint_blocks_materialization_without_replacing_inbox(self):
        with tempfile.TemporaryDirectory() as tmp:
            original = {"batch_type": "existing", "proposals": [{"id": "original"}]}
            path = os.path.join(tmp, "proposals.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(original, handle)
            runtime = self._runtime(tmp, [_targeted_proposal(9)])
            runtime.checkpoints = SimpleNamespace(scan=lambda: [{
                "path": os.path.join(tmp, "round_1.checkpoint.json"),
                "round_no": 1, "malformed": True, "checkpoint": {},
            }])
            result = research_api.materialize_targeted_batch(
                [child_decision("p1")], agent=runtime, state_dir=tmp,
            )
            with open(path, encoding="utf-8") as handle:
                after = json.load(handle)

        self.assertEqual(result["status"], "TARGETED_BATCH_RECOVERY_BLOCKED")
        self.assertEqual(after, original)

    def test_submit_unknown_checkpoint_blocks_materialization_without_replacement(self):
        with tempfile.TemporaryDirectory() as tmp:
            original = {"batch_type": "existing", "proposals": [{"id": "original"}]}
            path = os.path.join(tmp, "proposals.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(original, handle)
            runtime = self._runtime(tmp, [_targeted_proposal(9)])
            runtime.checkpoints = SimpleNamespace(scan=lambda: [{
                "path": os.path.join(tmp, "round_1.checkpoint.json"),
                "round_no": 1, "malformed": False,
                "checkpoint": {
                    "complete": True,
                    "experiments": [{"status": "SUBMIT_UNKNOWN"}],
                },
            }])
            result = research_api.materialize_targeted_batch(
                [child_decision("p1")], agent=runtime, state_dir=tmp,
            )
            with open(path, encoding="utf-8") as handle:
                after = json.load(handle)

        self.assertEqual(result["status"], "TARGETED_BATCH_RECOVERY_BLOCKED")
        self.assertEqual(after, original)


class TestFactoryNeverInventsAgentDecisions(unittest.TestCase):
    """P1-D：Python 不得替 Agent 伪造 CHILD decision。"""

    def _parent(self):
        field_profile = {
            "id": "field_a", "dataset": "fundamental6", "type": "MATRIX",
            "description": "已核验字段", "semantic_status": "KNOWN",
        }
        parent = parent_record(
            "p-repair",
            health={"ok": False, "reasons": ["CONCENTRATED_WEIGHT=FAIL v=0.9"]},
            metrics={
                "sharpe": 1.1, "fitness": 0.8, "turnover": 0.2,
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
        return parent, field_profile

    def test_structural_repair_parent_needs_an_agent_decision(self):
        parent, _profile = self._parent()
        flow = OptimizerWorkflow(
            trajectory=FakeTrajectory([parent]),
            alpha_feed_cache=_Cache(),
            alpha_factory=AlphaFactory(),
            quality_policy={},
            operator_reference={"operators": ["rank", "group_neutralize"]},
            hooks=OptimizerHooks(
                ensure_loaded=lambda: None,
                terminal_expressions=lambda: set(),
            ),
        )
        silent = flow.generate([parent], max_candidates=2)
        self.assertEqual(silent, [])
        self.assertEqual(flow.last_handoff_report["child_generated"], 0)
        self.assertEqual(flow.last_handoff_report["optimizer_rejected"], 1)
        authored = flow.generate_from_decisions(
            [child_decision("p-repair")], max_candidates=2,
        )
        self.assertEqual(len(authored["proposals"]), 1)
