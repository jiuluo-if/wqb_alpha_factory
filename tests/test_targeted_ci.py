from __future__ import annotations

import unittest

from scripts.run_targeted_tests import TargetSelectionError, select_tests


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
