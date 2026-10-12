import unittest

from wqb_agent.alpha_relationships import frequency_bucket
from wqb_agent.alpha_semantics import derive_field_semantic_traits


class EventLookbackFrequencyTests(unittest.TestCase):
    def test_observation_lookback_is_not_a_data_update_frequency(self):
        self.assertEqual(
            frequency_bucket(
                "story novelty linked to a previous story published within the last 24 hours"
            ),
            "unknown",
        )

    def test_explicit_intraday_cadence_is_preserved(self):
        self.assertEqual(frequency_bucket("intraday field updated hourly"), "intraday")

    def test_age_measured_in_days_is_not_daily_cadence(self):
        self.assertEqual(
            frequency_bucket("event similarity days since the previous event"),
            "unknown",
        )

    def test_days_since_similar_event_is_novelty_not_generic_news_sentiment(self):
        traits = derive_field_semantic_traits({
            "id": "nws17_event_similarity_days",
            "description": "Days since a similar event occurred within the last 365 days (0 to 365).",
            "category": {"id": "news"},
            "type": "VECTOR",
        })
        self.assertEqual(traits["concept"], "event_novelty")
        self.assertEqual(traits["semantic_admission"], "ALLOW")


if __name__ == "__main__":
    unittest.main()
