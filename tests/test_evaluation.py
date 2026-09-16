import unittest

from wqb_agent.experiment import Experiment
from wqb_agent.pnl import correlation_evidence
from wqb_agent.validation_report import (
    build_validation_report,
    default_validation_plan,
    deflated_sharpe_ratio,
    pbo_proxy,
    probabilistic_sharpe_ratio,
    validate_plan,
)


def metrics():
    return {"sharpe": 1.2, "fitness": 1.0, "turnover": .2, "margin": .01,
            "drawdown": .1, "checks": [{"name": "ALL", "pass": True}]}


class TestEvaluationEvidence(unittest.TestCase):
    def test_plan_hash_changes_when_content_changes(self):
        parent = {"expression": "rank(close)", "submission_fingerprint": "fp", "settings": {"decay": 4}}
        left = default_validation_plan(parent, timestamp=1.0)
        right = default_validation_plan(parent, timestamp=2.0, budget=6)
        self.assertNotEqual(left["plan_id"], right["plan_id"])
        self.assertTrue(validate_plan(left)[0])

    def test_not_applicable_window_is_not_a_failure_dimension(self):
        parent = Experiment(1, "h", "rank(close)", {}, ["close"])
        parent.status = "DONE"
        parent.metrics = metrics()
        plan = default_validation_plan(parent)
        self.assertEqual(next(row for row in plan["variables"] if row["variable"] == "window_locality")["requirement"], "NOT_APPLICABLE")
        self.assertTrue(validate_plan(plan)[0])

    def test_unavailable_dsr_is_not_pass(self):
        result = deflated_sharpe_ratio([], observed_sharpe=1.0)
        self.assertEqual(result["evidence_status"], "UNAVAILABLE")
        self.assertNotEqual(result["status"], "PASS")

    def test_reference_psr_and_proxy_labels_are_stable(self):
        values = [-.01, .02, .01, .03, -.02, .01, .015, -.005, .01, .02]
        result = probabilistic_sharpe_ratio(values, observed_sharpe=1.2)
        self.assertAlmostEqual(result["psr"], .9937524253, places=8)
        proxy = pbo_proxy([[1, 2, 3, 4, 5, 6, 7, 8], [0, 1, 2, 3, 4, 5, 6, 7]])
        self.assertEqual(proxy["method"], "pbo_proxy")
        self.assertEqual(proxy["evidence_status"], "APPROXIMATE")

    def test_date_aligned_correlation_uses_inner_join(self):
        result = correlation_evidence({"2024-01-02": 1, "2024-01-03": 2, "2024-01-04": 3},
                                       {"2024-01-01": 9, "2024-01-02": 2, "2024-01-03": 4, "2024-01-04": 6})
        self.assertEqual(result["overlap_count"], 3)
        self.assertEqual(result["signed_corr"], 1.0)
        self.assertEqual(result["abs_corr"], 1.0)


if __name__ == "__main__":
    unittest.main()
