"""Conservative color projections and explicitly authorized remote updates."""

from __future__ import annotations

from collections.abc import Callable, Mapping


def classify_remote_color(row):
    if not isinstance(row, Mapping):
        return None
    alpha = row.get("alpha") if isinstance(row.get("alpha"), Mapping) else row
    status = str(alpha.get("status") or row.get("status") or "UNKNOWN").upper()
    if status not in {"DONE", "SUCCESS", "COMPLETE"}:
        return None
    metrics = alpha.get("is") if isinstance(alpha.get("is"), Mapping) else {}
    checks = metrics.get("checks")
    if not isinstance(metrics, Mapping):
        return None
    if isinstance(checks, list) and any(
        isinstance(check, Mapping) and check.get("pass") is False for check in checks
    ):
        return "RED"
    correlation = row.get("self_correlation")
    if isinstance(correlation, Mapping) and str(correlation.get("status") or "").upper() == "FAIL":
        return "RED"
    try:
        sharpe = float(metrics["sharpe"])
        fitness = float(metrics["fitness"])
        turnover = float(metrics["turnover"])
    except (KeyError, TypeError, ValueError):
        return None
    if sharpe >= 1.25 and fitness >= 1.0 and turnover <= 0.7:
        return "GREEN"
    if sharpe >= 0.9 and fitness >= 0.6:
        return "BLUE"
    return "YELLOW"


def preview_remote_colors(rows):
    result = []
    for row in rows or ():
        if not isinstance(row, Mapping) or not row.get("alpha_id"):
            continue
        desired = classify_remote_color(row)
        if desired is None:
            continue
        alpha = row.get("alpha") if isinstance(row.get("alpha"), Mapping) else {}
        old = alpha.get("color")
        result.append({"alpha_id": str(row["alpha_id"]), "old_color": old,
                       "desired_color": desired,
                       "action": "NOOP" if old == desired else "DRY_RUN_PATCH"})
    return result


def sync_remote_colors(rows, *, get_alpha: Callable, set_alpha_color: Callable,
                       dry_run=False, overwrite=False):
    result = []
    for row in rows or ():
        if not isinstance(row, Mapping) or not row.get("alpha_id"):
            continue
        desired = classify_remote_color(row)
        if desired is None:
            continue
        alpha_id = str(row["alpha_id"])
        remote = get_alpha(alpha_id)
        old = remote.get("color") if isinstance(remote, Mapping) else None
        if old == desired:
            action = "NOOP"
        elif old is not None and not overwrite:
            action = "PRESERVED_EXISTING"
        elif dry_run:
            action = "DRY_RUN_PATCH"
        else:
            readback = set_alpha_color(alpha_id, desired, verify=True)
            if not isinstance(readback, Mapping) or readback.get("color") != desired:
                raise ValueError("COLOR_READBACK_MISMATCH")
            action = "PATCHED"
        result.append({"alpha_id": alpha_id, "old_color": old,
                       "desired_color": desired, "action": action})
    return result
