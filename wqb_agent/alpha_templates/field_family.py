"""Resolve the BRAIN field family of a bound field and refuse domain grafting.

A template declares ``allowed_field_families``: the field families its stated mechanism
is written over.  A bound field is resolved to a family from its own live metadata
(``category`` first, then dataset id).  When the declared families and the bound field
families do not intersect, the pair is refused *before* rendering: a news template bound
to a securities-lending field is not a news mechanism, and materialising it would
manufacture a candidate whose economic meaning cannot be stated.

The family is resolved from the field's *semantics* first.  Several BRAIN datasets carry
names that misdescribe their contents: ``news12`` is mostly intraday price reaction to
news (``market_price``) rather than text sentiment, so resolving by dataset prefix alone
would let a news template bind a price-reaction field and call the result a news
mechanism.  Semantic concept therefore outranks category and dataset id.

Unknown metadata never denies admission: synthetic fixtures and newly registered
datasets stay renderable, but they are reported as ``FIELD_FAMILY_UNKNOWN`` so a reviewer
can see that the match was not proved.
"""

FAMILY_GROUPS = {
    "NEWS": frozenset({"LIVE_NEWS_SENTIMENT"}),
    "SOCIAL": frozenset({"LIVE_SOCIAL_MEDIA"}),
    "SHORT_INTEREST": frozenset({"LIVE_SHORT_INTEREST"}),
    "FUNDAMENTAL": frozenset({"LIVE_FUNDAMENTAL"}),
    "ANALYST": frozenset({"LIVE_ANALYST_ESTIMATES"}),
    "MODEL": frozenset({"LIVE_MODEL_RISK"}),
    "OPTION": frozenset({"LIVE_OPTION_ANALYTICS", "LIVE_OPTION_VOLATILITY"}),
    "PRICE_VOLUME": frozenset({"LIVE_PRICE_VOLUME"}),
    "INSIDER": frozenset({"LIVE_INSIDER"}),
    "INSTITUTION": frozenset({"LIVE_INSTITUTION"}),
}

# Semantic concepts are the strongest evidence of what a field measures.  They are
# derived from the live description by ``alpha_semantics``, so a field named
# ``news_range_stddev`` is classified as price behaviour, not as text sentiment.
CONCEPT_GROUPS = {
    "market_price": "PRICE_VOLUME",
    "liquidity": "PRICE_VOLUME",
    "price_volume": "PRICE_VOLUME",
    "volatility": "PRICE_VOLUME",
    "sentiment": "NEWS",
    "event_novelty": "NEWS",
    "event_count": "NEWS",
    "social_attention": "SOCIAL",
    "fundamental": "FUNDAMENTAL",
    "asset_base": "FUNDAMENTAL",
    "equity_capital": "FUNDAMENTAL",
    "operating_cashflow": "FUNDAMENTAL",
    "debt_balance": "FUNDAMENTAL",
    "profitability_measure": "FUNDAMENTAL",
    "asset_efficiency": "FUNDAMENTAL",
    "valuation": "FUNDAMENTAL",
    "earnings": "FUNDAMENTAL",
    "analyst_revision": "ANALYST",
    "analyst_dispersion": "ANALYST",
    "analyst_expectation": "ANALYST",
    "market_beta": "MODEL",
    "market_correlation": "MODEL",
    "implied_volatility": "OPTION",
    "realized_volatility": "OPTION",
}

# A dataset name can lie about content, and a field id can be ambiguous, but the live
# description of a price/volume quantity is explicit.  These markers are checked before
# the category name for exactly that reason.
DESCRIPTION_GROUPS = (
    ("short interest", "SHORT_INTEREST"),
    ("securities lending", "SHORT_INTEREST"),
    ("insider", "INSIDER"),
    ("trading range", "PRICE_VOLUME"),
    ("session", "PRICE_VOLUME"),
    ("intraday", "PRICE_VOLUME"),
    ("price", "PRICE_VOLUME"),
    ("volume", "PRICE_VOLUME"),
    ("quote", "PRICE_VOLUME"),
    ("turnover", "PRICE_VOLUME"),
    ("true range", "PRICE_VOLUME"),
    ("sentiment", "NEWS"),
    ("tone", "NEWS"),
    ("article", "NEWS"),
    ("headline", "NEWS"),
    ("novelty", "NEWS"),
)

# Category names are matched case-insensitively as substrings, most specific first.
CATEGORY_GROUPS = (
    ("short interest", "SHORT_INTEREST"),
    ("securities lending", "SHORT_INTEREST"),
    ("insider", "INSIDER"),
    ("institution", "INSTITUTION"),
    ("social", "SOCIAL"),
    ("news", "NEWS"),
    ("sentiment", "NEWS"),
    ("analyst", "ANALYST"),
    ("estimate", "ANALYST"),
    ("option", "OPTION"),
    ("model", "MODEL"),
    ("fundamental", "FUNDAMENTAL"),
    ("price volume", "PRICE_VOLUME"),
    ("price", "PRICE_VOLUME"),
    ("volume", "PRICE_VOLUME"),
    ("technical", "PRICE_VOLUME"),
)

