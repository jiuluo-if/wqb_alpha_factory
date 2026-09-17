"""Pure grouping and duplicate projections over remote Alpha evidence."""

from __future__ import annotations

from collections.abc import Mapping

from .expression import expression_identity_keys, submission_fingerprint


def structural_fingerprint(expression):
    return expression_identity_keys(expression)[0]


def variant_family_fingerprint(expression):
    """Return an advisory family key; never use it for execution safety."""
    return expression_identity_keys(expression)[1]


def _expression(row):
    alpha = row.get("alpha") if isinstance(row.get("alpha"), Mapping) else {}
    return alpha.get("regular") or row.get("expression")


def quality_state(row):
    """Return textual evidence state without ranking or selecting an Alpha."""
    alpha = row.get("alpha") if isinstance(row.get("alpha"), Mapping) else {}
    metrics = alpha.get("is") if isinstance(alpha.get("is"), Mapping) else {}
    checks = metrics.get("checks")
    if isinstance(checks, list) and any(
        isinstance(check, Mapping) and check.get("pass") is False for check in checks
    ):
        return "FAILED_CHECK"
    status = str(row.get("status") or alpha.get("status") or "UNKNOWN").upper()
    return status if status else "UNKNOWN"


def group_remote_evidence(rows):
    groups = {"execution": {}, "structural": {}, "variant_family": {}, "quality": {}}
    for row in rows or ():
        if not isinstance(row, Mapping) or not row.get("alpha_id"):
            continue
        expression = _expression(row)
        if not isinstance(expression, str) or not expression.strip():
            continue
        settings = row.get("settings") if isinstance(row.get("settings"), Mapping) else {}
        execution = row.get("execution_fingerprint") or submission_fingerprint(expression, settings)
        derived_structural, variant_family = expression_identity_keys(expression)
        structural = row.get("structural_fingerprint") or derived_structural
        item = dict(row)
        item["structural_group_key"] = str(structural)
        item["variant_family_key"] = str(variant_family)
        for key, value in ((
            ("execution", execution), ("structural", structural),
            ("variant_family", variant_family), ("quality", quality_state(row)),
        )):
            groups[key].setdefault(str(value), []).append(item)
    for members in groups["variant_family"].values():
        for item in members:
            item["observed_variant_count"] = len(members)
    for projection in groups.values():
        for members in projection.values():
            members.sort(key=lambda item: str(item.get("alpha_id") or item.get("id") or ""))
    return groups


def find_remote_duplicates(rows, alpha_id):
    groups = group_remote_evidence(rows)
    for members in groups["execution"].values():
        if any(str(item.get("alpha_id")) == str(alpha_id) for item in members):
            return {"alpha_id": str(alpha_id), "kind": "EXACT", "matches": members}
    return {"alpha_id": str(alpha_id), "kind": "UNKNOWN", "matches": []}


def find_remote_similar(rows, expression_or_alpha_id):
    """Return advisory exact/strict/family matches from remote evidence only."""
    rows = [row for row in (rows or ()) if isinstance(row, Mapping)]
    target = None
    target_id = None
    for row in rows:
        row_id = str(row.get("alpha_id") or row.get("id") or "")
        expression = _expression(row)
        if str(expression_or_alpha_id) == row_id:
            target = expression
            target_id = row_id
            break
    if target is None:
        target = str(expression_or_alpha_id or "")
    if not target.strip():
        return {"kind": "UNKNOWN", "matches": []}
    target_exec = submission_fingerprint(
        target, next((row.get("settings") for row in rows
                      if str(row.get("alpha_id") or row.get("id") or "") == target_id), {})
    ) if target_id else None
    target_structural, target_family = expression_identity_keys(target)
    groups = group_remote_evidence(rows)

    def without_target(items):
        result = []
        for item in items:
            item_id = str(item.get("alpha_id") or item.get("id") or "")
            expression = _expression(item)
            if target_id and item_id == target_id:
                continue
            if not target_id and expression == target:
                continue
            result.append(item)
        return result

    exact = without_target(groups["execution"].get(target_exec, ())) if target_exec else []
    structural = without_target(groups["structural"].get(target_structural, ()))
    family = without_target(groups["variant_family"].get(target_family, ()))
    if exact:
        return {"kind": "EXACT", "matches": exact}
    if structural:
        return {"kind": "STRUCTURALLY_SIMILAR", "matches": structural}
    if family:
        return {"kind": "VARIANT_FAMILY", "matches": family}
    return {"kind": "UNKNOWN", "matches": []}
