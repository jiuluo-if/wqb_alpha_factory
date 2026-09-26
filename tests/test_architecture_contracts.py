import ast
import inspect
import pathlib
import unittest

from wqb_agent import research_api
from wqb_agent.alpha_factory import AlphaFactory

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
    def test_single_research_skill_declares_the_runtime_contract(self):
        skill_root = ROOT / "skills"
        skill_dirs = sorted(path.parent.name for path in skill_root.glob("*/SKILL.md"))
        self.assertEqual(skill_dirs, ["wqb-research"])

        self.assertEqual(research_api.RESEARCH_CONTRACT_VERSION, "2026-09-26")
        skill_path = skill_root / "wqb-research" / "SKILL.md"
        text = skill_path.read_text(encoding="utf-8")
        self.assertIn(
            f'compatible_research_contract: "{research_api.RESEARCH_CONTRACT_VERSION}"',
            text,
        )
        references = sorted(
            path.name
            for path in (skill_root / "wqb-research" / "references").glob("*.md")
        )
        self.assertLessEqual(len(references), 2)

    def test_mcp_reference_documents_prod_correlation_as_a_finalist_tool(self):
        text = (ROOT / "docs" / "MCP_READ_ONLY.md").read_text(encoding="utf-8")
        self.assertIn("Research Mode 暴露 10 个工具", text)
        self.assertIn("get_alpha_prod_correlation", text)
        self.assertIn("FINALIST_ONLY", text)
        self.assertIn("不隐式", text)

    def test_result_guide_uses_agent_owned_trial_context(self):
        text = (
            ROOT / "skills" / "wqb-research" / "references" / "result-interpretation.md"
        ).read_text(encoding="utf-8")
        self.assertNotIn("variant_family", text)
        self.assertNotIn("observed_execution_count", text)
        for name in ("family_label", "related_trial_count_lower_bound", "count_scope"):
            self.assertIn(name, text)
        self.assertIn("Agent-owned", text)

    def test_skill_workset_has_only_the_requested_trial_context_line(self):
        text = (ROOT / "skills" / "wqb-research" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("试验上下文", text)
        for name in ("family_label", "related_trial_count_lower_bound", "count_scope"):
            self.assertIn(name, text)

    def test_research_status_reports_the_contract_version(self):
        status = research_api.research_status()
        self.assertEqual(
            status["research_contract_version"],
            research_api.RESEARCH_CONTRACT_VERSION,
        )

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
        rows = research_api.research_tool_manifest(profile="full")
        names = [row["name"] for row in rows]

        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(set(names) - {"alpha_submission"}, expected)
        for name in names:
            if name == "alpha_submission":
                continue
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
            row["name"]: row
            for row in research_api.research_tool_manifest(profile="full")
        }
        for name in (
            "generate_probes", "validate_simulation_settings",
            "build_simulation_spec",
        ):
            self.assertEqual(rows[name]["mode"], "READ_ONLY", name)
        for name in (
            "build_simulation_variant", "group_alphas", "preview_alpha_colors",
        ):
            self.assertEqual(rows[name]["mode"], "PURE", name)
        for name in ("refresh_remote_alphas", "purge_remote_cache"):
            self.assertEqual(rows[name]["mode"], "LOCAL_CACHE_WRITE", name)
            self.assertTrue(rows[name].get("local_write"), name)
        self.assertEqual(rows["remote_cache_status"]["mode"], "READ_ONLY")
        self.assertFalse(rows["remote_cache_status"].get("local_write", False))

    def test_manifest_preserves_write_boundaries_and_has_one_public_name_per_operation(self):
        rows = {
            row["name"]: row
            for row in research_api.research_tool_manifest(profile="full")
        }
        import wqb_agent

        self.assertIn("find_duplicate_alphas", rows)
        self.assertNotIn("find_alpha_duplicates", rows)
        for name in (
            "simulate", "simulate_single", "simulate_batch",
            "simulate_multi_batch",
        ):
            self.assertEqual(rows[name]["mode"], "SIMULATION_WRITE", name)
            self.assertTrue(rows[name].get("remote_write"), name)
        for name in (
            "simulate_single_batch", "find_alpha_duplicates",
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
        manifest = {
            item["name"] for item in research_api.research_tool_manifest(profile="full")
        }
        self.assertTrue(canonical <= manifest)

    def test_legacy_runtime_and_optimizer_modules_are_absent(self):
        retired = (
            "agent.py", "runtime_components.py", "runtime_composition.py",
            "runtime_policy.py", "optimizer_workflow.py", "optimizer_selection.py",
            "heartbeat.py",
        )
        for name in retired:
            self.assertFalse((PACKAGE / name).exists(), name)

    def test_agent_field_discovery_is_raw_and_ranked_selector_is_retired(self):
        for name in ("discovery.py", "discovery_selection.py", "field_catalog.py"):
            self.assertFalse((PACKAGE / name).exists(), name)
        self.assertFalse(hasattr(research_api, "discover_fields"))
        for name in ("list_datasets", "list_datafields", "list_all_datafields"):
            self.assertTrue(callable(getattr(research_api, name)), name)

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
