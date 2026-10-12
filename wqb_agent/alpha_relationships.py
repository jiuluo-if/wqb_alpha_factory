"""Pure relationship labels and frequency admission helpers."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence


def relationship_labels(
    left: Mapping[str, object], right: Mapping[str, object]
) -> set[str]:
    left_tags = set(left.get("tags") or [])
    right_tags = set(right.get("tags") or [])
    left_concept = left.get("concept")
    right_concept = right.get("concept")
    labels = set()
    concepts = {left_concept, right_concept}
    if concepts == {"equity_capital", "asset_base"}:
        labels.add("equity_asset_base")
    if concepts == {"operating_cashflow", "debt_balance"}:
        labels.add("cashflow_debt_coverage")
    if concepts == {"realized_volatility", "implied_volatility"}:
        labels.add("realized_implied_volatility")
    if concepts == {"sentiment", "social_attention"}:
        labels.add("sentiment_attention_scale")
    if concepts == {"profitability_measure", "asset_efficiency"}:
        labels.add("profitability_efficiency")
    if concepts == {"market_beta", "market_correlation"}:
        labels.add("beta_correlation")
    if {left_concept, right_concept} == {"event_novelty", "sentiment"}:
        labels.add("novelty_weighted_sentiment")
    if left_concept == right_concept and left_concept != "unknown":
        labels.add("same_economic_concept")
    if {left_concept, right_concept} == {"market_price", "liquidity"}:
        labels.add("price_volume")
    if (
        left_concept == right_concept == "volatility"
        and {"option_put", "option_call"}.issubset(left_tags | right_tags)
        and bool(left_tags & {"option_put", "option_call"})
        and bool(right_tags & {"option_put", "option_call"})
    ):
        labels.add("option_pair")
    if ("analyst" in left_tags and "dispersion" in right_tags) or (
        "analyst" in right_tags and "dispersion" in left_tags
    ):
        labels.add("revision_dispersion")
    if {"price", "volatility"}.issubset(left_tags | right_tags):
        labels.add("complementary_expectations")
    if {"price", "fundamental_scale"}.issubset(left_tags | right_tags):
        labels.add("comparable_scale")
    if {"asset_scale", "earnings"}.issubset(left_tags | right_tags):
        labels.add("numerator_denominator")
    if {left_concept, right_concept} in (
        {"earnings", "valuation"},
        {"fundamental", "valuation"},
    ):
        labels.add("numerator_denominator")
    return labels


def frequency_bucket(value: object) -> str:
    text = str(value or "").lower()
    # A historical/lookback window describes the observation horizon, not how
    # often the source field is updated (for example, news linked within the
    # last 24 hours).
    text = re.sub(
        r"\b(?:within|over|during|for|in)\s+(?:the\s+)?"
        r"(?:last|past|previous|preceding)\s+\d+(?:\.\d+)?\s+"
        r"(?:minutes?|hours?|days?|weeks?|months?|years?)\b",
        " ",
        text,
    )
    if any(marker in text for marker in ("intraday", "minute", "hour")):
        return "intraday"
    if any(marker in text for marker in ("daily", "day")):
        return "daily"
    if any(marker in text for marker in ("weekly", "week")):
        return "weekly"
    if any(marker in text for marker in ("monthly", "month")):
        return "monthly"
    if "quarter" in text:
        return "quarterly"
    if any(marker in text for marker in ("annual", "year")):
        return "annual"
    return "unknown"


def frequency_compatibility(
    traits: Sequence[Mapping[str, object]], relationship_contract: str
) -> dict[str, object]:
    buckets = [frequency_bucket(item.get("frequency")) for item in traits]
    if any(bucket == "unknown" for bucket in buckets):
        return {
            "status": "REVIEW",
            "buckets": buckets,
            "reasons": ["frequency evidence is incomplete; relationship needs review"],
        }
    if len(set(buckets)) == 1:
        return {
            "status": "COMPATIBLE",
            "buckets": buckets,
            "reasons": [f"frequency compatible: {buckets[0]}"],
        }
    rank = {
        "intraday": 0,
        "daily": 1,
        "weekly": 2,
        "monthly": 3,
        "quarterly": 4,
        "annual": 5,
    }
    spread = max(rank[bucket] for bucket in buckets) - min(
        rank[bucket] for bucket in buckets
    )
    if relationship_contract == "CO_MOVEMENT" and spread >= 2:
        return {
            "status": "INCOMPATIBLE",
            "buckets": buckets,
            "reasons": [
                "frequency incompatible for direct co-movement: " + " vs ".join(buckets)
            ],
        }
    if (
        relationship_contract == "CO_MOVEMENT"
        and any(item.get("concept") == "event_count" for item in traits)
        and any(
            item.get("concept") in {"fundamental", "earnings", "valuation"}
            for item in traits
        )
        and spread >= 1
    ):
        return {
            "status": "INCOMPATIBLE",
            "buckets": buckets,
            "reasons": [
                "frequency incompatible for event-to-fundamental co-movement: "
                + " vs ".join(buckets)
            ],
        }
    return {
        "status": "REVIEW",
        "buckets": buckets,
        "reasons": ["frequency requires review: " + " vs ".join(buckets)],
    }


def relationship_type(labels: set[str]) -> str:
    for label in (
        "option_pair",
        "novelty_weighted_sentiment",
        "equity_asset_base",
        "cashflow_debt_coverage",
        "realized_implied_volatility",
        "sentiment_attention_scale",
        "profitability_efficiency",
        "beta_correlation",
        "revision_dispersion",
        "numerator_denominator",
        "same_economic_concept",
        "comparable_scale",
        "price_volume",
        "complementary_expectations",
    ):
        if label in labels:
            return label
    return "unknown"
