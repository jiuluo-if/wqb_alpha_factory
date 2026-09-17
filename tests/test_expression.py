import unittest

from wqb_agent.alpha_grouping import structural_fingerprint
from wqb_agent.expression import (
    ExpressionAnalysis,
    analyze_expression,
    expression_identity_keys,
    submission_fingerprint,
    variant_family_fingerprint,
)


class TestExpressionAnalysis(unittest.TestCase):
    def test_variant_family_abstracts_declared_horizon_literals_only(self):
        base = "ts_mean(close, 22)"
        numeric_variant = "ts_mean(close, 66)"
        field_variant = "ts_mean(volume, 22)"
        operator_variant = "ts_delta(close, 22)"

        self.assertNotEqual(structural_fingerprint(base), structural_fingerprint(numeric_variant))
        self.assertEqual(variant_family_fingerprint(base), variant_family_fingerprint(numeric_variant))
        self.assertEqual(variant_family_fingerprint(base), variant_family_fingerprint(field_variant))
        self.assertNotEqual(variant_family_fingerprint(base), variant_family_fingerprint(operator_variant))
        self.assertEqual(expression_identity_keys(base)[1], variant_family_fingerprint(base))

    def test_variant_family_preserves_fixed_and_unknown_numeric_literals(self):
        self.assertNotEqual(
            variant_family_fingerprint("divide(close, 0.001)"),
            variant_family_fingerprint("divide(close, 0.01)"),
        )
        self.assertNotEqual(
            variant_family_fingerprint("trade_when(close, 0.2, volume)"),
            variant_family_fingerprint("trade_when(close, 0.8, volume)"),
        )
        self.assertNotEqual(
            variant_family_fingerprint("power(close, 5)"),
            variant_family_fingerprint("power(close, 22)"),
        )
        self.assertNotEqual(
            variant_family_fingerprint("kth_element(close, 1, 2)"),
            variant_family_fingerprint("kth_element(close, 2, 2)"),
        )

    def test_settings_are_not_in_variant_family_but_remain_in_execution_identity(self):
        expression = "rank(close)"

        self.assertEqual(
            variant_family_fingerprint(expression),
            variant_family_fingerprint(expression),
        )
        self.assertNotEqual(
            submission_fingerprint(expression, {"decay": 4}),
            submission_fingerprint(expression, {"decay": 6}),
        )

    def test_analysis_is_deterministic_and_extracts_only_known_fields(self):
        result = analyze_expression(
            "rank(ts_delta(returns_5d, 5)) + rank(close)",
            known_fields=("returns", "returns_5d", "close"),
        )

        self.assertIsInstance(result, ExpressionAnalysis)
        self.assertEqual(result.canonical, "rank(ts_delta(returns_5d,5))+rank(close)")
        self.assertEqual(result.operators, ("rank", "ts_delta"))
        self.assertEqual(result.fields, ("close", "returns_5d"))
        self.assertIn("returns_5d", result.identifiers)
        self.assertNotIn("returns", result.fields)

    def test_analysis_handles_empty_and_non_string_input(self):
        result = analyze_expression(None, known_fields=("close",))

        self.assertEqual(result.original, "")
        self.assertEqual(result.canonical, "")
        self.assertEqual(result.operators, ())
        self.assertEqual(result.identifiers, ())
        self.assertEqual(result.fields, ())

    def test_known_field_matching_is_case_insensitive_but_preserves_declared_id(self):
        result = analyze_expression(
            "Rank(CLOSE) + rank(close_5d)",
            known_fields=("close", "close_5d"),
        )

        self.assertEqual(result.fields, ("close", "close_5d"))


if __name__ == "__main__":
    unittest.main()