# Dataset-id prefixes are the fallback when the live row carries no category.
DATASET_GROUPS = (
    ("shortinterest", "SHORT_INTEREST"),
    ("insiders", "INSIDER"),
    ("institutions", "INSTITUTION"),
    ("news", "NEWS"),
    ("ai_news_scores", "NEWS"),
    ("sentiment", "NEWS"),
    ("social", "SOCIAL"),
    ("analyst", "ANALYST"),
    ("earnings", "ANALYST"),
    ("option", "OPTION"),
    ("model", "MODEL"),
    ("fundamental", "FUNDAMENTAL"),
    ("fnd", "FUNDAMENTAL"),
    ("price", "PRICE_VOLUME"),
    ("volume", "PRICE_VOLUME"),
    ("pv", "PRICE_VOLUME"),
    ("techindi", "PRICE_VOLUME"),
)


def expand_families(families):
    """Expand declared family names (family or group names) into a family set."""
    expanded = set()
    for value in families or ():
        name = str(value or "").strip().upper()
        if not name:
            continue
        if name in FAMILY_GROUPS:
            expanded.update(FAMILY_GROUPS[name])
        else:
            expanded.add(name)
    return frozenset(expanded)


def _category_name(profile):
    category = profile.get("category") if isinstance(profile, dict) else None
    if isinstance(category, dict):
        return str(category.get("name") or "").strip()
    return str(category or "").strip()


def _description_text(profile):
    if not isinstance(profile, dict):
        return ""
    return str(profile.get("description") or "").strip().lower()


def _dataset_id(profile):
    dataset = profile.get("dataset") if isinstance(profile, dict) else None
    if isinstance(dataset, dict):
        return str(dataset.get("id") or "").strip()
    return str(dataset or "").strip()


def _semantic_group(profile):
    """Return the group implied by the field's derived semantic concept, if any."""
    try:
        from ..alpha_semantics import derive_field_semantic_traits
    except Exception:  # noqa: BLE001 - semantics stay optional for callers
        return ""
    try:
        traits = derive_field_semantic_traits(profile)
    except Exception:  # noqa: BLE001 - a broken profile must not crash rendering
        return ""
    concept = str((traits or {}).get("concept") or "").strip().lower()
    confident = (
        str((traits or {}).get("confidence") or "").upper() == "HIGH"
        and str((traits or {}).get("semantic_admission") or "").upper() == "ALLOW"
    )
    if concept in CONCEPT_GROUPS and confident:
        return CONCEPT_GROUPS[concept]
    if confident:
        tags = {str(tag).lower() for tag in ((traits or {}).get("tags") or ())}
        if {"price", "volatility"} & tags:
            return "PRICE_VOLUME"
    return ""


def field_families(profile):
    """Resolve a bound field profile to its possible BRAIN field families."""
    semantic = _semantic_group(profile)
    if semantic:
        return FAMILY_GROUPS[semantic]
    description = _description_text(profile)
    for needle, group in DESCRIPTION_GROUPS:
        if needle in description:
            return FAMILY_GROUPS[group]
    category = _category_name(profile).lower()
    for needle, group in CATEGORY_GROUPS:
        if needle in category:
            return FAMILY_GROUPS[group]
    dataset = _dataset_id(profile).lower()
    for prefix, group in DATASET_GROUPS:
        if dataset.startswith(prefix):
            return FAMILY_GROUPS[group]
    return frozenset()


def field_family_admission(template, profiles):
    """Return an auditable decision for one slot bundle before rendering."""
    declared = expand_families(getattr(template, "allowed_field_families", ()))
    if not declared:
        return {"admission": "ALLOW", "reasons": ["NO_DECLARED_FAMILY"], "field": ""}
    unknown = []
    for profile in profiles:
        families = field_families(profile)
        if not families:
            unknown.append(str(profile.get("id") or ""))
            continue
        if not families & declared:
            return {
                "admission": "DENY",
                "reasons": ["FIELD_FAMILY_MISMATCH"],
                "field": str(profile.get("id") or ""),
                "field_families": sorted(families),
                "declared_families": sorted(declared),
            }
    if unknown:
        return {"admission": "ALLOW", "reasons": ["FIELD_FAMILY_UNKNOWN"],
                "fields": unknown}
    return {"admission": "ALLOW", "reasons": ["FIELD_FAMILY_MATCH"], "field": ""}
