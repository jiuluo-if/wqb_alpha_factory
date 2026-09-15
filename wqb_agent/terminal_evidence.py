"""Pure terminal evidence predicates used by proposal execution."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from .experiment import Experiment, same_execution_identity


@dataclass(frozen=True)
class DurableTerminalEvidenceView:
    """Validated request-local terminal evidence; it performs no I/O."""

    rows: Mapping[str, Mapping]
    canonical_round_ids: frozenset[str] | None
    round_no: int

    @classmethod
    def from_validated(
        cls, rows: Mapping, canonical_round_ids, round_no: int
    ):
        return cls(
            MappingProxyType({str(key): MappingProxyType(dict(value))
                              for key, value in rows.items()
                              if isinstance(value, Mapping)}),
            (None if canonical_round_ids is None
             else frozenset(str(item) for item in canonical_round_ids)),
            int(round_no),
        )

    def assert_execution_set(self, experiments):
        """Reject identity drift after the durable snapshot was validated."""
        expected_ids = set(self.rows)
        actual_ids = {str(getattr(experiment, "id", "")) for experiment in experiments}
        if actual_ids != expected_ids:
            raise ValueError(f"TERMINAL_EVIDENCE_CHANGED: round {self.round_no}")
        for experiment in experiments:
            row = self.rows.get(str(experiment.id))
            if row is None or not same_execution_identity(experiment.to_dict(), row):
                raise ValueError(
                    f"TERMINAL_EVIDENCE_CHANGED: round {self.round_no} "
                    f"experiment {experiment.id}"
                )


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


def build_terminal_evidence_view(
    experiments, rows: Mapping, canonical_round_ids, round_no: int
):
    """Validate and freeze one durable terminal evidence read for reuse."""
    validate_terminal_snapshot(experiments, rows, canonical_round_ids, round_no)
    return DurableTerminalEvidenceView.from_validated(
        rows, canonical_round_ids, round_no
    )
