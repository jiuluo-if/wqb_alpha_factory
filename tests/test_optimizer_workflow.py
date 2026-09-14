"""OptimizerWorkflow 的行为、契约和只读边界测试。"""

import ast
import copy
import os
import unittest
from types import SimpleNamespace
from unittest import mock

from wqb_agent.agent import Agent
from wqb_agent.optimizer_workflow import OptimizerHooks, OptimizerWorkflow
from wqb_agent.research_guard import overfit_expression_reason

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACKAGE_ROOT = os.path.join(ROOT, "wqb_agent")


def _parent(expression="rank(field)", *, alpha_id=None, child=None):
    return {
        "status": "DONE",
        "expression": expression,
        "fields_used": ["field"],
        "datasets": ["fundamental6"],
        "metrics": {"sharpe": 1.1, "fitness": 0.8, "turnover": 0.2,
                    "checks": [{"name": "SELF_CORRELATION", "status": "UNKNOWN"}]},
        "health": {"ok": True},
        "field_understanding": {"field": "已核验字段"},
        "field_analysis": {"field": {"data_type": "MATRIX"}},
        "field_source": {"kind": "brain_api"},
        "field_hypothesis_basis": {"field": {"mechanism": "质量变化"}},
        "alpha_id": alpha_id,
        "child_economic_hypothesis": child,
        "hypothesis_id": "h1",
        "economic_mechanism": "信息变化导致相对定价差异",
    }


class FakeTrajectory:
    def __init__(self, rows):
        self.rows = list(rows)
        self.experiments = list(rows)

    def recent(self, limit):
        return self.rows[-limit:]


class FakeCache:
    def __init__(self, payload):
        self.payload = payload
        self.load_calls = 0

    def load(self):
        self.load_calls += 1
        return self.payload


class RecordingFactory:
    def __init__(self):
        self.screen_calls = []
        self.optimize_calls = []

    def screen_optimization_parents(self, parents, **kwargs):
        self.screen_calls.append((parents, kwargs))
        return list(parents)

    def optimize_signal_proposals(self, parents, operator_reference, **kwargs):
        self.optimize_calls.append((parents, operator_reference, kwargs))
        return ["factory-result"]


