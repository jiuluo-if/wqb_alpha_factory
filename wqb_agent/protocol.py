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
    EndpointTruth("authentication_status", "GET", "/authentication", CapabilityStatus.OFFICIAL,
                  "authenticated session", "bounded account capability", True),
    EndpointTruth("data_sets", "GET", "/data-sets", CapabilityStatus.OFFICIAL,
                  "instrumentType, region, delay, universe", "results[]", True),
    EndpointTruth("data_fields", "GET", "/data-fields", CapabilityStatus.OFFICIAL,
                  "dataset.id plus pagination/type", "results[], count", True),
    EndpointTruth("simulations", "POST", "/simulations", CapabilityStatus.OFFICIAL,
                  "type, settings, regular",
                  "Location, X-Ratelimit-Limit, X-Ratelimit-Remaining, "
                  "X-Ratelimit-Reset headers", False),
    EndpointTruth("simulation_options", "OPTIONS", "/simulations", CapabilityStatus.OFFICIAL,
                  "none", "actions.POST capability projection", True),
    EndpointTruth("simulation_progress", "GET", "/simulations/{id}", CapabilityStatus.OFFICIAL,
                  "known progress URL", "pending/terminal progress object", True),
    EndpointTruth("alphas", "GET", "/alphas/{id}", CapabilityStatus.OFFICIAL,
                  "alpha id", "alpha payload with metrics/checks", True),
    EndpointTruth("recordsets", "GET", "/alphas/{id}/recordsets", CapabilityStatus.OFFICIAL,
                  "alpha id", "recordsets[] name/title", True),
    EndpointTruth("recordset", "GET", "/alphas/{id}/recordsets/{recordset_name}", CapabilityStatus.OFFICIAL,
                  "alpha id and discovered recordset name", "schema.properties plus records", True),
    EndpointTruth("activity_diversity", "GET", "/users/{userid}/activities/diversity", CapabilityStatus.OFFICIAL,
                  "authenticated user id", "bounded region/delay/dataCategory projection", True),
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


def _bounded_text(value, limit=500):
    if value is None:
        return None
    return str(value)[:limit]


def classify_simulation_status(payload):
    """Classify one official Simulation progress payload without side effects."""
    if not isinstance(payload, dict):
        return {
            "phase": "UNKNOWN", "remote_status": None, "alpha": None,
            "children": [], "message": None, "diagnostic": None,
            "reason": "INVALID_PAYLOAD",
        }
    remote_status = str(payload.get("status") or "").strip().upper() or None
    alpha = payload.get("alpha")
    children = payload.get("children") if isinstance(payload.get("children"), list) else []
    message = _bounded_text(payload.get("message"))
    if remote_status in {"WAITING", "SIMULATING"}:
        phase = "PENDING"
    elif remote_status in {"COMPLETE", "WARNING"}:
        phase = "SUCCESS" if alpha or children else "TERMINAL_FAILURE"
    elif remote_status in {"CANCELLED", "ERROR", "TIMEOUT", "FAIL", "FAILED"}:
        phase = "TERMINAL_FAILURE"
    else:
        phase = "UNKNOWN"
    diagnostic = None
    if phase == "TERMINAL_FAILURE":
        diagnostic = {"remote_status": remote_status}
        if message is not None:
            diagnostic["message"] = message
        location = payload.get("location")
        if isinstance(location, dict):
            for key in ("property", "line", "start", "end"):
                if key in location and location[key] is not None:
                    value = location[key]
                    diagnostic[key] = _bounded_text(value, 200) if key == "property" else value
        if payload.get("id") is not None:
            diagnostic["simulation_id"] = _bounded_text(payload.get("id"), 200)
    result = {
        "phase": phase, "remote_status": remote_status, "alpha": alpha,
        "children": children, "message": message, "diagnostic": diagnostic,
    }
    if phase == "UNKNOWN":
        result["reason"] = "UNKNOWN_REMOTE_STATUS" if remote_status else "MISSING_STATUS"
    elif phase == "TERMINAL_FAILURE" and remote_status in {"COMPLETE", "WARNING"}:
        result["reason"] = "MISSING_ALPHA_OR_CHILDREN"
    return result


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
    if hasattr(headers, "items"):
        wanted = str(name).casefold()
        for key, value in headers.items():
            if str(key).casefold() == wanted:
                return value
    if hasattr(headers, "get"):
        return headers.get(name) or headers.get(name.lower())
    return None


_JSON_SAFE_NONNEGATIVE_MAX = 2**53 - 1


def _bounded_nonnegative_integer(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        parsed = float(str(value).strip())
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(parsed) or parsed < 0 or not parsed.is_integer():
        return None
    if parsed > _JSON_SAFE_NONNEGATIVE_MAX:
        return None
    return int(parsed)


def simulation_rate_limit_from_headers(response_or_headers):
    """Project official Simulation quota headers without owning any state."""
    headers = getattr(response_or_headers, "headers", response_or_headers)
    values = {
        "limit": _bounded_nonnegative_integer(
            _header_value(headers, "X-Ratelimit-Limit")
        ),
        "remaining": _bounded_nonnegative_integer(
            _header_value(headers, "X-Ratelimit-Remaining")
        ),
        "reset_seconds": _bounded_nonnegative_integer(
            _header_value(headers, "X-Ratelimit-Reset")
        ),
    }
    valid_count = sum(value is not None for value in values.values())
    if valid_count == len(values):
        status, evidence_status = "AVAILABLE", "AVAILABLE"
    elif valid_count:
        status, evidence_status = "PARTIAL", "INCONCLUSIVE"
    else:
        status, evidence_status = "UNKNOWN", "UNAVAILABLE"
    return {
        "status": status,
        "evidence_status": evidence_status,
        "source": "BRAIN_SIMULATION_HEADERS",
        **values,
    }


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
    if not isinstance(payload, (dict, list)):
        return False, ["payload 必须是对象或数组"]
    truth = endpoint_truth(key)
    if truth is None:
        return False, [f"未知 capability: {key}"]
    if key == "operators":
        # Live BRAIN /operators serves a bare operator array; the envelope
        # object form ({"operators": [...]}) remains a valid probe shape.
        operators = payload if isinstance(payload, list) else payload.get("operators")
        if not isinstance(operators, list):
            return False, ["operators 必须是数组"]
        if any(
            not isinstance(item, dict)
            or not isinstance(item.get("name"), str)
            or not item["name"].strip()
            for item in operators
        ):
            return False, ["operators 每项必须包含非空 name"]
        return True, []
    if not isinstance(payload, dict):
        return False, ["payload 必须是对象"]
    if key in {"data_sets", "data_fields"} and not isinstance(payload.get("results"), list):
        return False, ["results 必须是数组"]
    if key == "aggregates":
        yearly = payload.get("yearlyData")
        if yearly is None and isinstance(payload.get("is"), dict):
            yearly = payload["is"].get("yearlyData")
        if not isinstance(yearly, list):
            return False, ["yearlyData 必须是数组"]
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
            operators = payload if isinstance(payload, list) else payload.get("operators")
            names = sorted({item["name"].strip() for item in operators})
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
