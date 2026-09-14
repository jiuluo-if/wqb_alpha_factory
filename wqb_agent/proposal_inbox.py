"""Pure transient projection for the canonical proposal inbox payload."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ProposalBatchRequest:
    """Bounded request view; it never owns or writes ``proposals.json``."""

    round_no: int
    proposals: tuple[Any, ...]
    batch_type: str | None
    hypothesis: Mapping[str, Any] | None


def parse_proposal_payload(payload, *, default_round_no):
    """Validate the structural inbox envelope without performing execution."""
    if not isinstance(payload, dict):
        return None, ["PROPOSAL_PAYLOAD_NOT_OBJECT"]
    raw_round_no = payload.get("round_no")
    if raw_round_no is None:
        round_no = default_round_no
    elif isinstance(raw_round_no, bool):
        return None, ["round_no 必须是正整数"]
    else:
        try:
            round_no = int(raw_round_no)
        except (TypeError, ValueError):
            return None, ["round_no 必须是正整数"]
        if round_no <= 0:
            return None, ["round_no 必须是正整数"]
    proposals = payload.get("proposals") or []
    if not isinstance(proposals, list):
        return None, ["proposals 必须是数组"]
    hypothesis = payload.get("hypothesis")
    if hypothesis is not None and not isinstance(hypothesis, dict):
        return None, ["hypothesis 必须是对象"]
    return ProposalBatchRequest(
        round_no=round_no,
        proposals=tuple(proposals),
        batch_type=(str(payload.get("batch_type"))
                    if payload.get("batch_type") is not None else None),
        hypothesis=dict(hypothesis) if isinstance(hypothesis, dict) else None,
    ), []
