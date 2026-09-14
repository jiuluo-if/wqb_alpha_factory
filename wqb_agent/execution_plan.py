"""Pure checkpoint disposition planning for proposal execution."""

from __future__ import annotations

from dataclasses import dataclass

NEW = "NEW"
RESUME = "RESUME"
BLOCK = "BLOCK"
COMPLETE = "COMPLETE"
FORCE_NEW_AUTHORIZED = "FORCE_NEW_AUTHORIZED"


@dataclass(frozen=True)
class CheckpointPlan:
    disposition: str
    current: dict | None = None
    foreign: dict | None = None
    reason: str | None = None


def plan_checkpoint_disposition(records, round_no, *, allow_force=False):
    """Choose a disposition; reading and mutation remain with the workflow."""
    records = tuple(record for record in records or () if isinstance(record, dict))
    foreign = next(
        (
            record for record in records
            if record.get("round_no") != round_no
            and (
                record.get("malformed")
                or record.get("checkpoint", {}).get("complete") is not True
            )
        ),
        None,
    )
    if foreign and foreign.get("validation_code") is not None:
        return CheckpointPlan(BLOCK, foreign=foreign, reason="FOREIGN_IDENTITY")
    if foreign and not allow_force:
        return CheckpointPlan(BLOCK, foreign=foreign, reason="FOREIGN_UNFINISHED")
    current = next(
        (record for record in records if record.get("round_no") == round_no),
        None,
    )
    if current and current.get("malformed"):
        return CheckpointPlan(BLOCK, current=current, reason="CURRENT_MALFORMED")
    checkpoint = current.get("checkpoint") if current else None
    if checkpoint and checkpoint.get("complete") is not True:
        return CheckpointPlan(RESUME, current=current)
    if checkpoint and checkpoint.get("complete") is True:
        return CheckpointPlan(COMPLETE, current=current)
    if foreign and allow_force:
        return CheckpointPlan(FORCE_NEW_AUTHORIZED, current=current, foreign=foreign)
    return CheckpointPlan(NEW, current=current)
