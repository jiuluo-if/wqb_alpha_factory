"""Bounded, privacy-safe Factory blocker projections."""

from __future__ import annotations

import hashlib
import json


def safe_control_token(value, allowed, default="UNKNOWN"):
    if isinstance(value, dict):
        value = value.get("status") or value.get("state") or value.get("availability")
    if not isinstance(value, (str, int, float, bool)):
        return default
    token = str(value).strip().upper()
    return token if token in allowed else default


def safe_nonnegative_count(value):
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return 0


def blocker_signature(kind, probe, *, control_tokens):
    probe = probe if isinstance(probe, dict) else {}
    counts = probe.get("rejection_reason_counts")
    counts = counts if isinstance(counts, dict) else {}
    safe = {
        "kind": safe_control_token(kind, control_tokens),
        "failure_taxonomy": safe_control_token(probe.get("failure_taxonomy"), control_tokens),
        "frequency_evidence": safe_control_token(probe.get("frequency_evidence"), control_tokens),
        "capability_status": safe_control_token(probe.get("capability_status"), control_tokens),
        "shortage_reason": safe_control_token(probe.get("budget_shortage_reason"), control_tokens, "NONE"),
        "shortage_count": safe_nonnegative_count(probe.get("budget_shortage_count")),
        "rejection_reason_counts": dict(sorted(
            (safe_control_token(key, control_tokens), safe_nonnegative_count(value))
            for key, value in counts.items() if safe_nonnegative_count(value) > 0
        )),
    }
    return hashlib.sha256(json.dumps(safe, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def blocker_projection(kind, probe, *, now, previous=None, recheck_sec, control_tokens):
    previous = previous if isinstance(previous, dict) else {}
    signature = blocker_signature(kind, probe, control_tokens=control_tokens)
    same = previous.get("signature") == signature and previous.get("kind") == kind
    try:
        consecutive = int(previous.get("consecutive_same_count", 0)) if same else 0
    except (TypeError, ValueError):
        consecutive = 0
    return {
        "kind": str(kind), "signature": signature,
        "consecutive_same_count": consecutive + 1,
        "first_seen_at": previous.get("first_seen_at", now) if same else now,
        "last_seen_at": now, "next_recheck_at": now + max(0.0, float(recheck_sec)),
        "resume_hint": "等待上游 control-plane evidence 更新后重试一次",
    }


def control_probe(probe, *, allowed, stats=None):
    result = dict(probe) if isinstance(probe, dict) else {}
    if isinstance(stats, dict):
        result["rejection_reason_counts"] = dict(stats.get("rejection_reason_counts") or {})
        result["failure_taxonomy"] = stats.get("status") or result.get("failure_taxonomy")
    values = {
        "failure_taxonomy": safe_control_token(result.get("failure_taxonomy"), allowed),
        "frequency_evidence": safe_control_token(result.get("frequency_evidence"), allowed),
        "capability_status": safe_control_token(result.get("capability_status"), allowed),
        "budget_shortage_reason": safe_control_token(result.get("budget_shortage_reason"), allowed, "NONE"),
        "budget_shortage_count": safe_nonnegative_count(result.get("budget_shortage_count")),
        "rejection_reason_counts": {
            safe_control_token(key, allowed): safe_nonnegative_count(value)
            for key, value in (result.get("rejection_reason_counts") or {}).items()
            if safe_nonnegative_count(value) > 0
        },
    }
    return {key: values[key] for key in values if key in result or key == "failure_taxonomy"}
