import unittest

from wqb_agent.pnl import correlation_evidence


class TestEvaluationEvidence(unittest.TestCase):
    def test_date_aligned_correlation_uses_inner_join(self):
        result = correlation_evidence({"2024-01-02": 1, "2024-01-03": 2, "2024-01-04": 3},
                                       {"2024-01-01": 9, "2024-01-02": 2, "2024-01-03": 4, "2024-01-04": 6})
        self.assertEqual(result["overlap_count"], 3)
        self.assertEqual(result["signed_corr"], 1.0)
        self.assertEqual(result["abs_corr"], 1.0)


if __name__ == "__main__":
    unittest.main()
