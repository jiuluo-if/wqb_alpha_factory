"""Pure field and dataset metadata evidence helpers.

This module deliberately has no client, cache, or state owner.  It turns raw
platform metadata into conservative, auditable derived evidence.
"""

import math
import re
from copy import deepcopy


def profile_field(field, dataset_id, score, category, ranking_provenance=None,
                  *, alpha_count=None, dataset_description=None,
                  dataset_snapshot=None, source_provenance=None):
    """Build one Agent-facing field profile from prepared immutable facts."""
    field_id = str(field.get("id"))
    alpha_count = field.get("alphaCount") if alpha_count is None else alpha_count
    freq_ev = frequency_evidence(field)
    frequency = freq_ev["frequency"] if freq_ev["status"] in {"KNOWN", "INFERRED"} else None
    if frequency is None and freq_ev.get("source") == "UNKNOWN":
        dataset_evidence = dataset_description_frequency(dataset_description)
        if dataset_evidence is not None:
            snapshot = dataset_snapshot or {}
            dataset_evidence.update({
                "scope": "DATASET",
                "observed_at": snapshot.get("observed_at"),
                "fingerprint": snapshot.get("fingerprint"),
            })
            frequency = dataset_evidence["frequency"]
            freq_ev = dataset_evidence
    source = dict(source_provenance or {})
    return {
        "id": field_id,
        "name": field.get("name") or field.get("description") or field_id,
        "description": field.get("description") or "",
        "coverage": normalize_coverage(field),
        "alpha_count": alpha_count,
        "frequency": frequency,
        "frequency_evidence": freq_ev,
        "semantic_status": "KNOWN" if field.get("description") else "UNKNOWN",
        "category": category or "preferred",
        "dataset": str(dataset_id),
        "type": field.get("type"),
        "match_score": score,
        "ranking_provenance": dict(ranking_provenance or {
            "keyword_contribution": 0.0,
            "coverage_contribution": 0.0,
            "alpha_count_penalty": 0.0,
            "random_exploration_contribution": 0.0,
        }),
        "field_source": source,
        "platform_dedupe": {
            "source": source.get("kind"),
            "status": "KNOWN" if alpha_count is not None else "UNKNOWN",
            "alpha_count": alpha_count,
        },
    }


def _frequency_bucket(value):
    text = str(value or "").lower()
    markers = (
        ("intraday", "intraday"), ("minute", "intraday"),
        ("hour", "intraday"), ("daily", "daily"), ("day", "daily"),
        ("weekly", "weekly"), ("week", "weekly"),
        ("monthly", "monthly"), ("month", "monthly"),
        ("quarterly", "quarterly"), ("quarter", "quarterly"),
        ("annual", "annual"), ("yearly", "annual"), ("year", "annual"),
    )
    for marker, normalized in markers:
        if re.search(rf"\b{re.escape(marker)}\b", text):
            return normalized
    return "unknown"


def _description_frequency_matches(description):
    matches = []
    markers = (
        ("intraday", "intraday"), ("minute", "intraday"),
        ("hour", "intraday"), ("daily", "daily"), ("day", "daily"),
        ("weekly", "weekly"), ("week", "weekly"),
        ("monthly", "monthly"), ("month", "monthly"),
        ("quarterly", "quarterly"), ("quarter", "quarterly"),
        ("annual", "annual"), ("yearly", "annual"), ("year", "annual"),
    )
    for marker, normalized in markers:
        if re.search(rf"\b{re.escape(marker)}\b", description):
            if normalized not in matches:
                matches.append(normalized)
    return matches


