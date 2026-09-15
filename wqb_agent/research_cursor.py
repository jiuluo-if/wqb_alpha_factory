"""Pure projections for the two-loop research cursor and cycle identity."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping


def _digest(payload):
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), default=str).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _settled_rows(rows):
    return [
        row for row in (rows or ())
        if isinstance(row, Mapping)
        and (row.get("trajectory_revision") == "RESEARCH_SETTLED"
             or isinstance(row.get("final_outcome"), Mapping))
    ]


def build_research_cursor(rows=(), *, ledger_summary=None, checkpoints=(),
                          runtime_state="READY", active_batch=None):
    """Return a deterministic, opaque cursor derived from canonical owners.

    Expressions, Alpha IDs, metrics, and timestamps are intentionally absent.
    """
    settled = _settled_rows(rows)
    settled_projection = sorted({
        (str(row.get("id")), str(
            (row.get("final_outcome") or {}).get("settlement_id")
            or row.get("settlement_id") or ""))
        for row in settled if row.get("id")
    })
    rounds = [row.get("round") for row in settled
              if isinstance(row.get("round"), int) and not isinstance(row.get("round"), bool)]
    pending = sorted({
        int(item.get("round_no") or (item.get("checkpoint") or {}).get("round_no"))
        for item in (checkpoints or ())
        if isinstance(item, Mapping)
        and isinstance(item.get("round_no") or (
            item.get("checkpoint") or {}
        ).get("round_no"), int)
        and (item.get("malformed") or (
            isinstance(item.get("checkpoint"), Mapping)
            and item["checkpoint"].get("complete") is not True
        ))
    })
    ledger_summary = ledger_summary if isinstance(ledger_summary, Mapping) else {}
    batch = active_batch if isinstance(active_batch, Mapping) else None
    semantic = {
        "settled": settled_projection,
        "history_completeness": ledger_summary.get("history_completeness") or "UNKNOWN",
        "effective_trial_count": (
            ledger_summary.get("effective_trial_count")
            if ledger_summary.get("history_completeness") != "INCOMPLETE_LEGACY"
            else "UNAVAILABLE"
        ),
        "pending_rounds": pending,
        "active_batch": batch.get("decision_fingerprint") if batch else None,
        "runtime_state": str(runtime_state or "UNKNOWN"),
    }
    latest = max(rounds) if rounds else None
    return {
        "research_cursor": _digest(semantic),
        "latest_settled_execution_round": latest,
        "pending_execution_round": pending[0] if pending else None,
        "runtime_state": semantic["runtime_state"],
    }


def research_cycle_id(source_research_cursor, decision_ids=(), *, kind="decision"):
    """Derive a stable cycle identity without using wall-clock values."""
    return "research-cycle:" + _digest({
        "kind": str(kind),
        "source_research_cursor": str(source_research_cursor or ""),
        "decision_ids": sorted(str(item) for item in (decision_ids or ())),
    })[7:]


def cycle_projection(rows, cycle_id):
    """Project one cycle from canonical rows without exposing raw evidence."""
    target = str(cycle_id or "")
    selected = [row for row in (rows or ())
                if isinstance(row, Mapping)
                and str(row.get("research_cycle_id") or "") == target]
    return {
        "research_cycle_id": target,
        "source_cursor": next((row.get("source_research_cursor") for row in selected
                                if row.get("source_research_cursor")),
                               "LEGACY_RESEARCH_CYCLE_UNVERIFIABLE"),
        "decision_identities": sorted({str(row["optimization_decision_id"])
                                        for row in selected
                                        if row.get("optimization_decision_id")}),
        "execution_rounds": sorted({row["round"] for row in selected
                                     if isinstance(row.get("round"), int)}),
        "experiment_ids": sorted({str(row["id"]) for row in selected if row.get("id")}),
        "settlement_ids": sorted({str((row.get("final_outcome") or {}).get("settlement_id"))
                                   for row in selected
                                   if isinstance(row.get("final_outcome"), Mapping)
                                   and row["final_outcome"].get("settlement_id")}),
        "quality_status": sorted({str(row.get("research_classification") or "UNKNOWN")
                                   for row in selected}),
        "cycle_status": "UNVERIFIABLE" if not selected else "OBSERVED",
    }


__all__ = ["build_research_cursor", "research_cycle_id", "cycle_projection"]
