"""Live Alpha evidence access backed by the BRAIN client."""

from __future__ import annotations

import math
import time
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from .client import WQBCorrelationPendingError, WQBNotFoundError

MAX_PAIRWISE_ALPHA_IDS = 40


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


def _daily_pnl_series(payload):
    """Extract date-keyed daily PnL without guessing unsupported schemas."""
    if payload is None:
        return {}, "PNL_PENDING", 0
    if not isinstance(payload, Mapping):
        return {}, "PNL_SCHEMA_UNAVAILABLE", 0
    direct_rows = payload.get("pnl")
    if isinstance(direct_rows, list):
        rows = direct_rows
    else:
        decoded = decode_recordset(payload)
        if decoded["status"] != "AVAILABLE":
            return {}, f"PNL_{decoded['status']}", 0
        rows = decoded["rows"]
    if not rows:
        return {}, "PNL_EMPTY", 0

    columns = {
        str(key)
        for row in rows if isinstance(row, Mapping)
        for key in row
    }
    date_key = "date" if "date" in columns else None
    value_key = next((key for key in ("pnl", "value") if key in columns), None)
    if date_key is None or value_key is None:
        return {}, "PNL_SCHEMA_UNAVAILABLE", 0

    series = {}
    skipped = 0
    for row in rows:
        if not isinstance(row, Mapping):
            skipped += 1
            continue
        raw_date = row.get(date_key)
        if not isinstance(raw_date, str) or len(raw_date) < 10:
            return {}, "INVALID_PNL_DATE", skipped + 1
        try:
            day = date.fromisoformat(raw_date[:10]).isoformat()
        except ValueError:
            return {}, "INVALID_PNL_DATE", skipped + 1
        raw_value = row.get(value_key)
        if isinstance(raw_value, bool) or raw_value is None:
            skipped += 1
            continue
        try:
            value = float(raw_value)
        except (TypeError, ValueError, OverflowError):
            skipped += 1
            continue
        if not math.isfinite(value):
            skipped += 1
            continue
        if day in series:
            return {}, "DUPLICATE_PNL_DATE", skipped
        series[day] = value
    if not series:
        return {}, "PNL_EMPTY", skipped
    return series, None, skipped