def frequency_evidence(field):
    """Return auditable frequency evidence without hiding inference."""
    if not isinstance(field, dict):
        return {"frequency": None, "source": "UNKNOWN", "status": "UNKNOWN",
                "matched_evidence": [], "confidence": "NONE"}
    explicit = []
    for key in ("frequency", "dataFrequency", "updateFrequency"):
        value = field.get(key)
        if isinstance(value, str) and value.strip():
            explicit.append((key, value.strip()))
    if explicit:
        normalized = {_frequency_bucket(value) for _, value in explicit}
        if len(normalized) != 1 or "unknown" in normalized:
            return {"frequency": None, "source": "EXPLICIT_PLATFORM",
                    "status": "CONFLICT", "matched_evidence": explicit,
                    "confidence": "NONE"}
        description = str(field.get("description") or "").lower()
        inferred = _description_frequency_matches(description)
        if inferred and set(inferred) != normalized:
            return {"frequency": None,
                    "source": "CONFLICTING_PLATFORM_DESCRIPTION",
                    "status": "CONFLICT",
                    "matched_evidence": explicit + inferred, "confidence": "NONE"}
        return {"frequency": next(iter(normalized)),
                "source": "EXPLICIT_PLATFORM", "status": "KNOWN",
                "matched_evidence": explicit, "confidence": "HIGH"}
    inferred = _description_frequency_matches(
        str(field.get("description") or "").lower()
    )
    if len(inferred) == 1:
        return {"frequency": inferred[0], "source": "DESCRIPTION_INFERRED",
                "status": "INFERRED", "matched_evidence": inferred,
                "confidence": "MEDIUM"}
    if len(inferred) > 1:
        return {"frequency": None, "source": "DESCRIPTION_INFERRED",
                "status": "AMBIGUOUS", "matched_evidence": inferred,
                "confidence": "NONE"}
    return {"frequency": None, "source": "UNKNOWN", "status": "UNKNOWN",
            "matched_evidence": [], "confidence": "NONE"}


_PROFILE_FREQUENCY_SOURCES = frozenset({
    "EXPLICIT_PLATFORM", "DESCRIPTION_INFERRED",
    "DATASET_DESCRIPTION_INFERRED", "UNKNOWN", "CONFLICT",
    "CONFLICTING_PLATFORM_DESCRIPTION", "AMBIGUOUS", "LEGACY_REDERIVED",
})
_PROFILE_FREQUENCY_STATUSES = frozenset({
    "KNOWN", "INFERRED", "UNKNOWN", "CONFLICT", "AMBIGUOUS", "LEGACY",
})


def profile_frequency_evidence(profile):
    """Consume normalized profile evidence without re-parsing raw metadata."""
    nested = profile.get("frequency_evidence") if isinstance(profile, dict) else None
    if isinstance(nested, dict):
        source = str(nested.get("source") or "").strip().upper()
        status = str(nested.get("status") or "").strip().upper()
        if source in _PROFILE_FREQUENCY_SOURCES and status in _PROFILE_FREQUENCY_STATUSES:
            return deepcopy(nested)
    frequency = profile.get("frequency") if isinstance(profile, dict) else None
    bucket = _frequency_bucket(frequency) if isinstance(frequency, str) else "unknown"
    if bucket != "unknown":
        return {"frequency": bucket, "source": "LEGACY_REDERIVED",
                "status": "LEGACY", "matched_evidence": [], "confidence": "NONE"}
    return {"frequency": None, "source": "UNKNOWN", "status": "UNKNOWN",
            "matched_evidence": [], "confidence": "NONE"}


def normalize_frequency(field):
    evidence = frequency_evidence(field)
    return evidence["frequency"] if evidence["status"] in {"KNOWN", "INFERRED"} else None


def dataset_description_frequency(description):
    text = str(description or "").lower()
    if not text.strip():
        return None
    matches = _description_frequency_matches(text)
    if len(matches) != 1:
        return None
    bucket = matches[0]
    return {"frequency": bucket, "source": "DATASET_DESCRIPTION_INFERRED",
            "status": "INFERRED", "matched_evidence": ["dataset_description:" + bucket],
            "confidence": "MEDIUM"}


def normalize_coverage(field):
    if not isinstance(field, dict):
        return None
    for key in ("coverage", "coveragePercentage", "coverage_percent"):
        if key not in field or field[key] is None:
            continue
        value = field[key]
        if isinstance(value, bool):
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(number) or number < 0 or number > 100:
            return None
        return number if number <= 1 else number / 100.0
    return None
