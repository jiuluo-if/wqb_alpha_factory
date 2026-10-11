"""Read-only, deterministic Osmosis point allocation previews."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any

from .remote_evidence import decode_daily_pnl

TOTAL_NORMALIZED_POINTS = 10_000
DEFAULT_MAX_ALPHA_SHARE = 0.15
DEFAULT_MAX_FAMILY_SHARE = 0.25
DEFAULT_MIN_COVERAGE = 0.80
DEFAULT_MIN_CORRELATION_OBSERVATIONS = 120
MAX_ALLOCATION_CANDIDATES = 100
MAX_PNL_OBSERVATIONS = 10_000


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _excluded(reason: str) -> dict[str, Any]:
    return {"status": "INFEASIBLE", "reason_codes": [reason], "allocations": None}


def _eligible(candidates: Any, scope: Mapping[str, Any], min_coverage: float):
    accepted = []
    excluded: dict[str, str] = {}
    if not isinstance(candidates, Sequence) or isinstance(candidates, (str, bytes)):
        return accepted, {"INPUT": "CANDIDATES_INVALID"}
    if len(candidates) > MAX_ALLOCATION_CANDIDATES:
        raise ValueError(
            f"allocation accepts at most {MAX_ALLOCATION_CANDIDATES} candidates"
        )
    id_counts: dict[str, int] = {}
    for candidate in candidates:
        if isinstance(candidate, Mapping):
            alpha_id = str(candidate.get("alpha_id") or "").strip()
            if alpha_id and len(alpha_id) <= 128:
                id_counts[alpha_id] = id_counts.get(alpha_id, 0) + 1
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            continue
        alpha_id = str(candidate.get("alpha_id") or "").strip()
        if not alpha_id or len(alpha_id) > 128:
            continue
        if id_counts.get(alpha_id, 0) > 1:
            excluded[alpha_id] = "DUPLICATE_ALPHA_ID"
            continue
        if candidate.get("submission_status") != "SUBMITTED":
            excluded[alpha_id] = (
                "SUBMISSION_UNKNOWN" if candidate.get("submission_status") == "UNKNOWN"
                else "NOT_SUBMITTED"
            )
            continue
        candidate_scope = candidate.get("scope")
        if not isinstance(candidate_scope, Mapping):
            excluded[alpha_id] = "SCOPE_UNKNOWN"
            continue
        scope_keys = ("region", "delay")
        if "universe" in scope:
            scope_keys = (*scope_keys, "universe")
        if any(key not in candidate_scope for key in scope_keys):
            excluded[alpha_id] = "SCOPE_UNKNOWN"
            continue
        if any(candidate_scope.get(key) != scope.get(key) for key in scope_keys):
            excluded[alpha_id] = "OUT_OF_SCOPE"
            continue
        quality = candidate.get("quality")
        if not isinstance(quality, Mapping) or quality.get("status") != "AVAILABLE":
            excluded[alpha_id] = "QUALITY_EVIDENCE_UNKNOWN"
            continue
        grade = str(quality.get("grade") or "U").upper()
        coverage = _finite(quality.get("coverage"))
        q_low = _finite(quality.get("q_low"))
        if (coverage is None or q_low is None or not 0.0 <= coverage <= 1.0
                or not 0.0 <= q_low <= 100.0 or grade not in {"A", "B", "C", "D", "U"}):
            excluded[alpha_id] = "QUALITY_SCHEMA_INVALID"
            continue
        if coverage < min_coverage or grade in {"U", "D"}:
            excluded[alpha_id] = "QUALITY_CONFIDENCE_INSUFFICIENT"
            continue
        if candidate.get("disqualifying_risks"):
            excluded[alpha_id] = "CONFIRMED_DISQUALIFYING_RISK"
            continue
        family = candidate.get("family")
        if (not isinstance(family, Mapping) or not str(family.get("id") or "").strip()
                or not str(family.get("source") or "").strip()):
            excluded[alpha_id] = "FAMILY_PROVENANCE_UNKNOWN"
            continue
        accepted.append(dict(candidate))
    accepted.sort(key=lambda item: str(item["alpha_id"]))
    return accepted, excluded


def _pnl_series(candidate: Mapping[str, Any], min_observations: int):
    pnl = candidate.get("pnl")
    if not isinstance(pnl, Mapping):
        return None, "PNL_UNKNOWN"
    unit = str(pnl.get("unit") or "").strip()
    rows = pnl.get("rows")
    expected = pnl.get("expected_dates")
    if (not unit or not isinstance(rows, (list, Mapping))
            or not isinstance(expected, list)):
        return None, "PNL_UNIT_OR_CALENDAR_UNKNOWN"
    if len(expected) > MAX_PNL_OBSERVATIONS:
        return None, "PNL_HISTORY_EXCEEDS_SAFE_LIMIT"
    decoded = decode_daily_pnl({"pnl": rows} if isinstance(rows, list) else rows)
    if decoded["status"] != "AVAILABLE":
        return None, decoded["reason_code"] or "PNL_SCHEMA_INVALID"
    if len(decoded["rows"]) > MAX_PNL_OBSERVATIONS:
        return None, "PNL_HISTORY_EXCEEDS_SAFE_LIMIT"
    parsed = [(row["date"], row["pnl"]) for row in decoded["rows"]]
    dates = [item[0] for item in parsed]
    try:
        expected = [date.fromisoformat(str(item)).isoformat() for item in expected]
    except ValueError:
        return None, "TRADING_CALENDAR_INVALID"
    if len(dates) != len(set(dates)):
        return None, "PNL_DATE_DUPLICATE"
    if dates != sorted(dates) or expected != sorted(expected):
        return None, "PNL_DATE_ORDER_INVALID"
    if dates != expected:
        return None, "PNL_CALENDAR_MISMATCH"
    if len(dates) < min_observations:
        return None, "PNL_OVERLAP_INSUFFICIENT"
    return {"unit": unit, "values": dict(parsed)}, None


def _pearson(left: Sequence[float], right: Sequence[float]) -> float | None:
    if len(left) != len(right) or len(left) < 2:
        return None
    left_mean = sum(left) / len(left)
    right_mean = sum(right) / len(right)
    numerator = sum((x - left_mean) * (y - right_mean) for x, y in zip(left, right))
    left_ss = sum((x - left_mean) ** 2 for x in left)
    right_ss = sum((y - right_mean) ** 2 for y in right)
    denominator = math.sqrt(left_ss * right_ss)
    if denominator <= 0 or not math.isfinite(denominator):
        return None
    return max(-1.0, min(1.0, numerator / denominator))


def _drawdowns(values: Sequence[float]) -> list[float]:
    cumulative = peak = 0.0
    result = []
    for value in values:
        cumulative += value
        peak = max(peak, cumulative)
        result.append(peak - cumulative)
    return result


def _correlation_matrix(candidates, *, min_observations: int, shrinkage: float):
    series = {}
    reasons = {}
    for candidate in candidates:
        alpha_id = candidate["alpha_id"]
        series[alpha_id], reasons[alpha_id] = _pnl_series(candidate, min_observations)
    matrix: dict[tuple[str, str], float] = {}
    drawdown_matrix: dict[tuple[str, str], float] = {}
    for index, left in enumerate(candidates):
        for right in candidates[index + 1:]:
            left_id, right_id = left["alpha_id"], right["alpha_id"]
            left_data, right_data = series[left_id], series[right_id]
            if left_data is None or right_data is None:
                continue
            if left_data["unit"] != right_data["unit"]:
                reasons[left_id] = reasons[right_id] = "PNL_UNIT_MISMATCH"
                continue
            overlap = sorted(set(left_data["values"]) & set(right_data["values"]))
            if len(overlap) < min_observations:
                reasons[left_id] = reasons[right_id] = "CORRELATION_OVERLAP_INSUFFICIENT"
                continue
            left_values = [left_data["values"][day] for day in overlap]
            right_values = [right_data["values"][day] for day in overlap]
            correlation = _pearson(left_values, right_values)
            if correlation is None:
                reasons[left_id] = reasons[right_id] = "CORRELATION_UNKNOWN"
                continue
            drawdown_correlation = _pearson(
                _drawdowns(left_values), _drawdowns(right_values),
            )
            if drawdown_correlation is None:
                reasons[left_id] = reasons[right_id] = "COMMON_DRAWDOWN_RISK_UNKNOWN"
                continue
            shrunk = correlation * len(overlap) / (len(overlap) + shrinkage)
            shrunk_drawdown = drawdown_correlation * len(overlap) / (len(overlap) + shrinkage)
            matrix[(left_id, right_id)] = shrunk
            matrix[(right_id, left_id)] = shrunk
            drawdown_matrix[(left_id, right_id)] = shrunk_drawdown
            drawdown_matrix[(right_id, left_id)] = shrunk_drawdown
    all_pairs = len(candidates) * (len(candidates) - 1) // 2
    status = "AVAILABLE" if len(matrix) // 2 == all_pairs and len(drawdown_matrix) // 2 == all_pairs else "UNKNOWN"
    return series, matrix, drawdown_matrix, reasons, status


def _capped_proportions(raw: Mapping[str, float], caps: Mapping[str, float], total: float):
    result = {key: 0.0 for key in raw}
    active = sorted(raw)
    remaining = total
    while active and remaining > 1e-12:
        denominator = sum(max(0.0, raw[key]) for key in sorted(active))
        if denominator <= 0:
            return None
        proposed = {
            key: remaining * max(0.0, raw[key]) / denominator
            for key in sorted(active)
        }
        saturated = [
            key for key in sorted(active)
            if proposed[key] > caps[key] - result[key] + 1e-12
        ]
        if not saturated:
            for key in sorted(active):
                result[key] += proposed[key]
            remaining = 0.0
            break
        for key in sorted(saturated):
            capacity = max(0.0, caps[key] - result[key])
            result[key] += capacity
            remaining -= capacity
            active.remove(key)
    if remaining > 1e-8:
        return None
    return result


def _apply_caps(raw_by_alpha, families, max_alpha_share, max_family_share):
    members: dict[str, list[str]] = {}
    for alpha_id, family in families.items():
        members.setdefault(family, []).append(alpha_id)
    family_raw = {family: sum(raw_by_alpha[item] for item in ids)
                  for family, ids in members.items()}
    family_capacity = {
        family: min(max_family_share, max_alpha_share * len(ids))
        for family, ids in members.items()
    }
    if sum(family_capacity.values()) < 1.0 - 1e-10:
        return None
    family_weights = _capped_proportions(family_raw, family_capacity, 1.0)
    if family_weights is None:
        return None
    weights = {}
    for family, ids in members.items():
        within_raw = {alpha_id: raw_by_alpha[alpha_id] for alpha_id in ids}
        within_caps = {alpha_id: max_alpha_share / family_weights[family]
                       if family_weights[family] > 0 else 0.0 for alpha_id in ids}
        within = _capped_proportions(within_raw, within_caps, 1.0)
        if within is None:
            return None
        for alpha_id in ids:
            weights[alpha_id] = family_weights[family] * within[alpha_id]
    return weights


def _integerize(weights, families, *, total_points, max_alpha_share, max_family_share,
                min_positive_alphas):
    alpha_cap = math.floor(max_alpha_share * total_points + 1e-9)
    family_cap = math.floor(max_family_share * total_points + 1e-9)
    raw = {alpha_id: weights[alpha_id] * total_points for alpha_id in sorted(weights)}
    points = {alpha_id: min(alpha_cap, math.floor(value)) for alpha_id, value in raw.items()}
    family_totals: dict[str, int] = {}
    for alpha_id, value in points.items():
        family = families[alpha_id]
        family_totals[family] = family_totals.get(family, 0) + value
    remainder = total_points - sum(points.values())
    priority = sorted(raw, key=lambda alpha_id: (-(raw[alpha_id] - math.floor(raw[alpha_id])), alpha_id))
    for alpha_id in priority:
        family = families[alpha_id]
        if (remainder <= 0 or points[alpha_id] >= alpha_cap
                or family_totals[family] >= family_cap):
            continue
        points[alpha_id] += 1
        family_totals[family] += 1
        remainder -= 1
    if remainder or sum(points.values()) != total_points:
        return None, "INTEGER_REPAIR_INFEASIBLE"
    if min_positive_alphas is not None and sum(value > 0 for value in points.values()) < min_positive_alphas:
        return None, "MINIMUM_POSITIVE_ALPHA_COUNT_UNMET"
    if any(value > alpha_cap for value in points.values()):
        return None, "INTEGER_ALPHA_CAP_VIOLATION"
    if any(value > family_cap for value in family_totals.values()):
        return None, "INTEGER_FAMILY_CAP_VIOLATION"
    return points, None


def _portfolio_metrics(
    candidates, points, series, matrix, drawdown_matrix, *, min_observations,
):
    if not candidates or any(series.get(item["alpha_id"]) is None for item in candidates):
        return {"status": "UNKNOWN", "reason_code": "PORTFOLIO_PNL_UNAVAILABLE"}
    units = {series[item["alpha_id"]]["unit"] for item in candidates}
    if len(units) != 1:
        return {"status": "UNKNOWN", "reason_code": "PNL_UNIT_MISMATCH"}
    common = set.intersection(*[
        set(series[item["alpha_id"]]["values"]) for item in candidates
    ])
    if len(common) < min_observations:
        return {"status": "UNKNOWN", "reason_code": "PORTFOLIO_OVERLAP_INSUFFICIENT"}
    dates = sorted(common)
    weights = {key: value / TOTAL_NORMALIZED_POINTS for key, value in points.items()}
    pnl = [sum(
        weights[item["alpha_id"]] * series[item["alpha_id"]]["values"][day]
        for item in candidates
    ) for day in dates]
    if any(not math.isfinite(value) for value in pnl):
        return {"status": "UNKNOWN", "reason_code": "PORTFOLIO_PNL_OVERFLOW"}
    mean = sum(pnl) / len(pnl)
    variance = sum((value - mean) ** 2 for value in pnl) / len(pnl)
    volatility = math.sqrt(variance)
    if volatility <= 0 or not math.isfinite(volatility):
        return {"status": "UNKNOWN", "reason_code": "PORTFOLIO_ZERO_OR_INVALID_VOLATILITY"}
    cumulative = peak = 0.0
    drawdowns = []
    for value in pnl:
        cumulative += value
        if not math.isfinite(cumulative):
            return {"status": "UNKNOWN", "reason_code": "PORTFOLIO_DRAWDOWN_OVERFLOW"}
        peak = max(peak, cumulative)
        drawdowns.append(peak - cumulative)
    ordered = sorted(drawdowns)
    threshold = ordered[min(len(ordered) - 1, math.floor(.90 * (len(ordered) - 1)))]
    cdar = sum(value for value in drawdowns if value >= threshold) / sum(
        value >= threshold for value in drawdowns
    )
    corr_values = [
        matrix[(left["alpha_id"], right["alpha_id"])]
        for index, left in enumerate(candidates)
        for right in candidates[index + 1:]
        if (left["alpha_id"], right["alpha_id"]) in matrix
    ]
    drawdown_corr_values = [
        drawdown_matrix[(left["alpha_id"], right["alpha_id"])]
        for index, left in enumerate(candidates)
        for right in candidates[index + 1:]
        if (left["alpha_id"], right["alpha_id"]) in drawdown_matrix
    ]
    pair_count = len(candidates) * (len(candidates) - 1) // 2
    correlation_pairs_complete = len(corr_values) == pair_count
    drawdown_pairs_complete = len(drawdown_corr_values) == pair_count
    hhi = sum(value * value for value in weights.values())
    family_weights: dict[str, float] = {}
    for item in candidates:
        family = str(item["family"]["id"])
        family_weights[family] = family_weights.get(family, 0.0) + weights[item["alpha_id"]]
    return {
        "status": "AVAILABLE", "observations": len(pnl),
        "annualized_sharpe": mean / volatility * math.sqrt(252),
        "annualized_volatility": volatility * math.sqrt(252),
        "maximum_drawdown": max(drawdowns), "cdar90": cdar,
        "correlation_status": "AVAILABLE" if correlation_pairs_complete else "UNKNOWN",
        "correlation_pair_count": pair_count,
        "correlation_pair_coverage": len(corr_values) / pair_count if pair_count else None,
        "average_pairwise_correlation": (
            sum(corr_values) / len(corr_values) if corr_values else None
        ) if correlation_pairs_complete else None,
        "common_drawdown_correlation_status": (
            "AVAILABLE" if drawdown_pairs_complete else "UNKNOWN"
        ),
        "average_common_drawdown_correlation": (
            sum(drawdown_corr_values) / len(drawdown_corr_values)
            if drawdown_corr_values and drawdown_pairs_complete else None
        ),
        "hhi": hhi, "effective_alpha_count": 1.0 / hhi,
        "max_family_share": max(family_weights.values(), default=0.0),
        "data_coverage": len(pnl) / max(
            max(len(series[item["alpha_id"]]["values"]) for item in candidates), 1,
        ),
        "cost_sensitivity": "UNKNOWN",
        "pnl_unit": next(iter(units)),
        "sample_type": "HISTORICAL_IN_SAMPLE_ONLY",
    }


def _plan(
    candidates, *, name, raw_weights, families, series, matrix, drawdown_matrix,
    total_points, max_alpha_share, max_family_share, min_positive_alphas,
    platform_rules, min_correlation_observations=DEFAULT_MIN_CORRELATION_OBSERVATIONS,
):
    weights = _apply_caps(raw_weights, families, max_alpha_share, max_family_share)
    if weights is None:
        return {"status": "INFEASIBLE", "reason_codes": ["CONCENTRATION_CAPACITY_BELOW_TOTAL"],
                "allocations": None}
    points, reason = _integerize(
        weights, families, total_points=total_points,
        max_alpha_share=max_alpha_share, max_family_share=max_family_share,
        min_positive_alphas=min_positive_alphas,
    )
    if points is None:
        return {"status": "INFEASIBLE", "reason_codes": [reason], "allocations": None}
    family_totals: dict[str, int] = {}
    for alpha_id, value in points.items():
        family = families[alpha_id]
        family_totals[family] = family_totals.get(family, 0) + value
    family_labels = {
        family: f"family_{index:03d}"
        for index, family in enumerate(sorted(family_totals), start=1)
    }
    allocations = [{
        "alpha_id": alpha_id, "weight": weights[alpha_id],
        "target_weight": weights[alpha_id],
        "integerized_weight": points[alpha_id] / total_points,
        "normalized_points": points[alpha_id],
        "points": points[alpha_id],
        "platform_points": None,
        "family_id": family_labels[families[alpha_id]],
        "quality_q_low": _finite(next(
            item["quality"].get("q_low") for item in candidates
            if item["alpha_id"] == alpha_id
        )),
        "risk_source": "BRAIN_DAILY_PNL_AND_COMMON_DRAWDOWN" if matrix and drawdown_matrix else "UNKNOWN",
    } for alpha_id in sorted(points)]
    portfolio_metrics = _portfolio_metrics(
        candidates, points, series, matrix, drawdown_matrix,
        min_observations=min_correlation_observations,
    )
    hhi = sum((value / total_points) ** 2 for value in points.values())
    return {
        "status": "INTERNAL_PREVIEW", "name": name, "allocations": allocations,
        "points": points, "hhi": hhi,
        "effective_alpha_count": 1.0 / hhi if hhi else 0,
        "family_concentration": {
            family_labels[key]: value / total_points for key, value in sorted(family_totals.items())
        },
        "portfolio_metrics": portfolio_metrics,
        "platform_rules_status": "INTERNAL_PREVIEW",
        "platform_rule_evidence_status": "UNKNOWN",
        "platform_rule_diagnostics": [
            "AUTHORITATIVE_SOURCE_UNKNOWN", "ACQUISITION_TIME_UNVERIFIED",
            "APPLICABLE_SCOPE_UNKNOWN", "RULE_VERSION_UNKNOWN",
            "ACCOUNT_AUTHORITY_UNKNOWN",
        ],
        "total_normalized_points": total_points,
    }


def _validated_options(
    *, total_points, max_alpha_share, max_family_share, min_coverage,
    min_correlation_observations, min_positive_alphas,
):
    if isinstance(total_points, bool) or not isinstance(total_points, int) or total_points != TOTAL_NORMALIZED_POINTS:
        raise ValueError("total_points must equal the internal standard of 10000")
    for name, value in (("max_alpha_share", max_alpha_share),
                        ("max_family_share", max_family_share),
                        ("min_coverage", min_coverage)):
        if _finite(value) is None or not 0 < value <= 1:
            raise ValueError(f"{name} must be in (0, 1]")
    if (isinstance(min_correlation_observations, bool)
            or not isinstance(min_correlation_observations, int)
            or min_correlation_observations < 2):
        raise ValueError("min_correlation_observations must be at least 2")
    if (min_positive_alphas is not None and (
        isinstance(min_positive_alphas, bool) or not isinstance(min_positive_alphas, int)
        or min_positive_alphas < 1
    )):
        raise ValueError("min_positive_alphas must be positive or None")


def preview_osmosis_allocation(
    candidates: Sequence[Mapping[str, Any]], *, scope: Mapping[str, Any],
    total_points: int = TOTAL_NORMALIZED_POINTS,
    max_alpha_share: float = DEFAULT_MAX_ALPHA_SHARE,
    max_family_share: float = DEFAULT_MAX_FAMILY_SHARE,
    min_coverage: float = DEFAULT_MIN_COVERAGE,
    min_correlation_observations: int = DEFAULT_MIN_CORRELATION_OBSERVATIONS,
    min_positive_alphas: int | None = None,
    correlation_threshold: float = 0.25,
    correlation_lambda: float = 1.0,
    volatility_tilt_min: float = 0.5,
    volatility_tilt_max: float = 2.0,
    platform_rules: Mapping[str, Any] | None = None,
    _windowed: bool = False,
) -> dict[str, Any]:
    """Recommend one explicit scope; never call a platform write endpoint."""
    if (not isinstance(scope, Mapping)
            or not isinstance(scope.get("region"), str) or not scope["region"].strip()
            or isinstance(scope.get("delay"), bool)
            or not isinstance(scope.get("delay"), (str, int))
            or not str(scope.get("delay")).strip()
            or "universe" in scope and (
                not isinstance(scope.get("universe"), str)
                or not scope["universe"].strip()
            )):
        return _excluded("SCOPE_UNKNOWN")
    _validated_options(
        total_points=total_points, max_alpha_share=max_alpha_share,
        max_family_share=max_family_share, min_coverage=min_coverage,
        min_correlation_observations=min_correlation_observations,
        min_positive_alphas=min_positive_alphas,
    )
    if _finite(correlation_threshold) is None or not 0 <= correlation_threshold <= 1:
        raise ValueError("correlation_threshold must be in [0, 1]")
    if _finite(correlation_lambda) is None or correlation_lambda < 0:
        raise ValueError("correlation_lambda must be non-negative")
    if (_finite(volatility_tilt_min) is None or volatility_tilt_min <= 0
            or _finite(volatility_tilt_max) is None
            or volatility_tilt_max < volatility_tilt_min):
        raise ValueError("volatility tilt bounds must be positive and ordered")
    accepted, excluded = _eligible(candidates, scope, min_coverage)
    if not accepted:
        return {"status": "INFEASIBLE", "reason_codes": ["NO_ELIGIBLE_CANDIDATES"],
                "allocations": None, "excluded": excluded}
    families = {item["alpha_id"]: str(item["family"]["id"]) for item in accepted}
    if sum(min(max_family_share, max_alpha_share * sum(
        1 for item in accepted if families[item["alpha_id"]] == family
    )) for family in sorted(set(families.values()))) < 1.0 - 1e-10:
        return {"status": "INFEASIBLE", "reason_codes": ["CONCENTRATION_CAPACITY_BELOW_TOTAL"],
                "allocations": None, "excluded": excluded}

    series, correlations, drawdown_correlations, risk_reasons, corr_status = _correlation_matrix(
        accepted, min_observations=min_correlation_observations,
        shrinkage=float(min_correlation_observations),
    )
    if corr_status != "AVAILABLE":
        reason_codes = sorted(set(risk_reasons.values())) or ["CORRELATION_UNKNOWN"]
        return {
            "status": "UNKNOWN", "reason_codes": reason_codes,
            "allocations": None, "excluded": excluded,
            "eligible_count": len(accepted), "correlation_status": "UNKNOWN",
            "platform_rules_status": "INTERNAL_PREVIEW",
            "platform_rule_evidence_status": "UNKNOWN",
            "total_normalized_points": total_points,
        }
    vols = {}
    mdd_strengths = {}
    for item in accepted:
        values = [value for _, value in sorted(series[item["alpha_id"]]["values"].items())]
        mean = sum(values) / len(values)
        vol = math.sqrt(sum((value - mean) ** 2 for value in values) / len(values))
        if vol <= 0 or not math.isfinite(vol):
            return {"status": "UNKNOWN", "reason_codes": ["PNL_ZERO_OR_INVALID_VOLATILITY"],
                    "allocations": None, "excluded": excluded}
        vols[item["alpha_id"]] = vol
        mdd_strengths[item["alpha_id"]] = max(_drawdowns(values)) / (vol * math.sqrt(252.0))
        if not math.isfinite(mdd_strengths[item["alpha_id"]]):
            return {"status": "UNKNOWN", "reason_codes": ["PNL_DRAWDOWN_OVERFLOW"],
                    "allocations": None, "excluded": excluded}
    median_vol = sorted(vols.values())[len(vols) // 2]
    ordered_mdd = sorted(mdd_strengths.values())
    median_mdd = ordered_mdd[len(ordered_mdd) // 2]
    quality_factor = {
        item["alpha_id"]: math.exp(.25 * (_finite(item["quality"]["q_low"]) - 50.0) / 10.0)
        for item in accepted
    }
    risk_adjustment = {}
    for alpha_id, vol in vols.items():
        volatility_tilt = max(volatility_tilt_min, min(volatility_tilt_max, median_vol / vol))
        if median_mdd > 0:
            mdd_floor = median_mdd * 0.5
            mdd_tilt = max(.5, min(2.0, median_mdd / max(mdd_strengths[alpha_id], mdd_floor)))
        else:
            mdd_tilt = 1.0
        risk_adjustment[alpha_id] = volatility_tilt * mdd_tilt
    raw = {alpha_id: quality_factor[alpha_id] * risk_adjustment[alpha_id]
           for alpha_id in quality_factor}
    weights = None
    converged = False
    for _ in range(40):
        provisional = _apply_caps(raw, families, max_alpha_share, max_family_share)
        if provisional is None:
            break
        revised = {}
        for item in accepted:
            alpha_id = item["alpha_id"]
            others = [other["alpha_id"] for other in accepted if other["alpha_id"] != alpha_id]
            corr_risk = sum(
                provisional[other] * max(
                    0.0, correlations[(alpha_id, other)],
                    drawdown_correlations[(alpha_id, other)],
                )
                for other in others
            ) / max(sum(provisional[other] for other in others), 1e-12)
            diversification = math.exp(-correlation_lambda * max(
                0.0, corr_risk - correlation_threshold,
            ))
            revised[alpha_id] = quality_factor[alpha_id] * risk_adjustment[alpha_id] * diversification
        weights = _apply_caps(revised, families, max_alpha_share, max_family_share)
        if weights is None:
            break
        gap = max(abs(weights[key] - provisional[key]) for key in weights)
        raw = revised
        if gap < 1e-9:
            converged = True
            break
    if not converged:
        return {"status": "UNKNOWN", "reason_codes": ["WEIGHT_ITERATION_NOT_CONVERGED"],
                "allocations": None, "excluded": excluded,
                "eligible_count": len(accepted), "correlation_status": "AVAILABLE",
                "platform_rules_status": "INTERNAL_PREVIEW",
                "total_normalized_points": total_points}
    if weights is None:
        return {"status": "INFEASIBLE", "reason_codes": ["CONCENTRATION_CAPACITY_BELOW_TOTAL"],
                "allocations": None, "excluded": excluded}
    points, reason = _integerize(
        weights, families, total_points=total_points,
        max_alpha_share=max_alpha_share, max_family_share=max_family_share,
        min_positive_alphas=min_positive_alphas,
    )
    if points is None:
        return {"status": "INFEASIBLE", "reason_codes": [reason],
                "allocations": None, "excluded": excluded}
    result = _plan(
        accepted, name="risk_adjusted", raw_weights=raw, families=families,
        series=series, matrix=correlations,
        drawdown_matrix=drawdown_correlations, total_points=total_points,
        max_alpha_share=max_alpha_share, max_family_share=max_family_share,
        min_positive_alphas=min_positive_alphas, platform_rules=platform_rules,
        min_correlation_observations=min_correlation_observations,
    )
    result.update({
        "scope": dict(scope), "excluded": excluded,
        "eligible_count": len(accepted), "correlation_status": "AVAILABLE",
        "correlation_method": "SHRUNK_PEARSON_DAILY_PNL",
        "minimum_overlap": min_correlation_observations,
        "quality_factor_method": "EXP_0.25_Q_LOW_OVER_10_NOT_RETURN_FORECAST",
        "risk_adjustment_method": "CLIPPED_INVERSE_VOLATILITY_AND_WEIGHTED_POSITIVE_CORRELATION",
        "individual_drawdown_method": "CLIPPED_MDD_STRENGTH_TILT",
        "common_drawdown_method": "SHRUNK_PEARSON_OF_ALIGNED_CUMULATIVE_DRAWDOWNS",
        "constraints": {
            "max_alpha_share": max_alpha_share,
            "max_family_share": max_family_share,
            "min_positive_alphas": min_positive_alphas,
        },
    })
    rolling = {"status": "UNKNOWN", "reason_code": "INSUFFICIENT_ROLLING_WINDOWS"}
    if not _windowed:
        common_dates = sorted(set.intersection(*[
            set(series[item["alpha_id"]]["values"]) for item in accepted
        ]))
        window_size = min_correlation_observations
        step = max(1, window_size // 2)
        starts = list(range(0, max(0, len(common_dates) - window_size + 1), step))
        if len(starts) >= 3:
            changes = []
            window_rows = []
            full_weights = {
                key: value / total_points for key, value in points.items()
            }
            for start in starts:
                window_dates = common_dates[start:start + window_size]
                window_candidates = []
                for item in accepted:
                    alpha_id = item["alpha_id"]
                    window_candidates.append({
                        **item,
                        "pnl": {
                            "unit": series[alpha_id]["unit"],
                            "expected_dates": window_dates,
                            "rows": [{"date": day, "value": series[alpha_id]["values"][day]}
                                     for day in window_dates],
                        },
                    })
                window_plan = preview_osmosis_allocation(
                    window_candidates, scope=scope, total_points=total_points,
                    max_alpha_share=max_alpha_share, max_family_share=max_family_share,
                    min_coverage=min_coverage,
                    min_correlation_observations=window_size,
                    min_positive_alphas=min_positive_alphas,
                    correlation_threshold=correlation_threshold,
                    correlation_lambda=correlation_lambda,
                    volatility_tilt_min=volatility_tilt_min,
                    volatility_tilt_max=volatility_tilt_max,
                    platform_rules=platform_rules, _windowed=True,
                )
                if window_plan.get("status") != "INTERNAL_PREVIEW":
                    continue
                window_weights = {
                    key: value / total_points for key, value in window_plan["points"].items()
                }
                l1 = sum(
                    abs(window_weights.get(key, 0.0) - full_weights.get(key, 0.0))
                    for key in sorted(set(window_weights) | set(full_weights))
                )
                changes.append(l1)
                window_rows.append({
                    "start": window_dates[0], "end": window_dates[-1],
                    "hhi": window_plan["hhi"], "l1_weight_change_from_full": l1,
                })
            if len(window_rows) >= 3:
                rolling = {
                    "status": "AVAILABLE", "method": "OVERLAPPING_TRAILING_RISK_INPUT_WINDOWS",
                    "quality_scores_held_fixed": True,
                    "interpretation": "configuration sensitivity only; not OOS performance",
                    "window_observations": window_size, "step_observations": step,
                    "window_count": len(window_rows),
                    "mean_l1_weight_change_from_full": sum(changes) / len(changes),
                    "max_l1_weight_change_from_full": max(changes),
                    "windows": window_rows,
                }
    result["rolling_configuration_stability"] = rolling
    return result


def compare_osmosis_allocation_plans(
    candidates: Sequence[Mapping[str, Any]], *, scope: Mapping[str, Any], **options,
) -> dict[str, Any]:
    """Compare equal, quality-only, and risk-adjusted historical previews."""
    risk = preview_osmosis_allocation(candidates, scope=scope, **options)
    accepted, excluded = _eligible(
        candidates, scope, float(options.get("min_coverage", DEFAULT_MIN_COVERAGE)),
    )
    if not accepted:
        return {"status": "INFEASIBLE", "plans": {}, "excluded": excluded,
                "reason_codes": ["NO_ELIGIBLE_CANDIDATES"]}
    families = {item["alpha_id"]: str(item["family"]["id"]) for item in accepted}
    series, matrix, drawdown_matrix, _, _ = _correlation_matrix(
        accepted,
        min_observations=int(options.get(
            "min_correlation_observations", DEFAULT_MIN_CORRELATION_OBSERVATIONS,
        )),
        shrinkage=float(options.get(
            "min_correlation_observations", DEFAULT_MIN_CORRELATION_OBSERVATIONS,
        )),
    )
    common = {
        "total_points": options.get("total_points", TOTAL_NORMALIZED_POINTS),
        "max_alpha_share": options.get("max_alpha_share", DEFAULT_MAX_ALPHA_SHARE),
        "max_family_share": options.get("max_family_share", DEFAULT_MAX_FAMILY_SHARE),
        "min_positive_alphas": options.get("min_positive_alphas"),
        "platform_rules": options.get("platform_rules"),
        "min_correlation_observations": options.get(
            "min_correlation_observations", DEFAULT_MIN_CORRELATION_OBSERVATIONS,
        ),
    }
    equal_raw = {item["alpha_id"]: 1.0 for item in accepted}
    quality_raw = {
        item["alpha_id"]: math.exp(.25 * (_finite(item["quality"]["q_low"]) - 50.0) / 10.0)
        for item in accepted
    }
    plans = {
        "equal": _plan(
            accepted, name="equal", raw_weights=equal_raw, families=families,
            series=series, matrix=matrix, drawdown_matrix=drawdown_matrix, **common,
        ),
        "quality_only": _plan(
            accepted, name="quality_only", raw_weights=quality_raw, families=families,
            series=series, matrix=matrix, drawdown_matrix=drawdown_matrix, **common,
        ),
        "risk_adjusted": risk,
    }
    return {
        "status": "AVAILABLE" if all(
            plan.get("status") in {"INTERNAL_PREVIEW", "UNKNOWN"}
            for plan in plans.values()
        ) else "PARTIAL",
        "scope": dict(scope), "plans": plans, "excluded": excluded,
        "comparison_note": "Historical in-sample diagnostics are not out-of-sample evidence.",
    }
