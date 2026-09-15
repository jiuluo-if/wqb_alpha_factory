"""Derived research-state colors for real BRAIN Alpha metadata.

Colors are a thin view over existing evidence.  This module does not score
alphas, submit alphas, change Simulation state, or perform remote metadata
writes in its domain functions.  It decides whether an already-evaluated
experiment has enough evidence for one research-state color, summarizes that
evidence, and loads local candidates.  Explicit remote color synchronization
belongs to ``AlphaColorWorkflow``; the legacy wrapper below only delegates to
that workflow. Color evidence is not persisted locally.
"""

from __future__ import annotations

import os

from .artifacts import iter_jsonl_objects
from .experiment import Experiment
from .metrics import check_pass, num

ALPHA_COLORS = frozenset({"BLUE", "GREEN", "PURPLE", "RED", "YELLOW"})
PROJECT_COLOR_OWNER = "wqb_alpha_factory"
_POSITIVE_QUALITY = frozenset({
    "PROMISING", "SUCCESS", "SUSPICIOUS_HIGH_SIGNAL", "STABLE",
    "PORTFOLIO_CANDIDATE",
})
_STRONG_QUALITY = frozenset({
    "SUCCESS", "SUSPICIOUS_HIGH_SIGNAL", "STABLE", "PORTFOLIO_CANDIDATE",
})
_NEGATIVE_QUALITY = frozenset({
    "FAILED", "FAIL", "REJECTED", "RECONCILE", "UNRESOLVED", "UNKNOWN",
    "PARAMETER_LUCK", "DUPLICATE", "INFRASTRUCTURE_FAILURE",
})
_QUALITY_FAILURE_NAMES = (
    "sharpe", "fitness", "turnover", "margin", "drawdown", "sub_universe",
    "concentrat", "syntax", "expression", "coverage", "nan", "data",
)
_NONFAILURE_BLOCKER_NAMES = (
    "correlation", "validation", "robustness", "yearly", "aggregate",
)


def _value(experiment, key, default=None):
    if isinstance(experiment, dict):
        return experiment.get(key, default)
    return getattr(experiment, key, default)


def _upper(value):
    return str(value or "").strip().upper()


def _quality_labels(experiment):
    labels = []
    for value in (
        _value(experiment, "research_classification"),
        _value(experiment, "quality_label"),
    ):
        if isinstance(value, dict):
            value = value.get("label") or value.get("classification")
        if value:
            labels.append(_upper(value))
    for key in ("final_outcome", "search_outcome", "provisional_outcome"):
        value = _value(experiment, key)
        if isinstance(value, dict):
            for nested in (value.get("base_quality"), value.get("label"), value.get("classification")):
                if nested:
                    labels.append(_upper(nested))
    return tuple(dict.fromkeys(labels))


def _checks(experiment):
    metrics = _value(experiment, "metrics")
    if not isinstance(metrics, dict):
        return []
    checks = metrics.get("checks")
    return checks if isinstance(checks, list) else []


def _self_correlation(experiment):
    evidence = _value(experiment, "self_correlation")
    if isinstance(evidence, dict):
        status = _upper(evidence.get("status"))
        check = evidence.get("check")
        source = evidence.get("source")
        if status:
            return status, check if isinstance(check, dict) else {}, source
    for check in _checks(experiment):
        if isinstance(check, dict) and "correlation" in _upper(check.get("name")):
            passed = check_pass(check)
            return (
                "PASS" if passed is True else "FAIL" if passed is False else "PENDING",
                check,
                None,
            )
    return "UNKNOWN", {}, None


def _has_quality_failure(experiment):
    for check in _checks(experiment):
        if not isinstance(check, dict) or check_pass(check) is not False:
            continue
        name = _upper(check.get("name"))
        if "CORRELATION" in name or any(token in name.lower() for token in _NONFAILURE_BLOCKER_NAMES):
            continue
        if any(token in name.lower() for token in _QUALITY_FAILURE_NAMES):
            return True
        return True
    return False


def _is_duplicate(experiment):
    skip = _value(experiment, "skip_record")
    if isinstance(skip, dict):
        text = " ".join(str(skip.get(key, "")) for key in ("reason_code", "reason", "status"))
        if "DUPLICATE" in _upper(text):
            return True
    search = _value(experiment, "search_outcome")
    if isinstance(search, dict):
        text = " ".join(str(search.get(key, "")) for key in ("reason", "outcome_kind", "decision"))
        if "DUPLICATE" in _upper(text):
            return True
    return "DUPLICATE" in _upper(_value(experiment, "error"))


def has_research_signal(experiment):
    """Return whether existing evidence justifies retaining a real signal.

    This is deliberately a predicate, not a score.  It requires explicit
    research/economic evidence, complete finite platform metrics, healthy
    evaluation, and a positive existing quality label.  Self-correlation may
    be pending or failed because those are classification blockers, not proof
    that the signal never existed.
    """
    if _upper(_value(experiment, "status")) != "DONE":
        return False
    if _value(experiment, "error") or _is_duplicate(experiment):
        return False
    mechanism = _value(experiment, "economic_mechanism")
    falsification = _value(experiment, "falsification")
    if not isinstance(mechanism, str) or len(mechanism.strip()) < 12:
        return False
    if not isinstance(falsification, str) or not falsification.strip():
        return False
    health = _value(experiment, "health")
    if not isinstance(health, dict) or health.get("ok") is not True:
        return False
    labels = set(_quality_labels(experiment))
    if labels.intersection(_NEGATIVE_QUALITY):
        return False
    if not labels.intersection(_POSITIVE_QUALITY):
        return False
    metrics = _value(experiment, "metrics")
    if not isinstance(metrics, dict):
        return False
    if any(num(metrics.get(key)) is None for key in (
        "sharpe", "fitness", "turnover", "returns", "drawdown", "margin"
    )):
        return False
    if _has_quality_failure(experiment):
        return False
    for check in _checks(experiment):
        if not isinstance(check, dict):
            return False
        if check_pass(check) is None:
            name = _upper(check.get("name"))
            if "CORRELATION" not in name and not any(token in name.lower() for token in _NONFAILURE_BLOCKER_NAMES):
                return False
    return True


