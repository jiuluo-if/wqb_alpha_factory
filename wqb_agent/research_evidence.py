"""Thin serialization objects for platform and bounded external evidence."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

AUTHORITATIVE_SOURCE_CLASSES = frozenset({
    "OFFICIAL_PLATFORM", "OFFICIAL_REGULATOR", "OFFICIAL_STATISTICS",
    "CENTRAL_BANK", "EXCHANGE", "PEER_REVIEWED", "WORKING_PAPER",
    "PREPRINT", "INSTITUTIONAL_RESEARCH", "PLATFORM_OFFICIAL",
    "PRIMARY_AUTHORITY", "PRIMARY_ACADEMIC", "SECONDARY_DISCOVERY",
})
PUBLICATION_STATUSES = frozenset({
    "PEER_REVIEWED", "WORKING_PAPER", "PREPRINT", "INSTITUTIONAL_RESEARCH", "UNKNOWN",
})
CLAIM_TYPES = frozenset({
    "PLATFORM_RULE", "ECONOMIC_DEFINITION", "MARKET_STRUCTURE",
    "EMPIRICAL_FINDING", "MECHANISM_SUPPORT", "MECHANISM_CONTRADICTION",
    "DATA_RELEASE",
})
SUPPORT_VALUES = frozenset({
    "SUPPORT", "SUPPORTED", "CONTRADICTION", "CONTRADICTED", "CONFLICTED",
    "NEUTRAL", "UNRESOLVED", "UNKNOWN",
})
FRESHNESS_VALUES = frozenset({"CURRENT", "VALID", "STALE", "REVISED", "PRELIMINARY", "UNKNOWN"})
MAX_EXTERNAL_SOURCES = 8
MAX_CLAIMS_PER_SOURCE = 2
MAX_CLAIM_LENGTH = 2000


def _canonical_url(url):
    parts = urlsplit(str(url or "").strip())
    if parts.scheme.lower() not in {"http", "https"} or not parts.netloc:
        return ""
    hostname = (parts.hostname or "").lower()
    if not hostname:
        return ""
    port = parts.port
    netloc = hostname
    if port and not ((parts.scheme.lower() == "http" and port == 80)
                     or (parts.scheme.lower() == "https" and port == 443)):
        netloc = f"{hostname}:{port}"
    query = urlencode(sorted(parse_qsl(parts.query, keep_blank_values=True)))
    return urlunsplit((parts.scheme.lower(), netloc, parts.path or "/", query, ""))


def stable_source_identity(url, claim, published_at=None, version=None):
    """Build a deterministic identity; retrieval time is deliberately excluded."""
    canonical = {
        "url": _canonical_url(url),
        "claim": " ".join(str(claim or "").split()),
        "published_at": str(published_at or "").strip(),
        "version": str(version or "").strip(),
    }
    return "evidence-source|" + hashlib.sha256(
        json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True)
class AuthoritativeEvidenceItem:
    source_id: str
    source_class: str
    publisher: str
    domain: str
    title: str
    url: str
    published_at: object
    retrieved_at: object
    claim: str
    claim_type: str
    publication_status: str
    support: str
    freshness: str
    version: object = None

    @classmethod
    def from_mapping(cls, value):
        if not isinstance(value, dict):
            raise TypeError("authoritative evidence item must be an object")
        return cls(**{name: value.get(name) for name in (
            "source_id", "source_class", "publisher", "domain", "title", "url",
            "published_at", "retrieved_at", "claim", "claim_type", "publication_status",
            "support", "freshness", "version",
        )})

    def as_dict(self):
        return {name: getattr(self, name) for name in self.__dataclass_fields__}


def validate_authoritative_evidence_item(value):
    payload = value.as_dict() if isinstance(value, AuthoritativeEvidenceItem) else value
    if not isinstance(payload, dict):
        return False, ["evidence item must be an object"]
    errors = []
    for key in ("source_id", "source_class", "publisher", "domain", "title", "url",
                "published_at", "retrieved_at", "claim", "claim_type",
                "publication_status", "support", "freshness"):
        if not isinstance(payload.get(key), str) or not payload[key].strip():
            errors.append(f"{key} is required")
    url = _canonical_url(payload.get("url"))
    if payload.get("url") and not url:
        errors.append("url must be an absolute http(s) URL")
    if payload.get("domain") and url and urlsplit(url).hostname != payload["domain"].strip().lower():
        errors.append("domain must match url")
    if payload.get("source_class") not in AUTHORITATIVE_SOURCE_CLASSES:
        errors.append("source_class is not an allowed authoritative class")
    if payload.get("claim_type") not in CLAIM_TYPES:
        errors.append("claim_type is unknown")
    if payload.get("publication_status") not in PUBLICATION_STATUSES:
        errors.append("publication_status is unknown")
    if payload.get("support") not in SUPPORT_VALUES:
        errors.append("support is unknown")
    if payload.get("freshness") not in FRESHNESS_VALUES:
        errors.append("freshness is unknown")
    claim = payload.get("claim")
    if isinstance(claim, str):
        if len(claim) > MAX_CLAIM_LENGTH:
            errors.append("claim exceeds bounded length")
        if any(marker in claim.lower() for marker in ("<html", "<script", "<!doctype", "begin pdf")):
            errors.append("raw web document payload is not allowed")
    if payload.get("source_id") and url and payload.get("claim"):
        expected = stable_source_identity(url, payload["claim"], payload.get("published_at"), payload.get("version"))
        if payload["source_id"] != expected:
            errors.append("source_id is not deterministic for url/claim/version")
    return not errors, errors


@dataclass(frozen=True)
class AuthoritativeEvidencePack:
    items: tuple[AuthoritativeEvidenceItem, ...]

    @classmethod
    def from_items(cls, values):
        unique = {}
        for value in values or ():
            item_value = value if isinstance(value, AuthoritativeEvidenceItem) else AuthoritativeEvidenceItem.from_mapping(value)
            unique[item_value.source_id] = item_value
        return cls(tuple(unique[key] for key in sorted(unique)))

    @classmethod
    def from_mapping(cls, value):
        if not isinstance(value, dict):
            raise TypeError("authoritative evidence pack must be an object")
        return cls.from_items(value.get("items") or ())

    def as_dict(self):
        return {"items": [value.as_dict() for value in self.items]}

    @property
    def conflict_status(self):
        """Return explicit conflict state; never silently choose a claim."""
        groups = {}
        for value in self.items:
            key = (_canonical_url(value.url), value.claim_type)
            groups.setdefault(key, set()).add(value.support)
        return "CONFLICTED" if any(
            {"SUPPORT", "SUPPORTED"} & supports
            and {"CONTRADICTION", "CONTRADICTED"} & supports
            for supports in groups.values()
        ) else "UNRESOLVED" if any(
            {"UNKNOWN", "UNRESOLVED"} & supports for supports in groups.values()
        ) else "RESOLVED"


def validate_authoritative_evidence_pack(value):
    if isinstance(value, AuthoritativeEvidencePack):
        items = value.items
    elif isinstance(value, (list, tuple)):
        items = tuple(value)
    else:
        return False, ["evidence pack must be an object or sequence"]
    errors = []
    if len(items) > MAX_EXTERNAL_SOURCES:
        errors.append(f"evidence pack exceeds max_sources={MAX_EXTERNAL_SOURCES}")
    source_counts = Counter()
    seen = set()
    for index, item_value in enumerate(items):
        ok, item_errors = validate_authoritative_evidence_item(item_value)
        errors.extend(f"item {index}: {error}" for error in item_errors if not ok)
        source_id = getattr(item_value, "source_id", None) if isinstance(item_value, AuthoritativeEvidenceItem) else item_value.get("source_id") if isinstance(item_value, dict) else None
        source_key = _canonical_url(getattr(item_value, "url", None) if isinstance(item_value, AuthoritativeEvidenceItem) else item_value.get("url") if isinstance(item_value, dict) else None)
        if source_id in seen:
            errors.append(f"item {index}: duplicate source_id")
        seen.add(source_id)
        source_counts[source_key] += 1
    for source_id, count in source_counts.items():
        if count > MAX_CLAIMS_PER_SOURCE:
            errors.append(f"source {source_id} exceeds max claims per source={MAX_CLAIMS_PER_SOURCE}")
    return not errors, errors


def classify_research(quality, robustness, statistical, incremental, platform):
    values = {str(value or "").upper() for value in
              (quality, robustness, statistical, incremental, platform)}
    if "FAIL" in values or "REJECTED" in values:
        return "REJECTED"
    if str(quality or "").upper() in {"PROMISING", "STABLE", "PASS"} and str(robustness or "").upper() == "PASS":
        if str(statistical or "").upper() == "PASS" and str(platform or "").upper() == "PASS":
            if str(incremental or "").upper() in {"PASS", "UNAVAILABLE", "INCONCLUSIVE"}:
                return "PORTFOLIO_CANDIDATE" if str(incremental or "").upper() == "PASS" else "STABLE"
        return "STABLE"
    return "PROMISING" if str(quality or "").upper() in {"PROMISING", "STABLE"} else "REJECTED"


@dataclass(frozen=True)
class ResearchEvidenceBundle:
    quality: object
    robustness: object
    statistical: object
    incremental: object
    yearly: object
    platform: object

    @classmethod
    def from_parts(cls, quality, robustness, statistical, incremental, yearly, platform):
        return cls(quality, robustness, statistical, incremental, yearly, platform)

    def as_dict(self):
        return {
            "quality": self.quality,
            "robustness": self.robustness,
            "statistical": self.statistical,
            "incremental": self.incremental,
            "yearly": self.yearly,
            "platform": self.platform,
            "effective_trial_count": {
                "value": (self.quality or {}).get("effective_trial_count") if isinstance(self.quality, dict) else None,
                "method": "structural_cluster_proxy",
                "quality": "APPROXIMATE",
            },
        }
