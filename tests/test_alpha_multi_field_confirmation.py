"""Focused synthetic tests for conservative two-field confirmation admission."""

import unittest

from wqb_agent.alpha_factory import AlphaFactory
from wqb_agent.alpha_semantics import derive_field_semantic_traits
from wqb_agent.alpha_templates.model import AlphaTemplate


def _template():
    return AlphaTemplate(
        "synthetic-confirmation",
        family="synthetic",
        expression="rank(add(ts_zscore({p}, 5), ts_zscore({s}, 22)))",
        required_slots=("p", "s"),
        role="PROBE_ALPHA",
        semantic_contract="RELATIONAL_PRIMARY",
        relationship_contract="MULTI_FIELD_CONFIRMATION",
        field_roles=("sentiment", "event novelty"),
        economic_mechanism="synthetic joint news confirmation",
        field_relationship="sentiment conditioned on news novelty",
        direction_reason="synthetic test direction",
        expected_horizon="short-term",
        falsification="synthetic falsification",
    )


def _field(name, description, *, dataset="news_fixture", field_type="VECTOR"):
    return {
        "id": name,
        "description": description,
        "dataset": dataset,
        "type": field_type,
        "frequency": "daily",
        "category": {"id": "news", "name": "News"},
        "semantic_status": "KNOWN",
    }


class MultiFieldConfirmationTests(unittest.TestCase):
    def test_admits_same_dataset_daily_vector_sentiment_and_novelty(self):
        decision = AlphaFactory._relationship_gate(
            [
                _field("sentiment", "Daily news sentiment score"),
                _field("novelty", "Daily news novelty score"),
            ],
            _template(),
        )

        self.assertEqual(decision["admission"], "ALLOW")
        self.assertEqual(decision["relationship_type"], "novelty_weighted_sentiment")
        self.assertEqual(decision["confirmation_mechanism"], "event_novelty_sentiment")

    def test_rejects_confirmation_fields_from_different_datasets(self):
        decision = AlphaFactory._relationship_gate(
            [
                _field("sentiment", "Daily news sentiment score", dataset="news_a"),
                _field("novelty", "Daily news novelty score", dataset="news_b"),
            ],
            _template(),
        )

        self.assertEqual(decision["admission"], "REJECT")

    def test_rejects_non_vector_confirmation_fields(self):
        decision = AlphaFactory._relationship_gate(
            [
                _field("sentiment", "Daily news sentiment score", field_type="MATRIX"),
                _field("novelty", "Daily news novelty score"),
            ],
            _template(),
        )

        self.assertEqual(decision["admission"], "REJECT")

    def test_admits_unknown_frequency_only_with_same_dataset_scope(self):
        fields = [
            _field("sentiment", "Story polarity confidence"),
            _field("novelty", "Novelty score within subscribed content sets"),
        ]
        for item in fields:
            item["frequency"] = None

        decision = AlphaFactory._relationship_gate(fields, _template())

        self.assertEqual(decision["admission"], "ALLOW")
        self.assertEqual(decision["frequency_compatibility"]["status"], "REVIEW")
        self.assertIn("cadence metadata is unavailable", decision["reasons"][0])

    def test_reads_live_nested_news_category_for_explicit_novelty(self):
        traits = derive_field_semantic_traits(
            {
                "id": "novelty",
                "description": "Novelty score within subscribed content sets",
                "category": {"id": "news", "name": "News"},
            }
        )

        self.assertEqual(traits["concept"], "event_novelty")
        self.assertEqual(traits["semantic_admission"], "ALLOW")

    def test_keeps_known_frequency_mismatch_at_review(self):
        fields = [
            _field("sentiment", "Daily news sentiment score"),
            _field("novelty", "Annual news novelty score"),
        ]
        fields[0]["frequency"] = "daily"
        fields[1]["frequency"] = "annual"

        decision = AlphaFactory._relationship_gate(fields, _template())

        self.assertEqual(decision["admission"], "REVIEW")


if __name__ == "__main__":
    unittest.main()
