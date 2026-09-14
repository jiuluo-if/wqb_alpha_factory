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
        )
        imports = _package_imports(domain_paths)
        forbidden = {"wqb_agent.agent", "wqb_agent.client", "wqb_agent.simulator"}
        for path, dependencies in imports.items():
            self.assertTrue(
                forbidden.isdisjoint(dependencies),
                f"{path} imports forbidden modules: {sorted(forbidden & dependencies)}",
            )

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


if __name__ == "__main__":
    unittest.main()
