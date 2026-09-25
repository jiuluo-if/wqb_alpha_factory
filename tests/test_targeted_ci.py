from __future__ import annotations

import unittest
from pathlib import Path

from scripts.run_targeted_tests import (
    TargetSelectionError,
    mapping_inventory,
    select_tests,
    validate_mapping_integrity,
)


class TargetedCiSelectionTests(unittest.TestCase):
    def test_selects_only_directly_related_tests(self):
        selected = select_tests(["wqb_agent/locking.py", "docs/TESTING.md"])

        self.assertIn("tests/test_locking.py", selected)
        self.assertNotIn("tests/test_cli.py", selected)

    def test_changed_test_file_is_selected_without_discovery(self):
        self.assertEqual(
            select_tests(["tests/test_locking.py"]),
            ("tests/test_locking.py",),
        )

    def test_unmapped_code_fails_closed(self):
        with self.assertRaisesRegex(TargetSelectionError, "full-suite fallback"):
            select_tests(["wqb_agent/new_boundary.py"])

    def test_docs_only_change_has_no_unit_test_selection(self):
        self.assertEqual(select_tests(["docs/TESTING.md", "AGENTS.md"]), ())

    def test_mapping_graph_is_complete_and_stale_free(self):
        inventory = validate_mapping_integrity()

        self.assertEqual(inventory["unmapped_production_files"], set())
        self.assertEqual(inventory["mapped_missing_tests"], set())
        self.assertEqual(inventory["stale_mapping_keys"], set())
        self.assertEqual(inventory["duplicate_keys"], False)

    def test_mapping_inventory_reports_every_active_test_route(self):
        inventory = mapping_inventory()

        self.assertNotIn("tests/test_targeted_ci.py", inventory["unreachable_tests"])
        self.assertNotIn("tests/test_dependency_constraints.py", inventory["unreachable_tests"])

    def test_push_ci_is_limited_to_main(self):
        workflow = Path(__file__).resolve().parents[1] / ".github/workflows/ci.yml"
        contents = workflow.read_text(encoding="utf-8")
        push = contents.split("  push:\n", 1)[1].split("  pull_request:", 1)[0]

        self.assertIn('branches: ["main"]', push)
        self.assertNotIn("v0.2", push)
