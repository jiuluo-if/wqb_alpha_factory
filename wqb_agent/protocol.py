"""BRAIN protocol truth layer.

This module is deliberately a small fact registry, not an API crawler.  It
centralizes endpoint paths, request/response shape notes, Retry-After parsing,
and capability evidence levels.  Community-observed endpoints are represented
as probes/fixtures only and never become production dependencies implicitly.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from enum import Enum

from .evidence_status import EvidenceStatus


class CapabilityStatus(str, Enum):
    OFFICIAL = "OFFICIAL"
    LIVE_VERIFIED = "LIVE_VERIFIED"
    FIXTURE_VERIFIED = "FIXTURE_VERIFIED"
    COMMUNITY_OBSERVED = "COMMUNITY_OBSERVED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class EndpointTruth:
    key: str
    method: str
    path: str
    status: CapabilityStatus
    request_schema: str
    response_schema: str
    safe_probe: bool
    notes: str = ""


# These are protocol facts used by the current client.  The status describes
# the evidence source, not a promise that every account has the capability.
ENDPOINT_TRUTH = (
    EndpointTruth("authentication", "POST", "/authentication", CapabilityStatus.OFFICIAL,
                  "HTTP Basic Auth; empty body", "session/auth response", False),
    EndpointTruth("data_sets", "GET", "/data-sets", CapabilityStatus.OFFICIAL,
                  "instrumentType, region, delay, universe", "results[]", True),
    EndpointTruth("data_fields", "GET", "/data-fields", CapabilityStatus.OFFICIAL,
                  "dataset.id plus pagination/type", "results[], count", True),
    EndpointTruth("simulations", "POST", "/simulations", CapabilityStatus.OFFICIAL,
                  "type, settings, regular", "Location header", False),
    EndpointTruth("simulation_progress", "GET", "/simulations/{id}", CapabilityStatus.OFFICIAL,
                  "known progress URL", "pending/terminal progress object", True),
    EndpointTruth("alphas", "GET", "/alphas/{id}", CapabilityStatus.OFFICIAL,
                  "alpha id", "alpha payload with metrics/checks", True),
    EndpointTruth("aggregates", "GET", "/alphas/{id}/aggregates", CapabilityStatus.OFFICIAL,
                  "alpha id", "yearlyData aggregate payload", True),
    EndpointTruth("self_correlation", "GET", "/alphas/{id}/correlations/self", CapabilityStatus.OFFICIAL,
                  "alpha id", "asynchronous correlation payload", True),
    EndpointTruth("prod_correlation", "GET", "/alphas/{id}/correlations/prod", CapabilityStatus.OFFICIAL,
                  "alpha id", "asynchronous correlation payload", True),
    EndpointTruth("operators", "GET", "/operators", CapabilityStatus.COMMUNITY_OBSERVED,
                  "undocumented", "undocumented", True,
                  "仅 capability probe/fixture，不作为生产依赖"),
    EndpointTruth("alpha_check", "GET", "/alphas/{id}/check", CapabilityStatus.COMMUNITY_OBSERVED,
                  "undocumented", "undocumented", True,
                  "仅 capability probe/fixture，不作为生产依赖"),
    EndpointTruth("pnl", "GET", "/alphas/{id}/pnl", CapabilityStatus.COMMUNITY_OBSERVED,
                  "undocumented", "undocumented", True,
                  "仅 capability probe/fixture，不作为生产依赖"),
)


def endpoint_catalog():
    """Return a JSON-safe immutable snapshot of protocol facts."""
    return [
        {
            "key": item.key,
            "method": item.method,
            "path": item.path,
            "status": item.status.value,
            "request_schema": item.request_schema,
            "response_schema": item.response_schema,
            "safe_probe": item.safe_probe,
            "notes": item.notes,
        }
        for item in ENDPOINT_TRUTH
    ]


def endpoint_truth(key):
    for item in ENDPOINT_TRUTH:
        if item.key == key:
            return item
    return None


def _header_value(headers, name):
    if headers is None:
        return None
    if hasattr(headers, "get"):
        return headers.get(name) or headers.get(name.lower())
    return None


def retry_after_seconds(response_or_headers, now=None, default=5.0):
    """Parse numeric or HTTP-date Retry-After without returning NaN/negative."""
    headers = getattr(response_or_headers, "headers", response_or_headers)
    value = _header_value(headers, "Retry-After")
    try:
        parsed = float(value)
        return max(1.0, parsed) if math.isfinite(parsed) else float(default)
    except (TypeError, ValueError):
        pass
    if value:
        try:
            dt = parsedate_to_datetime(value)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            reference = now or datetime.now(UTC)
            delay = (dt - reference).total_seconds()
            return max(1.0, delay) if math.isfinite(delay) else float(default)
        except (TypeError, ValueError, OverflowError):
            pass
    return float(default)


def validate_fixture_payload(key, payload):
    """Validate only stable envelope facts; unknown vendor fields are allowed."""
    if not isinstance(payload, dict):
        return False, ["payload 必须是对象"]
    truth = endpoint_truth(key)
    if truth is None:
        return False, [f"未知 capability: {key}"]
    if key in {"data_sets", "data_fields"} and not isinstance(payload.get("results"), list):
        return False, ["results 必须是数组"]
    if key == "aggregates":
        yearly = payload.get("yearlyData")
        if yearly is None and isinstance(payload.get("is"), dict):
            yearly = payload["is"].get("yearlyData")
        if not isinstance(yearly, list):
            return False, ["yearlyData 必须是数组"]
    if key == "operators":
        operators = payload.get("operators")
        if not isinstance(operators, list):
            return False, ["operators 必须是数组"]
        if any(
            not isinstance(item, dict)
            or not isinstance(item.get("name"), str)
            or not item["name"].strip()
            for item in operators
        ):
            return False, ["operators 每项必须包含非空 name"]
    if key == "alpha_check" and not isinstance(payload.get("checks"), list):
        return False, ["checks 必须是数组"]
    if key == "pnl" and not isinstance(payload.get("pnl"), list):
        return False, ["pnl 必须是数组"]
    if key in {"self_correlation", "prod_correlation"} and not (
        isinstance(payload.get("is"), dict) or isinstance(payload.get("data"), list)
    ):
        return False, ["correlation payload 缺少 is/data 外壳"]
    return True, []


def fixture_capability(key, payload):
    """Return a capability record after fixture validation."""
    ok, errors = validate_fixture_payload(key, payload)
    truth = endpoint_truth(key)
    return {
        "key": key,
        "status": CapabilityStatus.FIXTURE_VERIFIED.value if ok else CapabilityStatus.UNKNOWN.value,
        "evidence_status": "INCONCLUSIVE" if ok else EvidenceStatus.UNAVAILABLE.value,
        # Fixtures can verify a shape, never the current account capability.
        "availability": "UNKNOWN" if ok else "UNAVAILABLE",
        "quality": "VERIFIED" if ok else None,
        "decision": "INCONCLUSIVE" if ok else "INCONCLUSIVE",
        "source": "fixture",
        "valid": ok,
        "errors": errors,
        "endpoint": truth.path if truth else None,
    }


def probe_capability_response(key, status_code, payload=None):
    """Classify a caller-supplied read-only HTTP probe response.

    The caller owns transport and authentication.  Keeping this function
    response-only prevents community endpoints from becoming an implicit
    production request path.
    """
    truth = endpoint_truth(key)
    if truth is None:
        return {"key": key, "status": CapabilityStatus.UNKNOWN.value,
                "evidence_status": EvidenceStatus.UNAVAILABLE.value,
                "availability": "UNAVAILABLE", "quality": None, "decision": "INCONCLUSIVE",
                "source": "probe", "valid": False, "errors": ["未知 capability"]}
    try:
        code = int(status_code)
    except (TypeError, ValueError):
        code = 0
    if 200 <= code < 300:
        valid, errors = validate_fixture_payload(key, payload or {})
        result = {
            "key": key,
            "status": CapabilityStatus.LIVE_VERIFIED.value if valid else CapabilityStatus.UNKNOWN.value,
            "evidence_status": "INCONCLUSIVE" if valid else EvidenceStatus.UNAVAILABLE.value,
            "availability": "AVAILABLE" if valid else "UNAVAILABLE",
            "quality": "VERIFIED" if valid else None,
            "decision": "INCONCLUSIVE",
            "source": "BRAIN_LIVE_ONLY",
            "valid": valid,
            "errors": errors,
            "endpoint": truth.path,
        }
        if valid and key == "operators":
            names = sorted({item["name"].strip() for item in payload["operators"]})
            result["operators"] = names
            result["capability_fingerprint"] = hashlib.sha256(
                json.dumps(names, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            result["sha256"] = result["capability_fingerprint"]
        return result
    # A community-observed endpoint describes provenance only.  A rejected
    # response is not evidence that the current account has the capability.
    return {
        "key": key,
        "status": CapabilityStatus.UNKNOWN.value,
        "evidence_status": EvidenceStatus.UNAVAILABLE.value,
        "availability": "UNKNOWN",
        "quality": None,
        "decision": "INCONCLUSIVE",
        "source": "BRAIN_LIVE_ONLY",
        "valid": False,
        "errors": [f"HTTP {code}"],
        "endpoint": truth.path,
    }
