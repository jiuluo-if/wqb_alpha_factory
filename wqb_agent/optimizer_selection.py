"""Pure optimizer eligibility policy."""

from collections.abc import Mapping


def metric_gap_distance(context):
    if not isinstance(context, Mapping):
        return float("inf")
    distance = 0.0
    for key in ("sharpe_gap", "fitness_gap"):
        gap = context.get(key)
        if isinstance(gap, bool) or not isinstance(gap, (int, float)):
            return float("inf")
        distance = max(distance, abs(float(gap)))
    return distance


def optimization_priority(summary, readiness_priority):
    context = summary.get("metric_optimization_context") or {}
    readiness = str(context.get("readiness") or "LOW_INFORMATION")
    return (
        readiness_priority.get(readiness, len(readiness_priority)),
        len(context.get("structural_blockers") or ()),
        len(context.get("repairable_blockers") or ()),
        metric_gap_distance(context),
        summary.get("opportunity") == "NO_CLEAR_OPPORTUNITY",
        str(summary.get("parent_id") or ""),
    )


def parent_rejections(parent):
    if not isinstance(parent, Mapping):
        return ["INVALID_PARENT"]
    if str(parent.get("status") or "").upper() != "DONE":
        return ["PARENT_NOT_DONE"]
    reasons = []
    metrics = parent.get("metrics")
    if not isinstance(metrics, dict) or not metrics:
        reasons.append("PARENT_METRICS_MISSING")
    if not isinstance(metrics, dict) or "checks" not in metrics:
        reasons.append("PARENT_CHECKS_INCOMPLETE")
    if not parent.get("expression"):
        reasons.append("PARENT_METRICS_MISSING")
    if any(not parent.get(key) for key in (
        "fields_used", "datasets", "field_understanding", "field_analysis",
        "field_source", "field_hypothesis_basis",
    )):
        reasons.append("PARENT_FIELD_EVIDENCE_MISSING")
    if (not parent.get("hypothesis_id")
            or not isinstance(parent.get("economic_mechanism"), str)
            or not parent["economic_mechanism"].strip()):
        reasons.append("PARENT_HYPOTHESIS_MISSING")
    return reasons
