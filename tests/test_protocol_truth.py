import json
import os
import unittest

from wqb_agent.protocol import (
    CapabilityStatus,
    endpoint_catalog,
    fixture_capability,
    probe_capability_response,
    retry_after_seconds,
)

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "brain")


class TestProtocolTruth(unittest.TestCase):
    def test_catalog_separates_official_and_community_capabilities(self):
        catalog = {row["key"]: row for row in endpoint_catalog()}
        self.assertEqual(catalog["authentication"]["status"], "OFFICIAL")
        self.assertEqual(catalog["aggregates"]["status"], "OFFICIAL")
        self.assertEqual(catalog["operators"]["status"], "COMMUNITY_OBSERVED")

    def test_fixture_and_static_operator_evidence_cannot_claim_availability(self):
        with open(os.path.join(FIXTURES, "aggregates.json"), encoding="utf-8") as handle:
            payload = json.load(handle)
        aggregate = fixture_capability("aggregates", payload)
        self.assertEqual(aggregate["status"], CapabilityStatus.FIXTURE_VERIFIED.value)
        self.assertNotEqual(aggregate["status"], CapabilityStatus.LIVE_VERIFIED.value)

        fixture = fixture_capability("operators", {"operators": [{"name": "rank"}]})
        self.assertEqual(fixture["status"], "FIXTURE_VERIFIED")
        self.assertNotEqual(fixture["availability"], "AVAILABLE")

        from wqb_agent.operator_reference import load_operator_syntax_reference

        static = load_operator_syntax_reference(
            os.path.join(os.path.dirname(os.path.dirname(__file__)),
                         "docs", "reference", "OPERATORS_CHEATSHEET.md")
        )
        self.assertEqual(static["source"], "STATIC_SYNTAX_REFERENCE")
        self.assertEqual(static["availability"], "UNKNOWN")

    def test_static_operator_reference_covers_pasted_operator_catalog(self):
        from wqb_agent.operator_reference import load_operator_syntax_reference

        expected = {
            "abs", "add", "and", "bucket", "days_from_last_change", "densify",
            "divide", "group_backfill", "group_cartesian_product", "group_extra",
            "group_mean", "group_neutralize", "group_rank", "group_scale",
            "group_zscore", "hump", "if_else", "inst_pnl", "inverse", "is_nan",
            "kth_element", "last_diff_value", "log", "max", "min", "multiply",
            "normalize", "not", "or", "power", "quantile", "rank",
            "regression_proj", "reverse", "scale", "sigmoid", "sign", "signed_power",
            "sqrt", "subtract", "tanh", "trade_when", "ts_arg_max", "ts_arg_min",
            "ts_av_diff", "ts_backfill", "ts_corr", "ts_count_nans", "ts_covariance",
            "ts_decay_linear", "ts_delay", "ts_delta", "ts_entropy", "ts_mean",
            "ts_min_diff", "ts_min_max_cps", "ts_min_max_diff", "ts_product",
            "ts_quantile", "ts_rank", "ts_regression", "ts_scale", "ts_skewness",
            "ts_std_dev", "ts_step", "ts_sum", "ts_target_tvr_decay", "ts_zscore",
            "vec_avg", "vec_count", "vec_max", "vec_min", "vec_range", "vec_stddev",
            "vec_sum", "vector_neut", "vector_proj", "winsorize", "zscore",
        }
        root = os.path.dirname(os.path.dirname(__file__))
        packaged = load_operator_syntax_reference(
            os.path.join(root, "wqb_agent", "reference", "OPERATORS_CHEATSHEET.md")
        )
        documented = load_operator_syntax_reference(
            os.path.join(root, "docs", "reference", "OPERATORS_CHEATSHEET.md")
        )
        self.assertEqual(set(packaged["operators"]), expected)
        self.assertEqual(packaged["operators"], documented["operators"])
        self.assertEqual(packaged["source"], "STATIC_SYNTAX_REFERENCE")
        self.assertEqual(packaged["availability"], "UNKNOWN")

    def test_retry_after_accepts_seconds_and_invalid_fails_closed(self):
        self.assertEqual(retry_after_seconds({"Retry-After": "2"}), 2.0)
        self.assertEqual(retry_after_seconds({"Retry-After": "bad"}), 5.0)
        self.assertGreaterEqual(retry_after_seconds({"Retry-After": "-3"}), 1.0)

    def test_probe_response_can_verify_live_shape_without_calling_transport(self):
        result = probe_capability_response(
            "operators", 200, {"operators": [{"name": "rank"}]}
        )
        self.assertEqual(result["status"], "LIVE_VERIFIED")
        self.assertEqual(result["availability"], "AVAILABLE")
        unavailable = probe_capability_response("pnl", 404, {})
        self.assertNotEqual(unavailable["availability"], "AVAILABLE")
        self.assertEqual(unavailable["status"], "UNKNOWN")

    def test_malformed_live_operator_responses_are_not_available(self):
        for payload in (
            {"operators": ["rank"]},
            [{"name": "rank"}, {"description": "no name"}],
        ):
            with self.subTest(payload=payload):
                result = probe_capability_response("operators", 200, payload)
                self.assertNotEqual(result["status"], "LIVE_VERIFIED")
                self.assertNotEqual(result["availability"], "AVAILABLE")

    def test_bare_operator_array_is_a_valid_live_envelope(self):
        # Live BRAIN /operators serves a bare JSON array, not an envelope object.
        result = probe_capability_response(
            "operators", 200, [{"name": "rank"}, {"name": "ts_mean"}]
        )
        self.assertEqual(result["status"], "LIVE_VERIFIED")
        self.assertEqual(result["availability"], "AVAILABLE")
        self.assertEqual(result["operators"], ["rank", "ts_mean"])
        self.assertIn("capability_fingerprint", result)

    def test_list_payload_is_rejected_for_non_operator_keys(self):
        result = probe_capability_response("data_sets", 200, [])
        self.assertNotEqual(result["availability"], "AVAILABLE")
