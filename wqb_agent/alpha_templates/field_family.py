"""Resolve the BRAIN field family of a bound field and refuse domain grafting.

A template declares ``allowed_field_families``: the field families its stated mechanism
is written over.  A bound field is resolved to a family from its own live metadata
(``category`` first, then dataset id).  When the declared families and the bound field
families do not intersect, the pair is refused *before* rendering: a news template bound
to a securities-lending field is not a news mechanism, and materialising it would
manufacture a candidate whose economic meaning cannot be stated.

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
}

# Category names are matched case-insensitively as substrings, most specific first.
CATEGORY_GROUPS = (
    ("short interest", "SHORT_INTEREST"),
    ("securities lending", "SHORT_INTEREST"),
    ("insider", "SHORT_INTEREST"),
    ("institution", "SHORT_INTEREST"),
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
    ("insiders", "SHORT_INTEREST"),
    ("institutions", "SHORT_INTEREST"),
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


def _dataset_id(profile):
    dataset = profile.get("dataset") if isinstance(profile, dict) else None
    if isinstance(dataset, dict):
        return str(dataset.get("id") or "").strip()
    return str(dataset or "").strip()


def field_families(profile):
    """Resolve a bound field profile to its possible BRAIN field families."""
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
