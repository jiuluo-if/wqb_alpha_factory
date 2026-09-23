import ast
import inspect
import pathlib
import unittest

from wqb_agent import research_api
from wqb_agent.alpha_factory import AlphaFactory
from wqb_agent.discovery import FieldDiscovery

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
    def test_public_api_has_no_retired_agent_parameter(self):
        public = [
            value for name, value in vars(research_api).items()
            if inspect.isfunction(value) and not name.startswith("_")
        ]
        for function in public:
            self.assertNotIn("agent", inspect.signature(function).parameters, function.__name__)

    def test_factory_exposes_only_spec_generation(self):
        factory = AlphaFactory()
        for name in ("assemble_proposals", "generate_factory_batch", "assess_feasibility"):
            self.assertFalse(hasattr(factory, name), name)

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
        import wqb_agent
        for name in (
            "get_alpha_metrics", "get_alpha_aggregates", "get_alpha_pnl",
            "get_alpha_self_correlation", "get_alpha_recordsets",
        ):
            self.assertIn(name, names)
            self.assertTrue(hasattr(wqb_agent, name), name)

    def test_research_surface_and_manifest_are_exactly_aligned(self):
        expected = set(research_api.__all__) - {"SimulationSpec", "research_tool_manifest"}
        rows = research_api.research_tool_manifest()
        names = [row["name"] for row in rows]

        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(set(names), expected)
        for name in names:
            self.assertTrue(callable(getattr(research_api, name)), name)

    def test_package_root_matches_research_surface_and_lazy_exports(self):
        import wqb_agent

        self.assertEqual(
            set(wqb_agent.__all__) - {"WQBClient"},
            set(research_api.__all__),
        )
        for name in wqb_agent.__all__:
            self.assertTrue(hasattr(wqb_agent, name), name)

    def test_manifest_modes_reflect_possible_io(self):
        rows = {
            row["name"]: row for row in research_api.research_tool_manifest()
        }
        for name in (
            "generate_probes", "validate_simulation_settings",
            "build_simulation_spec", "group_alphas", "preview_alpha_colors",
        ):
            self.assertEqual(rows[name]["mode"], "READ_ONLY", name)
        for name in ("refresh_remote_alphas", "purge_remote_cache"):
            self.assertEqual(rows[name]["mode"], "LOCAL_CACHE_WRITE", name)
            self.assertTrue(rows[name].get("local_write"), name)
        self.assertEqual(rows["remote_cache_status"]["mode"], "READ_ONLY")
        self.assertFalse(rows["remote_cache_status"].get("local_write", False))

    def test_manifest_preserves_write_boundaries_and_has_one_public_name_per_operation(self):
        rows = {
            row["name"]: row for row in research_api.research_tool_manifest()
        }
        import wqb_agent

        self.assertIn("find_duplicate_alphas", rows)
        self.assertNotIn("find_alpha_duplicates", rows)
        for name in ("simulate", "simulate_batch", "simulate_multi_batch"):
            self.assertEqual(rows[name]["mode"], "SIMULATION_WRITE", name)
            self.assertTrue(rows[name].get("remote_write"), name)
        for name in (
            "simulate_single", "simulate_single_batch", "find_alpha_duplicates",
        ):
            self.assertNotIn(name, rows)
            self.assertFalse(hasattr(research_api, name), name)
            self.assertFalse(hasattr(wqb_agent, name), name)

    def test_canonical_remote_read_surface_stays_available_at_public_facades(self):
        canonical = {
            "get_live_preflight", "get_simulation_modes", "get_alpha_evidence",
            "get_alpha_recordsets", "get_activity_diversity",
        }
        import wqb_agent

        for name in canonical:
            self.assertTrue(hasattr(research_api, name), name)
            self.assertTrue(hasattr(wqb_agent, name), name)
        manifest = {item["name"] for item in research_api.research_tool_manifest()}
        self.assertTrue(canonical <= manifest)

    def test_legacy_runtime_and_optimizer_modules_are_absent(self):
        retired = (
            "agent.py", "runtime_components.py", "runtime_composition.py",
            "runtime_policy.py", "optimizer_workflow.py", "optimizer_selection.py",
            "heartbeat.py",
        )
        for name in retired:
            self.assertFalse((PACKAGE / name).exists(), name)

    def test_field_discovery_has_no_retired_heartbeat_injection(self):
        self.assertNotIn("heartbeat", inspect.signature(FieldDiscovery.__init__).parameters)

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
