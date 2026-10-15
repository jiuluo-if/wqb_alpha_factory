from __future__ import annotations

import ast
import re
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import run_targeted_tests as runner
from scripts.run_targeted_tests import (
    TargetSelectionError,
    _unittest_test_files,
    mapping_inventory,
    select_tests,
    validate_mapping_integrity,
)

REQUIRED_CRITICAL_CONTRACTS = frozenset({
    "cache.refresh_due_rejects_invalid_time",
    "cache.read_preserves_bytes_and_freshness",
    "cache.contract_mismatch_unknown_without_rewrite",
    "cache.retention_mismatch_unknown",
    "cache.read_paths_do_not_cleanup_temp",
    "cache.refresh_owns_stale_temp_cleanup",
    "cache.retained_rows_deduped_without_quota_pruning",
    "semantics.relationship_labels_price_liquidity",
    "semantics.frequency_gap_incompatible",
    "ci.direct_dependencies_pinned",
    "ci.install_constraints_no_coverage_gate",
    "template.private_catalog_missing_fails_closed",
    "template.relative_private_path_rejected",
    "template.private_catalog_v2_explicit",
    "template.multi_field_relationship_contract_admitted",
    "template.public_catalog_synthetic_horizon_lattice",
    "template.operator_inventory_no_random_forcing",
    "privacy.private_assets_not_tracked",
    "repository.active_alpha_submitted_date",
    "repository.metadata_not_quota_pruned",
    "repository.refresh_shard_budget",
    "repository.configured_rolling_window",
    "repository.creation_and_submission_dates_distinct",
    "repository.submitted_without_created_date_visible",
    "repository.narrow_local_window_no_remote_io",
    "repository.invalid_retention_fails_closed",
    "repository.public_api_no_agent_state",
    "repository.live_vs_cache_source",
    "repository.explicit_purge_owner",
    "repository.status_read_no_temp_cleanup",
    "remote_color.structural_similarity_family_assignment",
    "remote_color.numeric_variants_family_color_strict_keys",
    "remote_color.unassigned_no_automatic_color",
    "remote_color.safety_constants_distinct",
    "remote_color.distinct_explicit_families_distinct_colors",
    "remote_color.quality_text_only",
    "remote_color.preview_immutable_fields",
    "remote_color.unassigned_other_family_no_hash",
    "remote_color.max_five_family_limit",
    "remote_color.assignment_outside_snapshot_rejected",
    "remote_color.mixed_existing_colors_reported",
    "remote_color.sync_exact_plan_no_quality_reclassification",
    "remote_color.stale_plan_no_patch",
    "remote_color.noop_preserve_overwrite",
    "remote_color.readback_mismatch_fails_closed",
    "remote_color.patch_color_only",
    "remote_color.partial_failure_not_success",
    "ci.python_version_and_full_discovery_gate",
    "discovery.datasets_malformed_envelope_rejected",
    "discovery.datafields_malformed_envelope_rejected",
    "discovery.dataset_metadata_conflicts_keep_region_source",
    "discovery.field_type_conflicts_keep_region_source",
    "discovery.field_type_unknown_not_verified",
    "discovery.field_type_conflict_not_verified",
    "discovery.field_lookup_incomplete_not_not_found",
    "discovery.duplicate_field_page_not_complete",
    "discovery.duplicate_field_in_page_rejected",
    "alpha_history.boolean_count_rejected",
    "alpha_history.count_change_fails_closed",
    "factory.no_template_identity_dispatch",
    "semantic.declared_vs_legacy_status",
    "semantic.evaluator_contracts_distinct",
    "semantic.family_id_renames_same_admission_score",
    "semantic.undeclared_contract_no_name_inference",
    "semantic.vector_decision_structural_family_independent",
    "semantic.factory_vector_operator_coverage",
    "semantic.matrix_field_rejected_by_vector_contract",
    "semantic.private_catalog_synthetic_bypass_rejected",
    "smoke.read_only_methods_only",
    "smoke.unavailable_read_capability_degrades",
    "smoke.raw_config_rejected",
    "simulation.corrupt_guard_blocks_post",
    "simulation.multi_parent_state_is_atomic",
    "simulation.unresolved_parent_holds_capacity",
    "simulation.ra_field_type_unverified_blocks",
})


