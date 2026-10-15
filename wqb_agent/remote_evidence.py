"""Live Alpha evidence access backed by the BRAIN client."""

from __future__ import annotations

import math
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from .client import WQBCorrelationPendingError, WQBNotFoundError

MAX_DAILY_PNL_ROWS = 10_000


def decode_recordset(payload):
    """Decode official ``schema.properties`` + ``records`` into a table."""
    if payload is None:
        return {"status": "PENDING_DATA", "columns": [], "rows": []}
    if not isinstance(payload, Mapping):
        return {"status": "UNAVAILABLE", "columns": [], "rows": []}
    schema = payload.get("schema")
    properties = schema.get("properties") if isinstance(schema, Mapping) else None
    records = payload.get("records")
    if isinstance(properties, Mapping):
        columns = [str(name) for name in properties]
    elif (isinstance(properties, list)
          and all(isinstance(item, Mapping) and isinstance(item.get("name"), str)
                  and item["name"].strip() for item in properties)):
        columns = [item["name"].strip() for item in properties]
    else:
        return {"status": "UNAVAILABLE", "columns": [], "rows": []}
    if not isinstance(records, list):
        return {"status": "UNAVAILABLE", "columns": [], "rows": []}
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


def _daily_date(value):
    if not isinstance(value, str):
        return None
    try:
        if len(value) == 10:
            normalized = date.fromisoformat(value).isoformat()
            return normalized if normalized == value else None
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return None


def decode_daily_pnl(payload):
    """Decode direct PnL rows and official recordsets into one daily contract."""
    if payload is None:
        return {"status": "PENDING_DATA", "reason_code": "PNL_PENDING", "rows": []}
    if isinstance(payload, Mapping) and isinstance(payload.get("pnl"), list):
        source_rows = payload["pnl"]
    elif (isinstance(payload, Mapping) and isinstance(payload.get("rows"), list)
          and isinstance(payload.get("columns"), list)):
        source_rows = payload["rows"]
    else:
        decoded = decode_recordset(payload)
        if decoded["status"] == "PENDING_DATA":
            return {"status": "PENDING_DATA", "reason_code": "PNL_PENDING", "rows": []}
        if decoded["status"] != "AVAILABLE":
            return {"status": decoded["status"], "reason_code": "PNL_SCHEMA_UNAVAILABLE", "rows": []}
        source_rows = decoded["rows"]
    if not isinstance(source_rows, list):
        return {"status": "UNAVAILABLE", "reason_code": "PNL_SCHEMA_UNAVAILABLE", "rows": []}
    if len(source_rows) > MAX_DAILY_PNL_ROWS:
        return {"status": "UNAVAILABLE", "reason_code": "PNL_HISTORY_EXCEEDS_SAFE_LIMIT", "rows": []}
    columns = {
        str(key) for row in source_rows if isinstance(row, Mapping) for key in row
    }
    value_key = "pnl" if "pnl" in columns else "value" if "value" in columns else None
    if value_key is None or "date" not in columns:
        return {"status": "UNAVAILABLE", "reason_code": "PNL_SCHEMA_UNAVAILABLE", "rows": []}
    normalized = []
    seen = set()
    for row in source_rows:
        if not isinstance(row, Mapping):
            return {"status": "UNAVAILABLE", "reason_code": "PNL_SCHEMA_UNAVAILABLE", "rows": []}
        day = _daily_date(row.get("date"))
        if day is None:
            return {"status": "UNAVAILABLE", "reason_code": "INVALID_PNL_DATE", "rows": []}
        raw_value = row.get(value_key)
        if isinstance(raw_value, bool) or raw_value is None:
            return {"status": "UNAVAILABLE", "reason_code": "INVALID_PNL_VALUE", "rows": []}
        try:
            value = float(raw_value)
        except (TypeError, ValueError, OverflowError):
            return {"status": "UNAVAILABLE", "reason_code": "INVALID_PNL_VALUE", "rows": []}
        if not math.isfinite(value):
            return {"status": "UNAVAILABLE", "reason_code": "INVALID_PNL_VALUE", "rows": []}
        if day in seen:
            return {"status": "UNAVAILABLE", "reason_code": "DUPLICATE_PNL_DATE", "rows": []}
        seen.add(day)
        normalized.append({"date": day, "pnl": value})
    if not normalized:
        return {"status": "EMPTY", "reason_code": "PNL_EMPTY", "rows": []}
    if [row["date"] for row in normalized] != sorted(seen):
        return {"status": "UNAVAILABLE", "reason_code": "PNL_DATE_ORDER_INVALID", "rows": []}
    return {"status": "AVAILABLE", "reason_code": None, "rows": normalized}


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

    def collect_full(self, alpha_id, *, recordsets=(), alpha_detail=None) -> RemoteAlphaEvidence:
        """Fetch finalist evidence while leaving ``collect`` cheap."""
        if alpha_detail is None:
            snapshot = self.collect(alpha_id, recordsets=recordsets)
        elif isinstance(alpha_detail, Mapping):
            snapshot = RemoteAlphaEvidence(
                alpha_id=str(alpha_id).strip(), alpha_detail=alpha_detail,
                status={"alpha_detail": "AVAILABLE"},
                availability={"alpha_detail": "AVAILABLE"},
            )
        else:
            raise ValueError("alpha_detail must be a verified mapping")
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

    def get_alpha_evidence(self, alpha_id, *, live=True, recordsets=(), depth="summary",
                           alpha_detail=None):
        if not live:
            raise ValueError("LIVE_EVIDENCE_REQUIRED")
        normalized_depth = str(depth or "").strip().lower()
        if normalized_depth == "summary":
            if recordsets:
                raise ValueError("recordsets require full evidence depth")
            return self.get_alpha_summary(alpha_id)
        if normalized_depth != "full":
            raise ValueError("depth must be 'summary' or 'full'")
        snapshot = self.collect_full(
            alpha_id, recordsets=recordsets, alpha_detail=alpha_detail,
        )
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
