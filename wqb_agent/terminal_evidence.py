"""Pure terminal evidence predicates used by proposal execution."""

from __future__ import annotations

from collections.abc import Mapping

from .state import Experiment, same_execution_identity


def has_full_terminal_evidence(experiment) -> bool:
    status = str(getattr(experiment, "status", "") or "").upper()
    if status == "DONE":
        return isinstance(getattr(experiment, "metrics", None), dict) and bool(
            experiment.metrics
        )
    if status == "FAILED":
        return bool(getattr(experiment, "error", None))
    if status in {"SKIPPED", "SKIPPED_STALE", "SKIPPED_UNKNOWN"}:
        return bool(
            getattr(experiment, "skip_record", None)
            or getattr(experiment, "error", None)
        )
    return False


def failure_category(experiment):
    if str(getattr(experiment, "status", "") or "").upper() != "FAILED":
        return None
    error = str(getattr(experiment, "error", "") or "").upper()
    if any(
        token in error
        for token in ("TIMEOUT", "RATE_LIMIT", "AUTH", "INFRA", "NETWORK", "HTTP")
    ):
        return "INFRA"
    if any(
        token in error
        for token in (
            "SIMULATION REJECTED",
            "SYNTAX",
            "INVALID SETTINGS",
            "INVALID FIELD",
        )
    ):
        return "RESEARCH"
    return None


def validate_terminal_snapshot(experiments, rows: Mapping, canonical_round_ids, round_no):
    """Validate a durable, immutable terminal snapshot without performing I/O."""
    expected_ids = {str(experiment.id) for experiment in experiments}
    if canonical_round_ids is not None and set(canonical_round_ids) != expected_ids:
        raise ValueError(f"FINALIZE_EXECUTION_SET_MISMATCH: round {round_no}")
    for experiment in experiments:
        row = rows.get(experiment.id)
        if row is None or not same_execution_identity(experiment.to_dict(), row):
            raise ValueError(
                f"TERMINAL_EVIDENCE_UNRECOVERABLE: round {round_no} experiment {experiment.id}"
            )
        durable = Experiment.from_dict(row)
        if not has_full_terminal_evidence(durable):
            raise ValueError(
                f"TERMINAL_EVIDENCE_UNRECOVERABLE: round {round_no} experiment {experiment.id}"
            )
