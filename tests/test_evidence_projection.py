import unittest

from wqb_agent.evidence_projection import alpha_rating, correlation_under


class EvidenceProjectionTests(unittest.TestCase):
    def test_correlation_under_requires_pass_and_numeric_value(self):
        self.assertTrue(correlation_under({"status": "PASS", "check": {"value": 0.2}}, 0.5))
        self.assertFalse(correlation_under({"status": "PASS", "check": {}}, 0.5))
        self.assertFalse(correlation_under({"status": "PENDING", "check": {"value": 0.2}}, 0.5))

    def test_alpha_rating_preserves_strict_quality_thresholds(self):
        metrics = {"sharpe": 2.1, "turnover": 0.2, "fitness": 2.6, "margin": 0.03}
        policy = {
            "excellent": {"min_sharpe": 1.0, "min_fitness": 0.8, "min_margin": 0.01, "max_turnover": 0.5},
            "spectacular": {"min_sharpe": 2.0, "min_fitness": 2.5, "min_margin": 0.02, "max_turnover": 0.3},
        }
        self.assertEqual(alpha_rating(metrics, policy), "SPECTACULAR")
        self.assertEqual(alpha_rating({**metrics, "margin": None}, policy), "UNRATED")


if __name__ == "__main__":
    unittest.main()
