import unittest

from wqb_agent.validation_statistics import (
    deflated_sharpe_ratio,
    pbo_cscv,
    pbo_proxy,
    probabilistic_sharpe_ratio,
)


class ValidationStatisticsKernelTests(unittest.TestCase):
    def test_psr_fixed_input_parity(self):
        values = [-.01, .02, .01, .03, -.02, .01, .015, -.005, .01, .02]
        result = probabilistic_sharpe_ratio(values, observed_sharpe=1.2)
        self.assertAlmostEqual(result["psr"], .9937524253, places=8)

    def test_dsr_selection_threshold_is_monotone(self):
        values = [-.01, .02, .01, .03, -.02, .01, .015, -.005, .01, .02]
        one = deflated_sharpe_ratio(values, observed_sharpe=1.2, n_trials=1)
        many = deflated_sharpe_ratio(values, observed_sharpe=1.2, n_trials=100)
        self.assertGreater(many["selection_threshold"], one["selection_threshold"])
        self.assertLess(many["psr"], one["psr"])

    def test_invalid_and_constant_observations_stay_unavailable(self):
        self.assertEqual(probabilistic_sharpe_ratio([1.0, 1.0])["status"], "UNAVAILABLE")
        self.assertEqual(deflated_sharpe_ratio([])["evidence_status"], "UNAVAILABLE")

    def test_pbo_nondivisible_folds_stay_unavailable(self):
        rows = [[float(index) for index in range(10)], [float(index - 1) for index in range(10)]]
        self.assertEqual(pbo_proxy(rows, n_splits=4)["status"], "UNAVAILABLE")
        valid = pbo_cscv([[1, 2, 3, 4, 5, 6, 7, 8], [0, 1, 2, 3, 4, 5, 6, 7]])
        self.assertEqual(valid["status"], "AVAILABLE")

    def test_compatibility_report_does_not_own_pbo_implementation(self):
        from wqb_agent import validation_report

        self.assertIs(validation_report.pbo_proxy, pbo_proxy)


if __name__ == "__main__":
    unittest.main()
