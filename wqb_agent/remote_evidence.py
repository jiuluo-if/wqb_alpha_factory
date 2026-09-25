"""Live Alpha evidence access backed by the BRAIN client."""

from __future__ import annotations

import time
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from .client import WQBCorrelationPendingError, WQBNotFoundError


def decode_recordset(payload):
    """Decode official ``schema.properties`` + ``records`` into a table."""
    if payload is None:
        return {"status": "PENDING_DATA", "columns": [], "rows": []}
    if not isinstance(payload, Mapping):
        return {"status": "UNAVAILABLE", "columns": [], "rows": []}
    schema = payload.get("schema")
    properties = schema.get("properties") if isinstance(schema, Mapping) else None
    records = payload.get("records")
    if not isinstance(properties, Mapping) or not isinstance(records, list):
        return {"status": "UNAVAILABLE", "columns": [], "rows": []}
    columns = [str(name) for name in properties]
    rows = []
    for record in records:
        if isinstance(record, Mapping):
            rows.append({name: record.get(name) for name in columns})
        elif isinstance(record, (list, tuple)):
            rows.append({name: record[index] if index < len(record) else None
                         for index, name in enumerate(columns)})
    return {
        "status": "AVAILABLE" if rows else "EMPTY",
        "columns": columns,
        "rows": rows,
    }


@dataclass(frozen=True)
class RemoteAlphaEvidence:
    """一个 Alpha 的真实只读证据快照；raw payload 仍由既有 owner 保存。"""

    alpha_id: str
    alpha_detail: Mapping[str, Any]
    aggregates: Mapping[str, Any] | None = None
    pnl: Any = None
    self_correlation: Mapping[str, Any] | None = None
    recordsets: Mapping[str, Any] = field(default_factory=dict)
    status: Mapping[str, str] = field(default_factory=dict)
    availability: Mapping[str, str] = field(default_factory=dict)


