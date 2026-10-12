import unittest

from wqb_agent.alpha_relationships import frequency_bucket


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


if __name__ == "__main__":
    unittest.main()
