"""Offline contract tests for bounded external evidence."""

import unittest

from wqb_agent.expression import submission_fingerprint
from wqb_agent.optimization_decision import (
    OptimizationDecision,
    optimization_decision_identity,
)
from wqb_agent.research_api import ExperimentSpec
from wqb_agent.research_evidence import (
    AuthoritativeEvidenceItem,
    AuthoritativeEvidencePack,
    stable_source_identity,
    validate_authoritative_evidence_item,
    validate_authoritative_evidence_pack,
)


def item(**overrides):
    payload = {
        "source_class": "OFFICIAL_PLATFORM",
        "publisher": "Example Authority",
        "domain": "example.org",
        "title": "A bounded source",
        "url": "https://example.org/report?id=1#ignored",
        "published_at": "2026-01-01",
        "retrieved_at": "2026-09-13T01:02:03Z",
        "claim": "The source describes a market relationship worth testing.",
        "claim_type": "MECHANISM_SUPPORT",
        "publication_status": "INSTITUTIONAL_RESEARCH",
        "support": "SUPPORT",
        "freshness": "CURRENT",
    }
    payload.update(overrides)
    payload["source_id"] = stable_source_identity(
        payload["url"], payload["claim"], payload["published_at"]
    )
    return payload


class TestAuthoritativeEvidence(unittest.TestCase):
    def test_valid_bounded_item_and_round_trip(self):
        value = AuthoritativeEvidenceItem.from_mapping(item())
        self.assertEqual(validate_authoritative_evidence_item(value), (True, []))
        self.assertEqual(value.as_dict()["source_id"], value.source_id)

    def test_missing_url_or_claim_rejected(self):
        for key in ("url", "claim"):
            payload = item(**{key: ""})
            payload["source_id"] = "source-invalid"
            ok, errors = validate_authoritative_evidence_item(payload)
            self.assertFalse(ok)
            self.assertTrue(any(key in error for error in errors))

    def test_raw_document_and_oversized_claim_rejected(self):
        for claim in ("<html>whole document</html>", "x" * 2001):
            ok, errors = validate_authoritative_evidence_item(item(claim=claim))
            self.assertFalse(ok)
            self.assertTrue(errors)

    def test_source_identity_ignores_retrieval_time(self):
        left = stable_source_identity("https://example.org/a#x", "A claim", "2020-01-01")
        right = stable_source_identity("https://EXAMPLE.ORG/a", " A claim ", "2020-01-01")
        self.assertEqual(left, right)

    def test_pack_deduplicates_order_independently_and_is_bounded(self):
        first = item()
        second = item(
            url="https://example.org/other",
            title="Another source",
            claim="An independent bounded claim.",
        )
        pack = AuthoritativeEvidencePack.from_items([second, first, first])
        reverse = AuthoritativeEvidencePack.from_items([first, second])
        self.assertEqual(pack.as_dict(), reverse.as_dict())
        self.assertEqual(len(pack.items), 2)
        self.assertEqual(validate_authoritative_evidence_pack(pack), (True, []))

    def test_unknown_source_class_fails_closed(self):
        ok, errors = validate_authoritative_evidence_item(item(source_class="BLOG"))
        self.assertFalse(ok)
        self.assertTrue(any("source_class" in error for error in errors))

    def test_pack_rejects_more_than_two_claims_per_source(self):
        values = [item(claim=f"Claim {index}") for index in range(3)]
        ok, errors = validate_authoritative_evidence_pack(
            AuthoritativeEvidencePack.from_items(values)
        )
        self.assertFalse(ok)
        self.assertTrue(any("claims per source" in error for error in errors))

    def test_conflicting_claims_are_explicitly_unresolved(self):
        left = item(support="SUPPORT")
        right = item(claim="The source reports a competing relationship.", support="CONTRADICTION")
        pack = AuthoritativeEvidencePack.from_items([left, right])
        self.assertEqual(pack.conflict_status, "CONFLICTED")

    def test_refs_are_bounded_provenance_and_do_not_change_decision_identity(self):
        base = OptimizationDecision(parent_id="p1", decision="STOP")
        cited = OptimizationDecision(
            parent_id="p1", decision="STOP", external_evidence_refs=("evidence-source|a",)
        )
        self.assertEqual(optimization_decision_identity(base), optimization_decision_identity(cited))

        spec = ExperimentSpec(
            hypothesis="test hypothesis", expression="rank(close)",
            external_evidence_refs=("evidence-source|a",),
        )
        proposal = spec.to_proposal()
        self.assertEqual(proposal["external_evidence_refs"], ["evidence-source|a"])
        self.assertEqual(submission_fingerprint(spec.expression, spec.settings),
                         submission_fingerprint(proposal["expression"], proposal["settings"]))
        self.assertNotIn("The source describes", str(proposal))


if __name__ == "__main__":
    unittest.main()