def _strong_signal(experiment):
    return bool(set(_quality_labels(experiment)).intersection(_STRONG_QUALITY))


def _submit_ready(experiment):
    eligibility = _value(experiment, "submission_eligibility")
    if not isinstance(eligibility, dict) or eligibility.get("eligible") is not True:
        return False
    if _upper(_value(experiment, "validation_status")) != "STABLE":
        return False
    report = _value(experiment, "validation_report")
    if not isinstance(report, dict) or report.get("status") != "PASS" or report.get("candidate") != "parent":
        return False
    yearly = _value(experiment, "yearly_evidence")
    if not isinstance(yearly, dict) or yearly.get("status") != "VERIFIED" or yearly.get("stable") is not True:
        return False
    self_status, check, source = _self_correlation(experiment)
    if self_status != "PASS" or source != "BRAIN alpha payload":
        return False
    if check_pass(check) is not True or num(check.get("value")) is None:
        return False
    return bool(_checks(experiment)) and all(check_pass(item) is True for item in _checks(experiment))


def _has_hard_blocker(experiment):
    self_status, _check, _source = _self_correlation(experiment)
    if self_status == "FAIL":
        return True
    if any(
        isinstance(check, dict)
        and "CORRELATION" in _upper(check.get("name"))
        and check_pass(check) is False
        for check in _checks(experiment)
    ):
        return True
    incremental = _value(experiment, "incremental_evidence")
    if isinstance(incremental, dict) and _upper(incremental.get("decision")) == "FAIL":
        return True
    yearly = _value(experiment, "yearly_evidence")
    if isinstance(yearly, dict) and (
        _upper(yearly.get("decision")) == "FAIL"
        or (yearly.get("status") == "VERIFIED" and yearly.get("stable") is False)
    ):
        return True
    report = _value(experiment, "validation_report")
    if isinstance(report, dict) and _upper(report.get("status")) == "FAIL":
        return True
    robustness = _value(experiment, "robustness_evidence")
    if isinstance(robustness, dict) and _upper(robustness.get("decision") or robustness.get("status")) == "FAIL":
        return True
    return False


def _has_pending_evidence(experiment):
    self_status, _check, _source = _self_correlation(experiment)
    if self_status in {"PENDING", "UNKNOWN"}:
        return True
    if _value(experiment, "validation_status") not in {"STABLE", "PASS"}:
        return True
    yearly = _value(experiment, "yearly_evidence")
    if not isinstance(yearly, dict) or _upper(yearly.get("status")) not in {"VERIFIED", "PASS"}:
        return True
    return False


def classify_alpha_color(experiment):
    """Return one research-state color or ``None``.

    Priority is PURPLE > GREEN > RED > BLUE > YELLOW.  The function never
    interprets a numeric metric as a standalone performance grade.
    """
    if not has_research_signal(experiment):
        return None
    if _submit_ready(experiment):
        incremental = _value(experiment, "incremental_evidence")
        if isinstance(incremental, dict) and (
            _upper(incremental.get("decision")) == "PASS"
            and _upper(incremental.get("availability")) == "AVAILABLE"
            and _upper(incremental.get("quality")) == "VERIFIED"
            and num(incremental.get("max_abs_corr")) is not None
        ):
            return "PURPLE"
        return "GREEN"
    if _has_hard_blocker(experiment):
        return "RED"
    if _strong_signal(experiment) and _has_pending_evidence(experiment):
        return "BLUE"
    return "YELLOW"


def _evidence_summary(experiment, classification):
    self_status, check, _source = _self_correlation(experiment)
    incremental = _value(experiment, "incremental_evidence")
    return {
        "classification": classification,
        "research_classification": _quality_labels(experiment),
        "self_correlation": self_status,
        "self_correlation_value": num(check.get("value")),
        "submission_eligible": bool((_value(experiment, "submission_eligibility") or {}).get("eligible")),
        "incremental_decision": incremental.get("decision") if isinstance(incremental, dict) else None,
    }


def load_color_candidates(state_dir):
    """Load the newest valid Experiment row per real Alpha id."""
    latest = {}
    path = os.path.join(state_dir, "trajectory.jsonl")
    for row in iter_jsonl_objects(path):
        alpha_id = row.get("alpha_id")
        if not alpha_id:
            continue
        current = latest.get(str(alpha_id))
        key = (num(row.get("created_at")) or 0, num(row.get("round")) or 0)
        previous_key = current[0] if current else (-1, -1)
        if key >= previous_key:
            try:
                latest[str(alpha_id)] = (key, Experiment.from_dict(row))
            except (KeyError, TypeError, ValueError):
                continue
    return [item[1] for item in sorted(latest.values(), key=lambda item: item[0])]
