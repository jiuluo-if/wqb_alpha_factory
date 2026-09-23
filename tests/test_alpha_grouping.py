import unittest

from wqb_agent import research_api
from wqb_agent.alpha_grouping import (
    find_remote_similar,
    group_remote_evidence,
    structural_fingerprint,
    variant_family_fingerprint,
)
from wqb_agent.expression import submission_fingerprint


class TestAlphaGrouping(unittest.TestCase):
    def test_identity_layers_do_not_mix(self):
        first = "ts_mean(close, 22)"
        field_variant = "ts_mean(volume, 22)"
        numeric_variant = "ts_mean(close, 66)"
        operator_variant = "ts_delta(close, 22)"

        self.assertNotEqual(structural_fingerprint(first), structural_fingerprint(numeric_variant))
        self.assertEqual(structural_fingerprint(first), structural_fingerprint(field_variant))
        self.assertEqual(variant_family_fingerprint(first), variant_family_fingerprint(field_variant))
        self.assertEqual(variant_family_fingerprint(first), variant_family_fingerprint(numeric_variant))
        self.assertNotEqual(variant_family_fingerprint(first), variant_family_fingerprint(operator_variant))
        self.assertNotEqual(
            submission_fingerprint(first, {"delay": 1}),
            submission_fingerprint(first, {"delay": 2}),
        )

    def test_formal_duplicate_lookup_name_is_the_only_public_spelling(self):
        rows = [
            {"alpha_id": "target", "alpha": {"regular": "rank(close)"}, "settings": {"delay": 1}},
            {"alpha_id": "match", "alpha": {"regular": "rank(close)"}, "settings": {"delay": 1}},
        ]

        result = research_api.find_duplicate_alphas("target", rows=rows)

        self.assertEqual(result["kind"], "EXACT")
        self.assertEqual([item["alpha_id"] for item in result["matches"]], ["match", "target"])
        self.assertFalse(hasattr(research_api, "find_alpha_duplicates"))

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

    def test_grouping_exposes_member_and_observed_execution_counts(self):
        rows = [
            {"alpha_id": "a", "alpha": {"regular": "ts_mean(close, 22)"}, "settings": {}},
            {"alpha_id": "b", "alpha": {"regular": "ts_mean(close, 66)"}, "settings": {}},
            {"alpha_id": "c", "alpha": {"regular": "ts_mean(close, 22)"}, "settings": {}},
        ]

        groups = group_remote_evidence(rows)

        self.assertEqual(len(groups["structural"]), 2)
        self.assertEqual(len(groups["variant_family"]), 1)
        family = next(iter(groups["variant_family"].values()))
        self.assertEqual(len(family), 3)
        self.assertEqual({item["family_member_count"] for item in family}, {3})
        self.assertEqual({item["observed_execution_count"] for item in family}, {2})
        self.assertNotIn("observed_variant_count", family[0])

    def test_settings_variation_stays_in_family_but_not_execution_group(self):
        rows = [
            {"alpha_id": "a", "alpha": {"regular": "rank(close)"}, "settings": {"decay": 4}},
            {"alpha_id": "b", "alpha": {"regular": "rank(close)"}, "settings": {"decay": 6}},
        ]

        groups = group_remote_evidence(rows)

        self.assertEqual(len(groups["execution"]), 2)
        self.assertEqual(len(groups["variant_family"]), 1)
        family = next(iter(groups["variant_family"].values()))
        self.assertEqual({item["family_member_count"] for item in family}, {2})
        self.assertEqual({item["observed_execution_count"] for item in family}, {2})

    def test_numeric_constants_and_thresholds_remain_separate_families(self):
        rows = [
            {"alpha_id": "epsilon-a", "alpha": {"regular": "divide(close, 0.001)"}, "settings": {}},
            {"alpha_id": "epsilon-b", "alpha": {"regular": "divide(close, 0.01)"}, "settings": {}},
            {"alpha_id": "threshold-a", "alpha": {"regular": "trade_when(close, 0.2, volume)"}, "settings": {}},
            {"alpha_id": "threshold-b", "alpha": {"regular": "trade_when(close, 0.8, volume)"}, "settings": {}},
        ]

        groups = group_remote_evidence(rows)

        self.assertEqual(len(groups["variant_family"]), 4)

    def test_similarity_prioritizes_exact_then_strict_then_variant_family(self):
        rows = [
            {"alpha_id": "target", "alpha": {"regular": "ts_mean(close, 22)"}, "settings": {"delay": 1}},
            {"alpha_id": "exact", "alpha": {"regular": "ts_mean(close, 22)"}, "settings": {"delay": 1}},
            {"alpha_id": "strict", "alpha": {"regular": "ts_mean(volume, 22)"}, "settings": {"delay": 1}},
            {"alpha_id": "family", "alpha": {"regular": "ts_mean(close, 66)"}, "settings": {"delay": 1}},
        ]

        result = find_remote_similar(rows, "target")

        self.assertEqual(result["kind"], "EXACT")
        self.assertEqual([item["alpha_id"] for item in result["matches"]], ["exact"])

    def test_similarity_falls_back_to_variant_family(self):
        rows = [
            {"alpha_id": "target", "alpha": {"regular": "ts_mean(close, 22)"}, "settings": {}},
            {"alpha_id": "family", "alpha": {"regular": "ts_mean(close, 66)"}, "settings": {}},
        ]

        result = find_remote_similar(rows, "target")

        self.assertEqual(result["kind"], "VARIANT_FAMILY")
        self.assertEqual([item["alpha_id"] for item in result["matches"]], ["family"])

    def test_invalid_remote_rows_are_ignored(self):
        self.assertEqual(group_remote_evidence([None, {}, {"alpha_id": "x"}]),
                         {"execution": {}, "structural": {},
                          "variant_family": {}, "quality": {}})

    def test_public_grouping_is_read_only_projection(self):
        result = research_api.group_alphas(rows=[
            {"alpha_id": "a", "alpha": {"regular": "rank(close)"}, "settings": {}}
        ])
        self.assertEqual(len(result["execution"]), 1)

    def test_public_color_preview_requires_explicit_family_assignment(self):
        key = variant_family_fingerprint("rank(close)")
        plan = research_api.preview_alpha_colors(
            rows=[{"alpha_id": "a", "alpha": {"regular": "rank(close)"}}],
            assignments={key: "BLUE"},
        )
        self.assertEqual(plan[0]["desired_color"], "BLUE")
        with self.assertRaisesRegex(ValueError, "EXACT_COLOR_PLAN_REQUIRED"):
            research_api.sync_alpha_colors()


if __name__ == "__main__":
    unittest.main()
