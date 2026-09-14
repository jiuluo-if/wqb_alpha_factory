"""Conservative, non-persistent field semantic profiling."""

from __future__ import annotations

from collections.abc import Mapping

from .field_metadata import normalize_coverage

_SEMANTIC_CONCEPT_RULES = (
    ("data_quality", ("missing", "null", "nan", "quality", "coverage", "stale")),
    ("analyst_revision", ("revision", "revised", "estimate change", "forecast change")),
    ("option_relative", ("put call", "put-call", "putcall", "iv skew")),
    (
        "liquidity",
        (
            "open interest",
            "option volume",
            "liquidity",
            "trading volume",
            "dollar volume",
            "turnover",
            "bid ask",
            "bid-ask",
        ),
    ),
    (
        "volatility",
        ("volatility", "implied vol", "realized vol", "iv_skew", "variance"),
    ),
    (
        "event_count",
        (
            "mention count",
            "event count",
            "number of events",
            "occurrence",
            "filing count",
        ),
    ),
    (
        "sentiment",
        ("sentiment", "social", "news", "recommendation", "bullish", "bearish"),
    ),
    (
        "valuation",
        (
            "valuation",
            "target price",
            "price target",
            "price-to",
            "price to",
            "multiple",
            "p/e",
            "p/b",
        ),
    ),
    (
        "earnings",
        ("earnings", "eps", "revenue", "sales", "profit", "cash flow", "fscore"),
    ),
    (
        "fundamental",
        (
            "total assets",
            "assets",
            "liabilities",
            "equity",
            "book value",
            "debt",
            "fundamental",
        ),
    ),
    ("market_price", ("price", "close", "open", "high", "low", "vwap", "return")),
)

_SEMANTIC_MEASUREMENT_RULES = (
    ("dispersion", ("dispersion", "skew", "spread", "standard deviation", "std dev")),
    ("ratio", ("ratio", "percent", "%", "margin", "yield", "multiple", "p/e", "p/b")),
    (
        "change",
        (
            "revision",
            "revised",
            "change",
            "delta",
            "growth",
            "return",
            "momentum",
            "surprise",
            "diff",
        ),
    ),
    ("count", ("count", "number", "mentions", "events", "occurrence", "volume")),
    ("probability", ("probability", "likelihood", "rating", "recommendation")),
)


def _profile_text(
    profile: Mapping[str, object], *, include_dataset: bool = True
) -> str:
    values = []
    keys = ("id", "name", "description")
    if include_dataset:
        keys = (*keys, "dataset", "frequency", "category")
    for key in keys:
        value = profile.get(key)
        if isinstance(value, Mapping):
            value = value.get("id") or value.get("name")
        if value is not None:
            values.append(str(value))
    return " ".join(values).lower().replace("_", " ")


def _has(text: str, phrase: object) -> bool:
    phrase_text = str(phrase).lower()
    return bool(phrase_text) and phrase_text in text


def _frequency_text(profile: Mapping[str, object]) -> str:
    value = profile.get("frequency")
    if isinstance(value, Mapping):
        value = value.get("name") or value.get("id")
    return str(value or "").lower()


