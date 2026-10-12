import unittest

from wqb_agent.alpha_factory import AlphaFactory
from wqb_agent.alpha_semantics import derive_field_semantic_traits
from wqb_agent.alpha_templates.model import AlphaTemplate


class OperatingIncomeRelationshipTests(unittest.TestCase):
    def test_news_source_text_does_not_override_a_return_measure(self):
        result = derive_field_semantic_traits({
            "id": "market_impact_score",
            "description": (
                "Estimated risk-adjusted 1 minute forward return for a news article"
            ),
            "category": {"id": "news"},
            "type": "VECTOR",
        })
        self.assertEqual(result["concept"], "market_price")

    def test_quarterly_operating_income_is_ordered_with_total_assets(self):
        income = {
            "id": "quarterly_operating_income",
            "description": "Quarterly Operating Income",
            "dataset": "fundamental_fixture",
            "type": "MATRIX",
            "frequency": "quarterly",
            "category": {"id": "fundamental"},
        }
        assets = {
            "id": "quarterly_total_assets",
            "description": "Quarterly Total Assets",
            "dataset": "fundamental_fixture",
            "type": "MATRIX",
            "frequency": "quarterly",
            "category": {"id": "fundamental"},
        }
        template = AlphaTemplate(
            "operating-income-assets-fixture",
            family="operating-income-assets-fixture",
            expression="rank(divide({p}, {s}))",
            required_slots=("p", "s"),
            role="PROBE_ALPHA",
            economic_mechanism="quarterly operating income scaled by assets",
            field_relationship="operating income relative to total assets",
            direction_reason="fixture",
            expected_horizon="quarterly",
            falsification="fixture",
            relationship_contract="DIRECTIONAL_RATIO",
            semantic_contract="RELATIONAL_PRIMARY",
        )

        income_traits = derive_field_semantic_traits(income)
        self.assertEqual(income_traits["concept"], "earnings")
        self.assertEqual(income_traits["semantic_admission"], "ALLOW")
        self.assertIn("earnings", income_traits["tags"])

        decision = AlphaFactory._relationship_gate([income, assets], template)
        self.assertEqual(decision["admission"], "ALLOW")
        self.assertEqual(decision["relationship_type"], "numerator_denominator")


if __name__ == "__main__":
    unittest.main()
