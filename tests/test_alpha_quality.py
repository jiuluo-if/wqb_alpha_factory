import unittest
from datetime import date, timedelta

from wqb_agent.alpha_quality import (
    METRIC_WEIGHTS,
    TYPE_WEIGHTS,
    evaluate_quality,
    pnl_drawdown_strength,
    resolve_alpha_type,
)


def _dates(count=252):
    start = date(2024, 1, 1)
    return [(start + timedelta(days=index)).isoformat() for index in range(count)]


def _calibration():
    references = {
        "sharpe": (1.0, 0.5, 0.1, "ratio"),
        "returns": (0.1, 0.05, 0.01, "fraction"),
        "mdd_strength": (1.0, 0.5, 0.1, "ratio"),
        "cdar_strength": (0.5, 0.25, 0.1, "ratio"),
        "margin": (5.0, 2.0, 0.5, "bp"),
        "turnover": (0.2, 0.1, 0.02, "fraction"),
        "stability": (0.5, 0.2, 0.05, "score"),
        "self_correlation": (0.2, 0.2, 0.05, "correlation"),
        "yearly_sharpe": (1.0, 0.5, 0.1, "ratio"),
        "recent_sharpe_change": (0.0, 0.5, 0.1, "ratio"),
    }
    return {
        "version": "AQAF-TEST-1",
        "reference_id": "synthetic-reference",
        "frozen_at": "2025-01-01T00:00:00Z",
        "source": "synthetic-fixture",
        "minimum_reference_count": 20,
        "scope": {"region": "USA", "universe": "TOP3000", "delay": 1},
        "pnl_unit": "SYNTHETIC_UNITS",
        "turnover_budget_unit": "fraction",
        "metric_references": {
            key: {"median": median, "mad": mad, "scale_floor": floor, "unit": unit,
                  "sample_count": 100, "source": "synthetic-fixture"}
            for key, (median, mad, floor, unit) in references.items()
        },
        "turnover_budgets": {
            "GENERAL": 0.3,
            "HIGH_TURNOVER": 0.5,
            "LOW_TURNOVER": 0.15,
        },
    }


