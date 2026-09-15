"""Pure deterministic search evidence and identity projections."""

from __future__ import annotations

import math
import re

from .evidence_status import annotate_evidence

_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|\d+(?:\.\d+)?|[^\s]")
_OPERATORS = {
    "rank", "zscore", "ts_mean", "ts_sum", "ts_std_dev", "ts_rank",
    "ts_zscore", "ts_delta", "ts_decay_linear", "delta", "delay",
    "group_neutralize", "group_rank", "scale", "log", "abs", "sign",
    "sqrt", "min", "max", "add", "sub", "mul", "div", "and", "or",
}


def _get(record, key, default=None):
    return record.get(key, default) if isinstance(record, dict) else getattr(record, key, default)


def _field_ids(record):
    values = _get(record, "fields_used") or _get(record, "fields") or []
    result = []
    for value in values:
        value = value.get("id") if isinstance(value, dict) else value
        if isinstance(value, (str, int)) and str(value):
            result.append(str(value).lower())
    return result


def research_arm_key(proposal):
    """Return the economic arm identity, excluding operator realization."""
    dataset = _get(proposal, "dataset_family") or _get(proposal, "datasets") or "unknown-dataset"
    mechanism = _get(proposal, "mechanism_family") or _get(proposal, "template_family") or "unknown-mechanism"
    if isinstance(dataset, (list, tuple)):
        dataset = "+".join(sorted(str(item) for item in dataset))
    return f"{dataset}::{mechanism}"


