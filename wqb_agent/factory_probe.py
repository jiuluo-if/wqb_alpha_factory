"""Pure, bounded and privacy-safe route probe projections for the legacy factory."""

from __future__ import annotations

import hashlib
import json
import re

from .diversity import field_concept_keys, semantic_mechanism_key
from .expression import canonical_expression
from .factory_blocker import control_probe, safe_control_token, safe_nonnegative_count
from .factory_control import CONTROL_TOKENS

ROUTE_DIMENSIONS = {
    "candidate_expression": "candidate_expression_fingerprints",
    "semantic_mechanism": "semantic_mechanism_fingerprints",
    "structural_family": "structural_family_fingerprints",
    "field_concept": "field_concept_fingerprints",
    "relationship": "relationship_fingerprints",
    "dataset_route": "dataset_route",
    "research_question": "research_question_fingerprints",
}


def route_set_digest(values, *, session_id, dimension):
    normalized = sorted({str(value) for value in (values or ()) if str(value).strip()})
    canonical = json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(f"{session_id}:{dimension}:{canonical}".encode()).hexdigest()


def selection_probe(proposals, feasibility, budget_audit):
    """Project selected proposals into bounded control metadata only."""
    probe = dict(feasibility) if isinstance(feasibility, dict) else {}
    items = [item for item in (proposals or []) if isinstance(item, dict)]
    probe["candidate_expression_fingerprints"] = sorted({
        canonical_expression(item.get("expression"))
        for item in items
        if canonical_expression(item.get("expression"))
    })
    probe["semantic_mechanism_fingerprints"] = sorted({
        key for key in (semantic_mechanism_key(item) for item in items)
        if key != "UNKNOWN"
    })
    probe["structural_family_fingerprints"] = sorted({
        str(item.get("template_family") or item.get("template_id"))
        for item in items
        if item.get("template_family") or item.get("template_id")
    })
    probe["field_concept_fingerprints"] = sorted({
        concept for item in items for concept in field_concept_keys(item)
        if concept != "unknown"
    })
    probe["dataset_route"] = sorted({
        str(dataset)
        for item in items
        for dataset in (item.get("datasets") or [])
        if dataset is not None and str(dataset).strip()
    })

    def question_key(item):
        if item.get("template_mode") == "PARTIAL_OPERATOR":
            return item.get("operator_contrast_question_key")
        return item.get("experiment_question") or item.get("research_question")

    probe["research_question_fingerprints"] = sorted({
        str(question_key(item)).strip().lower()
        for item in items
        if question_key(item)
    })
    probe["budget_shortage_count"] = (
        int(budget_audit.get("shortage_count", 0))
        if isinstance(budget_audit, dict) else 0
    )
    probe["budget_shortage_reason"] = (
        budget_audit.get("shortage_reason")
        if isinstance(budget_audit, dict) else None
    )
    return probe


def route_probe_projection(probe, *, session_id):
    """Persist only bounded counts and session-bound set digests."""
    source = probe if isinstance(probe, dict) else {}
    projection = control_probe(source, allowed=CONTROL_TOKENS)
    for dimension, source_key in ROUTE_DIMENSIONS.items():
        digest_key = f"{dimension}_set_digest"
        count_key = f"{dimension}_count"
        if source_key not in source and digest_key in source:
            projection[count_key] = safe_nonnegative_count(source.get(count_key))
            digest = str(source.get(digest_key, "")).lower()
            projection[digest_key] = (
                digest if re.fullmatch(r"[0-9a-f]{64}", digest)
                else route_set_digest((), session_id=session_id, dimension=dimension)
            )
            continue
        values = source.get(source_key)
        normalized = sorted({str(value) for value in (values or ()) if str(value).strip()})
        projection[count_key] = len(normalized)
        projection[digest_key] = route_set_digest(
            normalized, session_id=session_id, dimension=dimension
        )
    for key in ("eligible_count", "selected_count", "shortage_count"):
        if key in source:
            projection[key] = safe_nonnegative_count(source.get(key))
    if "budget_shortage_count" in source:
        projection["shortage_count"] = safe_nonnegative_count(
            source.get("budget_shortage_count")
        )
    if "budget_shortage_reason" in source:
        projection["shortage_reason"] = safe_control_token(
            source.get("budget_shortage_reason"), CONTROL_TOKENS, default="UNKNOWN"
        )
    return projection