def critical_contract(*contract_ids):
    def decorate(test_method):
        test_method.__critical_contract_ids__ = frozenset(contract_ids)
        return test_method

    return decorate


def _discovered_critical_contracts(tests_root):
    found = set()
    for relative_path in _unittest_test_files(tests_root):
        path = tests_root / relative_path
        tree = ast.parse(path.read_text(encoding="utf-8"))
        effective_classes = {}
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                effective_classes[node.name] = node
        for test_class in (
            effective_classes.values()
        ):
            is_test_case = any(
                (
                    isinstance(base, ast.Name)
                    and base.id in {"TestCase", "IsolatedAsyncioTestCase"}
                )
                or (
                    isinstance(base, ast.Attribute)
                    and base.attr in {"TestCase", "IsolatedAsyncioTestCase"}
                )
                for base in test_class.bases
            )
            if not is_test_case:
                continue
            effective_methods = {}
            for node in test_class.body:
                if (
                    isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and node.name.startswith("test_")
                ):
                    # Python keeps only the final class binding when a method
                    # name is repeated; a marker on an overwritten definition
                    # does not make the effective test discoverable.
                    effective_methods[node.name] = node
            for node in effective_methods.values():
                for decorator in node.decorator_list:
                    if (
                        not isinstance(decorator, ast.Call)
                        or not isinstance(decorator.func, ast.Name)
                        or decorator.func.id != "critical_contract"
                    ):
                        continue
                    found.update(
                        argument.value
                        for argument in decorator.args
                        if isinstance(argument, ast.Constant)
                        and isinstance(argument.value, str)
                    )
    return found


