"""Agent 运行时 policy、组件和 workflow 装配边界的回归测试。"""

import ast
import os
import shutil
import tempfile
import unittest
from unittest import mock

from wqb_agent.agent import Agent
from wqb_agent.config import normalize_config
from wqb_agent.incremental_policy import IncrementalValuePolicy
from wqb_agent.optimization_decision import OptimizationDecision
from wqb_agent.runtime_components import (
    CheckpointStore,
    Simulator,
    SubmissionPool,
    Trajectory,
    TrialLedger,
    build_runtime_components,
)
from wqb_agent.runtime_composition import AgentWorkflows, build_agent_workflows
from wqb_agent.runtime_policy import AgentRuntimePolicy, build_agent_runtime_policy

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACKAGE_ROOT = os.path.join(ROOT, "wqb_agent")


def _config(state_dir):
    return {
        "simulation": {"neutralization": "SUBINDUSTRY"},
        "agent": {
            "state_dir": state_dir,
            "max_rounds": 3,
            "candidates_per_round": 5,
            "max_proposals_per_round": 12,
            "research_integrity": True,
            "fields_per_discovery": 4,
            "context_experiments": 7,
            "factory": {
                "max_simulations": 100,
                "max_runtime_sec": 3600,
                "daily_simulation_cap": 80,
                "weekly_simulation_cap": 100,
            },
            "field_selection": {
                "dataset_pool": ["pv1", "fundamental6"],
                "min_datasets": 2,
                "min_cross_dataset_pairs": 1,
                "require_platform_alpha_count": True,
            },
            "incremental_value": {
                "mode": "required_when_available",
                "max_abs_correlation": 0.6,
                "min_overlap": 40,
            },
        },
    }


class TestRuntimePolicy(unittest.TestCase):
    def test_policy_is_one_resolved_projection_and_keeps_compatibility_shape(self):
        with tempfile.TemporaryDirectory(prefix="wqb_policy_") as state_dir:
            config = normalize_config(_config(state_dir))
            policy = build_agent_runtime_policy(config)

        self.assertIsInstance(policy, AgentRuntimePolicy)
        self.assertEqual(policy.state_dir, state_dir)
        self.assertEqual(policy.max_rounds, 3)
        self.assertEqual(policy.candidates_per_round, 5)
        self.assertEqual(policy.max_proposals_per_round, 12)
        self.assertEqual(policy.dataset_pool, ("pv1", "fundamental6"))
        self.assertTrue(policy.require_platform_alpha_count)
        self.assertEqual(policy.min_factory_datasets, 2)
        self.assertEqual(policy.min_cross_dataset_pairs, 1)
        self.assertIsInstance(policy.incremental_policy, IncrementalValuePolicy)
        self.assertEqual(policy.incremental_policy.max_abs_correlation, 0.6)
        self.assertEqual(
            policy.factory_config["weekly_simulation_cap"],
            config.factory.weekly_simulation_cap,
        )
        self.assertEqual(policy.factory_config["max_runtime_sec"], 3600)


