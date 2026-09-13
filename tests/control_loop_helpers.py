"""Shared synthetic fixtures for control-loop repair tests."""

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


class _Cache:
    def load(self):
        return {}


class _Factory:
    registry = None

    def screen_optimization_parents(self, parents, **kwargs):
        return list(parents)


def _child(*, stage="CHILD", decision=None, status="DONE", alpha_id=None):
    return Experiment(
        round=1,
        hypothesis_id="h1",
        expression="group_neutralize(rank(field_a), SUBINDUSTRY)",
        settings={"delay": 1},
        fields_used=["field_a"],
        datasets=["fundamental6"],
        experiment_stage=stage,
        status=status,
        alpha_id=alpha_id,
        incremental_evidence=({"decision": decision} if decision else None),
    )


def _parent():
    """已结算的 P0 父代：generation bound 必须忽略它，只认真实 CHILD。"""
    return Experiment(
        round=1,
        hypothesis_id="h0",
        expression="rank(field_a)",
        settings={"delay": 1},
        fields_used=["field_a"],
        datasets=["fundamental6"],
        field_understanding={"field_a": "已核验字段"},
        field_analysis={"field_a": {"data_type": "MATRIX"}},
        field_source={"kind": "brain_api"},
        field_hypothesis_basis={"field_a": {"mechanism": "质量变化"}},
        economic_mechanism="质量变化驱动的相对定价差异",
        status="DONE",
        alpha_id="alpha-p0",
        metrics=synthetic_metrics(),
        health={"ok": True},
    )


def _workflow(trajectory):
    return OptimizerWorkflow(
        trajectory=trajectory,
        alpha_feed_cache=_Cache(),
        alpha_factory=_Factory(),
        quality_policy={},
        operator_reference={"operators": []},
        hooks=OptimizerHooks(
            ensure_loaded=lambda: None,
            terminal_expressions=lambda: set(),
        ),
    )


class _CorrelationClient:
    def __init__(self, payload):
        self._session = object()
        self.payload = payload
        self.calls = []

    def get_self_correlation(self, alpha_id, timeout_sec=20):
        self.calls.append(str(alpha_id))
        return dict(self.payload)


def _correlation_parent(alpha_id="alpha-corr"):
    return Experiment(
        round=1,
        hypothesis_id="h1",
        expression="rank(field_a)",
        settings={"delay": 1},
        fields_used=["field_a"],
        datasets=["fundamental6"],
        field_understanding={"field_a": "已核验字段"},
        field_analysis={"field_a": {"data_type": "MATRIX"}},
        field_source={"kind": "brain_api"},
        field_hypothesis_basis={"field_a": {"mechanism": "质量变化"}},
        economic_mechanism="质量变化驱动的相对定价差异",
        status="DONE",
        alpha_id=alpha_id,
        health={"ok": True},
        metrics={
            "sharpe": 1.6,
            "fitness": 1.3,
            "turnover": 0.25,
            "returns": 0.05,
            "drawdown": 0.05,
            "checks": [
                {"name": "CONCENTRATED_WEIGHT", "pass": True},
                {"name": "SELF_CORRELATION", "pass": None, "result": "PENDING"},
            ],
        },
    )


def _targeted_proposal(index, *, stage="CHILD"):
    return {
        "expression": f"group_neutralize(rank(field_{index}), SUBINDUSTRY)",
        "proposal_origin": "agent_optimizer",
        "experiment_stage": stage,
        "settings": {"delay": 1, "decay": 4 + index},
        "datasets": ["fundamental6"],
        "fields": ["field_a"],
    }


def _factory_proposals():
    return [
        {
            "expression": f"rank(field_{index})",
            "proposal_origin": "factory",
            "research_layer": "exploration",
            "research_role": "EXPLORE",
            "experiment_stage": "BASELINE",
            "exploration_objective": "signal_discovery",
            "datasets": ["d1"],
        }
        for index in range(100)
    ]


def _factory_agent(tmp):
    agent = SimpleNamespace(
        state_dir=tmp,
        alpha_factory=Mock(),
        factory_config={},
        min_factory_datasets=1,
        min_cross_dataset_pairs=0,
        next_round_no=lambda: 1,
        run_suggestion_round=Mock(return_value={
            "research_space": {
                "id": "h1", "statement": "test", "datasets": ["d1"]
            },
            "fields": [],
            "operator_reference": {
                "operators": ["rank"], "status": "LIVE_VERIFIED",
                "availability": "AVAILABLE", "source": "BRAIN_LIVE_ONLY",
            },
            "field_source": {"kind": "brain_api", "snapshot_date": "today"},
        }),
        run_proposals=Mock(return_value=None),
        last_run_stats={"accepted": 0, "rejected": 0, "skipped": 0},
        checkpoints=CheckpointStore(tmp),
        memory=SimpleNamespace(seen_expressions=set()),
        trajectory=SimpleNamespace(experiments=[]),
    )
    agent.alpha_factory.generate_factory_batch.return_value = _factory_proposals()
    return agent


def _run_factory(agent, now):
    return AIFactoryRunner(
        agent, factory=agent.alpha_factory, quiet=True,
        clock=lambda: now[0],
        sleeper=lambda seconds: now.__setitem__(0, now[0] + seconds),
    ).run(
        duration_sec=1, idle_sleep_sec=1, max_simulations=11200,
        daily_simulation_cap=1600, weekly_simulation_cap=11200,
    )


def _write_field_cache(tmp):
    payload = {
        "datasets": {
            "fundamental6": [{
                "id": "field_a", "type": "MATRIX", "description": "已核验字段",
                "dataset": {"id": "fundamental6"},
            }],
        },
    }
    path = os.path.join(tmp, "fields_cache.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)


def _completed_parent():
    """已结算、结构可修的 parent：它自己的表达式已在终态集合里。"""
    return Experiment(
        round=1, hypothesis_id="h-1",
        expression=PARENT_EXPRESSION, settings={"delay": 1},
        fields_used=["field_a"], datasets=["fundamental6"],
        field_understanding={"field_a": "已核验字段"},
        field_analysis={"field_a": {"semantic": "已核验字段",
                                    "data_type": "MATRIX",
                                    "coverage": None, "frequency": None}},
        field_source={"kind": "brain_api", "snapshot_date": "2026-09-12"},
        field_hypothesis_basis={"field_a": {"mechanism": "质量变化"}},
        economic_mechanism="质量变化驱动的相对定价差异",
        status="DONE", alpha_id="alpha-parent",
        metrics={
            "sharpe": 1.1, "fitness": 0.8, "turnover": 0.2,
            "returns": 0.03, "drawdown": 0.1,
            "checks": [
                {"name": "CONCENTRATED_WEIGHT", "pass": False, "result": "FAIL"},
                {"name": "SELF_CORRELATION", "pass": None, "result": "PENDING"},
            ],
        },
        health={"ok": False, "reasons": ["CONCENTRATED_WEIGHT=FAIL v=0.9"]},
    )
