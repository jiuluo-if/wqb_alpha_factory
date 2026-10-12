"""Focused synthetic tests for conservative two-field confirmation admission."""

import unittest

from wqb_agent.alpha_factory import AlphaFactory
from wqb_agent.alpha_semantics import derive_field_semantic_traits
from wqb_agent.alpha_templates.model import AlphaTemplate


def _template(relationship_contract="MULTI_FIELD_CONFIRMATION"):
    return AlphaTemplate(
        "synthetic-confirmation",
        family="synthetic",
        expression="rank(add(ts_zscore({p}, 5), ts_zscore({s}, 22)))",
        required_slots=("p", "s"),
        role="PROBE_ALPHA",
        semantic_contract="RELATIONAL_PRIMARY",
        relationship_contract=relationship_contract,
        field_roles=("sentiment", "event novelty"),
        economic_mechanism="synthetic joint news confirmation",
        field_relationship="sentiment conditioned on news novelty",
        direction_reason="synthetic test direction",
        expected_horizon="short-term",
        falsification="synthetic falsification",
    )


def _field(
    name,
    description,
    *,
    dataset="news_fixture",
    field_type="VECTOR",
    category="news",
    frequency="daily",
):
    return {
        "id": name,
        "description": description,
        "dataset": dataset,
        "type": field_type,
        "frequency": frequency,
        "category": {"id": category, "name": category.title()},
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

    def test_admits_shared_dataset_social_sentiment_scaled_by_attention(self):
        decision = AlphaFactory._relationship_gate(
            [
                _field(
                    "social_sentiment",
                    "Daily social sentiment score",
                    category="socialmedia",
                    field_type="MATRIX",
                ),
                _field(
                    "social_buzz",
                    "Daily relative sentiment volume",
                    category="socialmedia",
                    field_type="MATRIX",
                ),
            ],
            _template("DIRECTIONAL_RATIO"),
        )

        self.assertEqual(decision["admission"], "ALLOW")
        self.assertEqual(decision["relationship_type"], "sentiment_attention_scale")

    def test_admits_shared_dataset_equity_to_asset_ratio_with_unknown_cadence(self):
        decision = AlphaFactory._relationship_gate(
            [
                _field(
                    "equity",
                    "Common equity",
                    category="fundamental",
                    field_type="MATRIX",
                    frequency=None,
                ),
                _field(
                    "assets",
                    "Assets - Total",
                    category="fundamental",
                    field_type="MATRIX",
                    frequency=None,
                ),
            ],
            _template("DIRECTIONAL_RATIO"),
        )

        self.assertEqual(decision["admission"], "ALLOW")
        self.assertEqual(decision["relationship_type"], "equity_asset_base")
        self.assertEqual(decision["frequency_compatibility"]["status"], "REVIEW")

    def test_admits_shared_dataset_cashflow_to_debt_ratio_with_unknown_cadence(self):
        decision = AlphaFactory._relationship_gate(
            [
                _field(
                    "cashflow_op",
                    "Operating Activities - Net Cash Flow",
                    category="fundamental",
                    field_type="MATRIX",
                    frequency=None,
                ),
                _field(
                    "debt",
                    "Debt",
                    category="fundamental",
                    field_type="MATRIX",
                    frequency=None,
                ),
            ],
            _template("DIRECTIONAL_RATIO"),
        )

        self.assertEqual(decision["admission"], "ALLOW")
        self.assertEqual(decision["relationship_type"], "cashflow_debt_coverage")

    def test_admits_same_dataset_historical_to_implied_volatility_ratio(self):
        realized = _field(
            "historical_volatility",
            "Historical volatility over trailing 60 days",
            dataset="option_fixture",
            category="option",
            field_type="MATRIX",
        )
        realized_traits = derive_field_semantic_traits(realized)
        self.assertEqual(realized_traits["concept"], "realized_volatility")
        self.assertEqual(realized_traits["behavior"], "signed")
        decision = AlphaFactory._relationship_gate(
            [
                realized,
                _field(
                    "implied_volatility",
                    "Implied volatility for call options 60 days to expiry",
                    dataset="option_fixture",
                    category="option",
                    field_type="MATRIX",
                ),
            ],
            _template("DIRECTIONAL_RATIO"),
        )

        self.assertEqual(decision["admission"], "ALLOW")
        self.assertEqual(decision["relationship_type"], "realized_implied_volatility")

    def test_admits_same_dataset_profitability_and_asset_efficiency_pair(self):
        fields = [
            _field(
                "gross_margin",
                "Gross margin over the trailing 12 months",
                category="fundamental",
                field_type="MATRIX",
            ),
            _field(
                "asset_turnover",
                "Asset turnover over the trailing 12 months",
                category="fundamental",
                field_type="MATRIX",
            ),
        ]

        for contract in ("COMPARABLE_SPREAD", "MULTI_FIELD_CONFIRMATION"):
            with self.subTest(contract=contract):
                decision = AlphaFactory._relationship_gate(fields, _template(contract))
                self.assertEqual(decision["admission"], "ALLOW")
                self.assertEqual(decision["relationship_type"], "profitability_efficiency")
                if contract == "COMPARABLE_SPREAD":
                    self.assertFalse(decision["symmetric"])
                    self.assertEqual(
                        decision["preferred_slot_assignment"],
                        {"p": "profitability_measure", "s": "asset_efficiency"},
                    )

    def test_specific_ratio_contracts_reject_reversed_slots_and_cross_dataset_fields(self):
        reversed_fields = [
            _field(
                "assets",
                "Assets - Total",
                category="fundamental",
                field_type="MATRIX",
                frequency=None,
            ),
            _field(
                "equity",
                "Common equity",
                category="fundamental",
                field_type="MATRIX",
                frequency=None,
            ),
        ]
        reversed_decision = AlphaFactory._relationship_gate(
            reversed_fields, _template("DIRECTIONAL_RATIO")
        )
        cross_dataset_fields = [
            _field(
                "equity",
                "Common equity",
                category="fundamental",
                field_type="MATRIX",
                frequency=None,
            ),
            _field(
                "assets",
                "Assets - Total",
                dataset="other_fundamental",
                category="fundamental",
                field_type="MATRIX",
                frequency=None,
            ),
        ]
        cross_dataset_decision = AlphaFactory._relationship_gate(
            cross_dataset_fields, _template("DIRECTIONAL_RATIO")
        )

        self.assertEqual(reversed_decision["admission"], "REJECT")
        self.assertEqual(cross_dataset_decision["admission"], "REJECT")

    def test_social_attention_ratio_rejects_mixed_field_types(self):
        decision = AlphaFactory._relationship_gate(
            [
                _field(
                    "social_sentiment",
                    "Daily social sentiment score",
                    category="socialmedia",
                    field_type="MATRIX",
                ),
                _field(
                    "social_buzz",
                    "Daily relative sentiment volume",
                    category="socialmedia",
                    field_type="VECTOR",
                ),
            ],
            _template("DIRECTIONAL_RATIO"),
        )

        self.assertEqual(decision["admission"], "REJECT")

    def test_specialized_relationships_reject_profiles_outside_their_live_category(self):
        decision = AlphaFactory._relationship_gate(
            [
                _field(
                    "equity",
                    "Common equity",
                    category="news",
                    field_type="MATRIX",
                    frequency=None,
                ),
                _field(
                    "assets",
                    "Assets - Total",
                    category="news",
                    field_type="MATRIX",
                    frequency=None,
                ),
            ],
            _template("DIRECTIONAL_RATIO"),
        )

        self.assertEqual(decision["admission"], "REJECT")


if __name__ == "__main__":
    unittest.main()