class TestOptimizerWorkflow(unittest.TestCase):
    def test_optimizable_records_use_one_trajectory_snapshot(self):
        class CountingTrajectory(FakeTrajectory):
            def __init__(self, rows):
                super().__init__(rows)
                self.recent_calls = 0

            def recent(self, limit):
                self.recent_calls += 1
                return super().recent(limit)

        row = SimpleNamespace(
            status="DONE", alpha_id="a",
            to_dict=lambda: _parent("rank(a)", alpha_id="a"),
        )
        trajectory = CountingTrajectory([row])
        self.workflow(trajectory).optimizable_signal_records()
        self.assertEqual(trajectory.recent_calls, 1)

    def test_done_parent_requires_complete_local_evidence_contract(self):
        incomplete = _parent()
        incomplete.pop("economic_mechanism")
        incomplete.pop("field_source")
        incomplete["metrics"].pop("checks")
        workflow = self.workflow(FakeTrajectory([]))
        records = workflow.optimizable_signal_records()
        self.assertEqual(records, [])
        report = workflow.gate_report([incomplete])
        self.assertEqual(report["ready_parent_count"], 0)
        self.assertGreater(report["blocked_reasons"].get("PARENT_CHECKS_INCOMPLETE", 0), 0)
        self.assertGreater(report["blocked_reasons"].get("PARENT_HYPOTHESIS_MISSING", 0), 0)
    def workflow(self, trajectory, cache=None, factory=None, *, ensure=None,
                 terminal=None):
        return OptimizerWorkflow(
            trajectory=trajectory,
            alpha_feed_cache=cache or FakeCache({}),
            alpha_factory=factory or RecordingFactory(),
            quality_policy={},
            operator_reference={"operators": ["rank", "group_neutralize"]},
            hooks=OptimizerHooks(
                ensure_loaded=ensure or (lambda: None),
                terminal_expressions=terminal or (lambda: set()),
            ),
        )

    def test_optimizable_records_keep_evidence_gate_and_cloud_priority(self):
        cloud = SimpleNamespace(
            status="DONE", metrics={"sharpe": 0.1},
            field_analysis={"field": {}}, field_understanding={"field": "ok"},
            alpha_id="cloud-alpha",
            to_dict=lambda: _parent("rank(cloud)", alpha_id="cloud-alpha"),
        )
        current = SimpleNamespace(
            status="DONE", metrics={"sharpe": 1.0},
            field_analysis={"field": {}}, field_understanding={"field": "ok"},
            alpha_id="current-alpha",
            to_dict=lambda: _parent("rank(current)", alpha_id="current-alpha"),
        )
        incomplete = SimpleNamespace(
            status="DONE", metrics={"sharpe": 2.0},
            field_analysis={}, field_understanding={"field": "ok"},
            alpha_id="incomplete-alpha",
            to_dict=lambda: dict(_parent("rank(incomplete)"), field_analysis={}),
        )
        cache = FakeCache({
            "days": {"2026-09-09": {"simulations": [
                {"alpha_id": "cloud-alpha"},
            ], "submitted_alphas": []}},
        })
        workflow = self.workflow(
            FakeTrajectory([cloud, current, incomplete]), cache=cache
        )

        records = workflow.optimizable_signal_records()

        self.assertEqual(
            [item["alpha_id"] for item in records],
            ["cloud-alpha", "current-alpha"],
        )
        self.assertEqual(records[0]["optimization_source"], "cloud")
        self.assertEqual(records[1]["optimization_source"], "current_run")
        self.assertEqual(records[0]["optimization_recency"], 2)
        self.assertEqual(records[1]["optimization_recency"], 1)

    def test_cloud_metadata_never_restores_evidence_and_source_is_explicit(self):
        cache = FakeCache({
            "days": {"2026-09-09": {"simulations": [
                {"alpha_id": "remote-only"},
            ], "submitted_alphas": []}},
        })
        workflow = self.workflow(FakeTrajectory([]), cache=cache)

        self.assertEqual(workflow.optimizable_signal_records(), [])

        local = SimpleNamespace(
            status="DONE", metrics={"sharpe": 1.0},
            field_analysis={"field": {}}, field_understanding={"field": "ok"},
            alpha_id="remote-only",
            to_dict=lambda: _parent("rank(local)", alpha_id="remote-only"),
        )
        records = self.workflow(FakeTrajectory([local]), cache=cache).optimizable_signal_records()

        self.assertEqual(records[0]["evidence_source"], "local_trajectory")
        self.assertEqual(records[0]["priority_source"], "cloud_metadata")

    def test_gate_report_preserves_blocked_reasons_and_does_not_leak_evidence(self):
        complete = _parent(child={
            "expression": "rank(group_neutralize(field, SUBINDUSTRY))",
            "economic_mechanism": "组内相对信息",
            "change_type": "neutralization_change",
        })
        parents = [
            "invalid",
            {"status": "PENDING"},
            {"status": "DONE", "metrics": {}},
            complete,
        ]
        original = copy.deepcopy(parents)
        workflow = self.workflow(FakeTrajectory([]))

        report = workflow.gate_report(parents)

        self.assertEqual(report["parent_count"], 4)
        self.assertEqual(report["done_parent_count"], 2)
        self.assertEqual(report["ready_parent_count"], 1)
        self.assertEqual(report["blocked_reasons"], {
            "INVALID_PARENT": 1,
            "PARENT_NOT_DONE": 1,
            "PARENT_METRICS_MISSING": 2,
            "PARENT_CHECKS_INCOMPLETE": 1,
            "PARENT_FIELD_EVIDENCE_MISSING": 1,
            "PARENT_HYPOTHESIS_MISSING": 1,
            "PARENT_INCREMENTAL_EVIDENCE_INSUFFICIENT": 1,
        })
        self.assertEqual(parents, original)
        serialized = repr(report)
        self.assertNotIn("alpha_id", serialized)
        self.assertNotIn("sharpe", serialized)

    def test_semantic_gate_rejects_missing_parameter_only_and_overfit_children(self):
        parameter_only = _parent(
            expression="rank(ts_zscore(field, 20))",
            child={
            "expression": "rank(ts_zscore(field, 60))",
            "economic_mechanism": "参数调整",
            "change_type": "window_change",
            },
        )
        overfit = _parent(child={
            "expression": (
                "0.3*rank(ts_decay_linear(ts_zscore(a, 5), 10)) + "
                "0.3*rank(ts_decay_linear(ts_zscore(b, 5), 10)) + "
                "0.4*rank(ts_decay_linear(ts_zscore(c, 5), 10))"
            ),
            "economic_mechanism": "堆叠",
            "change_type": "multi_leg_change",
        })
        valid = _parent(child={
            "expression": "rank(group_neutralize(field, SUBINDUSTRY))",
            "economic_mechanism": "组内相对信息",
            "change_type": "neutralization_change",
        })
        workflow = self.workflow(FakeTrajectory([]))

        selected = workflow._agent_screen_optimization_parents([
            _parent(child=None), parameter_only, overfit, valid,
        ])

        self.assertEqual(selected, [valid])
        self.assertIsNotNone(overfit_expression_reason(
            overfit["child_economic_hypothesis"]["expression"]
        ))

    def test_semantic_gate_rejects_incomplete_agent_hypothesis(self):
        incomplete_children = [
            "not-a-dict",
            {},
            {"expression": "rank(field)"},
            {
                "expression": "rank(field)",
                "economic_mechanism": "有机制但缺变更类型",
            },
            {
                "economic_mechanism": "有机制但缺表达式",
                "change_type": "field_change",
            },
        ]
        parents = [
            _parent(child=child) for child in incomplete_children
        ]
        workflow = self.workflow(FakeTrajectory([]))

        self.assertEqual(
            workflow._agent_screen_optimization_parents(parents), []
        )

    def test_semantic_gate_rejects_parameter_only_window_and_weight_changes(self):
        cases = (
            (
                "rank(ts_zscore(field, 10))",
                "rank(ts_zscore(field, 20))",
                "window_change",
            ),
            (
                "0.5*rank(field)",
                "0.8*rank(field)",
                "weight_change",
            ),
        )
        workflow = self.workflow(FakeTrajectory([]))

        for parent_expression, child_expression, change_type in cases:
            parent = _parent(
                expression=parent_expression,
                child={
                    "expression": child_expression,
                    "economic_mechanism": "参数变化不构成新机制",
                    "change_type": change_type,
                },
            )
            self.assertEqual(
                workflow._agent_screen_optimization_parents([parent]), []
            )

    def test_generate_preserves_order_defaults_and_dynamic_terminal_reads(self):
        parent = _parent(child={
            "expression": "rank(group_neutralize(field, SUBINDUSTRY))",
            "economic_mechanism": "组内相对信息",
            "change_type": "neutralization_change",
        })
        factory = RecordingFactory()
        events = []
        terminal_values = iter(({"old"}, {"new"}))
        workflow = self.workflow(
            FakeTrajectory([]), factory=factory,
            ensure=lambda: events.append("ensure"),
            terminal=lambda: (events.append("terminal") or next(terminal_values)),
        )

        result = workflow.generate([parent], max_candidates=4)

        self.assertEqual(result, ["factory-result"])
        self.assertEqual(events, ["ensure", "terminal", "terminal"])
        self.assertEqual(len(factory.screen_calls), 1)
        screened_parents, screen_kwargs = factory.screen_calls[0]
        self.assertEqual(screened_parents, [parent])
        self.assertEqual(screen_kwargs, {
            "excluded_expressions": {"old"},
            "min_sharpe": 0.9,
            "min_fitness": 0.6,
            "min_turnover": 0.01,
            "max_turnover": 0.7,
        })
        optimized_parents, reference, optimize_kwargs = factory.optimize_calls[0]
        self.assertEqual(optimized_parents, [parent])
        self.assertEqual(reference, {"operators": ["rank", "group_neutralize"]})
        self.assertEqual(optimize_kwargs, {
            "max_candidates": 4,
            "excluded_expressions": {"new"},
            "min_sharpe": 0.9,
            "min_fitness": 0.6,
            "min_turnover": 0.01,
            "max_turnover": 0.7,
        })

    def test_explicit_empty_parents_are_not_replaced_by_trajectory_records(self):
        factory = RecordingFactory()
        workflow = self.workflow(
            FakeTrajectory([_parent()]), factory=factory,
        )

        workflow.generate([], max_candidates=4)

        self.assertEqual(factory.screen_calls[0][0], [])
        self.assertEqual(factory.optimize_calls[0][0], [])

    def test_agent_optimizer_methods_are_thin_compatibility_facades(self):
        workflow = mock.Mock()
        workflow.optimizable_signal_records.return_value = ["records"]
        workflow.gate_report.return_value = {"ready_parent_count": 0}
        workflow.generate.return_value = ["proposal"]
        agent = Agent.__new__(Agent)
        agent.optimizer_workflow = workflow

        self.assertEqual(agent.optimizable_signal_records(limit=9), ["records"])
        self.assertEqual(agent.optimizer_gate_report(["parent"]), {
            "ready_parent_count": 0,
        })
        self.assertEqual(
            agent.generate_optimized_proposals(["parent"], max_candidates=3),
            ["proposal"],
        )
        workflow.optimizable_signal_records.assert_called_once_with(limit=9)
        workflow.gate_report.assert_called_once_with(["parent"])
        workflow.generate.assert_called_once_with(
            ["parent"], max_candidates=3
        )

    def test_direct_workflow_and_agent_facade_have_equivalent_generation(self):
        parent = _parent(child={
            "expression": "rank(group_neutralize(field, SUBINDUSTRY))",
            "economic_mechanism": "组内相对信息",
            "change_type": "neutralization_change",
        })
        direct_factory = RecordingFactory()
        facade_factory = RecordingFactory()
        direct = self.workflow(FakeTrajectory([]), factory=direct_factory)
        facade = self.workflow(FakeTrajectory([]), factory=facade_factory)
        agent = Agent.__new__(Agent)
        agent.optimizer_workflow = facade

        self.assertEqual(
            direct.generate([parent], max_candidates=2),
            agent.generate_optimized_proposals([parent], max_candidates=2),
        )
        self.assertEqual(direct_factory.screen_calls, facade_factory.screen_calls)
        self.assertEqual(direct_factory.optimize_calls, facade_factory.optimize_calls)


