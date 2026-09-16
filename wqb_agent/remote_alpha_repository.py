"""Rebuildable remote Alpha repository over the existing feed cache."""

from __future__ import annotations

import os
import time
from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from .alpha_feed_cache import WEEKLY_SIMULATION_CAP, WeeklyAlphaFeedCache
from .optimization_interfaces import RemoteAlphaEvidenceProvider
from .query_errors import QueryTooBroadError

NEW_YORK = ZoneInfo("America/New_York")


def _remote_local_date(value):
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(NEW_YORK).date().isoformat()


class RemoteAlphaRepository:
    """Expose remote Alpha metadata and live evidence without local ownership."""

    def __init__(self, alpha_reader, *, cache_path, retention_days=7,
                 clock=None, evidence_client=None):
        try:
            days = int(retention_days)
        except (TypeError, ValueError) as exc:
            raise ValueError("retention_days 必须是 1-90 的整数") from exc
        if days < 1 or days > 90:
            raise ValueError("retention_days 必须是 1-90 的整数")
        self.retention_days = days
        self.cache = WeeklyAlphaFeedCache(
            cache_path, clock=clock,
            weekly_simulation_cap=days * 1600 if retention_days != 7 else WEEKLY_SIMULATION_CAP,
            retention_days=days,
        )
        self.alpha_reader = alpha_reader
        self._now = clock or time.time
        self.evidence = (
            RemoteAlphaEvidenceProvider(evidence_client)
            if evidence_client is not None else None
        )

    def refresh_remote_alphas(self, *, limit=100):
        if self.alpha_reader is None:
            raise RuntimeError("REMOTE_ALPHA_CLIENT_REQUIRED")
        refreshed_at = self._now()
        current_day = date.fromisoformat(self.cache.local_date)
        window_start = current_day - timedelta(days=self.retention_days - 1)
        days: dict[str, dict[str, list[dict[str, object]]]] = {}

        def fetch(status, date_field):
            start = datetime.combine(window_start, datetime.min.time(), tzinfo=NEW_YORK) - timedelta(seconds=1)
            end = datetime.combine(current_day + timedelta(days=1), datetime.min.time(), tzinfo=NEW_YORK)

            def read(start_at, end_at, depth):
                kwargs = {"status": status, "limit": limit, "max_results": 1000}
                kwargs[f"date_{date_field}_after"] = start_at.isoformat()
                kwargs[f"date_{date_field}_before"] = end_at.isoformat()
                try:
                    return self.alpha_reader(**kwargs)
                except QueryTooBroadError:
                    if depth >= 12 or end_at - start_at <= timedelta(minutes=1):
                        raise
                    midpoint = start_at + (end_at - start_at) / 2
                    return (read(start_at, midpoint + timedelta(seconds=1), depth + 1)
                            + read(midpoint - timedelta(seconds=1), end_at, depth + 1))

            return read(start, end, 0)

        def unique(rows):
            result, seen = [], set()
            for row in rows or ():
                if not isinstance(row, dict) or row.get("id") is None:
                    continue
                identity = str(row["id"])
                if identity not in seen:
                    seen.add(identity)
                    result.append(row)
            return result

        submitted_rows = unique(fetch("SUBMITTED", "submitted"))
        simulated_rows = unique(fetch("UNSUBMITTED", "created"))

        def bucket_for(value):
            local_value = _remote_local_date(value)
            if local_value is None:
                return None
            parsed = date.fromisoformat(local_value)
            if not window_start <= parsed <= current_day:
                return None
            return days.setdefault(parsed.isoformat(), {"simulations": [], "submitted_alphas": []})

        submitted = []
        for row in submitted_rows:
            record = {"alpha_id": str(row["id"]), "status": row.get("status"),
                      "date_submitted": row.get("dateSubmitted"),
                      "local_date": _remote_local_date(row.get("dateSubmitted")),
                      "source": "/users/self/alphas"}
            bucket = bucket_for(row.get("dateSubmitted"))
            if bucket is not None:
                bucket["submitted_alphas"].append(record)
                submitted.append(record)
        submitted.sort(key=lambda item: str(item.get("date_submitted") or ""), reverse=True)

        simulated = []
        for row in simulated_rows:
            local_value = _remote_local_date(row.get("dateCreated"))
            bucket = bucket_for(row.get("dateCreated"))
            if bucket is None:
                continue
            record = {"alpha_id": str(row["id"]), "status": row.get("status"),
                      "date_created": row.get("dateCreated"), "local_date": local_value,
                      "source": "/users/self/alphas"}
            bucket["simulations"].append(record)
            simulated.append(record)
        simulated.sort(key=lambda item: str(item.get("date_created") or ""), reverse=True)
        cache_result = self.cache.refresh(days)
        return {"local_date": current_day.isoformat(), "refreshed_at": refreshed_at,
                "submitted_count": len(submitted),
                "today_simulated_count": sum(item.get("local_date") == current_day.isoformat() for item in simulated),
                "weekly_simulated_count": len(simulated), **cache_result,
                "retention_days": self.retention_days, "source": "/users/self/alphas"}

    def list_remote_alphas(self, *, days=None, status=None):
        days = self.retention_days if days is None else int(days)
        if days < 1 or days > self.retention_days:
            raise ValueError("days must be within the configured retention window")
        payload = self.cache.load() or {}
        latest = date.fromisoformat(self.cache.local_date)
        start = latest - timedelta(days=days - 1)
        result = []
        for raw_day, bucket in (payload.get("days") or {}).items():
            try:
                bucket_day = date.fromisoformat(str(raw_day))
            except (TypeError, ValueError):
                continue
            if bucket_day < start or bucket_day > latest:
                continue
            for key in ("simulations", "submitted_alphas"):
                for row in bucket.get(key, ()) if isinstance(bucket, Mapping) else ():
                    if status is None or str(row.get("status") or "").upper() == str(status).upper():
                        result.append(dict(row))
        seen = set()
        return [row for row in result if not (str(row.get("alpha_id")) in seen or seen.add(str(row.get("alpha_id"))))]

    def get_remote_alpha(self, alpha_id, *, live=False):
        if live:
            if self.evidence is None:
                raise RuntimeError("LIVE_ALPHA_CLIENT_REQUIRED")
            return self.evidence.get_alpha(alpha_id)
        for row in self.list_remote_alphas():
            if str(row.get("alpha_id")) == str(alpha_id):
                return row
        return None

    def get_remote_alpha_evidence(self, alpha_id, *, live=True):
        if self.evidence is None:
            raise RuntimeError("LIVE_ALPHA_CLIENT_REQUIRED")
        return self.evidence.get_alpha_evidence(alpha_id, live=live)

    def cache_status(self):
        return {"retention_days": self.retention_days, **self.cache.freshness_snapshot()}

    def purge_remote_cache(self):
        try:
            os.unlink(self.cache.path)
        except FileNotFoundError:
            return False
        return True
