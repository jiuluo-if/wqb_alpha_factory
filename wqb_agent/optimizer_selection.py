"""Pure optimizer eligibility and request-local evidence projections."""

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from .optimization_decision import OptimizationDecision

_CORRELATION_FAIL_STATES = frozenset({"FAIL", "FAILED", "BLOCK", "HIGHER"})
_NEXT_ACTION_BY_READINESS = {
    "PRE_CORRELATION_READY": "CHECK_SELF_CORRELATION",
    "STRUCTURAL_REPAIR_REQUIRED": "CONSIDER_CHILD",
    "NUMERIC_VALIDATION_CANDIDATE": "CONSIDER_VALIDATE",
    "ONE_REPAIR_AWAY": "CONSIDER_VALIDATE",
    "LOW_INFORMATION": "REROUTE_OR_STOP",
    "STOP": "STOP",
}


@dataclass(frozen=True)
class OptimizerLocalEvidenceView:
    """Immutable, request-scoped projection of local trajectory evidence."""

    records: tuple[Mapping, ...]

    @classmethod
    def from_rows(cls, rows):
        projected = []
        for row in rows or ():
            record = row.to_dict() if hasattr(row, "to_dict") else row
            if isinstance(record, Mapping):
                projected.append(MappingProxyType(dict(record)))
        return cls(tuple(projected))

    def by_identity(self):
        result = {}
        for record in self.records:
            for key in ("id", "proposal_id"):
                value = record.get(key)
                if value is not None and str(value) not in result:
                    result[str(value)] = record
        return MappingProxyType(result)


def next_action(readiness, *, self_correlation_status="UNKNOWN",
                generation_allowed=True):
    """Pure derived next-step hint; never an autonomous research decision."""
    status = str(self_correlation_status or "UNKNOWN").upper()
    band = str(readiness or "")
    if status in _CORRELATION_FAIL_STATES:
        return "CONSIDER_CORRELATION_REPAIR"
    if band == "STRUCTURAL_REPAIR_REQUIRED":
        return "CONSIDER_CHILD" if generation_allowed else "STOP"
    if status == "PASS":
        return "READY_TO_ADVANCE"
    return _NEXT_ACTION_BY_READINESS.get(band, "REROUTE_OR_STOP")


def same_value(left, right):
    """Compare numeric values without treating booleans as numbers."""
    if isinstance(left, bool) or isinstance(right, bool):
        return str(left) == str(right)
    try:
        return float(left) == float(right)
    except (TypeError, ValueError):
        return str(left) == str(right)


def decision_for_parent(parent):
    """Parse an Agent-authored decision from one projected parent record."""
    payload = parent.get("optimization_decision")
    if isinstance(payload, Mapping):
        try:
            return OptimizationDecision.from_mapping(payload)
        except (TypeError, ValueError):
            return None
    child = parent.get("child_economic_hypothesis")
    if isinstance(child, Mapping):
        try:
            return OptimizationDecision.from_child_hypothesis(
                str(parent.get("id") or ""), child
            )
        except (TypeError, ValueError):
            return None
    return None


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
