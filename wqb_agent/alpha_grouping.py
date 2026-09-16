"""Pure grouping and duplicate projections over remote Alpha evidence."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping

from .expression import analyze_expression, submission_fingerprint


def structural_fingerprint(expression):
    analysis = analyze_expression(expression)
    operators = set(analysis.operators)
    skeleton = re.sub(
        r"\b[A-Za-z_][A-Za-z0-9_]*\b",
        lambda match: match.group(0) if match.group(0).casefold() in operators else "FIELD",
        analysis.canonical,
    )
    return hashlib.sha256(skeleton.encode("utf-8")).hexdigest()


def _quality(row):
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
    groups = {"execution": {}, "structural": {}, "quality": {}}
    for row in rows or ():
        if not isinstance(row, Mapping) or not row.get("alpha_id"):
            continue
        alpha = row.get("alpha") if isinstance(row.get("alpha"), Mapping) else {}
        expression = alpha.get("regular") or row.get("expression")
        if not isinstance(expression, str) or not expression.strip():
            continue
        settings = row.get("settings") if isinstance(row.get("settings"), Mapping) else {}
        execution = row.get("execution_fingerprint") or submission_fingerprint(expression, settings)
        structural = row.get("structural_fingerprint") or structural_fingerprint(expression)
        item = dict(row)
        for key, value in (("execution", execution), ("structural", structural), ("quality", _quality(row))):
            groups[key].setdefault(str(value), []).append(item)
    return groups


def find_remote_duplicates(rows, alpha_id):
    groups = group_remote_evidence(rows)
    for members in groups["execution"].values():
        if any(str(item.get("alpha_id")) == str(alpha_id) for item in members):
            return {"alpha_id": str(alpha_id), "kind": "EXACT", "matches": members}
    return {"alpha_id": str(alpha_id), "kind": "UNKNOWN", "matches": []}


def find_remote_similar(rows, expression_or_alpha_id):
    """Return advisory exact/structural matches from remote evidence only."""
    rows = [row for row in (rows or ()) if isinstance(row, Mapping)]
    target = None
    target_id = None
    for row in rows:
        row_id = str(row.get("alpha_id") or row.get("id") or "")
        alpha = row.get("alpha") if isinstance(row.get("alpha"), Mapping) else {}
        expression = alpha.get("regular") or row.get("expression")
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
    target_structural = structural_fingerprint(target)
    exact = []
    structural = []
    for row in rows:
        row_id = str(row.get("alpha_id") or row.get("id") or "")
        if target_id and row_id == target_id:
            continue
        alpha = row.get("alpha") if isinstance(row.get("alpha"), Mapping) else {}
        expression = alpha.get("regular") or row.get("expression")
        if not isinstance(expression, str) or not expression.strip():
            continue
        item = dict(row)
        if not target_id and expression.strip() == target.strip():
            continue
        fingerprint = row.get("execution_fingerprint") or submission_fingerprint(
            expression, row.get("settings") if isinstance(row.get("settings"), Mapping) else {}
        )
        if target_exec and fingerprint == target_exec:
            exact.append(item)
        elif structural_fingerprint(expression) == target_structural:
            structural.append(item)
    if exact:
        return {"kind": "EXACT", "matches": exact}
    if structural:
        return {"kind": "STRUCTURALLY_SIMILAR", "matches": structural}
    return {"kind": "UNKNOWN", "matches": []}
