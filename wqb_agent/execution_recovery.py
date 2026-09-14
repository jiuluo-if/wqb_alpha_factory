"""Pure checkpoint and canonical Trajectory recovery projections."""

from __future__ import annotations

from collections.abc import Mapping, Sequence


def merge_checkpoint_with_trajectory(
    experiments: Sequence,
    canonical_rows: Mapping,
    round_no: int,
    *,
    terminal_statuses: set[str],
):
    """Prefer complete canonical evidence and repoll sparse known identities."""
    merged = []
    for experiment in experiments:
        row = canonical_rows.get(experiment.id)
        if row is not None:
            from .state import Experiment, same_execution_identity
            from .terminal_evidence import has_full_terminal_evidence

            if not same_execution_identity(experiment.to_dict(), row):
                raise ValueError(
                    f"CHECKPOINT_TRAJECTORY_IDENTITY_MISMATCH: round {round_no} experiment {experiment.id}"
                )
            canonical = Experiment.from_dict(row)
            if canonical.status in terminal_statuses and has_full_terminal_evidence(
                canonical
            ):
                merged.append(canonical)
                continue
        if experiment.status in terminal_statuses:
            if not getattr(experiment, "progress_url", None):
                raise ValueError(
                    f"TERMINAL_EVIDENCE_UNRECOVERABLE: round {round_no} experiment {experiment.id}"
                )
            experiment.status = "RUNNING"
        merged.append(experiment)
    return merged