class TestOptimizerWorkflowArchitecture(unittest.TestCase):
    def test_workflow_has_narrow_read_only_dependencies(self):
        path = os.path.join(PACKAGE_ROOT, "optimizer_workflow.py")
        with open(path, encoding="utf-8") as handle:
            source = handle.read()
            tree = ast.parse(source, filename=path)
        imports = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imports.add("." * node.level + (node.module or ""))
        for forbidden in (
            ".agent", ".suggestion_workflow", ".proposal_execution",
            ".alpha_feed_workflow", ".simulator", ".client", "main", "cli",
            ".factory_runner", "wqb_agent.factory_runner",
        ):
            self.assertNotIn(forbidden, imports)
        for forbidden in (
            "submit_simulation(", "run_proposals(", "patch_alpha(",
            "submit_alpha(", "write proposals.json", "AlphaFactory()",
            "alpha_feed_workflow.refresh(",
        ):
            self.assertNotIn(forbidden, source)
        self.assertIn("child_economic_hypothesis", source)

    def test_workflow_constructor_does_not_accept_agent_or_client(self):
        parameter_names = set(__import__("inspect").signature(OptimizerWorkflow).parameters)
        self.assertNotIn("agent", parameter_names)
        self.assertNotIn("client", parameter_names)


if __name__ == "__main__":
    unittest.main()