class CriticalContractInventoryTests(unittest.TestCase):
    def test_required_critical_contracts_have_discovered_tests(self):
        tests_root = Path(__file__).resolve().parent

        self.assertEqual(
            _discovered_critical_contracts(tests_root),
            REQUIRED_CRITICAL_CONTRACTS,
        )

    def test_critical_contract_inventory_ignores_non_test_functions(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test_not_discovered.py"
            path.write_text(
                "@critical_contract('ghost.contract')\n"
                "def test_helper_not_in_test_case():\n"
                "    pass\n",
                encoding="utf-8",
            )

            discovered = _discovered_critical_contracts(Path(tmp))

        self.assertEqual(discovered, set())

    def test_shadowed_test_definition_does_not_count_critical_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test_shadowed.py"
            path.write_text(
                "import unittest\n"
                "class TestShadowed(unittest.TestCase):\n"
                "    @critical_contract('ghost.contract')\n"
                "    def test_behavior(self):\n"
                "        pass\n"
                "    def test_behavior(self):\n"
                "        pass\n",
                encoding="utf-8",
            )

            discovered = _discovered_critical_contracts(Path(tmp))

        self.assertEqual(discovered, set())

    def test_shadowed_test_class_does_not_count_critical_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test_shadowed_class.py"
            path.write_text(
                "import unittest\n"
                "class TestShadowed(unittest.TestCase):\n"
                "    @critical_contract('ghost.contract')\n"
                "    def test_behavior(self):\n"
                "        pass\n"
                "class TestShadowed(unittest.TestCase):\n"
                "    def test_other_behavior(self):\n"
                "        pass\n",
                encoding="utf-8",
            )

            discovered = _discovered_critical_contracts(Path(tmp))

        self.assertEqual(discovered, set())


class TargetedCiSelectionTests(unittest.TestCase):
    def test_selects_only_directly_related_tests(self):
        selected = select_tests(["wqb_agent/locking.py", "docs/TESTING.md"])

        self.assertIn("tests/test_locking.py", selected)
        self.assertIn("tests/test_architecture_contracts.py", selected)
        self.assertIn("tests/test_targeted_ci.py", selected)
        self.assertNotIn("tests/test_cli.py", selected)

    def test_gateway_changes_select_parent_capacity_regressions(self):
        selected = select_tests(["wqb_agent/simulation_gateway.py"])

        self.assertIn("tests/test_multi_parent_capacity.py", selected)
        self.assertIn("tests/test_architecture_contracts.py", selected)
        self.assertIn("tests/test_targeted_ci.py", selected)

    def test_ci_policy_and_constraint_changes_select_contract_tests(self):
        selected = select_tests([
            ".github/workflows/ci.yml",
            "constraints/ci-py311.txt",
            "AGENTS.md",
            "docs/TESTING.md",
        ])

        self.assertIn("tests/test_targeted_ci.py", selected)
        self.assertIn("tests/test_architecture_contracts.py", selected)

    def test_ci_workflow_path_selects_contract_tests(self):
        selected = select_tests([".github/workflows/ci.yml"])

        self.assertEqual(
            selected,
            (
                "tests/test_architecture_contracts.py",
                "tests/test_targeted_ci.py",
            ),
        )

    def test_changed_test_file_is_selected_without_discovery(self):
        self.assertEqual(
            select_tests(["tests/test_locking.py"]),
            ("tests/test_locking.py",),
        )

    def test_unmapped_code_fails_closed(self):
        with self.assertRaisesRegex(TargetSelectionError, "full-suite fallback"):
            select_tests(["wqb_agent/new_boundary.py"])

    def test_mapping_graph_is_complete_and_stale_free(self):
        inventory = validate_mapping_integrity()

        self.assertEqual(inventory["unmapped_production_files"], set())
        self.assertEqual(inventory["mapped_missing_tests"], set())
        self.assertEqual(inventory["undiscoverable_tests"], set())
        self.assertEqual(inventory["unreachable_tests"], set())
        self.assertEqual(inventory["stale_mapping_keys"], set())
        self.assertEqual(inventory["duplicate_keys"], False)

    def test_active_production_inventory_includes_nested_modules(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            nested_script = root / "scripts" / "checks" / "new_check.py"
            nested_agent = root / "wqb_agent" / "alpha_templates" / "new_template.py"
            package_init = root / "wqb_agent" / "alpha_templates" / "__init__.py"
            nested_script.parent.mkdir(parents=True)
            nested_agent.parent.mkdir(parents=True)
            nested_script.write_text("VALUE = 1\n", encoding="utf-8")
            nested_agent.write_text("VALUE = 1\n", encoding="utf-8")
            package_init.write_text("VALUE = 1\n", encoding="utf-8")

            with patch.object(runner, "ROOT", root):
                production = runner._active_production_files()

        self.assertIn("scripts/checks/new_check.py", production)
        self.assertIn("wqb_agent/alpha_templates/new_template.py", production)
        self.assertIn("wqb_agent/alpha_templates/__init__.py", production)

    def test_unmapped_nested_production_modules_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            nested_script = root / "scripts" / "checks" / "new_check.py"
            nested_agent = root / "wqb_agent" / "alpha_templates" / "new_template.py"
            test_file = root / "tests" / "test_support.py"
            nested_script.parent.mkdir(parents=True)
            nested_agent.parent.mkdir(parents=True)
            test_file.parent.mkdir(parents=True)
            nested_script.write_text("VALUE = 1\n", encoding="utf-8")
            nested_agent.write_text("VALUE = 1\n", encoding="utf-8")
            test_file.write_text("import unittest\n", encoding="utf-8")

            with (
                patch.object(runner, "ROOT", root),
                patch.object(
                    runner,
                    "DIRECT_TESTS",
                    {"scripts/checks/new_check.py": ("tests/test_support.py",)},
                ),
                patch.object(runner, "COMMON_CONTRACT_TESTS", ()),
                patch.object(runner, "FRONTEND_CONFIG_TESTS", ()),
            ):
                with self.assertRaisesRegex(
                    TargetSelectionError,
                    "wqb_agent/alpha_templates/new_template.py",
                ):
                    validate_mapping_integrity()

    def test_nested_unittest_package_is_present_in_route_inventory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            production = root / "wqb_agent" / "nested.py"
            package = root / "tests" / "feature"
            nested_test = package / "test_nested.py"
            package.mkdir(parents=True)
            (root / "wqb_agent").mkdir(exist_ok=True)
            production.write_text("VALUE = 1\n", encoding="utf-8")
            (package / "__init__.py").write_text("", encoding="utf-8")
            nested_test.write_text("import unittest\n", encoding="utf-8")

            with (
                patch.object(runner, "ROOT", root),
                patch.object(
                    runner,
                    "DIRECT_TESTS",
                    {"wqb_agent/nested.py": ("tests/feature/test_nested.py",)},
                ),
                patch.object(runner, "COMMON_CONTRACT_TESTS", ()),
                patch.object(runner, "FRONTEND_CONFIG_TESTS", ()),
            ):
                inventory = runner.mapping_inventory()

        self.assertEqual(inventory["mapped_missing_tests"], set())
        self.assertEqual(inventory["unreachable_tests"], set())

    def test_nested_test_without_package_init_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hidden_test = root / "tests" / "feature" / "test_hidden.py"
            hidden_test.parent.mkdir(parents=True)
            hidden_test.write_text("import unittest\n", encoding="utf-8")

            with (
                patch.object(runner, "ROOT", root),
                patch.object(runner, "DIRECT_TESTS", {}),
                patch.object(runner, "COMMON_CONTRACT_TESTS", ()),
                patch.object(runner, "FRONTEND_CONFIG_TESTS", ()),
            ):
                with self.assertRaisesRegex(TargetSelectionError, "unittest discover"):
                    validate_mapping_integrity()

    def test_push_ci_is_limited_to_main(self):
        workflow = Path(__file__).resolve().parents[1] / ".github/workflows/ci.yml"
        contents = workflow.read_text(encoding="utf-8")
        push = contents.split("  push:\n", 1)[1].split("  pull_request:", 1)[0]

        self.assertIn('branches: ["main"]', push)
        self.assertNotIn("v0.2", push)


class DependencyConstraintTests(unittest.TestCase):
    root = Path(__file__).resolve().parents[1]

    @critical_contract("ci.direct_dependencies_pinned")
    def test_direct_runtime_and_tool_dependencies_are_pinned_for_ci(self):
        with (self.root / "pyproject.toml").open("rb") as stream:
            project = tomllib.load(stream)["project"]
        direct = list(project.get("dependencies", []))
        for values in project.get("optional-dependencies", {}).values():
            direct.extend(values)
        constraints = {
            match.group(1).lower(): match.group(2)
            for line in (self.root / "constraints" / "ci-py311.txt").read_text(encoding="utf-8").splitlines()
            if (match := re.match(r"^([A-Za-z0-9_.-]+)==([^#\s]+)", line.strip()))
        }
        missing = []
        for requirement in direct:
            name = re.split(r"[<>=!~;\[]", requirement, maxsplit=1)[0].strip().lower()
            if name not in constraints:
                missing.append(name)
        self.assertEqual(missing, [])

    @critical_contract("ci.install_constraints_no_coverage_gate")
    def test_ci_installs_from_constraints_and_does_not_run_coverage(self):
        workflow = (self.root / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8",
        )
        testing = (self.root / "docs" / "TESTING.md").read_text(encoding="utf-8")
        with (self.root / "pyproject.toml").open("rb") as stream:
            project = tomllib.load(stream)

        self.assertIn(
            "python -m pip install -c constraints/ci-py311.txt",
            workflow,
        )
        self.assertNotRegex(
            workflow,
            r"(?im)^\s*(?:python\s+-m\s+)?coverage\s+(?:run|report|xml)\b",
        )
        self.assertNotIn("fail_under", workflow)
        final_gate = testing.split("## CI 最终门", 1)[1].split("## 契约重点", 1)[0]
        self.assertNotRegex(final_gate, r"(?im)^\s*.*\bcoverage\b")
        self.assertIsNotNone(
            project["tool"]["coverage"]["report"].get("fail_under"),
        )

    @critical_contract("ci.python_version_and_full_discovery_gate")
    def test_ci_python_version_and_final_test_gates_match_project_contract(self):
        workflow = (self.root / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8",
        )
        with (self.root / "pyproject.toml").open("rb") as stream:
            project = tomllib.load(stream)["project"]

        self.assertEqual(project["requires-python"], ">=3.11")
        self.assertIn('python-version: "3.11"', workflow)
        self.assertIn(
            "python scripts/run_targeted_tests.py --base-sha",
            workflow,
        )
        self.assertIn("python -m unittest discover -s tests", workflow)
        self.assertIn(
            "python main.py --state-dir tests/fixtures diagnostics doctor --offline",
            workflow,
        )
        self.assertIn(
            "python main.py --state-dir tests/fixtures diagnostics audit --offline",
            workflow,
        )
        self.assertIn("python scripts/check_repo_privacy.py", workflow)
        selected = select_tests([".github/workflows/ci.yml"])
        self.assertIn("tests/test_architecture_contracts.py", selected)

if __name__ == "__main__":
    unittest.main()
