"""Executable dependency-direction contracts for the modular architecture."""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

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
                "Agent", "WQBClient", "ExperimentSpec", "inspect_state",
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
        )
        imports = _package_imports(paths)
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
