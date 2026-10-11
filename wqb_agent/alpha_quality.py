"""Pure AQAF scoring for explicitly supplied, provenance-labeled evidence.

This module never reads the network and never treats missing evidence as zero.
The frozen calibration object is an input so the candidate set cannot silently
change its own score scale.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from typing import Any

METRIC_WEIGHTS = {
    "sharpe": 25,
    "returns": 10,
    "mdd_strength": 14,
    "cdar_strength": 6,
    "margin": 15,
    "turnover": 5,
    "stability": 15,
    "self_correlation": 10,
}
WEIGHT_SCALE = 100

TYPE_WEIGHTS = {
    "GENERAL": dict(METRIC_WEIGHTS),
    "HIGH_TURNOVER": {
        "sharpe": 20, "returns": 8, "mdd_strength": 12,
        "cdar_strength": 5, "margin": 25, "turnover": 10,
        "stability": 10, "self_correlation": 10,
    },
    "TREND_MOMENTUM": {
        "sharpe": 20, "returns": 8, "mdd_strength": 20,
        "cdar_strength": 10, "margin": 10, "turnover": 3,
        "stability": 19, "self_correlation": 10,
    },
    "LOW_TURNOVER": {
        "sharpe": 25, "returns": 12, "mdd_strength": 14,
        "cdar_strength": 6, "margin": 8, "turnover": 3,
        "stability": 22, "self_correlation": 10,
    },
    "EVENT": {
        "sharpe": 20, "returns": 10, "mdd_strength": 15,
        "cdar_strength": 8, "margin": 17, "turnover": 5,
        "stability": 17, "self_correlation": 8,
    },
    "DEFENSIVE": {
        "sharpe": 15, "returns": 5, "mdd_strength": 25,
        "cdar_strength": 12, "margin": 8, "turnover": 3,
        "stability": 17, "self_correlation": 15,
    },
}

_DIRECTIONS = {
    "sharpe": 1.0,
    "returns": 1.0,
    "mdd_strength": -1.0,
    "cdar_strength": -1.0,
    "margin": 1.0,
    "stability": 1.0,
    "self_correlation": -1.0,
}
MAX_PNL_OBSERVATIONS = 10_000


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _clip(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _smooth(value: float, low: float, high: float) -> float:
    if high <= low:
        raise ValueError("turnover interpolation range must increase")
    return _clip((value - low) / (high - low), 0.0, 1.0)


def resolve_alpha_type(
    *, turnover: Any, type_label: str | None = None,
    type_source: str | None = None,
) -> dict[str, Any]:
    """Resolve a trusted economic type or interpolate using turnover only."""
    label = str(type_label or "").strip().upper()
    if label in TYPE_WEIGHTS:
        if not type_source or not str(type_source).strip():
            return {
                "status": "UNKNOWN", "label": "UNKNOWN",
                "reason_code": "TYPE_PROVENANCE_REQUIRED",
            }
        source_label = str(type_source).strip().upper()
        if (len(source_label) > 48 or not source_label.isascii()
                or not source_label.replace("_", "").isalnum()):
            return {
                "status": "UNKNOWN", "label": "UNKNOWN",
                "reason_code": "TYPE_PROVENANCE_INVALID",
            }
        return {
            "status": "EXPLICIT", "label": label,
            "source": source_label,
            "weights": dict(TYPE_WEIGHTS[label]),
            "blend_weights": {label: 1.0},
        }
    turnover_value = _number(turnover)
    if turnover_value is None or turnover_value < 0:
        return {
            "status": "UNKNOWN", "label": "UNKNOWN",
            "reason_code": "TURNOVER_TYPE_EVIDENCE_UNAVAILABLE",
        }
    low = 1.0 - _smooth(turnover_value, 0.08, 0.15)
    high = _smooth(turnover_value, 0.35, 0.50)
    general = 1.0 - low - high
    # Roundoff at the shared boundaries must not create a negative mixture.
    general = max(0.0, general)
    blend = {
        "LOW_TURNOVER": low,
        "GENERAL": general,
        "HIGH_TURNOVER": high,
    }
    weights = {key: 0.0 for key in METRIC_WEIGHTS}
    for kind, share in blend.items():
        for metric, weight in TYPE_WEIGHTS[kind].items():
            weights[metric] += share * weight
    last_metric = "self_correlation"
    weights[last_metric] = WEIGHT_SCALE - sum(
        value for metric, value in weights.items() if metric != last_metric
    )
    return {
        "status": "TURNOVER_INTERPOLATED", "label": "TURNOVER_INTERPOLATED",
        "source": "BRAIN_ALPHA_IS.turnover",
        "blend_weights": blend, "weights": weights,
    }


def _reference(calibration: Any, metric: str, unit: str) -> tuple[Mapping[str, Any] | None, str | None]:
    if not isinstance(calibration, Mapping):
        return None, "CALIBRATION_UNAVAILABLE"
    minimum = calibration.get("minimum_reference_count")
    if (isinstance(minimum, bool) or not isinstance(minimum, int)
            or minimum < 1):
        return None, "CALIBRATION_PROVENANCE_INCOMPLETE"
    refs = calibration.get("metric_references")
    item = refs.get(metric) if isinstance(refs, Mapping) else None
    if not isinstance(item, Mapping):
        return None, "REFERENCE_UNAVAILABLE"
    median = _number(item.get("median"))
    mad = _number(item.get("mad"))
    floor = _number(item.get("scale_floor"))
    if median is None or mad is None or floor is None or mad < 0 or floor <= 0:
        return None, "REFERENCE_SCHEMA_INVALID"
    if str(item.get("unit") or "") != unit:
        return None, "UNIT_MISMATCH"
    sample_count = item.get("sample_count")
    if (isinstance(sample_count, bool) or not isinstance(sample_count, int)
            or sample_count < minimum or not str(item.get("source") or "").strip()):
        return None, "REFERENCE_SAMPLE_OR_SOURCE_UNAVAILABLE"
    return item, None


def _normalize(
    value: Any, *, metric: str, unit: str, calibration: Any,
    direction: float | None = None,
) -> dict[str, Any]:
    number = _number(value)
    if number is None:
        return {"score": None, "status": "UNAVAILABLE", "reason_code": "VALUE_UNAVAILABLE"}
    ref, reason = _reference(calibration, metric, unit)
    if ref is None:
        return {"score": None, "status": "UNAVAILABLE", "reason_code": reason}
    scale = max(1.4826 * float(ref["mad"]), float(ref["scale_floor"]))
    orient = _DIRECTIONS.get(metric, 1.0) if direction is None else direction
    z = orient * (number - float(ref["median"])) / scale
    score = 100.0 / (1.0 + math.exp(-_clip(z, -4.0, 4.0) / 1.5))
    return {"score": score, "status": "AVAILABLE", "reason_code": None}


def _extract_yearly_data(payload: Any) -> list[Mapping[str, Any]]:
    if isinstance(payload, Mapping):
        nested = payload.get("is")
        if isinstance(nested, Mapping):
            payload = nested.get("yearlyData")
        elif isinstance(payload.get("yearlyData"), list):
            payload = payload.get("yearlyData")
        elif isinstance(payload.get("rows"), list):
            payload = payload.get("rows")
    if not isinstance(payload, list):
        return []
    return [row for row in payload if isinstance(row, Mapping)]


def _quantile(values: Sequence[float], probability: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + fraction * (ordered[upper] - ordered[lower])


def _stability(
    yearly_data: Any, *, calibration: Any, as_of: date,
) -> dict[str, Any]:
    rows = _extract_yearly_data(yearly_data)
    yearly: list[tuple[int, float, float]] = []
    for row in rows:
        year = row.get("year")
        try:
            year = int(year)
        except (TypeError, ValueError, OverflowError):
            continue
        returns = _number(row.get("returns"))
        sharpe = _number(row.get("sharpe"))
        explicit_complete = row.get("complete") is True
        start = row.get("start_date")
        end = row.get("end_date")
        if isinstance(start, str) and isinstance(end, str):
            explicit_complete = explicit_complete or (
                start[:10] == f"{year:04d}-01-01"
                and end[:10] == f"{year:04d}-12-31"
            )
        if (year < as_of.year and explicit_complete
                and returns is not None and sharpe is not None):
            yearly.append((year, returns, sharpe))
    yearly.sort(key=lambda row: row[0])
    if len(yearly) < 3 or len({row[0] for row in yearly}) != len(yearly):
        return {"value": None, "status": "UNAVAILABLE",
                "reason_code": "INSUFFICIENT_COMPLETE_YEARS"}
    positive_years = sum(1 for _, returns, _ in yearly if returns > 0) / len(yearly)
    yearly_sharpes = [row[2] for row in yearly]
    weak = _normalize(
        _quantile(yearly_sharpes, 0.25), metric="yearly_sharpe",
        unit="ratio", calibration=calibration,
    )
    earlier_median = _quantile(yearly_sharpes[:-1], 0.5)
    change = yearly_sharpes[-1] - earlier_median
    recent = _normalize(
        change, metric="recent_sharpe_change", unit="ratio",
        calibration=calibration,
    )
    if weak["score"] is None or recent["score"] is None:
        return {"value": None, "status": "UNAVAILABLE",
                "reason_code": weak["reason_code"] or recent["reason_code"]}
    return {
        "value": 0.50 * positive_years * 100.0
        + 0.30 * weak["score"] + 0.20 * recent["score"],
        "status": "AVAILABLE", "reason_code": None,
        "years": [row[0] for row in yearly],
        "method": "PREVIOUS_CALENDAR_YEARS_NOT_OOS",
    }


def _correlation_value(payload: Any) -> tuple[float | None, str | None]:
    if not isinstance(payload, Mapping):
        return None, "SELF_CORRELATION_UNAVAILABLE"
    if str(payload.get("status") or "").upper() in {
        "PENDING", "RUNNING", "QUEUED", "UNSETTLED", "UNKNOWN",
    }:
        return None, "SELF_CORRELATION_PENDING"
    body = payload.get("is")
    if not isinstance(body, Mapping):
        body = payload
    value = _number(body.get("correlation"))
    if value is None or not -1.0 <= value <= 1.0:
        return None, "SELF_CORRELATION_SCHEMA_OR_RANGE_UNKNOWN"
    return value, None


def _daily_rows(payload: Any) -> list[Mapping[str, Any]]:
    if isinstance(payload, Mapping):
        direct = payload.get("pnl")
        if isinstance(direct, list):
            payload = direct
        else:
            rows = payload.get("records")
            schema = payload.get("schema")
            props = schema.get("properties") if isinstance(schema, Mapping) else None
            if not isinstance(rows, list) or not isinstance(props, Mapping):
                return []
            names = list(props)
            payload = [
                ({name: item.get(name) for name in names} if isinstance(item, Mapping)
                 else {name: item[index] if index < len(item) else None
                       for index, name in enumerate(names)}
                 if isinstance(item, (list, tuple)) else {})
                for item in rows
            ]
    if not isinstance(payload, list):
        return []
    return [row for row in payload if isinstance(row, Mapping)]


def pnl_drawdown_strength(
    pnl_rows: Any, *, expected_dates: Sequence[str] | None,
    pnl_unit: str | None, expected_unit: str | None,
) -> dict[str, Any]:
    """Calculate scale-invariant MDD/CDaR only on a proved 252-day window."""
    if not pnl_unit or not expected_unit or pnl_unit != expected_unit:
        return {"status": "UNAVAILABLE", "reason_code": "PNL_UNIT_UNKNOWN"}
    if not isinstance(expected_dates, Sequence) or isinstance(expected_dates, (str, bytes)):
        return {"status": "UNAVAILABLE", "reason_code": "TRADING_CALENDAR_UNKNOWN"}
    rows = _daily_rows(pnl_rows)
    if len(rows) > MAX_PNL_OBSERVATIONS or len(expected_dates) > MAX_PNL_OBSERVATIONS:
        return {"status": "UNAVAILABLE", "reason_code": "PNL_HISTORY_EXCEEDS_SAFE_LIMIT"}
    if len(rows) < 252 or len(expected_dates) < 252:
        return {"status": "UNAVAILABLE", "reason_code": "INSUFFICIENT_PNL_HISTORY"}
    parsed: list[tuple[str, float]] = []
    for row in rows:
        raw_day = row.get("date")
        raw_value = row.get("pnl", row.get("value"))
        if not isinstance(raw_day, str):
            return {"status": "UNAVAILABLE", "reason_code": "INVALID_PNL_DATE"}
        try:
            if len(raw_day) == 10:
                normalized_day = date.fromisoformat(raw_day).isoformat()
            else:
                parsed_timestamp = datetime.fromisoformat(raw_day.replace("Z", "+00:00"))
                normalized_day = parsed_timestamp.date().isoformat()
        except ValueError:
            return {"status": "UNAVAILABLE", "reason_code": "INVALID_PNL_DATE"}
        value = _number(raw_value)
        if value is None:
            return {"status": "UNAVAILABLE", "reason_code": "INVALID_PNL_VALUE"}
        parsed.append((normalized_day, value))
    dates = [row[0] for row in parsed]
    try:
        expected = []
        for item in expected_dates:
            value = str(item)
            if len(value) != 10:
                raise ValueError("calendar day must be YYYY-MM-DD")
            expected.append(date.fromisoformat(value).isoformat())
    except ValueError:
        return {"status": "UNAVAILABLE", "reason_code": "TRADING_CALENDAR_INVALID"}
    if len(dates) != len(set(dates)):
        return {"status": "UNAVAILABLE", "reason_code": "DUPLICATE_PNL_DATE"}
    if expected != sorted(expected) or len(expected) != len(set(expected)):
        return {"status": "UNAVAILABLE", "reason_code": "TRADING_CALENDAR_INVALID"}
    if dates != sorted(dates) or dates[-252:] != expected[-252:]:
        return {"status": "UNAVAILABLE", "reason_code": "PNL_DATE_ORDER_INVALID"
                if dates != sorted(dates) else "PNL_DATE_COVERAGE_INCOMPLETE"}
    values = [value for _, value in parsed[-252:]]
    mean = sum(values) / len(values)
    variance = sum((value - mean) ** 2 for value in values) / len(values)
    volatility = math.sqrt(variance * 252.0)
    if volatility <= 0 or not math.isfinite(volatility):
        return {"status": "UNAVAILABLE", "reason_code": "PNL_ZERO_OR_INVALID_VOLATILITY"}
    cumulative = 0.0
    peak = 0.0
    drawdowns = []
    for value in values:
        cumulative += value
        if not math.isfinite(cumulative):
            return {"status": "UNAVAILABLE", "reason_code": "PNL_CUMULATION_OVERFLOW"}
        peak = max(peak, cumulative)
        drawdowns.append(peak - cumulative)
    mdd = max(drawdowns)
    threshold = _quantile(drawdowns, 0.90)
    cdar = sum(value for value in drawdowns if value >= threshold) / sum(
        1 for value in drawdowns if value >= threshold
    )
    mdd_strength = mdd / volatility
    cdar_strength = cdar / volatility
    if not math.isfinite(mdd_strength) or not math.isfinite(cdar_strength):
        return {"status": "UNAVAILABLE", "reason_code": "PNL_DRAWDOWN_OVERFLOW"}
    return {
        "status": "AVAILABLE", "reason_code": None,
        "mdd_strength": mdd_strength,
        "cdar_strength": cdar_strength,
        "volatility": volatility,
        "window_observations": 252,
        "unit": "SCALE_INVARIANT_PNL_STRENGTH",
    }


def _metric_result(value: Any, metric: str, calibration: Any, unit: str) -> dict[str, Any]:
    normalized = _normalize(value, metric=metric, unit=unit, calibration=calibration)
    source = {
        "sharpe": "BRAIN_ALPHA_IS",
        "returns": "BRAIN_ALPHA_IS",
        "margin": "BRAIN_ALPHA_IS",
        "turnover": "BRAIN_ALPHA_IS",
        "mdd_strength": "BRAIN_DAILY_PNL",
        "cdar_strength": "BRAIN_DAILY_PNL",
        "stability": "BRAIN_YEARLY_AGGREGATES",
        "self_correlation": "BRAIN_SELF_CORRELATION",
    }.get(metric, "BRAIN_EVIDENCE")
    return {
        "value": value if _number(value) is not None else None,
        "score": normalized["score"],
        "status": normalized["status"],
        "reason_code": normalized["reason_code"],
        "source": source,
        "unit": unit,
    }


def evaluate_quality(
    *, alpha_id: str, alpha_scope: Mapping[str, Any] | None,
    metrics: Mapping[str, Any] | None, yearly_data: Any, pnl_rows: Any,
    pnl_unit: str | None, expected_trading_dates: Sequence[str] | None,
    self_correlation: Any, calibration: Mapping[str, Any] | None,
    as_of: date, metric_units: Mapping[str, str] | None = None,
    type_label: str | None = None,
    type_source: str | None = None,
) -> dict[str, Any]:
    """Score one Alpha without retaining expressions or inferring its strategy."""
    alpha_id = str(alpha_id).strip()
    if not alpha_id or len(alpha_id) > 128:
        raise ValueError("alpha_id must be non-empty and at most 128 characters")
    if not isinstance(as_of, date):
        raise ValueError("as_of must be a date")
    scope = dict(alpha_scope) if isinstance(alpha_scope, Mapping) else None
    compatible = bool(
        isinstance(calibration, Mapping)
        and _safe_calibration_version(calibration.get("version"))
        and str(calibration.get("reference_id") or "").strip()
        and str(calibration.get("frozen_at") or "").strip()
        and str(calibration.get("source") or "").strip()
        and isinstance(calibration.get("minimum_reference_count"), int)
        and not isinstance(calibration.get("minimum_reference_count"), bool)
        and isinstance(scope, Mapping)
        and isinstance(calibration.get("scope"), Mapping)
        and dict(calibration["scope"]) == scope
    )
    if not compatible:
        calibration = None

    metrics = metrics if isinstance(metrics, Mapping) else {}
    metric_units = metric_units if isinstance(metric_units, Mapping) else {}
    turnover_value = _number(metrics.get("turnover"))
    turnover_unit = str(metric_units.get("turnover") or "").strip().lower()
    turnover = (
        turnover_value / 100.0 if turnover_value is not None and turnover_unit == "percent"
        else turnover_value if turnover_value is not None and turnover_unit == "fraction"
        else None
    )
    resolved = resolve_alpha_type(
        turnover=turnover, type_label=type_label, type_source=type_source,
    )
    if resolved.get("status") == "UNKNOWN":
        weights = dict(METRIC_WEIGHTS)
    else:
        weights = dict(resolved["weights"])

    results: dict[str, dict[str, Any]] = {}
    raw_sources = {
        "sharpe": _number(metrics.get("sharpe")),
        "returns": _number(metrics.get("returns")),
        "margin": _number(metrics.get("margin")),
        "turnover": turnover_value,
    }
    raw_units = {
        "sharpe": "ratio",
        "returns": str(metric_units.get("returns") or "unknown-unit"),
        "margin": str(metric_units.get("margin") or "unknown-unit"),
        "turnover": str(metric_units.get("turnover") or "unknown-unit"),
    }
    for name, value in raw_sources.items():
        if name != "sharpe" and raw_units[name] == "unknown-unit":
            results[name] = {
                "value": value, "score": None, "status": "UNAVAILABLE",
                "reason_code": "UNIT_UNKNOWN", "source": "BRAIN_ALPHA_IS",
                "unit": "UNKNOWN",
            }
            continue
        if name == "turnover":
            results[name] = {
                "value": value, "score": None, "status": "UNAVAILABLE",
                "reason_code": "TURNOVER_BUDGET_UNAVAILABLE",
                "source": "BRAIN_ALPHA_IS", "unit": raw_units[name],
            }
            continue
        results[name] = _metric_result(
            value, name, calibration, raw_units[name],
        ) if compatible else {
            "value": value, "score": None, "status": "UNAVAILABLE",
            "reason_code": "CALIBRATION_SCOPE_UNKNOWN", "source": "BRAIN_ALPHA_IS",
            "unit": raw_units[name],
        }

    drawdown = pnl_drawdown_strength(
        pnl_rows, expected_dates=expected_trading_dates,
        pnl_unit=pnl_unit,
        expected_unit=(calibration.get("pnl_unit") if compatible else None),
    )
    for name in ("mdd_strength", "cdar_strength"):
        value = drawdown.get(name)
        results[name] = _metric_result(
            value, name, calibration, "ratio",
        ) if compatible and drawdown.get("status") == "AVAILABLE" else {
            "value": None, "score": None, "status": "UNAVAILABLE",
            "reason_code": drawdown.get("reason_code") or "CALIBRATION_SCOPE_UNKNOWN",
            "source": "BRAIN_DAILY_PNL", "unit": "SCALE_INVARIANT_PNL_STRENGTH",
        }

    stability = _stability(yearly_data, calibration=calibration, as_of=as_of)
    results["stability"] = _metric_result(
        stability.get("value"), "stability", calibration, "score",
    ) if compatible and stability.get("status") == "AVAILABLE" else {
        "value": None, "score": None, "status": "UNAVAILABLE",
        "reason_code": stability.get("reason_code") if compatible
        else "CALIBRATION_SCOPE_UNKNOWN",
        "source": "BRAIN_YEARLY_AGGREGATES", "unit": "score",
    }

    corr, corr_reason = _correlation_value(self_correlation)
    results["self_correlation"] = _metric_result(
        corr, "self_correlation", calibration, "correlation",
    ) if compatible and corr_reason is None else {
        "value": None, "score": None, "status": "UNAVAILABLE",
        "reason_code": corr_reason or "CALIBRATION_SCOPE_UNKNOWN",
        "source": "BRAIN_SELF_CORRELATION", "unit": "correlation",
    }

    # Turnover has a type-specific budget penalty; a calibration must freeze
    # budget and penalty scale for each possible turnover class.
    if compatible and turnover is not None:
        blend = resolved.get("blend_weights") or {resolved.get("label"): 1.0}
        budgets = calibration.get("turnover_budgets")
        budget = None
        if (isinstance(budgets, Mapping)
                and str(calibration.get("turnover_budget_unit") or "") == raw_units["turnover"]
                and all(
                    share <= 1e-12 or _number(budgets.get(kind)) is not None
                    for kind, share in blend.items()
                )):
            budget = sum(
                share * float(_number(budgets.get(kind)) or 0.0)
                for kind, share in blend.items()
            )
        reference, reason = _reference(calibration, "turnover", raw_units["turnover"])
        if budget is not None and reference is not None:
            scale = max(1.4826 * float(reference["mad"]), float(reference["scale_floor"]))
            z = -max(0.0, turnover_value - budget) / scale
            score = 100.0 / (1.0 + math.exp(-_clip(z, -4.0, 4.0) / 1.5))
            results["turnover"].update({
                "score": score, "status": "AVAILABLE", "reason_code": None,
                "budget": budget, "method": "TYPE_BUDGET_EXCESS_PENALTY",
            })
        else:
            results["turnover"].update({
                "score": None, "status": "UNAVAILABLE",
                "reason_code": reason or "TURNOVER_BUDGET_UNAVAILABLE",
            })

    coverage = sum(
        weights[name] / WEIGHT_SCALE for name, result in results.items()
        if result.get("status") == "AVAILABLE" and _number(result.get("score")) is not None
    )
    q_mid = 50.0 + sum(
        weights[name] / WEIGHT_SCALE * (float(result["score"]) - 50.0)
        for name, result in results.items()
        if result.get("status") == "AVAILABLE" and _number(result.get("score")) is not None
    )
    q_low = q_mid - 50.0 * (1.0 - coverage)
    q_high = q_mid + 50.0 * (1.0 - coverage)
    if not compatible or coverage < 0.50 or results["sharpe"]["status"] != "AVAILABLE":
        grade = "U"
    elif q_low >= 75.0 and coverage >= 0.90:
        grade = "A"
    elif q_low >= 60.0 and coverage >= 0.80:
        grade = "B"
    elif q_low < 40.0 and coverage >= 0.80:
        grade = "D"
    else:
        grade = "C"
    alerts = [
        result["reason_code"] for result in results.values()
        if result.get("status") != "AVAILABLE" and result.get("reason_code")
    ]
    return {
        "alpha_id": alpha_id,
        "scope": scope,
        "type": resolved,
        "metrics": results,
        "quality": {
            "q_mid": q_mid, "q_low": q_low, "q_high": q_high,
            "coverage": coverage, "grade": grade,
            "status": "AVAILABLE" if compatible else "UNKNOWN",
            "config_version": calibration.get("version") if compatible else None,
        },
        "risk_alerts": sorted(set(alerts)),
        "fitness_diagnostic": {
            "value": _number(metrics.get("fitness")),
            "source": "BRAIN_ALPHA_IS", "weight": 0.0,
        },
    }


def _safe_calibration_version(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    version = value.strip()
    return bool(
        version and len(version) <= 64 and version.isascii()
        and all(char.isalnum() or char in "._-" for char in version)
    )
