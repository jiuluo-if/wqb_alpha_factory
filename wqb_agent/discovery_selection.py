"""Pure field ranking primitives used by :mod:`wqb_agent.discovery`."""

import math

from .field_metadata import normalize_coverage

_UNSET = object()


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
