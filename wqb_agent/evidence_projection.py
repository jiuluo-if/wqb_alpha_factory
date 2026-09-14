"""Pure projections for evidence gates and quality labels."""

from __future__ import annotations

from collections.abc import Mapping

from .metrics import num
from .pre_correlation import delay_metric_thresholds, turnover_bounds


def correlation_under(evidence: Mapping[str, object] | None, max_corr: float) -> bool:
    if not evidence or evidence.get("status") != "PASS":
        return False
    value = (evidence.get("check") or {}).get("value")
    if value is None:
        return False
    try:
        return float(value) < float(max_corr)
    except (TypeError, ValueError):
        return False


def alpha_rating(metrics: Mapping[str, object], quality_policy: Mapping[str, object] | None, *, delay=None) -> str:
    required = ("sharpe", "turnover", "fitness", "margin")
    if any(metrics.get(key) is None for key in required):
        return "UNRATED"
    values = {key: num(metrics[key]) for key in required}
    if any(value is None for value in values.values()):
        return "UNRATED"
    policy = quality_policy if isinstance(quality_policy, Mapping) else {}

    def threshold(section, name, default):
        values_for_section = policy.get(section, {})
        value = num(values_for_section.get(name, default)) if isinstance(values_for_section, Mapping) else None
        return default if value is None else value

    if (
        values["sharpe"] > threshold("spectacular", "min_sharpe", 2.0)
        and threshold("spectacular", "min_turnover", 0.10) <= values["turnover"] <= threshold("spectacular", "max_turnover", 0.20)
        and values["fitness"] > threshold("spectacular", "min_fitness", 2.5)
        and values["margin"] > threshold("spectacular", "min_margin", 0.0006)
    ):
        return "SPECTACULAR"
    if (
        values["sharpe"] > threshold("excellent", "min_sharpe", 1.58)
        and threshold("excellent", "min_turnover", 0.049) <= values["turnover"] <= threshold("excellent", "max_turnover", 0.30)
        and values["fitness"] > threshold("excellent", "min_fitness", 1.5)
        and values["margin"] > threshold("excellent", "min_margin", 0.0004)
    ):
        return "EXCELLENT"
    thresholds = delay_metric_thresholds(delay)
    min_turnover, max_turnover = turnover_bounds(policy)
    if thresholds is not None and values["sharpe"] > thresholds["sharpe"] and min_turnover <= values["turnover"] <= max_turnover and values["fitness"] > thresholds["fitness"]:
        return "GOOD"
    return "BELOW_GOOD"
