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
