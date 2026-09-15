"""Pure field ranking primitives used by :mod:`wqb_agent.discovery`."""

import hashlib
import math
import re

from .field_metadata import normalize_coverage

_UNSET = object()
_STOPWORDS = frozenset({
    "with", "from", "that", "this", "will", "have", "been", "being",
    "into", "over", "under", "across", "about", "their", "there", "which",
    "while", "using", "should", "would", "where", "when", "after", "before",
    "and", "or", "a", "an", "the", "of", "to", "in", "on", "for",
    "的", "了", "和", "与", "及", "在", "对", "将", "从", "是",
})


def categorize_hypothesis(hypothesis, category_keywords, category_values):
    """Return deterministic category ordering from caller-provided facts."""
    hypothesis = hypothesis if isinstance(hypothesis, dict) else {}
    text = str(hypothesis.get("statement") or "")
    tags = hypothesis.get("tags")
    tags = tags if isinstance(tags, list) else []
    combined = " ".join([text] + [str(tag or "") for tag in tags]).lower()
    scores = {
        category: sum(1 for keyword in keywords if keyword in combined)
        for category, keywords in category_keywords.items()
    }
    scores = {category: score for category, score in scores.items() if score}
    return sorted(scores, key=lambda category: (scores[category], category_values[category]), reverse=True)


def keywords_from_hypothesis(hypothesis, limit=12):
    """Extract ordered semantic tokens without consulting discovery state."""
    hypothesis = hypothesis if isinstance(hypothesis, dict) else {}
    ordered = []
    seen = set()

    def push(word):
        word = str(word or "").lower()
        if len(word) > 1 and word not in seen and word not in _STOPWORDS:
            seen.add(word)
            ordered.append(word)

    def push_text(text):
        for piece in re.findall(r"[a-z0-9]+|[\u3400-\u9fff]+", text.lower()):
            if re.fullmatch(r"[\u3400-\u9fff]+", piece):
                if len(piece) > 1:
                    push(piece)
                for size in (3, 2, 4):
                    for start in range(0, len(piece) - size + 1):
                        push(piece[start:start + size])
            else:
                push(piece)

    tags = hypothesis.get("tags")
    for tag in tags if isinstance(tags, list) else []:
        push_text(str(tag or ""))
    push_text(str(hypothesis.get("statement") or ""))
    try:
        limit = max(0, int(limit))
    except (TypeError, ValueError):
        limit = 12
    return ordered[:limit]


def select_active_dataset_ids(dataset_ids, categories, keywords, round_no, random_seed,
                              max_active_datasets, dataset_categories, provenance=None):
    """Bound a live dataset universe using frozen deterministic ordering."""
    dataset_ids = list(dict.fromkeys(str(value) for value in dataset_ids or []))
    provenance = dict(provenance or {})
    if len(dataset_ids) <= max_active_datasets:
        return dataset_ids, {
            **provenance, "active_count": len(dataset_ids), "active_pool": list(dataset_ids),
            "selection_strategy": "bounded_all",
        }
    category_ids = {
        dataset_id for category in categories
        for dataset_id in dataset_categories.get(category, [])
    }
    scored = []
    for dataset_id in dataset_ids:
        text = dataset_id.lower().replace("_", " ")
        keyword_score = sum(1 for keyword in keywords if keyword in text)
        category_score = 4 if dataset_id in category_ids else 0
        tie = hashlib.sha256(f"{random_seed}|{round_no}|{dataset_id}".encode()).hexdigest()
        scored.append((-(keyword_score + category_score), tie, dataset_id))
    scored.sort()
    active = [item[2] for item in scored[:max_active_datasets]]
    return active, {
        **provenance, "active_count": len(active), "active_pool": list(active),
        "selection_strategy": "bounded_semantic_dataset_pool",
    }


