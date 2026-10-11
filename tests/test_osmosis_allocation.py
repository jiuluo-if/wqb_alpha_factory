import math
import unittest
from datetime import date, timedelta

from wqb_agent.osmosis_allocation import (
    compare_osmosis_allocation_plans,
    preview_osmosis_allocation,
)


def _candidates(count=10, date_count=150):
    dates = [(date(2025, 1, 1) + timedelta(days=index)).isoformat()
             for index in range(date_count)]
    rows = []
    for index in range(count):
        pnl = [
            math.sin((day + index * 3) / 8.0) + 0.01 * (index + 1)
            for day in range(len(dates))
        ]
        rows.append({
            "alpha_id": f"alpha-{index:02d}",
            "submission_status": "SUBMITTED",
            "scope": {"region": "USA", "universe": "TOP3000", "delay": 1},
            "quality": {
                "status": "AVAILABLE", "grade": "B", "coverage": .95,
                "q_low": 70.0 - index,
            },
            "family": {"id": f"family-{index:02d}", "source": "synthetic-label"},
            "pnl": {
                "unit": "SYNTHETIC_UNITS", "expected_dates": dates,
                "rows": [{"date": day, "value": value}
                         for day, value in zip(dates, pnl)],
            },
        })
    return rows


SCOPE = {"region": "USA", "delay": 1}


class OsmosisAllocationTests(unittest.TestCase):
    def test_preview_is_order_invariant_and_sums_to_10000(self):
        rows = _candidates()
        first = preview_osmosis_allocation(rows, scope=SCOPE)
        second = preview_osmosis_allocation(rows[::-1], scope=SCOPE)
        self.assertEqual(first["status"], "READY")
        self.assertEqual(first["points"], second["points"])
        self.assertEqual(sum(row["points"] for row in first["allocations"]), 10000)
        self.assertTrue(all(isinstance(row["points"], int) for row in first["allocations"]))
        self.assertEqual(
            sum(row["normalized_points"] for row in first["allocations"]), 10000,
        )
        self.assertAlmostEqual(sum(row["weight"] for row in first["allocations"]), 1.0)
        self.assertEqual(first["platform_rules_status"], "UNKNOWN")
        self.assertIsNone(first["platform_points_total"])

    def test_unsubmitted_and_wrong_scope_candidates_are_excluded(self):
        rows = _candidates()
        rows[0]["submission_status"] = "NOT_SUBMITTED"
        rows[1]["scope"] = {"region": "EUR", "delay": 1}
        result = preview_osmosis_allocation(rows, scope=SCOPE)
        self.assertEqual(result["status"], "READY")
        self.assertEqual(result["excluded"]["alpha-00"], "NOT_SUBMITTED")
        self.assertEqual(result["excluded"]["alpha-01"], "OUT_OF_SCOPE")
        self.assertNotIn("alpha-00", result["points"])

    def test_too_few_candidates_for_caps_is_infeasible(self):
        result = preview_osmosis_allocation(_candidates(6), scope=SCOPE)
        self.assertEqual(result["status"], "INFEASIBLE")
        self.assertIn("CONCENTRATION_CAPACITY_BELOW_TOTAL", result["reason_codes"])

    def test_missing_correlation_evidence_stays_unknown(self):
        rows = _candidates()
        for row in rows:
            row["pnl"]["expected_dates"] = row["pnl"]["expected_dates"][-100:]
            row["pnl"]["rows"] = row["pnl"]["rows"][-100:]
        result = preview_osmosis_allocation(rows, scope=SCOPE, min_correlation_observations=120)
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertIn("PNL_OVERLAP_INSUFFICIENT", result["reason_codes"])
        self.assertIsNone(result["allocations"])

    def test_family_cap_is_enforced_and_unknown_family_is_not_assumed_unique(self):
        rows = _candidates()
        for row in rows[:3]:
            row["family"]["id"] = "family-shared"
        result = preview_osmosis_allocation(rows, scope=SCOPE)
        self.assertEqual(result["status"], "READY")
        family_aliases = {
            row["family_id"] for row in result["allocations"]
            if row["alpha_id"] in {"alpha-00", "alpha-01", "alpha-02"}
        }
        self.assertEqual(len(family_aliases), 1)
        self.assertLessEqual(result["family_concentration"][family_aliases.pop()], .25)
        rows[0]["family"] = {"id": None}
        unknown = preview_osmosis_allocation(rows, scope=SCOPE)
        self.assertEqual(unknown["status"], "READY")
        self.assertEqual(unknown["excluded"]["alpha-00"], "FAMILY_PROVENANCE_UNKNOWN")

    def test_verified_platform_total_is_scaled_only_when_explicit(self):
        result = preview_osmosis_allocation(
            _candidates(), scope=SCOPE,
            platform_rules={
                "status": "LIVE_VERIFIED", "source": "BRAIN_ACCOUNT",
                "fetched_at": "2026-01-01T00:00:00Z", "point_total": 100000,
            },
        )
        self.assertEqual(result["platform_points_total"], 100000)
        self.assertTrue(all(
            row["platform_points"] == row["points"] * 10
            for row in result["allocations"]
        ))

    def test_three_comparison_plans_report_unknown_costs_and_risk_metrics(self):
        result = compare_osmosis_allocation_plans(_candidates(), scope=SCOPE)
        self.assertEqual(set(result["plans"]), {"equal", "quality_only", "risk_adjusted"})
        risk = result["plans"]["risk_adjusted"]
        self.assertEqual(risk["status"], "READY")
        self.assertEqual(sum(item["points"] for item in risk["allocations"]), 10000)
        self.assertEqual(risk["portfolio_metrics"]["cost_sensitivity"], "UNKNOWN")

    def test_highly_correlated_candidates_are_reported_as_concentration_risk(self):
        rows = _candidates()
        rows[1]["pnl"]["rows"] = [dict(row) for row in rows[0]["pnl"]["rows"]]
        result = preview_osmosis_allocation(rows, scope=SCOPE)
        self.assertEqual(result["status"], "READY")
        self.assertGreater(result["portfolio_metrics"]["average_pairwise_correlation"], 0)
        self.assertIn("average_common_drawdown_correlation", result["portfolio_metrics"])

    def test_ambiguous_scope_returns_explicit_infeasible_status(self):
        result = preview_osmosis_allocation(_candidates(), scope={})
        self.assertEqual(result["status"], "INFEASIBLE")
        self.assertIn("SCOPE_UNKNOWN", result["reason_codes"])

    def test_rolling_window_configuration_stability_is_descriptive_only(self):
        result = preview_osmosis_allocation(_candidates(date_count=300), scope=SCOPE)
        rolling = result["rolling_configuration_stability"]
        self.assertEqual(rolling["status"], "AVAILABLE")
        self.assertTrue(rolling["quality_scores_held_fixed"])
        self.assertIn("not OOS", rolling["interpretation"])
        self.assertGreaterEqual(rolling["window_count"], 3)


if __name__ == "__main__":
    unittest.main()