class _RemoteEvidenceCollector:
    """Cheap live collection shared by the evidence provider subclasses."""

    def __init__(self, client):
        self.client = client

    def collect(self, alpha_id: str, *, recordsets=()) -> RemoteAlphaEvidence:
        alpha_id = str(alpha_id).strip()
        if not alpha_id:
            raise ValueError("alpha_id must be non-empty")
        alpha_detail = self.client.get_alpha(alpha_id)
        if not isinstance(alpha_detail, Mapping):
            raise ValueError("alpha detail response malformed")
        requested = [str(name).strip() for name in (recordsets or ()) if str(name).strip()]
        decoded_recordsets = {}
        if requested:
            available = self.client.list_alpha_recordsets(alpha_id)
            available_names = {item["name"] for item in available}
            for name in requested:
                if name not in available_names:
                    decoded_recordsets[name] = {
                        "status": "UNAVAILABLE", "columns": [], "rows": [],
                        "reason": "RECORDSET_NOT_DISCOVERED",
                    }
                    continue
                try:
                    decoded_recordsets[name] = decode_recordset(
                        self.client.get_recordset(alpha_id, name, available=available)
                    )
                except WQBNotFoundError:
                    decoded_recordsets[name] = {
                        "status": "UNAVAILABLE", "columns": [], "rows": [],
                        "reason": "CAPABILITY_UNAVAILABLE",
                    }
        status = {"alpha_detail": "AVAILABLE"}
        availability = {"alpha_detail": "AVAILABLE"}
        status.update({name: value.get("status", "UNKNOWN") for name, value in decoded_recordsets.items()})
        availability.update({name: (
            "AVAILABLE" if value.get("status") in {"AVAILABLE", "EMPTY", "PENDING_DATA"}
            else "UNAVAILABLE"
        ) for name, value in decoded_recordsets.items()})
        return RemoteAlphaEvidence(
            alpha_id=alpha_id, alpha_detail=alpha_detail,
            recordsets=decoded_recordsets,
            status=status, availability=availability,
        )

    def collect_full(self, alpha_id, *, recordsets=()) -> RemoteAlphaEvidence:
        """Fetch finalist evidence while leaving ``collect`` cheap."""
        snapshot = self.collect(alpha_id, recordsets=recordsets)
        alpha_id = snapshot.alpha_id
        aggregates = self._slot(
            "aggregates", self.client.get_aggregates, alpha_id, allow_not_found=True,
        )
        pnl = self._slot(
            "pnl", self.client.get_pnl, alpha_id, allow_not_found=True,
        )
        self_correlation = self._slot(
            "self_correlation", self.client.get_self_correlation, alpha_id,
            allow_not_found=True, pending_unknown=True,
        )
        status = dict(snapshot.status)
        availability = dict(snapshot.availability)
        for name, value in (
            ("aggregates", aggregates), ("pnl", pnl),
            ("self_correlation", self_correlation),
        ):
            slot_status = self._slot_value(value, "status", "AVAILABLE")
            status[name] = slot_status
            availability[name] = self._slot_value(
                value, "availability",
                "UNAVAILABLE" if slot_status == "UNAVAILABLE" else "AVAILABLE",
            )
        return RemoteAlphaEvidence(
            alpha_id=alpha_id, alpha_detail=snapshot.alpha_detail,
            aggregates=aggregates, pnl=pnl, self_correlation=self_correlation,
            recordsets=snapshot.recordsets, status=status,
            availability=availability,
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

    def get_alpha_summary(self, alpha_id):
        alpha_id = str(alpha_id).strip()
        if not alpha_id:
            raise ValueError("alpha_id must be non-empty")
        detail = self.get_alpha(alpha_id)
        if not isinstance(detail, Mapping):
            raise ValueError("alpha detail response malformed")
        status = {
            "alpha_detail": "AVAILABLE", "aggregates": "NOT_REQUESTED",
            "pnl": "NOT_REQUESTED", "self_correlation": "NOT_REQUESTED",
            "recordsets": "NOT_REQUESTED",
        }
        return {
            "alpha_id": alpha_id, "source": "LIVE",
            "fetched_at": time.time(), "age_sec": 0.0,
            "alpha": dict(detail), "aggregates": None, "pnl": None,
            "self_correlation": None, "recordsets": {},
            "status": status, "availability": dict(status),
            "depth": "SUMMARY",
        }

    def get_alpha_evidence(self, alpha_id, *, live=True, recordsets=(), depth="summary"):
        if not live:
            raise ValueError("LIVE_EVIDENCE_REQUIRED")
        normalized_depth = str(depth or "").strip().lower()
        if normalized_depth == "summary":
            if recordsets:
                raise ValueError("recordsets require full evidence depth")
            return self.get_alpha_summary(alpha_id)
        if normalized_depth != "full":
            raise ValueError("depth must be 'summary' or 'full'")
        snapshot = self.collect_full(alpha_id, recordsets=recordsets)
        return {
            "alpha_id": snapshot.alpha_id,
            "source": "LIVE",
            "fetched_at": time.time(),
            "age_sec": 0.0,
            "alpha": dict(snapshot.alpha_detail),
            "aggregates": snapshot.aggregates,
            "pnl": snapshot.pnl,
            "self_correlation": snapshot.self_correlation,
            "recordsets": dict(snapshot.recordsets),
            "status": dict(snapshot.status),
            "availability": dict(snapshot.availability),
            "depth": "FULL",
        }

    def get_alpha_metrics(self, alpha_id):
        detail = self.get_alpha(str(alpha_id).strip())
        if not isinstance(detail, Mapping):
            raise ValueError("alpha detail response malformed")
        return detail.get("is", {})

    def get_alpha_aggregates(self, alpha_id):
        return self.client.get_aggregates(str(alpha_id).strip())

    def get_alpha_pnl(self, alpha_id):
        return self.client.get_pnl(str(alpha_id).strip())

    def get_alpha_self_correlation(self, alpha_id):
        return self.client.get_self_correlation(str(alpha_id).strip())

    def get_alpha_recordsets(self, alpha_id, names):
        """Decode only explicitly requested recordsets without deep reads."""
        snapshot = self.collect(str(alpha_id).strip(), recordsets=names)
        return dict(snapshot.recordsets)

    def get_alpha_prod_correlation(self, alpha_id):
        return self.client.get_correlation(str(alpha_id).strip(), kind="prod")

    def compare_alphas(self, alpha_ids, *, depth="summary", max_concurrent=4):
        ids = [str(item).strip() for item in (alpha_ids or ()) if str(item).strip()]
        try:
            concurrency = int(max_concurrent)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("max_concurrent must be an integer") from exc
        if isinstance(max_concurrent, bool) or not 1 <= concurrency <= 4:
            raise ValueError("max_concurrent must be between 1 and 4")
        if not ids:
            return {"source": "LIVE", "status": "AVAILABLE",
                    "evidence_status": "AVAILABLE", "depth": str(depth).upper(),
                    "alphas": []}
        with ThreadPoolExecutor(max_workers=min(concurrency, len(ids))) as pool:
            rows = list(pool.map(
                lambda alpha_id: self.get_alpha_evidence(alpha_id, depth=depth), ids
            ))
        return {"source": "LIVE", "status": "AVAILABLE",
                "evidence_status": "AVAILABLE", "depth": str(depth).upper(),
                "alphas": rows}