class AlphaQualityTests(unittest.TestCase):
    def test_type_weights_sum_to_one_hundred(self):
        self.assertEqual(set(TYPE_WEIGHTS), {
            "GENERAL", "HIGH_TURNOVER", "TREND_MOMENTUM",
            "LOW_TURNOVER", "EVENT", "DEFENSIVE",
        })
        for weights in [METRIC_WEIGHTS, *TYPE_WEIGHTS.values()]:
            self.assertEqual(sum(weights.values()), 100)

    def test_unknown_economic_type_uses_turnover_interpolation(self):
        resolved = resolve_alpha_type(turnover=0.425)
        self.assertEqual(resolved["status"], "TURNOVER_INTERPOLATED")
        self.assertAlmostEqual(resolved["blend_weights"]["HIGH_TURNOVER"], 0.5)
        self.assertAlmostEqual(sum(resolved["blend_weights"].values()), 1.0)
        self.assertEqual(sum(resolved["weights"].values()), 100.0)

    def test_trusted_general_label_uses_exact_general_weights(self):
        resolved = resolve_alpha_type(
            turnover=0.5, type_label="GENERAL", type_source="ALPHA_METADATA",
        )
        self.assertEqual(resolved["status"], "EXPLICIT")
        self.assertEqual(resolved["weights"], TYPE_WEIGHTS["GENERAL"])

    def test_drawdown_strength_is_scale_invariant_and_rejects_bad_dates(self):
        days = _dates()
        rows = [{"date": day, "value": (-1.0 if index > 210 else 1.0)}
                for index, day in enumerate(days)]
        original = pnl_drawdown_strength(
            rows, expected_dates=days, pnl_unit="SYNTHETIC_UNITS",
            expected_unit="SYNTHETIC_UNITS",
        )
        scaled = pnl_drawdown_strength(
            [{**row, "value": row["value"] * 1000} for row in rows],
            expected_dates=days, pnl_unit="SYNTHETIC_UNITS",
            expected_unit="SYNTHETIC_UNITS",
        )
        self.assertEqual(original["status"], "AVAILABLE")
        self.assertAlmostEqual(original["mdd_strength"], scaled["mdd_strength"])
        self.assertAlmostEqual(original["cdar_strength"], scaled["cdar_strength"])
        self.assertEqual(
            pnl_drawdown_strength(
                rows[::-1], expected_dates=days,
                pnl_unit="SYNTHETIC_UNITS", expected_unit="SYNTHETIC_UNITS",
            )["reason_code"],
            "PNL_DATE_ORDER_INVALID",
        )
        duplicate = [*rows[:-1], {**rows[-1], "date": rows[-2]["date"]}]
        self.assertEqual(pnl_drawdown_strength(
            duplicate, expected_dates=days, pnl_unit="SYNTHETIC_UNITS",
            expected_unit="SYNTHETIC_UNITS",
        )["reason_code"], "DUPLICATE_PNL_DATE")
        invalid_value = [*rows[:-1], {**rows[-1], "value": float("nan")}]
        self.assertEqual(pnl_drawdown_strength(
            invalid_value, expected_dates=days, pnl_unit="SYNTHETIC_UNITS",
            expected_unit="SYNTHETIC_UNITS",
        )["reason_code"], "INVALID_PNL_VALUE")
        self.assertEqual(pnl_drawdown_strength(
            rows[:100], expected_dates=days[:100], pnl_unit="SYNTHETIC_UNITS",
            expected_unit="SYNTHETIC_UNITS",
        )["reason_code"], "INSUFFICIENT_PNL_HISTORY")

    def test_two_years_do_not_produce_stability(self):
        result = evaluate_quality(
            alpha_id="synthetic-a",
            alpha_scope={"region": "USA", "universe": "TOP3000", "delay": 1},
            metrics={"sharpe": 1.2, "returns": 0.1, "margin": 4, "turnover": 0.2},
            yearly_data=[
                {"year": 2023, "returns": 0.08, "sharpe": 1.1},
                {"year": 2024, "returns": -0.02, "sharpe": 0.4},
            ], pnl_rows=None, pnl_unit=None, expected_trading_dates=None,
            self_correlation={"is": {"correlation": 0.1}},
            calibration=_calibration(), as_of=date(2025, 6, 1),
            metric_units={"returns": "fraction", "margin": "bp", "turnover": "fraction"},
        )
        self.assertEqual(result["metrics"]["stability"]["status"], "UNAVAILABLE")
        self.assertEqual(result["metrics"]["stability"]["reason_code"], "INSUFFICIENT_COMPLETE_YEARS")

    def test_complete_synthetic_evidence_scores_all_eight_metrics(self):
        days = _dates()
        pnl = [{"date": day, "value": (-1.0 if index > 210 else 1.0)}
               for index, day in enumerate(days)]
        result = evaluate_quality(
            alpha_id="synthetic-complete",
            alpha_scope={"region": "USA", "universe": "TOP3000", "delay": 1},
            metrics={"sharpe": 1.4, "returns": .12, "margin": 6,
                     "turnover": .2, "fitness": 2.4},
            yearly_data=[
                {"year": year, "returns": .1, "sharpe": 1.1, "complete": True}
                for year in (2021, 2022, 2023)
            ], pnl_rows={"pnl": pnl}, pnl_unit="SYNTHETIC_UNITS",
            expected_trading_dates=days,
            self_correlation={"is": {"correlation": .1}},
            calibration=_calibration(), as_of=date(2025, 6, 1),
            metric_units={"returns": "fraction", "margin": "bp", "turnover": "fraction"},
        )
        self.assertEqual(result["quality"]["coverage"], 1.0)
        self.assertTrue(all(
            metric["status"] == "AVAILABLE"
            for metric in result["metrics"].values()
        ))

    def test_unknown_reference_and_missing_metrics_never_become_zero(self):
        result = evaluate_quality(
            alpha_id="synthetic-a",
            alpha_scope={"region": "USA", "universe": "TOP3000", "delay": 1},
            metrics={"sharpe": 1.2, "fitness": 9.0}, yearly_data=None,
            pnl_rows=None, pnl_unit=None, expected_trading_dates=None,
            self_correlation=None,
            calibration={"version": "AQAF-TEST-1", "scope": {"region": "USA"},
                         "metric_references": {}},
            as_of=date(2025, 6, 1),
        )
        self.assertEqual(result["metrics"]["sharpe"]["status"], "UNAVAILABLE")
        self.assertIsNone(result["metrics"]["sharpe"]["score"])
        self.assertEqual(result["quality"]["grade"], "U")
        self.assertEqual(result["quality"]["coverage"], 0.0)

    def test_margin_with_unknown_units_is_not_scored(self):
        result = evaluate_quality(
            alpha_id="synthetic-a",
            alpha_scope={"region": "USA", "universe": "TOP3000", "delay": 1},
            metrics={"margin": 5.0, "turnover": 0.2, "sharpe": 1.2},
            yearly_data=None, pnl_rows=None, pnl_unit=None,
            expected_trading_dates=None, self_correlation=None,
            calibration=_calibration(), as_of=date(2025, 6, 1),
        )
        self.assertEqual(result["metrics"]["margin"]["reason_code"], "UNIT_UNKNOWN")
        self.assertIsNone(result["metrics"]["margin"]["score"])

    def test_turnover_is_not_scored_without_matching_type_budget_unit(self):
        calibration = _calibration()
        calibration["turnover_budget_unit"] = "percent"
        result = evaluate_quality(
            alpha_id="synthetic-a",
            alpha_scope={"region": "USA", "universe": "TOP3000", "delay": 1},
            metrics={"sharpe": 1.2, "turnover": .2}, yearly_data=None,
            pnl_rows=None, pnl_unit=None, expected_trading_dates=None,
            self_correlation=None, calibration=calibration,
            as_of=date(2025, 6, 1),
            metric_units={"turnover": "fraction"},
        )
        self.assertIsNone(result["metrics"]["turnover"]["score"])
        self.assertEqual(
            result["metrics"]["turnover"]["reason_code"],
            "TURNOVER_BUDGET_UNAVAILABLE",
        )

    def test_fitness_does_not_change_quality_score(self):
        args = {
            "alpha_id": "synthetic-a",
            "alpha_scope": {"region": "USA", "universe": "TOP3000", "delay": 1},
            "metrics": {"sharpe": 1.4, "returns": .12, "margin": 6,
                        "turnover": .2, "fitness": .4},
            "yearly_data": [
                {"year": year, "returns": .1, "sharpe": 1.0 + year / 10000,
                 "complete": True}
                for year in (2021, 2022, 2023)
            ],
            "pnl_rows": None, "pnl_unit": None,
            "expected_trading_dates": None,
            "self_correlation": {"is": {"correlation": .1}},
            "calibration": _calibration(), "as_of": date(2025, 6, 1),
            "metric_units": {"returns": "fraction", "margin": "bp", "turnover": "fraction"},
        }
        first = evaluate_quality(**args)
        args["metrics"] = {**args["metrics"], "fitness": 99.0}
        second = evaluate_quality(**args)
        self.assertEqual(first["quality"]["q_mid"], second["quality"]["q_mid"])
        self.assertEqual(second["fitness_diagnostic"]["weight"], 0.0)

    def test_year_rows_without_completion_evidence_are_not_counted(self):
        result = evaluate_quality(
            alpha_id="synthetic-a",
            alpha_scope={"region": "USA", "universe": "TOP3000", "delay": 1},
            metrics={"sharpe": 1.2, "returns": 0.1, "margin": 4, "turnover": 0.2},
            yearly_data=[
                {"year": year, "returns": 0.1, "sharpe": 1.1}
                for year in (2021, 2022, 2023)
            ], pnl_rows=None, pnl_unit=None, expected_trading_dates=None,
            self_correlation=None, calibration=_calibration(),
            as_of=date(2025, 6, 1),
            metric_units={"returns": "fraction", "margin": "bp", "turnover": "fraction"},
        )
        self.assertEqual(result["metrics"]["stability"]["reason_code"], "INSUFFICIENT_COMPLETE_YEARS")

    def test_missing_inputs_widen_conservative_interval_without_zero_filling(self):
        result = evaluate_quality(
            alpha_id="synthetic-a",
            alpha_scope={"region": "USA", "universe": "TOP3000", "delay": 1},
            metrics={"sharpe": 1.2}, yearly_data=None, pnl_rows=None,
            pnl_unit=None, expected_trading_dates=None, self_correlation=None,
            calibration=_calibration(), as_of=date(2025, 6, 1),
            metric_units={},
        )
        self.assertIsNone(result["metrics"]["returns"]["score"])
        self.assertIsNone(result["metrics"]["margin"]["score"])
        self.assertIsNone(result["metrics"]["self_correlation"]["score"])
        self.assertLess(result["quality"]["q_low"], result["quality"]["q_mid"])
        self.assertGreater(result["quality"]["q_high"], result["quality"]["q_mid"])


if __name__ == "__main__":
    unittest.main()
