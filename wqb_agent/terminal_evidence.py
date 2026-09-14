"""Pure terminal evidence predicates used by proposal execution."""

from __future__ import annotations


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
