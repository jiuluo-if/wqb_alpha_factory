"""Pure optimizer eligibility policy."""

from collections.abc import Mapping


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