def _window_bucket(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return "OTHER"
    if value <= 10:
        return "SHORT"
    if value <= 60:
        return "MEDIUM"
    return "LONG"


def structural_fingerprint(expression, fields=None):
    """Return a bounded AST-like fingerprint with meaningful window buckets."""
    known = {str(item).lower() for item in (fields or [])}
    output = []
    for token in _TOKEN_RE.findall(str(expression or "").lower()):
        if token in known:
            output.append("<field>")
        elif re.fullmatch(r"\d+(?:\.\d+)?", token):
            output.append(f"<number:{_window_bucket(token)}>")
        elif re.fullmatch(r"[a-z_][a-z0-9_]*", token):
            output.append(token if token in _OPERATORS else "<identifier>")
        else:
            output.append(token)
    return " ".join(output)


def token_ngram_fingerprints(expression, fields=None):
    """Return token n-grams; this is not claimed to be a parsed AST subtree."""
    tokens = structural_fingerprint(expression, fields).split()
    return frozenset(
        " ".join(tokens[index:index + width])
        for width in (2, 3, 4, 5)
        for index in range(max(0, len(tokens) - width + 1))
    )


def syntax_diversity(record, pool):
    current = structural_fingerprint(_get(record, "expression", ""), _field_ids(record))
    other = [structural_fingerprint(_get(item, "expression", ""), _field_ids(item)) for item in pool]
    if not other:
        return {"status": "NO_POOL", "nearest": None, "score": 1.0}
    same = current in other
    return {"status": "AVAILABLE", "nearest": 1.0 if same else 0.0, "score": 0.0 if same else 1.0}


def _series_points(record):
    for key in ("returns", "pnl", "pnl_series", "signal_returns"):
        values = _get(record, key)
        if not isinstance(values, (list, tuple)):
            continue
        points = []
        for index, item in enumerate(values):
            date = None
            if isinstance(item, dict):
                date = item.get("date") or item.get("timestamp") or item.get("time")
                item = item.get("return", item.get("returns", item.get("pnl", item.get("value"))))
            try:
                value = float(item)
            except (TypeError, ValueError):
                continue
            if math.isfinite(value):
                points.append((str(date) if date is not None else index, value))
        if len(points) >= 3:
            return points
    return None


def _corr_evidence(left, right):
    if not left or not right:
        return annotate_evidence({"status": "UNAVAILABLE", "reason": "missing series"}, status="UNAVAILABLE")
    left, right = dict(left), dict(right)
    keys = sorted(set(left) & set(right))
    overlap_ratio = len(keys) / max(len(left), len(right))
    if len(keys) < 3:
        return annotate_evidence({"status": "UNAVAILABLE", "reason": "fewer than 3 date-aligned observations", "overlap_count": len(keys), "overlap_ratio": overlap_ratio}, status="UNAVAILABLE")
    a, b = [left[key] for key in keys], [right[key] for key in keys]
    ma, mb = sum(a) / len(a), sum(b) / len(b)
    da = sum((value - ma) ** 2 for value in a)
    db = sum((value - mb) ** 2 for value in b)
    if da <= 0 or db <= 0:
        return annotate_evidence({"status": "UNAVAILABLE", "reason": "constant series", "overlap_count": len(keys), "overlap_ratio": overlap_ratio}, status="UNAVAILABLE")
    signed = sum((x - ma) * (y - mb) for x, y in zip(a, b)) / math.sqrt(da * db)
    return annotate_evidence({"status": "AVAILABLE", "signed_corr": signed, "abs_corr": abs(signed), "overlap_count": len(keys), "overlap_ratio": overlap_ratio}, status="INCONCLUSIVE", availability="AVAILABLE", quality="VERIFIED", decision="INCONCLUSIVE")


def empirical_pool_summary(record, pool):
    rows = [_corr_evidence(_series_points(record), _series_points(item)) for item in pool]
    correlations = [row["signed_corr"] for row in rows if row.get("signed_corr") is not None]
    if not correlations:
        return annotate_evidence({"status": "UNAVAILABLE", "max_corr": None, "median_corr": None, "nearest_corr": None, "incremental_value": None, "correlations": rows}, status="UNAVAILABLE")
    ordered = sorted(correlations)
    max_corr = max(correlations, key=abs)
    incremental = max(0.0, 1.0 - max(abs(value) for value in correlations))
    return annotate_evidence({"status": "AVAILABLE", "max_corr": max_corr, "median_corr": ordered[len(ordered) // 2], "nearest_corr": max_corr, "incremental_value": incremental, "correlations": rows}, status="INCONCLUSIVE", availability="AVAILABLE", quality="VERIFIED", decision="INCONCLUSIVE")


def incremental_novelty(record, pool):
    syntax = syntax_diversity(record, pool)
    empirical = empirical_pool_summary(record, pool)
    fields = set(_field_ids(record))
    overlap = max((len(fields & set(_field_ids(item))) / len(fields | set(_field_ids(item))) for item in pool if fields or _field_ids(item)), default=0.0)
    empirical_score = empirical["incremental_value"]
    score = (0.4 * syntax["score"] + 0.2 * (1.0 - overlap) + 0.4 * empirical_score if empirical_score is not None else 0.7 * syntax["score"] + 0.3 * (1.0 - overlap))
    return {"score": round(max(0.0, min(1.0, score)), 6), "syntax": syntax, "empirical": empirical, "field_overlap": round(overlap, 6)}


def pareto_front(records):
    dimensions = ("sharpe", "fitness", "turnover", "margin", "drawdown", "novelty")
    maximize = {"sharpe", "fitness", "margin", "novelty"}

    def numeric(row, key):
        raw = row.get(key)
        if raw is None and key == "novelty":
            raw = row.get("novelty_score", 0.0)
        try:
            raw = float(raw)
            return raw if math.isfinite(raw) else None
        except (TypeError, ValueError):
            return None

    valid = []
    for record in records or []:
        row = dict(record) if isinstance(record, dict) else {key: _get(record, key) for key in dimensions}
        metrics = _get(record, "metrics", {}) or {}
        for key in dimensions:
            row.setdefault(key, metrics.get(key))
        if all(numeric(row, key) is not None for key in dimensions):
            valid.append((record, row))
    result = []
    for candidate, row in valid:
        dominated = False
        for other, other_row in valid:
            if other is candidate:
                continue
            comparisons = [(numeric(other_row, key), numeric(row, key), key in maximize) for key in dimensions]
            no_worse = all(left >= right if high else left <= right for left, right, high in comparisons)
            better = any(left > right if high else left < right for left, right, high in comparisons)
            if no_worse and better:
                dominated = True
                break
        if not dominated:
            result.append(candidate)
    return result
