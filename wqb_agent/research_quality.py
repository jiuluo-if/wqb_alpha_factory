"""Pure canonical quality projections over existing evidence owners."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field

_UNRESOLVED = {"PENDING", "RUNNING", "SUBMITTING", "UNKNOWN", "SUBMIT_UNKNOWN"}


def _upper(value, default="UNKNOWN"):
    text = str(value or "").upper()
    return text or default


def _dimension(report, name, *fallbacks):
    if not isinstance(report, Mapping):
        return "UNKNOWN"
    dimensions = report.get("dimensions")
    values = [
        dimensions.get(name) if isinstance(dimensions, Mapping) else None,
        report.get(name),
        report.get("status"),
        report.get("decision"),
        report.get("result"),
        *(report.get(item) for item in fallbacks),
    ]
    for value in values:
        if isinstance(value, Mapping):
            value = value.get("status") or value.get("decision") or value.get("result")
        if value not in (None, ""):
            return _upper(value)
    return "UNKNOWN"


def _checks(metrics):
    checks = metrics.get("checks") if isinstance(metrics, Mapping) else None
    if not isinstance(checks, list) or not checks:
        return "UNKNOWN"
    values = [_upper(item.get("result") if isinstance(item, Mapping) else None)
              if not (isinstance(item, Mapping) and item.get("pass") is True)
              else "PASS" for item in checks]
    if any(value in _UNRESOLVED for value in values):
        return "INCOMPLETE"
    if any(value in {"FAIL", "REJECTED"} for value in values):
        return "FAIL"
    if all(value == "PASS" for value in values):
        return "PASS"
    return "UNAVAILABLE"


@dataclass(frozen=True)
class ResearchQualityAssessment:
    experiment_id: str
    execution_round_no: int | None
    research_cycle_id: str | None
    execution_status: str
    finality: str
    evidence_completeness: str
    quality_status: str
    checks_status: str
    health_status: str
    validation_status: str
    statistical_status: str
    incremental_status: str
    yearly_status: str
    correlation_status: str
    research_classification: str
    submission_eligibility: str
    raw_trial_count: object = None
    effective_trial_count: object = None
    history_completeness: str = "UNKNOWN"
    settlement_id: str | None = None
    missing_evidence: tuple[str, ...] = field(default_factory=tuple)
    blocking_reasons: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self):
        return asdict(self)


def assess_experiment(record, *, trial_summary=None):
    """Describe evidence; never choose a research direction or recompute metrics."""
    row = record.to_dict() if hasattr(record, "to_dict") else record
    row = row if isinstance(row, Mapping) else {}
    status = _upper(row.get("status"))
    final = row.get("final_outcome") if isinstance(row.get("final_outcome"), Mapping) else None
    report = row.get("validation_report") if isinstance(row.get("validation_report"), Mapping) else {}
    metrics = row.get("metrics") if isinstance(row.get("metrics"), Mapping) else {}
    unresolved = status in _UNRESOLVED
    finality = "FINAL" if final or row.get("trajectory_revision") == "RESEARCH_SETTLED" else (
        "PROVISIONAL" if status == "DONE" else "UNRESOLVED"
    )
    validation = _upper(report.get("status") or row.get("validation_status"))
    statistical = _dimension(report, "statistical", "statistical_decision")
    incremental = _dimension(row.get("incremental_evidence"), "decision")
    yearly = _dimension(row, "yearly_evidence")
    correlation = _dimension(row, "self_correlation")
    health = "PASS" if isinstance(row.get("health"), Mapping) and row["health"].get("ok") is True else (
        "UNKNOWN" if not isinstance(row.get("health"), Mapping) else "FAIL"
    )
    quality = _upper((final or {}).get("base_quality") if final else
                     (row.get("provisional_outcome") or {}).get("base_quality") or
                     row.get("quality_label"))
    classification = _upper(row.get("research_classification"))
    eligibility = _upper(row.get("submission_eligibility"))
    summary = trial_summary if isinstance(trial_summary, Mapping) else {}
    history = _upper(summary.get("history_completeness"))
    effective = summary.get("effective_trial_count")
    if history == "INCOMPLETE_LEGACY":
        effective = "UNAVAILABLE"
    missing = []
    for name, value in (("metrics", "PASS" if metrics else "UNKNOWN"),
                        ("validation", validation), ("statistical", statistical),
                        ("yearly", yearly),
                        ("correlation", correlation)):
        if not value or value in {"UNKNOWN", "INCOMPLETE", "PENDING", "UNAVAILABLE"}:
            missing.append(name)
    blockers = []
    if unresolved:
        blockers.append("execution_unresolved")
    if validation in {"INCOMPLETE", "UNKNOWN", "PENDING"}:
        blockers.append("validation_incomplete")
    if correlation in {"UNKNOWN", "PENDING", "INCOMPLETE"}:
        blockers.append("self_correlation_unresolved")
    completeness = "INCOMPLETE" if unresolved or blockers else (
        "UNAVAILABLE" if any(value in {"UNKNOWN", "UNAVAILABLE"} for value in
                               (statistical, incremental, yearly, correlation)) else "COMPLETE"
    )
    return ResearchQualityAssessment(
        experiment_id=str(row.get("id") or row.get("experiment_id") or ""),
        execution_round_no=row.get("round"),
        research_cycle_id=row.get("research_cycle_id"),
        execution_status=status,
        finality=finality,
        evidence_completeness=completeness,
        quality_status=quality,
        checks_status=_checks(metrics),
        health_status=health,
        validation_status=validation,
        statistical_status=statistical,
        incremental_status=incremental,
        yearly_status=yearly,
        correlation_status=correlation,
        research_classification=classification,
        submission_eligibility=eligibility,
        raw_trial_count=summary.get("trial_count"),
        effective_trial_count=effective,
        history_completeness=history,
        settlement_id=(final or {}).get("settlement_id"),
        missing_evidence=tuple(sorted(set(missing))),
        blocking_reasons=tuple(sorted(set(blockers))),
    )


def assess_records(records, *, trial_summary=None, round_no=None):
    selected = [row for row in (records or ())
                if not isinstance(round_no, int) or row.get("round") == round_no]
    assessments = [assess_experiment(row, trial_summary=trial_summary) for row in selected]
    statuses = Counter(item.execution_status for item in assessments)
    summary = trial_summary if isinstance(trial_summary, Mapping) else {}
    infra = sum(
        _upper(row.get("status")) == "FAILED"
        and any(token in str(row.get("error") or row.get("reason") or "").upper()
                for token in ("TIMEOUT", "RATE_LIMIT", "AUTH", "INFRA", "NETWORK", "HTTP"))
        for row in selected
    )
    research_failures = sum(
        _upper(row.get("status")) == "FAILED"
        and not any(token in str(row.get("error") or row.get("reason") or "").upper()
                    for token in ("TIMEOUT", "RATE_LIMIT", "AUTH", "INFRA", "NETWORK", "HTTP"))
        for row in selected
    )
    row_scoped = round_no is not None
    return {
        "execution_round_no": round_no,
        "total_trials": len(assessments) if row_scoped else max(
            len(assessments), int(summary.get("trial_count", 0) or 0)
        ),
        "executed_trials": (
            sum(item.execution_status not in {"PENDING", "UNKNOWN", "SUBMIT_UNKNOWN"}
                for item in assessments)
            if row_scoped else max(
                sum(item.execution_status not in {"PENDING", "UNKNOWN", "SUBMIT_UNKNOWN"}
                    for item in assessments),
                int(summary.get("submitted_count", 0) or 0),
            )
        ),
        "unresolved_trials": sum(item.execution_status in _UNRESOLVED for item in assessments),
        "infra_failures": infra,
        "research_failures": research_failures,
        "settled_experiments": sum(item.finality == "FINAL" for item in assessments),
        "validation_completeness": Counter(item.validation_status for item in assessments),
        "classification_counts": Counter(item.research_classification for item in assessments),
        "effective_trial_count": summary.get("effective_trial_count"),
        "omitted_unverifiable_evidence_count": sum(bool(item.missing_evidence) for item in assessments),
        "settlement_conflicts": 0,
        "missing_evidence": sorted({gap for item in assessments for gap in item.missing_evidence}),
        "status_counts": statuses,
        "assessments": assessments,
    }


__all__ = ["ResearchQualityAssessment", "assess_experiment", "assess_records"]
