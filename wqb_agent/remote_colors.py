"""Structural color plans and explicitly authorized remote metadata updates.

Colors are a human-facing projection of structural groups. They are never
derived from quality metrics and never act as a research decision or winner
selector.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from types import MappingProxyType

from .alpha_grouping import group_remote_evidence, quality_state
from .client import ALPHA_COLOR_VALUES

MAX_ACTIVE_COLOR_GROUPS = 5


def _normalize_color(value):
    if value is None:
        return None
    return str(value).strip().upper() or None


def _normalize_assignments(assignments):
    if assignments is None:
        return {}
    if not isinstance(assignments, Mapping):
        raise TypeError("assignments must be a mapping of structural group to color")
    if len(assignments) > MAX_ACTIVE_COLOR_GROUPS:
        raise ValueError(f"MAX_ACTIVE_COLOR_GROUPS={MAX_ACTIVE_COLOR_GROUPS}")
    normalized = {}
    for group_key, color in assignments.items():
        key = str(group_key).strip()
        desired = _normalize_color(color)
        if not key:
            raise ValueError("structural_group_key must be non-empty")
        if desired not in ALPHA_COLOR_VALUES:
            raise ValueError(
                f"unsupported Alpha color {color!r}; expected one of "
                f"{sorted(ALPHA_COLOR_VALUES)}"
            )
        normalized[key] = desired
    return normalized


def _row_color(row):
    alpha = row.get("alpha") if isinstance(row.get("alpha"), Mapping) else {}
    return _normalize_color(alpha.get("color") or row.get("color"))


def _plan_action(old_color, desired_color, *, overwrite):
    if desired_color is None:
        return "UNASSIGNED" if old_color is None else "PRESERVED_EXISTING"
    if old_color == desired_color:
        return "NOOP"
    if old_color is not None and not overwrite:
        return "PRESERVED_EXISTING"
    return "PATCH"


def preview_remote_colors(rows, assignments=None, *, overwrite=False):
    """Build an immutable, reviewable plan from one remote evidence snapshot."""
    normalized_assignments = _normalize_assignments(assignments)
    groups = group_remote_evidence(rows)
    unknown_groups = set(normalized_assignments) - set(groups["structural"])
    if unknown_groups:
        raise ValueError("UNKNOWN_STRUCTURAL_GROUP")
    result = []
    for structural_key, members in sorted(groups["structural"].items()):
        existing_colors = tuple(sorted({color for color in (_row_color(row) for row in members) if color}))
        qualities = {quality_state(row) for row in members}
        group_quality = next(iter(qualities), "UNKNOWN") if len(qualities) == 1 else "MIXED_QUALITY"
        existing_color_state = (
            "MIXED_EXISTING_COLOR" if len(existing_colors) > 1
            else "UNIFORM_EXISTING_COLOR" if existing_colors
            else "NO_EXISTING_COLOR"
        )
        desired = normalized_assignments.get(structural_key)
        for row in sorted(members, key=lambda item: str(item.get("alpha_id"))):
            old_color = _row_color(row)
            result.append(MappingProxyType({
                "alpha_id": str(row["alpha_id"]),
                "structural_group_key": structural_key,
                "group_size": len(members),
                "quality_state": group_quality,
                "existing_color_state": existing_color_state,
                "expected_old_color": old_color,
                "desired_color": desired,
                "action": _plan_action(old_color, desired, overwrite=overwrite),
                "existing_colors": existing_colors,
            }))
    return tuple(result)


def _plan_entry(entry):
    if not isinstance(entry, Mapping):
        raise TypeError("sync_alpha_colors requires an exact preview plan")
    required = {
        "alpha_id", "structural_group_key", "group_size", "quality_state",
        "existing_color_state", "expected_old_color", "desired_color", "action",
        "existing_colors",
    }
    if not required.issubset(entry):
        raise ValueError("EXACT_COLOR_PLAN_REQUIRED")
    alpha_id = str(entry["alpha_id"]).strip()
    if not alpha_id:
        raise ValueError("alpha_id must be non-empty")
    desired = _normalize_color(entry["desired_color"])
    if desired is not None and desired not in ALPHA_COLOR_VALUES:
        raise ValueError("EXACT_COLOR_PLAN_REQUIRED")
    return alpha_id, _normalize_color(entry["expected_old_color"]), desired


def sync_remote_colors(plan, *, get_alpha: Callable, set_alpha_color: Callable,
                       dry_run=False, overwrite=False):
    """Apply only an exact preview plan with a client-side stale read gate."""
    if isinstance(plan, Mapping) or isinstance(plan, (str, bytes)):
        raise TypeError("sync_alpha_colors requires an exact preview plan")
    result = []
    for entry in plan or ():
        alpha_id, expected, desired = _plan_entry(entry)
        item = dict(entry)
        if desired is None:
            item["action"] = "UNASSIGNED" if expected is None else "PRESERVED_EXISTING"
            result.append(item)
            continue
        remote = get_alpha(alpha_id)
        current = _normalize_color(remote.get("color") if isinstance(remote, Mapping) else None)
        item["current_color"] = current
        if current != expected:
            item["action"] = "STALE_PLAN"
            result.append(item)
            continue
        if current == desired:
            item["action"] = "NOOP"
        elif current is not None and not overwrite:
            item["action"] = "PRESERVED_EXISTING"
        elif dry_run:
            item["action"] = "DRY_RUN_PATCH"
        else:
            readback = set_alpha_color(alpha_id, desired, verify=True)
            actual = _normalize_color(
                readback.get("color") if isinstance(readback, Mapping) else None
            )
            if actual != desired:
                raise ValueError("COLOR_READBACK_MISMATCH")
            item["action"] = "PATCHED"
        result.append(item)
    return result
