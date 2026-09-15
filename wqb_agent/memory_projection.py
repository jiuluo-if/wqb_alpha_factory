"""Pure bounded projections over ExperienceMemory rows.

The functions here never mutate rows or touch ``experience.json``/``garbage``.
``ExperienceMemory`` remains the sole owner of all durable memory mutation.
"""

import math


def _number(value, default=0.0):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) else default


def recent_short_term(entries, n=5):
    ordered = sorted(
        entries or (),
        key=lambda row: (-row.get("updated", 0), -row.get("round", 0)),
    )
    return ordered[:n]


def top_next(entries, n=5):
    ordered = sorted(entries or (), key=lambda row: -_number(row.get("priority")))
    return ordered[:n]


def next_with_fields(entries, now_round=None, max_age_rounds=20):
    """Select the highest-priority actionable, non-stale next item."""
    with_fields = [row for row in entries or () if row.get("fields") or row.get("datasets")]
    if now_round is not None:
        def last_evidence_round(item):
            values = [item.get("round", 0), item.get("source", 0)]
            return max((value for value in values if isinstance(value, int)), default=0)

        with_fields = [
            row for row in with_fields
            if now_round - last_evidence_round(row) <= max_age_rounds
        ]
    if not with_fields:
        return None
    return max(with_fields, key=lambda row: _number(row.get("priority")))


def garbage_stats(entries):
    by_kind = {}
    by_reason = {}
    for tomb in entries or ():
        kind = tomb.get("kind", "?")
        reason = tomb.get("reason", "?")
        by_kind[kind] = by_kind.get(kind, 0) + 1
        by_reason[reason] = by_reason.get(reason, 0) + 1
    return {"total": len(entries or ()), "by_kind": by_kind, "by_reason": by_reason}


def learning_context(entries, question_entries=None):
    question_entries = entries if question_entries is None else question_entries
    rows = []
    for item in entries or ():
        metadata = item.get("metadata") if isinstance(item, dict) else None
        if not isinstance(metadata, dict):
            metadata = item if isinstance(item, dict) else {}
        learning = metadata.get("mechanism_learning")
        outcome = metadata.get("hypothesis_outcome")
        if not isinstance(learning, str) or not learning.strip():
            continue
        rows.append({
            "learning": learning,
            "outcome": outcome,
            "evidence_refs": list(metadata.get("evidence_refs") or []),
            "confirmation_status": metadata.get("confirmation_status"),
            "independent_lineages": list(metadata.get("independent_lineages") or []),
            "outcome_reason": metadata.get("outcome_reason"),
            "round": item.get("source_round", item.get("round")),
        })
    supported = [
        row for row in rows
        if row.get("outcome") == "SUPPORTED"
        and row.get("confirmation_status") == "INDEPENDENT_CONFIRMED"
    ][:3]
    contradicted = [
        row for row in rows
        if row.get("outcome") == "CONTRADICTED"
        and row.get("confirmation_status") == "INDEPENDENT_CONFIRMED"
    ][:3]
    unresolved_mechanisms = [
        row for row in rows if row not in supported and row not in contradicted
    ][:5]
    unresolved = []
    discriminating = []
    for item in question_entries or ():
        if not isinstance(item, dict):
            continue
        question = item.get("unresolved_question")
        if isinstance(question, str) and question.strip() and question not in unresolved:
            unresolved.append(question)
        question = item.get("next_discriminating_question")
        if isinstance(question, str) and question.strip() and question not in discriminating:
            discriminating.append(question)
    return (
        supported, contradicted, unresolved[:5], unresolved_mechanisms,
        discriminating[:5],
    )