def derive_field_semantic_traits(
    profile: Mapping[str, object] | None,
) -> dict[str, object]:
    """Derive a conservative semantic view without changing the field profile."""
    if not isinstance(profile, Mapping):
        return {
            "concept": "unknown",
            "measurement": "unknown",
            "behavior": "unknown",
            "frequency": "unknown",
            "sign_semantics": "unknown",
            "direction_meaning": "unknown",
            "update_style": "unknown",
            "tags": [],
            "status": "UNKNOWN",
            "semantic_admission": "UNKNOWN",
            "metadata_semantics": "UNKNOWN",
        }
    text = _profile_text(profile)
    direct_text = _profile_text(profile, include_dataset=False)
    category = str(profile.get("category") or "").lower()
    frequency = _frequency_text(profile)
    concept = "unknown"
    concept_hits = []
    direct_concept_hits = []
    for candidate, keywords in _SEMANTIC_CONCEPT_RULES:
        hits = [word for word in keywords if _has(text, word)]
        if hits:
            concept = candidate
            concept_hits = hits
            direct_concept_hits = [word for word in keywords if _has(direct_text, word)]
            break
    if concept == "unknown":
        concept = {
            "analyst": "analyst",
            "option": "option",
            "options": "option",
            "fundamental": "fundamental",
            "social": "sentiment",
            "news": "sentiment",
            "liquidity": "liquidity",
        }.get(category, "unknown")
        concept_hits = [category] if concept != "unknown" else []
        direct_concept_hits = []
    measurement = "level"
    measurement_hits = []
    for candidate, keywords in _SEMANTIC_MEASUREMENT_RULES:
        hits = [word for word in keywords if _has(text, word)]
        if hits:
            measurement = candidate
            measurement_hits = hits
            break
    if concept == "analyst_revision":
        measurement = "change"
        if "revision" not in measurement_hits:
            measurement_hits = ["revision"] + measurement_hits
    if concept == "event_count":
        measurement = "count"
    if (
        measurement == "dispersion"
        and "analyst" in direct_text
        and concept != "analyst_revision"
    ):
        concept = "analyst_dispersion"
        concept_hits = ["analyst", "dispersion"]
        direct_concept_hits = ["analyst", "dispersion"]
    frequency_slow = any(
        marker in frequency
        for marker in (
            "quarter",
            "monthly",
            "month",
            "annual",
            "year",
            "weekly",
            "week",
        )
    )
    frequency_fast = any(
        marker in frequency for marker in ("intraday", "minute", "hour", "daily", "day")
    )
    event_signal = concept in {
        "analyst_revision",
        "analyst_dispersion",
        "event_count",
        "sentiment",
    }
    slow_signal = (
        frequency_slow
        or concept in {"fundamental", "earnings", "valuation"}
        and not frequency_fast
    )
    coverage = normalize_coverage(profile)
    sparse = coverage is not None and coverage < 0.5
    if slow_signal:
        behavior = "slow_moving"
    elif event_signal:
        behavior = "event_driven"
    elif sparse:
        behavior = "sparse"
    elif measurement in {"change", "dispersion"} or concept in {
        "market_price",
        "volatility",
        "liquidity",
    }:
        behavior = "signed"
    elif measurement in {"ratio", "probability"}:
        behavior = "bounded"
    elif measurement == "count" or concept in {"event_count", "fundamental"}:
        behavior = "nonnegative"
    else:
        behavior = "unknown"
    update_style = (
        "event_driven"
        if event_signal
        else "periodic"
        if slow_signal
        else "continuous"
        if frequency_fast
        else "unknown"
    )
    if concept == "analyst_revision" or measurement == "change":
        sign_semantics = "signed_change"
    elif concept in {
        "volatility",
        "event_count",
        "fundamental",
        "earnings",
        "data_quality",
    }:
        sign_semantics = "nonnegative_level"
    elif measurement == "dispersion":
        sign_semantics = "nonnegative_dispersion"
    elif measurement in {"ratio", "probability"}:
        sign_semantics = "bounded"
    elif concept in {"market_price", "sentiment"}:
        sign_semantics = "signed_level"
    elif concept == "liquidity":
        sign_semantics = "nonnegative_level"
    else:
        sign_semantics = "unknown"
    direction_meaning = {
        "analyst_revision": "information_update",
        "analyst_dispersion": "expectation_dispersion",
        "option_relative": "relative_option_position",
        "option": "option_measurement",
        "volatility": "risk_exposure",
        "event_count": "attention_events",
        "liquidity": "market_participation",
        "sentiment": "attention_or_belief",
        "valuation": "relative_value",
        "earnings": "operating_expectation",
        "fundamental": "economic_scale",
        "market_price": "market_level",
        "data_quality": "data_availability",
    }.get(concept, "unknown")
    tags = set()
    if concept == "market_price" or any(
        word in text for word in ("price", "close", "high", "low", "vwap")
    ):
        tags.add("price")
    if concept == "liquidity" or any(
        word in text for word in ("volume", "turnover", "liquidity")
    ):
        tags.add("volume")
    if concept == "volatility":
        tags.add("volatility")
    if concept == "option_relative":
        tags.add("relative")
    if any(word in text for word in ("option", "call", "put")):
        tags.add("option")
    if "put" in text:
        tags.add("option_put")
    if "call" in text:
        tags.add("option_call")
    if concept == "analyst_revision" or "analyst" in text:
        tags.add("analyst")
    if measurement == "dispersion":
        tags.add("dispersion")
    if concept == "event_count":
        tags.add("event_count")
    if concept in {"fundamental", "earnings", "valuation"}:
        tags.add("fundamental_scale")
    if any(
        word in text
        for word in ("revenue", "sales", "earnings", "eps", "profit", "cash flow")
    ):
        tags.add("earnings")
    if any(word in text for word in ("assets", "equity", "book value", "debt")):
        tags.add("asset_scale")
    if any(word in text for word in ("social", "news", "mention")):
        tags.add("attention")
    semantic_admission = (
        "ALLOW"
        if direct_concept_hits
        else "REVIEW"
        if concept != "unknown"
        else "UNKNOWN"
    )
    metadata_semantics = (
        "AVAILABLE" if str(profile.get("description") or "").strip() else "UNKNOWN"
    )
    if metadata_semantics != "AVAILABLE" and semantic_admission == "ALLOW":
        semantic_admission = "REVIEW"
    return {
        "concept": concept,
        "measurement": measurement,
        "behavior": behavior,
        "frequency": frequency or "unknown",
        "sign_semantics": sign_semantics,
        "direction_meaning": direction_meaning,
        "update_style": update_style,
        "tags": sorted(tags),
        "status": "KNOWN"
        if concept != "unknown" and direct_concept_hits
        else "UNKNOWN",
        "confidence": "HIGH" if direct_concept_hits else "LOW",
        "semantic_admission": semantic_admission,
        "metadata_semantics": metadata_semantics,
        "evidence": {
            "concept": concept_hits,
            "direct_concept": direct_concept_hits,
            "measurement": measurement_hits,
            "frequency": frequency,
        },
    }
