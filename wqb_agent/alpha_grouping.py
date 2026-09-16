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