def _pearson_correlation(left, right):
    if len(left) != len(right) or len(left) < 2:
        return None, "INSUFFICIENT_OVERLAP"
    left_mean = sum(left) / len(left)
    right_mean = sum(right) / len(right)
    numerator = sum(
        (x - left_mean) * (y - right_mean)
        for x, y in zip(left, right)
    )
    left_sum_squares = sum((x - left_mean) ** 2 for x in left)
    right_sum_squares = sum((y - right_mean) ** 2 for y in right)
    denominator = math.sqrt(left_sum_squares * right_sum_squares)
    if denominator == 0:
        return None, "ZERO_VARIANCE"
    value = numerator / denominator
    if not math.isfinite(value):
        return None, "CORRELATION_UNAVAILABLE"
    return max(-1.0, min(1.0, value)), None


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

    def compare_alphas(
        self, alpha_ids, *, depth="summary", max_concurrent=4,
        pairwise_pnl=False, max_pairs=20,
    ):
        """Read Alpha evidence or calculate bounded pairwise daily-PnL evidence."""
        ids = [str(item).strip() for item in (alpha_ids or ()) if str(item).strip()]
        try:
            concurrency = int(max_concurrent)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("max_concurrent must be an integer") from exc
        if isinstance(max_concurrent, bool) or not 1 <= concurrency <= 4:
            raise ValueError("max_concurrent must be between 1 and 4")
        if pairwise_pnl:
            if (isinstance(max_pairs, bool) or not isinstance(max_pairs, int)
                    or not 1 <= max_pairs <= 20):
                raise ValueError("max_pairs must be between 1 and 20")
            return self._compare_pairwise_pnl(
                ids, max_concurrent=concurrency, max_pairs=max_pairs,
            )
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

    def _compare_pairwise_pnl(self, alpha_ids, *, max_concurrent, max_pairs):
        ids = list(dict.fromkeys(alpha_ids))
        if len(ids) > MAX_PAIRWISE_ALPHA_IDS:
            raise ValueError(
                f"pairwise comparison accepts at most {MAX_PAIRWISE_ALPHA_IDS} Alpha IDs"
            )
        if not ids:
            return {
                "source": "UNKNOWN", "status": "UNKNOWN",
                "evidence_status": "UNKNOWN", "method": "PEARSON_DAILY_PNL",
                "alpha_count": 0, "total_pair_count": 0,
                "available_pair_count": 0, "unknown_pair_count": 0,
                "strongest_pairs": [], "per_alpha_max": [],
                "numeric_threshold": "UNKNOWN",
            }

        def read_series(alpha_id):
            try:
                payload = self.client.get_pnl(alpha_id)
            except WQBNotFoundError:
                return alpha_id, {}, "PNL_UNAVAILABLE", 0
            except Exception:
                return alpha_id, {}, "PNL_READ_FAILED", 0
            series, reason, skipped = _daily_pnl_series(payload)
            return alpha_id, series, reason, skipped

        with ThreadPoolExecutor(max_workers=min(max_concurrent, len(ids))) as pool:
            observations = list(pool.map(read_series, ids))
        by_id = {
            alpha_id: {"series": series, "reason": reason, "skipped": skipped}
            for alpha_id, series, reason, skipped in observations
        }
        per_alpha = {}
        for alpha_id in ids:
            row = by_id[alpha_id]
            dates = sorted(row["series"])
            per_alpha[alpha_id] = {
                "alpha_id": alpha_id,
                "status": "AVAILABLE" if dates else "UNKNOWN",
                "observation_count": len(dates),
                "skipped_observation_count": row["skipped"],
                "first_date": dates[0] if dates else None,
                "last_date": dates[-1] if dates else None,
                "max_pairwise_status": "UNKNOWN",
                "max_abs_correlation": None,
                "max_correlation": None,
                "max_overlap_count": 0,
                "max_pairwise_alpha_id": None,
                "max_known_abs_correlation": None,
                "max_known_correlation": None,
                "max_known_overlap_count": 0,
                "max_known_pairwise_alpha_id": None,
                "available_pair_count": 0,
                "unknown_pair_count": 0,
                **({"reason_code": row["reason"]} if row["reason"] else {}),
            }

        total_pair_count = len(ids) * (len(ids) - 1) // 2
        available_pair_count = 0
        unknown_pair_count = 0
        unknown_reasons = {}
        strongest_pairs = []

        def note_unknown(alpha_a, alpha_b, reason, overlap):
            nonlocal unknown_pair_count
            unknown_pair_count += 1
            unknown_reasons[reason] = unknown_reasons.get(reason, 0) + 1
            per_alpha[alpha_a]["unknown_pair_count"] += 1
            per_alpha[alpha_b]["unknown_pair_count"] += 1
            per_alpha[alpha_a].setdefault("reason_code", reason)
            per_alpha[alpha_b].setdefault("reason_code", reason)
            pair = {
                "alpha_id_a": alpha_a, "alpha_id_b": alpha_b,
                "status": "UNKNOWN", "reason_code": reason,
                "correlation": None, "overlap_count": len(overlap),
                "overlap_start": overlap[0] if overlap else None,
                "overlap_end": overlap[-1] if overlap else None,
                "source": "BRAIN_LIVE", "freshness": "READ_AT_CALL",
            }
            if len(unknown_pairs) < max_pairs:
                unknown_pairs.append(pair)

        unknown_pairs = []
        for index, alpha_a in enumerate(ids):
            series_a = by_id[alpha_a]["series"]
            for alpha_b in ids[index + 1:]:
                series_b = by_id[alpha_b]["series"]
                if by_id[alpha_a]["reason"] or by_id[alpha_b]["reason"]:
                    reason = by_id[alpha_a]["reason"] or by_id[alpha_b]["reason"]
                    note_unknown(alpha_a, alpha_b, reason, [])
                    continue
                overlap = sorted(series_a.keys() & series_b.keys())
                if len(overlap) < 2:
                    note_unknown(alpha_a, alpha_b, "INSUFFICIENT_OVERLAP", overlap)
                    continue
                correlation, reason = _pearson_correlation(
                    [series_a[day] for day in overlap],
                    [series_b[day] for day in overlap],
                )
                if reason:
                    note_unknown(alpha_a, alpha_b, reason, overlap)
                    continue

                available_pair_count += 1
                absolute = abs(correlation)
                pair = {
                    "alpha_id_a": alpha_a, "alpha_id_b": alpha_b,
                    "status": "AVAILABLE", "correlation": correlation,
                    "absolute_correlation": absolute,
                    "overlap_count": len(overlap),
                    "overlap_start": overlap[0], "overlap_end": overlap[-1],
                    "source": "BRAIN_LIVE", "freshness": "READ_AT_CALL",
                }
                for alpha_id in (alpha_a, alpha_b):
                    row = per_alpha[alpha_id]
                    row["available_pair_count"] += 1
                    if (row["max_known_abs_correlation"] is None
                            or absolute > row["max_known_abs_correlation"]):
                        row["max_known_abs_correlation"] = absolute
                        row["max_known_correlation"] = correlation
                        row["max_known_overlap_count"] = len(overlap)
                        row["max_known_pairwise_alpha_id"] = (
                            alpha_b if alpha_id == alpha_a else alpha_a
                        )
                strongest_pairs.append(pair)

        strongest_pairs.sort(key=lambda row: (
            -row["absolute_correlation"], row["alpha_id_a"], row["alpha_id_b"],
        ))
        for _alpha_id, row in per_alpha.items():
            expected_pairs = max(0, len(ids) - 1)
            if row["available_pair_count"] == expected_pairs and expected_pairs:
                row["max_pairwise_status"] = "AVAILABLE"
                row["max_abs_correlation"] = row["max_known_abs_correlation"]
                row["max_correlation"] = row["max_known_correlation"]
                row["max_overlap_count"] = row["max_known_overlap_count"]
                row["max_pairwise_alpha_id"] = row["max_known_pairwise_alpha_id"]
            elif row["available_pair_count"]:
                row["max_pairwise_status"] = "PARTIAL"
                row.setdefault("reason_code", "PAIRWISE_EVIDENCE_INCOMPLETE")
        pairs_truncated = len(strongest_pairs) > max_pairs
        strongest_pairs = strongest_pairs[:max_pairs]
        if available_pair_count == total_pair_count and total_pair_count:
            status, evidence_status = "AVAILABLE", "AVAILABLE"
        elif available_pair_count:
            status, evidence_status = "PARTIAL", "PARTIAL"
        else:
            status, evidence_status = "UNKNOWN", "UNKNOWN"
        return {
            "source": "BRAIN_LIVE", "status": status,
            "evidence_status": evidence_status,
            "method": "PEARSON_DAILY_PNL",
            "fetched_at": time.time(), "age_sec": 0.0,
            "alpha_count": len(ids),
            "total_pair_count": total_pair_count,
            "available_pair_count": available_pair_count,
            "unknown_pair_count": unknown_pair_count,
            "unknown_pair_reasons": unknown_reasons,
            "strongest_pairs": strongest_pairs,
            "unknown_pairs": unknown_pairs,
            "unknown_pairs_truncated": unknown_pair_count > len(unknown_pairs),
            "per_alpha_max": [per_alpha[alpha_id] for alpha_id in ids],
            "pairs_truncated": pairs_truncated,
            "numeric_threshold": "UNKNOWN",
        }
