"""Pure canonical semantics for final research settlement replay.

This module is a value/reducer layer only.  It owns no persistence, trajectory,
ledger, memory, client, or settlement side effects.
"""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json


_VOLATILE_KEYS = frozenset({
    "settled_at", "recorded_at", "timestamp", "updated_at", "settlement_id",
})


class SettlementReplayConflict(ValueError):
    """A settlement identity was reused for different semantic evidence."""

    code = "SETTLEMENT_REPLAY_CONFLICT"


def canonical_settlement_semantic(settlement):
    """Return semantic settlement data with provenance-only fields removed."""
    if not isinstance(settlement, Mapping):
        return settlement
    return {
        str(key): canonical_settlement_semantic(value)
        for key, value in settlement.items()
        if str(key) not in _VOLATILE_KEYS
    }


def _serialized(value):
    return json.dumps(
        canonical_settlement_semantic(value),
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )


def settlement_id(settlement):
    """Derive a stable ID from all semantic settlement evidence."""
    return hashlib.sha256(_serialized(settlement).encode("utf-8")).hexdigest()


def compare_settlements(existing, incoming):
    """Compare two settlement payloads using one canonical replay contract."""
    existing_id = existing.get("settlement_id") if isinstance(existing, Mapping) else None
    incoming_id = incoming.get("settlement_id") if isinstance(incoming, Mapping) else None
    if existing_id and incoming_id and str(existing_id) == str(incoming_id):
        if canonical_settlement_semantic(existing) != canonical_settlement_semantic(incoming):
            raise SettlementReplayConflict(
                f"{SettlementReplayConflict.code}: settlement_id={existing_id}"
            )
        return "NO_OP"
    if canonical_settlement_semantic(existing) == canonical_settlement_semantic(incoming):
        return "NO_OP"
    if settlement_id(existing) == settlement_id(incoming):
        return "NO_OP"
    return "DIFFERENT"
