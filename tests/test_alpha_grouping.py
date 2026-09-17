import unittest

from wqb_agent import research_api
from wqb_agent.alpha_grouping import group_remote_evidence, structural_fingerprint


class TestAlphaGrouping(unittest.TestCase):
    def test_grouping_uses_remote_expression_and_settings(self):
        rows = [
            {"alpha_id": "a", "alpha": {"regular": "rank(close)"}, "settings": {"delay": 1}},
            {"alpha_id": "b", "alpha": {"regular": "rank(close)"}, "settings": {"delay": 1}},
            {"alpha_id": "c", "alpha": {"regular": "rank(volume)"}, "settings": {"delay": 1}},
        ]

        groups = group_remote_evidence(rows)

        self.assertEqual(sorted(len(group) for group in groups["execution"].values()), [1, 2])
        self.assertEqual(structural_fingerprint("rank(close)"),
                         structural_fingerprint("rank(volume)"))

    def test_invalid_remote_rows_are_ignored(self):
        self.assertEqual(group_remote_evidence([None, {}, {"alpha_id": "x"}]),
                         {"execution": {}, "structural": {}, "quality": {}})

    def test_public_grouping_is_read_only_projection(self):
        result = research_api.group_alphas(rows=[
            {"alpha_id": "a", "alpha": {"regular": "rank(close)"}, "settings": {}}
        ])
        self.assertEqual(len(result["execution"]), 1)

    def test_public_color_preview_requires_explicit_structural_assignment(self):
        key = structural_fingerprint("rank(close)")
        plan = research_api.preview_alpha_colors(
            rows=[{"alpha_id": "a", "alpha": {"regular": "rank(close)"}}],
            assignments={key: "BLUE"},
        )
        self.assertEqual(plan[0]["desired_color"], "BLUE")
        with self.assertRaisesRegex(ValueError, "EXACT_COLOR_PLAN_REQUIRED"):
            research_api.sync_alpha_colors()


if __name__ == "__main__":
    unittest.main()
