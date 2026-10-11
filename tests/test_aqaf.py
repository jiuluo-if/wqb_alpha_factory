import math
import unittest
from datetime import date, timedelta
from unittest import mock

from wqb_agent import research_api
from wqb_agent.alpha_quality import (
    CALIBRATION_ARTIFACT_SCHEMA,
    METRIC_WEIGHTS,
    TYPE_WEIGHTS,
    _calibration_digest,
    _correlation_value,
    build_calibration,
    evaluate_quality,
    pnl_drawdown_strength,
    resolve_alpha_type,
)
from wqb_agent.osmosis_allocation import (
    _portfolio_metrics,
    compare_osmosis_allocation_plans,
    preview_osmosis_allocation,
)
from wqb_agent.remote_evidence import decode_recordset


def _dates(count=252):
    start = date(2024, 1, 1)
    return [(start + timedelta(days=index)).isoformat() for index in range(count)]


def _complete_dates():
    dates = [
        (date(year, 1, 1) + timedelta(days=offset)).isoformat()
        for year in (2021, 2022, 2023)
        for offset in range(365)
    ]
    return dates + _dates()


def _calibration(alpha_id="synthetic-a"):
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
    artifact = {
        "status": "AVAILABLE",
        "schema": CALIBRATION_ARTIFACT_SCHEMA,
        "version": "AQAF-TEST-1",
        "reference_id": "synthetic-reference",
        "frozen_at": "2025-01-01T00:00:00Z",
        "source": "synthetic-fixture",
        "minimum_reference_count": 20,
        "scope": {"region": "USA", "universe": "TOP3000", "delay": 1},
        "pnl_unit": "SYNTHETIC_UNITS",
        "turnover_budget_unit": "fraction",
        "exclusion_policy": "LEAVE_CANDIDATE_OUT",
        "excluded_alpha_id": alpha_id,
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
    artifact["artifact_digest"] = _calibration_digest(artifact)
    return artifact


class AlphaQualityTests(unittest.TestCase):
    def test_correlation_pending_and_failed_statuses_win_over_nested_numbers(self):
        for payload in (
            {"status": "PENDING", "is": {"correlation": 0.1}},
            {"is": {"status": "FAILED", "correlation": 0.1}},
            {"data": {"status": "UNKNOWN", "correlation": 0.1}},
            {"status": "UNRECOGNIZED", "correlation": 0.1},
        ):
            with self.subTest(payload=payload):
                self.assertEqual(_correlation_value(payload)[0], None)

    def test_correlation_extracts_only_named_correlation_fields(self):
        self.assertEqual(_correlation_value({"is": {"correlation": .25}}), (.25, None))
        self.assertEqual(_correlation_value({"data": {"correlation": .25}}), (.25, None))
        self.assertIsNone(_correlation_value({"is": {"max": .25}})[0])
        self.assertIsNone(_correlation_value({
            "is": {}, "data": {"correlation": .25},
        })[0])

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
            {"pnl": rows}, expected_dates=days, pnl_unit="SYNTHETIC_UNITS",
            expected_unit="SYNTHETIC_UNITS",
        )
        scaled = pnl_drawdown_strength(
            {"pnl": [{**row, "value": row["value"] * 1000} for row in rows]},
            expected_dates=days, pnl_unit="SYNTHETIC_UNITS",
            expected_unit="SYNTHETIC_UNITS",
        )
        self.assertEqual(original["status"], "AVAILABLE")
        self.assertAlmostEqual(original["mdd_strength"], scaled["mdd_strength"])
        self.assertAlmostEqual(original["cdar_strength"], scaled["cdar_strength"])
        self.assertEqual(
            pnl_drawdown_strength(
                {"pnl": rows[::-1]}, expected_dates=days,
                pnl_unit="SYNTHETIC_UNITS", expected_unit="SYNTHETIC_UNITS",
            )["reason_code"],
            "PNL_DATE_ORDER_INVALID",
        )
        duplicate = [*rows[:-1], {**rows[-1], "date": rows[-2]["date"]}]
        self.assertEqual(pnl_drawdown_strength(
            {"pnl": duplicate}, expected_dates=days, pnl_unit="SYNTHETIC_UNITS",
            expected_unit="SYNTHETIC_UNITS",
        )["reason_code"], "DUPLICATE_PNL_DATE")
        invalid_value = [*rows[:-1], {**rows[-1], "value": float("nan")}]
        self.assertEqual(pnl_drawdown_strength(
            {"pnl": invalid_value}, expected_dates=days, pnl_unit="SYNTHETIC_UNITS",
            expected_unit="SYNTHETIC_UNITS",
        )["reason_code"], "INVALID_PNL_VALUE")
        self.assertEqual(pnl_drawdown_strength(
            {"pnl": rows[:100]}, expected_dates=days[:100], pnl_unit="SYNTHETIC_UNITS",
            expected_unit="SYNTHETIC_UNITS",
        )["reason_code"], "INSUFFICIENT_PNL_HISTORY")

    def test_official_recordset_decoding_is_shared_with_drawdown_scoring(self):
        days = _dates()
        payload = {
            "schema": {"properties": {"date": {}, "pnl": {}}},
            "records": [[day, (1.0 if index < 200 else -1.0)]
                        for index, day in enumerate(days)],
        }
        decoded = decode_recordset(payload)
        result = pnl_drawdown_strength(
            decoded, expected_dates=days, pnl_unit="SYNTHETIC_UNITS",
            expected_unit="SYNTHETIC_UNITS",
        )
        self.assertEqual(result["status"], "AVAILABLE")

    def test_yearly_data_counts_only_years_with_full_verified_calendar_and_pnl(self):
        dates = [
            (date(year, 1, 1) + timedelta(days=offset)).isoformat()
            for year in (2021, 2022, 2023)
            for offset in range(365)
            if (date(year, 1, 1) + timedelta(days=offset)).weekday() < 5
        ]
        pnl = [{"date": day, "pnl": 1.0} for day in dates]
        args = {
            "alpha_id": "synthetic-a",
            "alpha_scope": {"region": "USA", "universe": "TOP3000", "delay": 1},
            "metrics": {"sharpe": 1.2, "returns": .1, "margin": 4, "turnover": .2},
            "yearly_data": [
                {"year": year, "returns": .1, "sharpe": 1.1}
                for year in (2021, 2022, 2023)
            ],
            "pnl_rows": {"pnl": pnl}, "pnl_unit": "SYNTHETIC_UNITS",
            "expected_trading_dates": dates,
            "self_correlation": {"is": {"correlation": .1}},
            "calibration": _calibration(), "as_of": date(2024, 6, 1),
            "metric_units": {"returns": "fraction", "margin": "bp", "turnover": "fraction"},
        }
        result = evaluate_quality(**args)
        self.assertEqual(result["metrics"]["stability"]["status"], "AVAILABLE")
        deltas = [row for row in pnl if row["date"] != "2022-01-03"]
        args["pnl_rows"] = {"pnl": deltas}
        result = evaluate_quality(**args)
        self.assertEqual(result["metrics"]["stability"]["status"], "UNAVAILABLE")

    def test_offline_calibration_is_frozen_scoped_and_excludes_candidate(self):
        scope = {"region": "USA", "universe": "TOP3000", "delay": 1}
        metric_names = set(METRIC_WEIGHTS) | {"yearly_sharpe", "recent_sharpe_change"}
        units = {name: "ratio" for name in metric_names}
        floors = {name: .01 for name in metric_names}
        rows = [
            {"alpha_id": "candidate", "scope": scope,
             "metrics": {name: 999. for name in metric_names}},
            *[{
                "alpha_id": f"reference-{index}", "scope": scope,
                "metrics": {name: float(index) for name in metric_names},
            } for index in range(20)],
            {"alpha_id": "wrong-scope", "scope": {**scope, "delay": 2},
             "metrics": {name: 500. for name in metric_names}},
        ]
        options = {
            "reference_rows": rows, "candidate_alpha_id": "candidate",
            "scope": scope, "minimum_reference_count": 20,
            "scale_floors": floors, "units": units,
            "frozen_at": "2026-10-11T00:00:00Z", "reference_id": "offline-v1",
            "source": "synthetic-fixture", "version": "AQAF-CAL-1",
            "pnl_unit": "SYNTHETIC_UNITS", "turnover_budget_unit": "fraction",
            "turnover_budgets": {"GENERAL": .3, "HIGH_TURNOVER": .5,
                                  "LOW_TURNOVER": .15},
        }
        first = build_calibration(**options)
        second = build_calibration(**options)
        facade_artifact = research_api.build_aqaf_calibration(**options)
        self.assertEqual(first, second)
        self.assertEqual(facade_artifact, first)
        self.assertEqual(first["status"], "AVAILABLE")
        self.assertEqual(first["reference_count"], 20)
        self.assertEqual(first["excluded_alpha_id"], "candidate")
        self.assertRegex(first["artifact_digest"], r"^[0-9a-f]{64}$")
        self.assertNotIn("reference-0", str(first))
        self.assertEqual(first["metric_references"]["sharpe"]["median"], 9.5)

    def test_mutated_frozen_calibration_artifact_fails_closed(self):
        calibration = _calibration()
        calibration["metric_references"]["sharpe"]["median"] += 0.25
        result = evaluate_quality(
            alpha_id="synthetic-a",
            alpha_scope={"region": "USA", "universe": "TOP3000", "delay": 1},
            metrics={"sharpe": 1.2}, yearly_data=None, pnl_rows=None,
            pnl_unit=None, expected_trading_dates=None, self_correlation=None,
            calibration=calibration, as_of=date(2025, 6, 1),
        )
        self.assertEqual(result["quality"]["status"], "UNKNOWN")
        self.assertEqual(result["quality"]["grade"], "U")

    def test_calibration_for_another_candidate_does_not_score_this_candidate(self):
        result = evaluate_quality(
            alpha_id="synthetic-a",
            alpha_scope={"region": "USA", "universe": "TOP3000", "delay": 1},
            metrics={"sharpe": 1.2}, yearly_data=None, pnl_rows=None,
            pnl_unit=None, expected_trading_dates=None, self_correlation=None,
            calibration=_calibration("different-alpha"), as_of=date(2025, 6, 1),
        )
        self.assertEqual(result["quality"]["status"], "UNKNOWN")

    def test_economic_anomalies_prevent_a_grade_without_changing_weights(self):
        result = evaluate_quality(
            alpha_id="synthetic-a",
            alpha_scope={"region": "USA", "universe": "TOP3000", "delay": 1},
            metrics={"sharpe": 1.2, "returns": 0.0, "margin": -1.0,
                     "turnover": .2},
            yearly_data=None, pnl_rows=None, pnl_unit=None,
            expected_trading_dates=None,
            self_correlation={"is": {"correlation": .1}},
            calibration=_calibration(), as_of=date(2025, 6, 1),
            metric_units={"returns": "fraction", "margin": "bp", "turnover": "fraction"},
        )
        self.assertIn("NON_POSITIVE_RETURNS", result["economic_diagnostics"])
        self.assertIn("NEGATIVE_MARGIN", result["economic_diagnostics"])
        self.assertNotEqual(result["quality"]["grade"], "A")
        self.assertEqual(result["type"]["weights"], METRIC_WEIGHTS)
        extreme = evaluate_quality(
            alpha_id="synthetic-a",
            alpha_scope={"region": "USA", "universe": "TOP3000", "delay": 1},
            metrics={"sharpe": -2.5, "returns": 1.0, "margin": 1.0,
                     "turnover": .2, "fitness": 1.0},
            yearly_data=None, pnl_rows=None, pnl_unit=None,
            expected_trading_dates=None,
            self_correlation={"is": {"correlation": .1}},
            calibration=_calibration(), as_of=date(2025, 6, 1),
            metric_units={"returns": "fraction", "margin": "bp", "turnover": "fraction"},
        )
        self.assertIn("RETURNS_OUTSIDE_CALIBRATION_RANGE", extreme["economic_diagnostics"])
        self.assertIn("SHARPE_RETURNS_SIGN_CONFLICT", extreme["economic_diagnostics"])
        self.assertIn("FITNESS_SHARPE_SIGN_CONFLICT", extreme["economic_diagnostics"])
        self.assertNotEqual(extreme["quality"]["grade"], "A")

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
        self.assertEqual(
            result["metrics"]["stability"]["reason_code"],
            "YEAR_COMPLETENESS_EVIDENCE_UNKNOWN",
        )

    def test_complete_synthetic_evidence_scores_all_eight_metrics(self):
        days = _complete_dates()
        pnl = [{"date": day, "value": (-1.0 if index % 2 else 1.0)}
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
            calibration=_calibration("synthetic-complete"), as_of=date(2025, 6, 1),
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
        self.assertEqual(
            result["metrics"]["stability"]["reason_code"],
            "YEAR_COMPLETENESS_EVIDENCE_UNKNOWN",
        )

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
        self.assertEqual(first["status"], "INTERNAL_PREVIEW")
        self.assertEqual(first["points"], second["points"])
        self.assertEqual(sum(row["points"] for row in first["allocations"]), 10000)
        self.assertTrue(all(isinstance(row["points"], int) for row in first["allocations"]))
        self.assertEqual(
            sum(row["normalized_points"] for row in first["allocations"]), 10000,
        )
        self.assertAlmostEqual(sum(row["weight"] for row in first["allocations"]), 1.0)
        for allocation in first["allocations"]:
            self.assertEqual(allocation["target_weight"], allocation["weight"])
            self.assertEqual(allocation["integerized_weight"], allocation["points"] / 10_000)
        self.assertEqual(first["platform_rules_status"], "INTERNAL_PREVIEW")
        self.assertNotIn("platform_points_total", first)

    def test_unsubmitted_and_wrong_scope_candidates_are_excluded(self):
        rows = _candidates()
        rows[0]["submission_status"] = "NOT_SUBMITTED"
        rows[1]["scope"] = {"region": "EUR", "delay": 1}
        result = preview_osmosis_allocation(rows, scope=SCOPE)
        self.assertEqual(result["status"], "INTERNAL_PREVIEW")
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

    def test_osmosis_reads_the_same_official_recordset_daily_pnl_shape(self):
        rows = _candidates()
        for item in rows:
            days = item["pnl"]["expected_dates"]
            values = [row["value"] for row in item["pnl"]["rows"]]
            item["pnl"]["rows"] = {
                "schema": {"properties": {"date": {}, "pnl": {}}},
                "records": [[day, value] for day, value in zip(days, values)],
            }
        result = preview_osmosis_allocation(rows, scope=SCOPE)
        self.assertEqual(result["status"], "INTERNAL_PREVIEW")

    def test_zero_drawdown_series_has_undefined_correlation(self):
        rows = _candidates()
        for item in rows:
            item["pnl"]["rows"] = [
                {"date": day, "pnl": 0.0}
                for day in item["pnl"]["expected_dates"]
            ]
        result = preview_osmosis_allocation(rows, scope=SCOPE)
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertIsNone(result["allocations"])

    def test_nonconvergent_weights_and_integerization_constraints_fail_closed(self):
        nonconvergent = preview_osmosis_allocation(
            _candidates(), scope=SCOPE, correlation_lambda=100,
        )
        self.assertEqual(nonconvergent["status"], "UNKNOWN")
        self.assertIn("WEIGHT_ITERATION_NOT_CONVERGED", nonconvergent["reason_codes"])
        too_many_positive = preview_osmosis_allocation(
            _candidates(), scope=SCOPE, min_positive_alphas=11,
        )
        self.assertEqual(too_many_positive["status"], "INFEASIBLE")
        self.assertIn("MINIMUM_POSITIVE_ALPHA_COUNT_UNMET", too_many_positive["reason_codes"])

    def test_portfolio_diagnostic_uses_same_requested_window_as_correlations(self):
        result = preview_osmosis_allocation(
            _candidates(), scope=SCOPE, min_correlation_observations=130,
        )
        self.assertEqual(result["status"], "INTERNAL_PREVIEW")
        self.assertGreaterEqual(result["portfolio_metrics"]["observations"], 130)

    def test_portfolio_metric_enforces_the_requested_correlation_window(self):
        days = _dates(125)
        candidates = [{"alpha_id": "alpha-a", "family": {"id": "family-a"}}]
        series = {"alpha-a": {
            "unit": "SYNTHETIC_UNITS",
            "values": {day: float(index + 1) for index, day in enumerate(days)},
        }}
        too_short = _portfolio_metrics(
            candidates, {"alpha-a": 10_000}, series, {}, {}, min_observations=130,
        )
        enough = _portfolio_metrics(
            candidates, {"alpha-a": 10_000}, series, {}, {}, min_observations=120,
        )
        self.assertEqual(too_short["reason_code"], "PORTFOLIO_OVERLAP_INSUFFICIENT")
        self.assertEqual(enough["status"], "AVAILABLE")

    def test_family_cap_is_enforced_and_unknown_family_is_not_assumed_unique(self):
        rows = _candidates()
        for row in rows[:3]:
            row["family"]["id"] = "family-shared"
        result = preview_osmosis_allocation(rows, scope=SCOPE)
        self.assertEqual(result["status"], "INTERNAL_PREVIEW")
        family_aliases = {
            row["family_id"] for row in result["allocations"]
            if row["alpha_id"] in {"alpha-00", "alpha-01", "alpha-02"}
        }
        self.assertEqual(len(family_aliases), 1)
        self.assertLessEqual(result["family_concentration"][family_aliases.pop()], .25)
        rows[0]["family"] = {"id": None}
        unknown = preview_osmosis_allocation(rows, scope=SCOPE)
        self.assertEqual(unknown["status"], "INTERNAL_PREVIEW")
        self.assertEqual(unknown["excluded"]["alpha-00"], "FAMILY_PROVENANCE_UNKNOWN")

    def test_platform_total_is_never_projected_from_rule_input(self):
        result = preview_osmosis_allocation(
            _candidates(), scope=SCOPE,
            platform_rules={
                "status": "LIVE_VERIFIED", "source": "BRAIN_ACCOUNT",
                "fetched_at": "2026-01-01T00:00:00Z", "point_total": 100000,
            },
        )
        self.assertEqual(result["status"], "INTERNAL_PREVIEW")
        self.assertNotIn("platform_points_total", result)
        self.assertEqual(result["platform_rule_evidence_status"], "UNKNOWN")
        self.assertIn("ACCOUNT_AUTHORITY_UNKNOWN", result["platform_rule_diagnostics"])
        self.assertTrue(all(row["platform_points"] is None for row in result["allocations"]))

    def test_three_comparison_plans_report_unknown_costs_and_risk_metrics(self):
        result = compare_osmosis_allocation_plans(_candidates(), scope=SCOPE)
        self.assertEqual(set(result["plans"]), {"equal", "quality_only", "risk_adjusted"})
        risk = result["plans"]["risk_adjusted"]
        self.assertEqual(risk["status"], "INTERNAL_PREVIEW")
        self.assertEqual(sum(item["points"] for item in risk["allocations"]), 10000)
        self.assertEqual(risk["portfolio_metrics"]["cost_sensitivity"], "UNKNOWN")

    def test_highly_correlated_candidates_are_reported_as_concentration_risk(self):
        rows = _candidates()
        rows[1]["pnl"]["rows"] = [dict(row) for row in rows[0]["pnl"]["rows"]]
        result = preview_osmosis_allocation(rows, scope=SCOPE)
        self.assertEqual(result["status"], "INTERNAL_PREVIEW")
        self.assertGreater(result["portfolio_metrics"]["average_pairwise_correlation"], 0)
        self.assertIn("average_common_drawdown_correlation", result["portfolio_metrics"])

    def test_ambiguous_scope_returns_explicit_infeasible_status(self):
        result = preview_osmosis_allocation(_candidates(), scope={})
        self.assertEqual(result["status"], "INFEASIBLE")
        self.assertIn("SCOPE_UNKNOWN", result["reason_codes"])

class _ReadOnlyClient:
    def __init__(self, submitted=True):
        self.submitted = submitted
        self.write_calls = 0

    def get_alpha(self, alpha_id):
        alpha = {
            "id": alpha_id,
            "status": "ACTIVE",
            "expression": "SYNTHETIC_EXPRESSION_SENTINEL",
            "settings": {"region": "USA", "universe": "TOP3000", "delay": 1},
            "is": {"sharpe": 1.2, "fitness": 2.0, "returns": .1,
                   "turnover": .2, "margin": 3.0,
                   "checks": [{"name": "synthetic-check", "result": "PASS"}]},
        }
        if self.submitted:
            alpha["dateSubmitted"] = "2025-01-01T00:00:00Z"
        return alpha

    def get_aggregates(self, alpha_id):
        return {"is": {"yearlyData": []}}

    def get_pnl(self, alpha_id):
        return {"pnl": []}

    def get_self_correlation(self, alpha_id):
        return {"is": {"correlation": .2}}

    def list_alpha_recordsets(self, alpha_id):
        return []

    def get_recordset(self, *args, **kwargs):
        return None


class AqafResearchApiTests(unittest.TestCase):
    def test_checks_pass_does_not_replace_submission_fact(self):
        result = research_api.evaluate_submitted_alpha(
            "synthetic-alpha", client=_ReadOnlyClient(submitted=False),
            calibration=None, as_of=date(2025, 6, 1),
        )
        self.assertEqual(result["submission_status"], "SUBMISSION_UNKNOWN")
        self.assertEqual(result["quality"]["grade"], "U")
        self.assertEqual(result["reason_code"], "SUBMISSION_UNKNOWN")

    def test_unsubmitted_alpha_stops_after_cheap_detail_read(self):
        class CountingClient(_ReadOnlyClient):
            def __init__(self):
                super().__init__(submitted=False)
                self.deep_reads = 0

            def get_aggregates(self, alpha_id):
                self.deep_reads += 1
                return super().get_aggregates(alpha_id)

            def get_pnl(self, alpha_id):
                self.deep_reads += 1
                return super().get_pnl(alpha_id)

            def get_self_correlation(self, alpha_id):
                self.deep_reads += 1
                return super().get_self_correlation(alpha_id)

        client = CountingClient()
        research_api.evaluate_submitted_alpha(
            "synthetic-alpha", client=client, calibration=None,
            as_of=date(2025, 6, 1),
        )
        self.assertEqual(client.deep_reads, 0)

    def test_conflicting_submission_status_and_date_is_unknown(self):
        class ConflictingClient(_ReadOnlyClient):
            def get_alpha(self, alpha_id):
                alpha = super().get_alpha(alpha_id)
                alpha["status"] = "UNSUBMITTED"
                alpha["dateSubmitted"] = "2025-01-01T00:00:00Z"
                return alpha

        result = research_api.evaluate_submitted_alpha(
            "synthetic-alpha", client=ConflictingClient(submitted=False),
            calibration=None, as_of=date(2025, 6, 1),
        )
        self.assertEqual(result["submission_status"], "SUBMISSION_UNKNOWN")

    def test_date_submitted_authorizes_evaluation_but_missing_units_stay_unknown(self):
        result = research_api.evaluate_submitted_alpha(
            "synthetic-alpha", client=_ReadOnlyClient(), calibration=None,
            as_of=date(2025, 6, 1),
        )
        self.assertEqual(result["submission_status"], "SUBMITTED")
        self.assertEqual(result["quality"]["status"], "UNKNOWN")
        self.assertEqual(result["quality"]["grade"], "U")
        self.assertEqual(result["source"], "BRAIN_LIVE")
        self.assertNotIn("SYNTHETIC_EXPRESSION_SENTINEL", str(result))

    def test_live_facade_scores_complete_synthetic_evidence(self):
        class CompleteClient(_ReadOnlyClient):
            def __init__(self):
                super().__init__(submitted=True)
                self.days = _complete_dates()

            def get_aggregates(self, alpha_id):
                return {"is": {"yearlyData": [
                    {"year": year, "returns": .1, "sharpe": 1.1, "complete": True}
                    for year in (2021, 2022, 2023)
                ]}}

            def get_pnl(self, alpha_id):
                return {"pnl": [
                    {"date": day, "value": (-1.0 if index % 2 else 1.0)}
                    for index, day in enumerate(self.days)
                ]}

        client = CompleteClient()
        result = research_api.evaluate_submitted_alpha(
            "synthetic-alpha", client=client, calibration=_calibration("synthetic-alpha"),
            metric_units={
                "returns": {"unit": "fraction", "source": "SYNTHETIC_SCHEMA"},
                "margin": {"unit": "bp", "source": "SYNTHETIC_SCHEMA"},
                "turnover": {"unit": "fraction", "source": "SYNTHETIC_SCHEMA"},
            },
            expected_trading_dates=client.days,
            expected_trading_dates_source="SYNTHETIC_CALENDAR",
            pnl_unit="SYNTHETIC_UNITS", pnl_unit_source="SYNTHETIC_SCHEMA",
            as_of=date(2025, 6, 1),
        )
        self.assertEqual(result["submission_status"], "SUBMITTED")
        self.assertEqual(result["quality"]["coverage"], 1.0)
        self.assertTrue(all(
            metric["status"] == "AVAILABLE"
            for metric in result["metrics"].values()
        ))

    def test_batch_ids_are_bounded_and_unique(self):
        with self.assertRaisesRegex(ValueError, "unique"):
            research_api.evaluate_submitted_alphas(
                ["a", "a"], client=_ReadOnlyClient(), calibration=None,
            )
        with self.assertRaisesRegex(ValueError, "at most"):
            research_api.evaluate_submitted_alphas(
                [f"a-{index}" for index in range(101)],
                client=_ReadOnlyClient(), calibration=None,
            )

    def test_evaluation_calls_only_read_methods(self):
        client = _ReadOnlyClient()
        research_api.evaluate_submitted_alpha(
            "synthetic-alpha", client=client, calibration=None,
            as_of=date(2025, 6, 1),
        )
        self.assertEqual(client.write_calls, 0)

    def test_failed_brain_check_caps_quality_and_allocation_eligibility(self):
        class FailedCheckClient(_ReadOnlyClient):
            def get_alpha(self, alpha_id):
                alpha = super().get_alpha(alpha_id)
                alpha["is"]["checks"] = [{"name": "synthetic-check", "result": "FAIL"}]
                return alpha

        result = research_api.evaluate_submitted_alpha(
            "synthetic-alpha", client=FailedCheckClient(), calibration=None,
            as_of=date(2025, 6, 1),
        )
        self.assertEqual(result["quality"]["grade"], "U")
        self.assertIn("BRAIN_CHECK_FAILED", result["risk_alerts"])

    def test_verified_failed_check_overrides_calculated_a_grade(self):
        calculated_a = {
            "quality": {"status": "AVAILABLE", "grade": "A", "q_mid": 90.0,
                        "q_low": 80.0, "q_high": 95.0, "coverage": .95,
                        "config_version": "synthetic-v1"},
            "type": {"status": "EXPLICIT", "label": "GENERAL", "weights": METRIC_WEIGHTS},
            "metrics": {}, "risk_alerts": [],
            "fitness_diagnostic": {"value": 2.0, "weight": 0.0},
        }
        class FailedCheckClient(_ReadOnlyClient):
            def get_alpha(self, alpha_id):
                alpha = super().get_alpha(alpha_id)
                alpha["is"]["checks"] = [{"name": "synthetic-check", "status": "FAILED"}]
                return alpha

        with mock.patch("wqb_agent.research_api.evaluate_quality", return_value=calculated_a):
            result = research_api.evaluate_submitted_alpha(
                "synthetic-alpha", client=FailedCheckClient(),
                calibration=_calibration("synthetic-alpha"),
                as_of=date(2025, 6, 1),
            )
        self.assertEqual(result["quality"]["grade"], "U")
        self.assertEqual(result["quality"]["status"], "UNKNOWN")
        self.assertEqual(result["quality"]["risk_grade_cap"], "U")
        self.assertEqual(result["status"], "UNKNOWN")
        allocation = preview_osmosis_allocation([{
            "alpha_id": "synthetic-alpha",
            "submission_status": result["submission_status"],
            "scope": result["scope"], "quality": result["quality"],
            "disqualifying_risks": result["risk_alerts"],
            "family": {"id": "synthetic-family", "source": "synthetic-fixture"},
        }], scope={"region": "USA", "delay": 1})
        self.assertNotIn("synthetic-alpha", allocation.get("points", {}))

    def test_failed_check_compat_shapes_are_detected_and_unknown_shapes_stay_unknown(self):
        for item in (
            {"name": "synthetic-check", "pass": False},
            {"name": "synthetic-check", "result": "FAIL"},
            {"name": "synthetic-check", "status": "FAILED"},
        ):
            with self.subTest(item=item):
                class CheckClient(_ReadOnlyClient):
                    def get_alpha(self, alpha_id):
                        alpha = super().get_alpha(alpha_id)
                        alpha["is"]["checks"] = [item]
                        return alpha

                result = research_api.evaluate_submitted_alpha(
                    "synthetic-alpha", client=CheckClient(), calibration=None,
                    as_of=date(2025, 6, 1),
                )
                self.assertIn("BRAIN_CHECK_FAILED", result["risk_alerts"])

        class UnknownCheckClient(_ReadOnlyClient):
            def get_alpha(self, alpha_id):
                alpha = super().get_alpha(alpha_id)
                alpha["is"]["checks"] = [{"name": "synthetic-check", "outcome": "bad"}]
                return alpha

        unknown = research_api.evaluate_submitted_alpha(
            "synthetic-alpha", client=UnknownCheckClient(), calibration=None,
            as_of=date(2025, 6, 1),
        )
        self.assertNotIn("BRAIN_CHECK_FAILED", unknown["risk_alerts"])
        self.assertIn("BRAIN_CHECK_STATUS_UNKNOWN", unknown["risk_alerts"])

        class MissingChecksClient(_ReadOnlyClient):
            def get_alpha(self, alpha_id):
                alpha = super().get_alpha(alpha_id)
                alpha["is"].pop("checks")
                return alpha

        missing = research_api.evaluate_submitted_alpha(
            "synthetic-alpha", client=MissingChecksClient(), calibration=None,
            as_of=date(2025, 6, 1),
        )
        self.assertIn("BRAIN_CHECK_STATUS_UNKNOWN", missing["risk_alerts"])

    def test_quality_tools_are_full_profile_only(self):
        manifest = {item["name"]: item for item in research_api.research_tool_manifest("full")}
        core = {item["name"] for item in research_api.research_tool_manifest("core")}
        for name in (
            "build_aqaf_calibration",
            "evaluate_submitted_alpha", "evaluate_submitted_alphas",
            "preview_osmosis_allocation", "compare_osmosis_allocation_plans",
        ):
            expected_mode = "PURE" if name == "build_aqaf_calibration" else "READ_ONLY"
            self.assertEqual(manifest[name]["mode"], expected_mode)
            self.assertNotIn(name, core)


if __name__ == "__main__":
    unittest.main()
