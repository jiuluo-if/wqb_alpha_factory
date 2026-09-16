import ast
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "wqb_agent"


def _imports(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    result = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            result.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            result.add(f"{node.level}:{node.module}")
    return result


class RemoteFirstArchitectureTests(unittest.TestCase):
    def test_public_package_exports_remote_first_tools_only(self):
        tree = ast.parse((PACKAGE / "__init__.py").read_text(encoding="utf-8"))
        exports = next(
            node.value for node in tree.body
            if isinstance(node, ast.Assign)
            and any(getattr(target, "id", None) == "__all__" for target in node.targets)
        )
        names = {item.value for item in exports.elts}
        self.assertNotIn("Agent", names)
        self.assertNotIn("run_experiment", names)
        self.assertIn("simulate", names)
        self.assertIn("get_alpha_evidence", names)
        self.assertIn("sync_alpha_colors", names)

    def test_legacy_runtime_and_optimizer_modules_are_absent(self):
        retired = (
            "agent.py", "runtime_components.py", "runtime_composition.py",
            "runtime_policy.py", "optimizer_workflow.py", "optimizer_selection.py",
        )
        for name in retired:
            self.assertFalse((PACKAGE / name).exists(), name)

    def test_public_simulation_path_is_gateway_to_simulator_to_client(self):
        gateway_imports = _imports(PACKAGE / "simulation_gateway.py")
        simulator_imports = _imports(PACKAGE / "simulator.py")
        self.assertIn("1:simulator", gateway_imports)
        self.assertIn("1:client", simulator_imports)

        callers = []
        for path in PACKAGE.glob("*.py"):
            if path.name in {"simulation_gateway.py", "simulator.py", "client.py"}:
                continue
            if "submit_simulation(" in path.read_text(encoding="utf-8"):
                callers.append(path.name)
        self.assertEqual(callers, [])

    def test_guard_and_repository_do_not_import_legacy_state(self):
        for name in ("simulation_gateway.py", "remote_alpha_repository.py", "remote_quota.py"):
            imports = _imports(PACKAGE / name)
            self.assertNotIn("1:state", imports, name)
            self.assertNotIn("1:trial_ledger", imports, name)
            self.assertNotIn("1:memory", imports, name)
            self.assertNotIn("1:optimizer_workflow", imports, name)

    def test_retired_trajectory_recovery_is_not_a_public_entry(self):
        for path in (ROOT / "main.py", PACKAGE / "cli.py", PACKAGE / "research_api.py"):
            source = path.read_text(encoding="utf-8")
            self.assertNotIn("settle-stale-trajectory", source, str(path))
            self.assertNotIn("settle_stale_trajectory", source, str(path))


if __name__ == "__main__":
    unittest.main()