class TestAgentRuntimeComposition(unittest.TestCase):
    def setUp(self):
        self.state_dir = tempfile.mkdtemp(prefix="wqb_runtime_")

    def tearDown(self):
        shutil.rmtree(self.state_dir, ignore_errors=True)

    def test_constructor_preserves_public_projection_and_initial_state(self):
        agent = Agent(object(), _config(self.state_dir))
        public_attributes = (
            "state_dir", "simulation_settings", "factory_config",
            "max_rounds", "candidates_per_round", "max_proposals_per_round",
            "factory_batch_size", "research_allocation", "research_integrity",
            "fields_per_discovery", "context_experiments", "quality_policy",
            "statistical_policy", "robustness_policy", "incremental_policy",
            "max_field_alpha_count", "dataset_pool", "search_policy", "memory",
            "trajectory", "trial_ledger", "builder", "alpha_factory", "discovery",
            "simulator", "reflector", "checkpoints", "submission_pool",
            "suggestion_workflow", "proposal_execution", "daily_cache",
            "alpha_feed_cache", "alpha_feed_workflow", "optimizer_workflow",
        )
        for attribute in public_attributes:
            self.assertTrue(hasattr(agent, attribute), attribute)

        self.assertEqual(agent._last_round_skipped, False)
        self.assertEqual(agent.last_run_stats["status"], "NOT_STARTED")
        self.assertFalse(agent.memory.best_exhausted)

    def test_constructor_does_not_materialize_trial_ledger(self):
        agent = Agent(object(), _config(self.state_dir))
        agent.inspect_optimizer_parents()
        agent.optimizer_context()
        self.assertFalse(os.path.exists(os.path.join(self.state_dir, "trial_ledger.jsonl")))
        self.assertFalse(os.path.exists(os.path.join(self.state_dir, "proposals.json")))
        self.assertFalse(any(name.endswith(".checkpoint.json") for name in os.listdir(self.state_dir)))

    def test_components_are_built_once_and_workflows_share_their_identity(self):
        with mock.patch(
            "wqb_agent.agent.build_runtime_components",
            wraps=build_runtime_components,
        ) as build:
            agent = Agent(object(), _config(self.state_dir))

        build.assert_called_once()
        self.assertIs(agent.suggestion_workflow.discovery, agent.discovery)
        self.assertIs(agent.suggestion_workflow.memory, agent.memory)
        self.assertIs(agent.suggestion_workflow.trajectory, agent.trajectory)
        context = agent.proposal_execution.context
        self.assertIs(context.simulator, agent.simulator)
        self.assertIs(context.trajectory, agent.trajectory)
        self.assertIs(context.trial_ledger, agent.trial_ledger)
        self.assertIs(context.checkpoints, agent.checkpoints)
        self.assertIs(context.memory, agent.memory)
        self.assertIs(context.search_policy, agent.search_policy)
        self.assertIs(context.reflector, agent.reflector)
        self.assertIs(agent.alpha_feed_workflow.daily_cache, agent.daily_cache)
        self.assertIs(agent.alpha_feed_workflow.weekly_cache, agent.alpha_feed_cache)
        self.assertIs(agent.optimizer_workflow.trajectory, agent.trajectory)
        self.assertIs(agent.optimizer_workflow.alpha_feed_cache, agent.alpha_feed_cache)
        self.assertIs(agent.optimizer_workflow.alpha_factory, agent.alpha_factory)
        self.assertTrue(agent.trial_ledger.persist)

    def test_selection_accounting_survives_rebuilt_agent_runtime(self):
        decision = OptimizationDecision(parent_id="parent", decision="STOP")
        first = Agent(object(), _config(self.state_dir))
        self.assertFalse(os.path.exists(os.path.join(self.state_dir, "trial_ledger.jsonl")))
        first.trial_ledger.record_optimization_selection(
            decision, outcome="STOP", emitted=False, timestamp=1
        )
        first.trial_ledger.record(
            {"candidate_id": "candidate", "expression": "rank(signal)"},
            "candidate_generated", timestamp=2,
        )

        second = Agent(object(), _config(self.state_dir))
        summary = second.trial_ledger.summarize()
        self.assertEqual(summary["optimization_selection_count"], 1)
        self.assertEqual(summary["non_emitted_optimization_selection_count"], 1)
        self.assertEqual(summary["selection_trial_count"], 2)
        self.assertEqual(summary["history_completeness"], "COMPLETE_FROM_START")
        self.assertEqual(summary["history_completeness"], "COMPLETE_FROM_START")

    def test_domain_component_constructors_are_not_duplicated(self):
        constructors = {
            "trajectory": ("wqb_agent.runtime_components.Trajectory", Trajectory),
            "trial_ledger": ("wqb_agent.runtime_components.TrialLedger", TrialLedger),
            "checkpoints": ("wqb_agent.runtime_components.CheckpointStore", CheckpointStore),
            "simulator": ("wqb_agent.runtime_components.Simulator", Simulator),
            "submission_pool": ("wqb_agent.runtime_components.SubmissionPool", SubmissionPool),
        }
        patches = [mock.patch(path, wraps=constructor) for path, constructor in constructors.values()]
        with patches[0] as trajectory, patches[1] as ledger, patches[2] as checkpoints, patches[3] as simulator, patches[4] as pool:
            Agent(object(), _config(self.state_dir))
        for constructor in (trajectory, ledger, checkpoints, simulator, pool):
            self.assertEqual(constructor.call_count, 1)


class TestRuntimeCompositionBoundaries(unittest.TestCase):
    def test_runtime_components_and_composition_do_not_reverse_import_agent(self):
        for filename in ("runtime_components.py", "runtime_composition.py", "runtime_policy.py"):
            path = os.path.join(PACKAGE_ROOT, filename)
            with open(path, encoding="utf-8") as handle:
                tree = ast.parse(handle.read(), filename=path)
            imports = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imports.update(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom):
                    imports.add("." * node.level + (node.module or ""))
            self.assertNotIn(".agent", imports, filename)
            self.assertNotIn("wqb_agent.agent", imports, filename)
        component_imports = self._direct_imports("runtime_components")
        self.assertNotIn(".suggestion_workflow", component_imports)
        self.assertNotIn(".proposal_execution", component_imports)

    @staticmethod
    def _direct_imports(module_name):
        path = os.path.join(PACKAGE_ROOT, module_name + ".py")
        with open(path, encoding="utf-8") as handle:
            tree = ast.parse(handle.read(), filename=path)
        imports = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imports.add("." * node.level + (node.module or ""))
        return imports

    def test_workflow_composition_accepts_existing_objects_without_rebuilding_them(self):
        path = os.path.join(PACKAGE_ROOT, "runtime_composition.py")
        with open(path, encoding="utf-8") as handle:
            source = handle.read()
        self.assertNotIn("build_runtime_components", source)
        for expression in (
            "Trajectory(", "TrialLedger(", "CheckpointStore(",
            "Simulator(", "SubmissionPool(",
        ):
            self.assertNotIn(expression, source)
        self.assertTrue(hasattr(AgentWorkflows, "__dataclass_fields__"))
        self.assertTrue(callable(build_agent_workflows))

    def test_constructor_consumes_policy_instead_of_reinterpreting_runtime_sections(self):
        path = os.path.join(PACKAGE_ROOT, "agent.py")
        with open(path, encoding="utf-8") as handle:
            source = handle.read()
        constructor_source = source.split("    def __init__(self, client, config):", 1)[1]
        constructor_source = constructor_source.split("    # ------------------------------------------------------------ running", 1)[0]
        for expression in (
            "config.runtime", "config.factory", "config.search",
            "config.incremental_value", "runtime.field_selection.get(",
        ):
            self.assertNotIn(expression, constructor_source)


if __name__ == "__main__":
    unittest.main()
