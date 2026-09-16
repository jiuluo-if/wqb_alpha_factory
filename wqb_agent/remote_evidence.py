"""Live Alpha evidence access backed by the BRAIN client."""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from .client import (
    WQBCorrelationPendingError,
    WQBNotFoundError,
)


@dataclass(frozen=True)
class RemoteAlphaEvidence:
    """一个 Alpha 的真实只读证据快照；raw payload 仍由既有 owner 保存。"""

    alpha_id: str
    alpha_detail: Mapping[str, Any]
    aggregates: Mapping[str, Any]
    pnl: Any
    self_correlation: Mapping[str, Any] | None
    status: Mapping[str, str] = field(default_factory=dict)
    availability: Mapping[str, str] = field(default_factory=dict)


class RemoteEvidenceProvider(Protocol):
    def collect(self, alpha_id: str) -> RemoteAlphaEvidence: ...


class _RemoteEvidenceCollector:
    """Project one WQBClient into a live, read-only Alpha evidence API."""

    def __init__(self, client):
        self.client = client

    def collect(self, alpha_id: str) -> RemoteAlphaEvidence:
        alpha_id = str(alpha_id).strip()
        if not alpha_id:
            raise ValueError("alpha_id must be non-empty")
        alpha_detail = self.client.get_alpha(alpha_id)
        if not isinstance(alpha_detail, Mapping):
            raise ValueError("alpha detail response malformed")
        slots = {
            "alpha_detail": alpha_detail,
            "aggregates": self._slot("aggregates", self.client.get_aggregates, alpha_id,
                                      allow_not_found=True),
            "pnl": self._slot("pnl", self.client.get_pnl, alpha_id),
            "self_correlation": self._slot("self_correlation", self.client.get_self_correlation,
                                             alpha_id, pending_unknown=True),
        }
        status = {name: self._slot_value(value, "status", "AVAILABLE") for name, value in slots.items()}
        availability = {name: self._slot_value(value, "availability", "AVAILABLE") for name, value in slots.items()}
        return RemoteAlphaEvidence(
            alpha_id=alpha_id,
            alpha_detail=slots["alpha_detail"], aggregates=slots["aggregates"],
            pnl=slots["pnl"], self_correlation=slots["self_correlation"],
            status=status, availability=availability,
        )

    @staticmethod
    def _slot_value(value, key, default):
        if isinstance(value, Mapping):
            return str(value.get(key) or default).upper()
        return default

    @staticmethod
    def _slot(name, operation, alpha_id, *, allow_not_found=False, pending_unknown=False):
        try:
            value = operation(alpha_id)
            if pending_unknown and isinstance(value, Mapping) and str(value.get("status") or "").upper() in {
                "PENDING", "RUNNING", "QUEUED", "UNSETTLED",
            }:
                value = dict(value)
                value.update({"status": "UNKNOWN", "availability": "AVAILABLE",
                              "reason_code": "CORRELATION_PENDING"})
            return value
        except WQBCorrelationPendingError:
            return {"status": "UNKNOWN", "availability": "AVAILABLE",
                    "reason_code": "CORRELATION_PENDING", "slot": name}
        except WQBNotFoundError:
            if not allow_not_found:
                raise
            return {"status": "UNAVAILABLE", "availability": "UNAVAILABLE",
                    "slot": name, "reason_code": "CAPABILITY_UNAVAILABLE"}
        except (AttributeError, NotImplementedError) as exc:
            return {"status": "UNAVAILABLE", "availability": "UNAVAILABLE",
                    "slot": name, "reason": str(exc) or "capability unavailable"}
        except Exception as exc:
            if str(getattr(exc, "kind", "")).upper() in {"UNAVAILABLE", "CAPABILITY_UNAVAILABLE"}:
                return {"status": "UNAVAILABLE", "availability": "UNAVAILABLE",
                        "slot": name, "reason": str(exc) or "capability unavailable"}
            raise


class RemoteAlphaEvidenceProvider(_RemoteEvidenceCollector):
    """Live, read-only Alpha evidence API."""

    def get_alpha(self, alpha_id):
        return self.client.get_alpha(str(alpha_id).strip())

    def get_alpha_evidence(self, alpha_id, *, live=True):
        if not live:
            raise ValueError("LIVE_EVIDENCE_REQUIRED")
        snapshot = self.collect(alpha_id)
        return {
            "alpha_id": snapshot.alpha_id,
            "source": "LIVE",
            "fetched_at": time.time(),
            "age_sec": 0.0,
            "alpha": dict(snapshot.alpha_detail),
            "aggregates": snapshot.aggregates,
            "pnl": snapshot.pnl,
            "self_correlation": snapshot.self_correlation,
            "status": dict(snapshot.status),
            "availability": dict(snapshot.availability),
        }

    def get_alpha_metrics(self, alpha_id):
        return self.get_alpha_evidence(alpha_id)["alpha"].get("is", {})

    def get_alpha_aggregates(self, alpha_id):
        return self.get_alpha_evidence(alpha_id)["aggregates"]

    def get_alpha_pnl(self, alpha_id):
        return self.get_alpha_evidence(alpha_id)["pnl"]

    def get_alpha_self_correlation(self, alpha_id):
        return self.get_alpha_evidence(alpha_id)["self_correlation"]

    def compare_alphas(self, alpha_ids):
        return {"source": "LIVE", "alphas": [
            self.get_alpha_evidence(item) for item in (alpha_ids or ())
        ]}