def rank_fields(fields, dataset_id, keywords, *, selection_mode, random_seed,
                random_fraction, candidate_pool_size, max_alpha_count=None,
                require_platform_alpha_count=False, hypothesis=None):
    """Rank prepared field rows and return ranking/exclusion projections.

    The input rows are already owned by the caller; this function performs no
    cache or client access and returns all diagnostics needed by the owner.
    """
    excluded_high_usage = []
    excluded_unknown_usage = []
    cheap_candidates = []
    for field in fields or ():
        if not isinstance(field, dict) or not isinstance(field.get("id"), (str, int)):
            continue
        field_id = str(field["id"])
        actual_dataset = field.get("dataset")
        if isinstance(actual_dataset, dict):
            actual_dataset = actual_dataset.get("id") or actual_dataset.get("name")
        actual_dataset = str(actual_dataset) if isinstance(actual_dataset, (str, int)) else str(dataset_id)
        alpha_count = field.get("alphaCount")
        if alpha_count is None:
            alpha_count = field.get("alpha_count")
        try:
            numeric_alpha_count = float(alpha_count)
            if numeric_alpha_count < 0 or not math.isfinite(numeric_alpha_count):
                numeric_alpha_count = None
        except (TypeError, ValueError):
            numeric_alpha_count = None
        if max_alpha_count is not None and numeric_alpha_count is None and require_platform_alpha_count:
            excluded_unknown_usage.append({"id": field_id, "dataset": actual_dataset,
                                           "reason": "platform alphaCount unavailable"})
            continue
        if max_alpha_count is not None and numeric_alpha_count is not None:
            try:
                if numeric_alpha_count > float(max_alpha_count):
                    excluded_high_usage.append({"id": field_id, "alpha_count": alpha_count,
                                                "dataset": actual_dataset})
                    continue
            except (TypeError, ValueError):
                pass
        coverage = normalize_coverage(field)
        components = score_components(field, keywords, alpha_count, coverage)
        cheap_score = components["keyword_contribution"] + components["coverage_contribution"]
        if selection_mode == "semantic" and components["keyword_contribution"] <= 0:
            continue
        if cheap_score <= 0 and selection_mode not in {"random", "semantic_random", "broad"}:
            continue
        token = "|".join((str(random_seed), str(hypothesis or {}), actual_dataset, field_id))
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        noise = int(digest[:12], 16) / float(16 ** 12)
        if selection_mode == "random":
            cheap_rank = noise
        elif selection_mode in {"semantic_random", "broad"}:
            cheap_rank = (1.0 - random_fraction) * cheap_score + random_fraction * noise
        else:
            cheap_rank = cheap_score
        cheap_candidates.append((cheap_rank, noise, field, actual_dataset, field_id, alpha_count))
    cheap_candidates.sort(key=lambda item: (-item[0], str(item[2].get("id"))))
    cheap_candidates = cheap_candidates[:candidate_pool_size]
    ranked = []
    for _cheap_rank, noise, field, actual_dataset, _field_id, alpha_count in cheap_candidates:
        components = score_components(field, keywords, alpha_count)
        score = components["keyword_contribution"] + components["coverage_contribution"] - components["alpha_count_penalty"]
        random_contribution = 0.0
        if selection_mode == "random":
            rank = noise
            random_contribution = noise
        elif selection_mode in {"semantic_random", "broad"}:
            random_contribution = random_fraction * noise
            rank = (1.0 - random_fraction) * score + random_fraction * noise
        else:
            rank = score
        if score <= 0 and selection_mode not in {"random", "semantic_random", "broad"}:
            continue
        ranked.append((rank, score, field, actual_dataset, {
            **components, "random_exploration_contribution": random_contribution,
        }))
    ranked.sort(key=lambda item: (-item[0], -item[1], str(item[2].get("id"))))
    return ranked, {
        "candidate_count": len(cheap_candidates),
        "excluded_high_usage": excluded_high_usage,
        "excluded_unknown_usage": excluded_unknown_usage,
    }


def keyword_contribution(haystack_id, haystack_name, haystack_desc, keywords):
    """Score textual evidence; this function performs no selection or I/O."""
    contribution = 0.0
    for keyword in keywords:
        if keyword in haystack_id:
            contribution += 3.0
        if keyword in haystack_name:
            contribution += 2.0
        if keyword in haystack_desc:
            contribution += 1.0
    return contribution


def score_components(field, keywords, alpha_count=None, coverage_value=_UNSET):
    """Return explainable ranking components from one raw field row."""
    haystack_id = str(field.get("id") or "").lower()
    haystack_name = str(field.get("name") or "").lower()
    haystack_desc = str(field.get("description") or "").lower()
    coverage = (
        normalize_coverage(field) if coverage_value is _UNSET else coverage_value
    )
    try:
        count = float(alpha_count)
        alpha_count_penalty = min(1.5, math.log1p(max(0.0, count)) / 10.0)
    except (TypeError, ValueError):
        alpha_count_penalty = 0.0
    return {
        "keyword_contribution": keyword_contribution(
            haystack_id, haystack_name, haystack_desc, keywords
        ),
        "coverage_contribution": (
            0.0 if coverage is None else min(2.0, max(0.0, coverage * 2.0))
        ),
        "alpha_count_penalty": alpha_count_penalty,
    }


def score_field(field, keywords, alpha_count=None):
    components = score_components(field, keywords, alpha_count)
    return (
        components["keyword_contribution"]
        + components["coverage_contribution"]
        - components["alpha_count_penalty"]
    )
