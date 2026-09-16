"""Executable dependency-direction contracts for the modular architecture."""

from __future__ import annotations

import ast
import dataclasses
import re
import unittest
from pathlib import Path

from wqb_agent.config import AppConfig

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = ROOT / "wqb_agent"


def _module_name(path: Path) -> str:
    relative = path.relative_to(ROOT).with_suffix("")
    return ".".join(relative.parts)


def _imports(path: Path) -> set[str]:
    module = _module_name(path)
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                imports.add(node.module)
                continue
            package = module.rsplit(".", 1)[0]
            if node.level > 1:
                package = ".".join(package.split(".")[: -(node.level - 1)])
            target = f"{package}.{node.module}" if node.module else package
            imports.add(target)
    return imports


def _package_imports(relative_paths: tuple[str, ...]) -> dict[str, set[str]]:
    return {
        relative: _imports(ROOT / relative)
        for relative in relative_paths
    }


class ArchitectureDependencyContracts(unittest.TestCase):
    def test_legacy_architecture_config_boundary_parity(self):
        field_names = {field.name for field in dataclasses.fields(AppConfig)}
        self.assertNotIn("agent", field_names)
        self.assertNotIn("simulation", field_names)

        forbidden = (
            r"\b(?:config|app_config|typed_config)\.(?:agent|simulation)(?:\b|[.(])",
        )
        for path in PACKAGE_ROOT.glob("*.py"):
            if path.name == "config.py":
                continue
            source = path.read_text(encoding="utf-8")
            for expression in forbidden:
                self.assertIsNone(
                    re.search(expression, source),
                    f"{path.name} 重新解释了 raw AppConfig section",
                )

    def test_legacy_architecture_simulation_write_path_parity(self):
        allowed = {"client.py", "simulator.py"}
        for path in PACKAGE_ROOT.glob("*.py"):
            if path.name in allowed:
                continue
            self.assertNotIn("submit_simulation(", path.read_text(encoding="utf-8"), str(path))

        simulator = (PACKAGE_ROOT / "simulator.py").read_text(encoding="utf-8")
        self.assertIn("self.client.submit_simulation(", simulator)
        for path in (ROOT / "main.py", *((ROOT / "scripts").glob("*.py"))):
            self.assertNotIn(".run_simulation(", path.read_text(encoding="utf-8"), str(path))

    def test_legacy_architecture_non_execution_surfaces_remain_write_free(self):
        for name in (
            "suggestion_workflow.py", "alpha_feed_workflow.py",
            "optimizer_workflow.py", "alpha_color_workflow.py",
            "research_api.py", "doctor.py", "audit.py", "preflight.py",
        ):
            source = (PACKAGE_ROOT / name).read_text(encoding="utf-8")
            self.assertNotIn("submit_simulation(", source, name)
            self.assertNotIn("submit_alpha(", source, name)

        optimizer = (PACKAGE_ROOT / "optimizer_workflow.py").read_text(encoding="utf-8")
        for forbidden in (
            "trajectory.add(", "trajectory.write(", "weekly_cache.refresh(",
            "get_all_user_alphas(", "set_alpha_color(",
        ):
            self.assertNotIn(forbidden, optimizer, forbidden)

    def test_legacy_architecture_research_yield_is_pure(self):
        imports = _imports(PACKAGE_ROOT / "research_yield.py")
        forbidden = {
            "wqb_agent.client", "wqb_agent.state", "wqb_agent.simulator",
            "wqb_agent.agent", "wqb_agent.alpha_feed_workflow",
            "wqb_agent.proposal_execution",
        }
        self.assertTrue(forbidden.isdisjoint(imports))
        source = (PACKAGE_ROOT / "research_yield.py").read_text(encoding="utf-8")
        self.assertNotIn("submit_simulation(", source)
        self.assertNotIn("run_proposals", source)

    def test_legacy_architecture_color_view_cannot_feed_optimizer(self):
        for name in ("optimizer_workflow.py", "research_yield.py"):
            self.assertNotIn(
                "wqb_agent.alpha_colors",
                _imports(PACKAGE_ROOT / name),
                name,
            )

    def test_package_root_exports_only_agent_facing_api(self):
        package = ast.parse(
            (ROOT / "wqb_agent/__init__.py").read_text(encoding="utf-8")
        )
        exports = next(
            node for node in package.body
            if isinstance(node, ast.Assign)
            and any(getattr(target, "id", None) == "__all__" for target in node.targets)
        )
        values = {item.value for item in exports.value.elts}
        self.assertEqual(
            values,
            {
                "Agent", "WQBClient", "ExperimentSpec", "SimulationSpec",
                "simulate", "simulate_batch", "get_pending_executions",
                "resume_execution", "get_alpha", "get_alpha_evidence",
                "compare_alphas", "inspect_state",
                "discover_fields", "get_operator_reference",
                "get_operator_syntax_reference", "run_experiment",
                "get_experiment", "compare_experiments", "search_history",
                "reconcile", "inspect_optimizer_parents",
                "inspect_optimizer_context", "propose_optimization",
                "materialize_targeted_batch",
            },
        )

    def test_workflows_do_not_import_agent_or_transport(self):
        workflow_paths = (
            "wqb_agent/suggestion_workflow.py",
            "wqb_agent/proposal_execution.py",
            "wqb_agent/alpha_feed_workflow.py",
            "wqb_agent/optimizer_workflow.py",
        )
        imports = _package_imports(workflow_paths)
        for path, dependencies in imports.items():
            self.assertNotIn("wqb_agent.agent", dependencies, path)
            self.assertNotIn("wqb_agent.client", dependencies, path)

    def test_domain_modules_do_not_import_transport_or_orchestration(self):
        domain_paths = (
            "wqb_agent/alpha_colors.py",
            "wqb_agent/diversity.py",
            "wqb_agent/metrics.py",
            "wqb_agent/pre_correlation.py",
            "wqb_agent/research_guard.py",
            "wqb_agent/alpha_assembly.py",
            "wqb_agent/proposal_admission.py",
            "wqb_agent/optimization_screening.py",
            "wqb_agent/validation_proposals.py",
            "wqb_agent/factory_blocker.py",
        )
        imports = _package_imports(domain_paths)
        forbidden = {"wqb_agent.agent", "wqb_agent.client", "wqb_agent.simulator"}
        for path, dependencies in imports.items():
            self.assertTrue(
                forbidden.isdisjoint(dependencies),
                f"{path} imports forbidden modules: {sorted(forbidden & dependencies)}",
            )

    def test_canonical_factory_modules_do_not_depend_on_legacy_facades(self):
        paths = (
            "wqb_agent/alpha_assembly.py",
            "wqb_agent/alpha_feasibility.py",
            "wqb_agent/alpha_relationships.py",
            "wqb_agent/alpha_semantics.py",
            "wqb_agent/execution_identity.py",
            "wqb_agent/optimization_screening.py",
            "wqb_agent/validation_proposals.py",
            "wqb_agent/factory_blocker.py",
        )
        imports = _package_imports(paths)
        for path, dependencies in imports.items():
            self.assertNotIn("wqb_agent.candidate", dependencies, path)
            self.assertNotIn("wqb_agent.factory_runner", dependencies, path)
            self.assertNotIn("wqb_agent.runtime_components", dependencies, path)

    def test_alpha_assembly_owns_candidate_to_proposal_projection(self):
        source = (ROOT / "wqb_agent/alpha_assembly.py").read_text(encoding="utf-8")
        self.assertIn("def assemble_factory_realizations(", source)
        self.assertNotIn("def assemble_factory_realizations(\n    self", source)

    def test_execution_helper_does_not_import_transport_or_workflow(self):
        imports = _imports(ROOT / "wqb_agent/execution_identity.py")
        forbidden = {
            "wqb_agent.agent",
            "wqb_agent.client",
            "wqb_agent.proposal_execution",
            "wqb_agent.simulator",
        }
        self.assertTrue(forbidden.isdisjoint(imports))

    def test_state_owners_do_not_import_orchestration(self):
        state_paths = (
            "wqb_agent/artifacts.py",
            "wqb_agent/checkpoints.py",
            "wqb_agent/state.py",
            "wqb_agent/trial_ledger.py",
        )
        imports = _package_imports(state_paths)
        forbidden = {
            "wqb_agent.agent",
            "wqb_agent.proposal_execution",
            "wqb_agent.factory_runner",
        }
        for path, dependencies in imports.items():
            self.assertTrue(
                forbidden.isdisjoint(dependencies),
                f"{path} imports forbidden modules: {sorted(forbidden & dependencies)}",
            )

    def test_research_kernels_do_not_own_clients_or_durable_state(self):
        paths = (
            "wqb_agent/field_metadata.py",
            "wqb_agent/field_catalog.py",
            "wqb_agent/discovery_selection.py",
            "wqb_agent/memory_codec.py",
            "wqb_agent/memory_policy.py",
            "wqb_agent/reflection_evaluation.py",
            "wqb_agent/reflection_learning.py",
            "wqb_agent/optimizer_selection.py",
            "wqb_agent/client_transport.py",
            "wqb_agent/research_catalog.py",
            "wqb_agent/factory_control.py",
            "wqb_agent/factory_probe.py",
            "wqb_agent/execution_plan.py",
            "wqb_agent/proposal_inbox.py",
            "wqb_agent/proposal_schema.py",
            "wqb_agent/proposal_batch.py",
            "wqb_agent/proposal_validation.py",
        )
        imports = _package_imports(paths)
        self.assertNotIn("wqb_agent.proposal_contract", imports["wqb_agent/proposal_batch.py"])
        self.assertNotIn("wqb_agent.proposal_contract", imports["wqb_agent/proposal_validation.py"])
        forbidden = {
            "wqb_agent.agent", "wqb_agent.client", "wqb_agent.memory",
            "wqb_agent.reflection", "wqb_agent.optimizer_workflow",
            "requests",
        }
        for path, dependencies in imports.items():
            self.assertTrue(
                forbidden.isdisjoint(dependencies),
                f"{path} imports forbidden owner/transport modules: "
                f"{sorted(forbidden & dependencies)}",
            )

    def test_phase_five_statistics_and_search_boundaries(self):
        statistics_imports = _imports(ROOT / "wqb_agent/validation_statistics.py")
        self.assertTrue({
            "wqb_agent.state", "wqb_agent.client", "wqb_agent.proposal_execution",
            "wqb_agent.optimizer_workflow",
        }.isdisjoint(statistics_imports))
        search_imports = _imports(ROOT / "wqb_agent/search_evidence.py")
        self.assertTrue({
            "wqb_agent.search_policy", "wqb_agent.search_snapshot", "wqb_agent.agent",
            "wqb_agent.client",
        }.isdisjoint(search_imports))
        snapshot_imports = _imports(ROOT / "wqb_agent/search_snapshot.py")
        self.assertNotIn("wqb_agent.search_policy", snapshot_imports)
        validation = (ROOT / "wqb_agent/validation_report.py").read_text(encoding="utf-8")
        self.assertNotIn("def pbo_proxy(", validation)
        self.assertIn("from .validation_statistics", validation)

    def test_experiment_model_is_persistence_free_and_state_is_compatibility_owner(self):
        experiment_imports = _imports(ROOT / "wqb_agent/experiment.py")
        forbidden = {
            "wqb_agent.state", "wqb_agent.artifacts", "pathlib", "os", "sqlite3",
            "wqb_agent.client", "wqb_agent.simulator", "wqb_agent.workflow",
        }
        self.assertTrue(forbidden.isdisjoint(experiment_imports))
        self.assertIn("wqb_agent.experiment", _imports(ROOT / "wqb_agent/state.py"))

    def test_memory_projection_is_pure_and_memory_keeps_persistence_owner(self):
        projection_imports = _imports(ROOT / "wqb_agent/memory_projection.py")
        self.assertTrue({"wqb_agent.artifacts", "wqb_agent.state", "os", "sqlite3"}.isdisjoint(projection_imports))
        source = (ROOT / "wqb_agent/memory.py").read_text(encoding="utf-8")
        self.assertIn("atomic_write_json_if_changed", source)
        self.assertIn("def save(", source)

    def test_memory_replay_reducer_is_pure_and_io_free(self):
        imports = _imports(ROOT / "wqb_agent/memory_replay.py")
        self.assertTrue({"os", "sqlite3", "pathlib", "wqb_agent.artifacts"}.isdisjoint(imports))
        source = (ROOT / "wqb_agent/memory.py").read_text(encoding="utf-8")
        self.assertIn("from .memory_replay import", source)

    def test_installed_facade_delegates_operator_resource(self):
        source = (ROOT / "wqb_agent/research_api.py").read_text(encoding="utf-8")
        self.assertIn("load_packaged_operator_syntax_reference", source)
        self.assertNotIn("importlib.resources", source)

    def test_factory_probe_is_canonical_and_runner_has_no_probe_wrappers(self):
        runner = (ROOT / "wqb_agent/factory_runner.py").read_text(encoding="utf-8")
        self.assertNotIn("def _selection_probe(", runner)
        self.assertNotIn("def _route_set_digest(", runner)
        self.assertNotIn("def _route_probe_projection(", runner)
        self.assertNotIn("def _begin_route_episode(", runner)
        self.assertNotIn("def _finish_route_episode(", runner)
        self.assertNotIn("def _advance_route_episode(", runner)
        imports = _imports(ROOT / "wqb_agent/factory_probe.py")
        self.assertNotIn("wqb_agent.agent", imports)
        self.assertNotIn("wqb_agent.state", imports)

    def test_factory_runner_uses_bound_checkpoint_owner(self):
        runner = (ROOT / "wqb_agent/factory_runner.py").read_text(encoding="utf-8")
        self.assertNotIn("self.agent.checkpoints", runner)
        self.assertIn("self.checkpoints", runner)

    def test_proposal_execution_writes_lifecycle_audit_through_context_owner(self):
        source = (ROOT / "wqb_agent/proposal_execution.py").read_text(encoding="utf-8")
        self.assertNotIn("record_trial_phase: Callable", source)
        self.assertNotIn("record_candidate_rejection: Callable", source)
        self.assertIn("self._ctx.trial_ledger.record(", source)

    def test_proposal_contract_is_only_a_compatibility_facade(self):
        source = (ROOT / "wqb_agent/proposal_contract.py").read_text(encoding="utf-8")
        self.assertNotIn("def validate_proposal(", source)
        self.assertNotIn("def validate_factory_batch(", source)
        self.assertIn("from . import proposal_batch", source)
        self.assertIn("from . import proposal_validation", source)

    def test_package_import_graph_has_no_cycles(self):
        modules = {
            _module_name(path): path
            for path in PACKAGE_ROOT.rglob("*.py")
        }
        graph = {
            module: {dependency for dependency in _imports(path) if dependency in modules}
            for module, path in modules.items()
        }
        visiting, visited = set(), set()

        def visit(module):
            if module in visiting:
                return [module]
            if module in visited:
                return None
            visiting.add(module)
            for dependency in graph[module]:
                cycle = visit(dependency)
                if cycle:
                    return [module, *cycle]
            visiting.remove(module)
            visited.add(module)
            return None

        cycles = [cycle for module in graph if (cycle := visit(module))]
        self.assertEqual(cycles, [], f"package import cycle(s): {cycles}")


if __name__ == "__main__":
    unittest.main()
