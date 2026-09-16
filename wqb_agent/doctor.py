"""Offline health checks for the Remote-First local safety surface."""

from __future__ import annotations

import os
from pathlib import Path

from .alpha_feed_cache import WeeklyAlphaFeedCache
from .config import normalize_config
from .diagnostics import DiagnosticEvent
from .simulation_gateway import ExecutionGuard

_LEGACY_ARTIFACTS = (
    "trajectory.jsonl", "trial_ledger.jsonl", "experience.json",
    "validation_reports.jsonl", "round_1.checkpoint.json",
)


def run_doctor(raw_config, *, offline=True, snapshot=None):
    """Inspect only rebuildable cache and unresolved remote-write guards.

    ``snapshot`` is accepted for callers migrating from the old diagnostic
    surface, but is deliberately ignored: local research records are not an
    input to the Remote-First health result.
    """
    del snapshot
    parsed = normalize_config(raw_config)
    state_dir = os.path.abspath(parsed.runtime.state_dir)
    guard = ExecutionGuard(state_dir)
    cache_path = os.path.join(state_dir, ".alpha_feed_cache", "remote.json")
    cache = WeeklyAlphaFeedCache(
        cache_path,
        retention_days=parsed.remote_cache.retention_days,
        weekly_simulation_cap=parsed.quota.rolling_limit,
    )
    entries = guard.entries()
    legacy = [name for name in _LEGACY_ARTIFACTS
              if Path(state_dir, name).exists()]
    diagnostics = []
    if not os.path.isdir(state_dir) or not os.access(state_dir, os.W_OK):
        diagnostics.append(DiagnosticEvent(
            "STATE_DIR_NOT_WRITABLE", "ERROR", "execution_guard", message=state_dir
        ).as_dict())
    if any(row.get("status") == "SUBMIT_UNKNOWN" for row in entries):
        diagnostics.append(DiagnosticEvent(
            "SUBMIT_UNKNOWN_REQUIRES_RECONCILIATION", "WARN", "execution_guard",
            message="只读对账已知 progress URL；不得自动重发 POST",
        ).as_dict())
    if legacy:
        diagnostics.append(DiagnosticEvent(
            "LEGACY_LOCAL_STATE_IGNORED", "WARN", "remote_first",
            message=", ".join(legacy),
        ).as_dict())
    return {
        "config_valid": True,
        "offline": bool(offline),
        "state_dir": state_dir,
        "state_dir_writable": os.path.isdir(state_dir) and os.access(state_dir, os.W_OK),
        "execution_guard": {
            "path": guard.path,
            "active_count": len(entries),
            "submit_unknown_count": sum(
                row.get("status") == "SUBMIT_UNKNOWN" for row in entries
            ),
            "statuses": [row.get("status") for row in entries],
        },
        "cache": {"path": cache.path, **cache.freshness_snapshot()},
        "legacy_artifacts_ignored": legacy,
        "diagnostics": diagnostics,
        "network_write": False,
    }
