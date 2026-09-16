"""Read-only audit of the Remote-First execution safety records."""

from __future__ import annotations

import json
import os

from .alpha_feed_cache import WeeklyAlphaFeedCache
from .simulation_gateway import ExecutionGuard


def _raw_guard_entries(path):
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError:
        return [], []
    except (OSError, ValueError, TypeError):
        return [], ["EXECUTION_GUARD_UNREADABLE"]
    rows = payload.get("entries") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return [], ["EXECUTION_GUARD_SCHEMA_INVALID"]
    return [row for row in rows if isinstance(row, dict)], []


def audit_execution_surface(state_dir):
    """Audit only the local ExecutionGuard and rebuildable remote cache."""
    directory = os.path.abspath(str(state_dir))
    guard = ExecutionGuard(directory)
    raw_rows, errors = _raw_guard_entries(guard.path)
    fingerprints = [str(row.get("execution_fingerprint")) for row in raw_rows
                    if row.get("execution_fingerprint")]
    if len(fingerprints) != len(set(fingerprints)):
        errors.append("DUPLICATE_EXECUTION_FINGERPRINT")
    valid = ExecutionGuard.STATUSES
    if any(row.get("status") not in valid for row in raw_rows):
        errors.append("EXECUTION_GUARD_STATUS_INVALID")
    cache_path = os.path.join(directory, ".alpha_feed_cache", "remote.json")
    # Audit must remain usable without credentials or a config file.  The
    # default seven-day cache contract is the only cache shape checked here.
    cache = WeeklyAlphaFeedCache(cache_path, retention_days=7)
    findings = [
        {"code": code, "source": "execution_guard"} for code in errors
    ]
    return {
        "ok": not errors,
        "errors": list(dict.fromkeys(errors)),
        "warnings": [],
        "blocking": bool(errors),
        "findings": findings,
        "execution_guard": {
            "path": guard.path,
            "active_count": len(guard.entries()),
            "statuses": [row.get("status") for row in guard.entries()],
        },
        "cache": {"path": cache.path, **cache.freshness_snapshot()},
        "network_write": False,
    }
