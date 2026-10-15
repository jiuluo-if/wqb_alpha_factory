import unittest

from wqb_agent.alpha_semantics import derive_field_semantic_traits


class LiveFieldSemanticPrecedenceTests(unittest.TestCase):
    def test_missing_fill_note_does_not_hide_social_signal_roles(self):
        cases = (
            (
                "Negative sentiment score/indicator for current day, with missing values filled as 0",
                "sentiment",
            ),
            (
                "Negative relative sentiment volume measure for current day, with missing values filled as 0",
                "social_attention",
            ),
        )
        for description, expected in cases:
            with self.subTest(expected=expected):
                result = derive_field_semantic_traits({
                    "id": "social_measure",
                    "description": description,
                    "category": {"id": "socialmedia"},
                    "type": "MATRIX",
                })
                self.assertEqual(result["concept"], expected)

    def test_operating_cash_flow_live_wordings_map_to_cashflow(self):
        descriptions = (
            "Net cash flow from operating activities during the period.",
            "Annual net cash provided by operating activities.",
        )
        for description in descriptions:
            with self.subTest(description=description):
                result = derive_field_semantic_traits({
                    "id": "operating_cash_flow_measure",
                    "description": description,
                    "category": {"id": "fundamental"},
                    "type": "MATRIX",
                })
                self.assertEqual(result["concept"], "operating_cashflow")

    def test_news_category_alone_does_not_turn_market_impact_into_sentiment(self):
        result = derive_field_semantic_traits({
            "id": "market_impact_score",
            "description": "Estimated risk-adjusted 1 minute forward return for a news article",
            "category": {"id": "news"},
            "type": "VECTOR",
        })
        self.assertEqual(result["concept"], "market_price")

    def test_pure_missingness_field_remains_data_quality(self):
        result = derive_field_semantic_traits({
            "id": "missing_observation_count",
            "description": "Count of missing observations for the field",
            "category": {"id": "other"},
            "type": "MATRIX",
        })
        self.assertEqual(result["concept"], "data_quality")


if __name__ == "__main__":
    unittest.main()
