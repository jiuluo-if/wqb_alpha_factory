"""Weekly, bounded cache for lightweight remote Alpha metadata."""

from __future__ import annotations

import json
import os
from datetime import UTC, date, datetime, timedelta

from .artifacts import atomic_write_json_if_changed
from .daily_cache import NEW_YORK

SCHEMA_VERSION = 1
WEEKLY_SIMULATION_CAP = 7 * 1600
TEMP_RESOURCE_TTL_SEC = 7 * 24 * 60 * 60


def refresh_due(now, last_refresh, interval_sec=3 * 3600):
    """Return whether a feed refresh is due, failing safe on bad timestamps."""
    try:
        current = float(now)
        previous = float(last_refresh)
        interval = float(interval_sec)
    except (TypeError, ValueError, OverflowError):
        return True
    if interval <= 0 or current < previous:
        return True
    return current - previous >= interval


def _local_date(timestamp):
    return datetime.fromtimestamp(timestamp, tz=UTC).astimezone(
        NEW_YORK
    ).date()


def _week_start(local_day):
    """Return the rolling seven-day window start, inclusive."""
    return local_day - timedelta(days=6)


def _utc_iso(timestamp):
    return datetime.fromtimestamp(timestamp, tz=UTC).isoformat().replace(
        "+00:00", "Z"
    )


class WeeklyAlphaFeedCache:
    """Persist only the rolling seven-day New York window of Alpha metadata.

    This is a rebuildable view, not research evidence.  It stores no metrics,
    expressions, trajectory rows, or platform result payloads.
    """

    def __init__(self, path, *, clock=None, weekly_simulation_cap=WEEKLY_SIMULATION_CAP,
                 retention_days=7):
        if not path:
            raise ValueError("Alpha feed cache path 不能为空")
        try:
            cap = int(weekly_simulation_cap)
        except (TypeError, ValueError) as exc:
            raise ValueError("weekly_simulation_cap 必须是正整数") from exc
        if cap < 1:
            raise ValueError("weekly_simulation_cap 必须是正整数")
        try:
            days = int(retention_days)
        except (TypeError, ValueError) as exc:
            raise ValueError("retention_days 必须是 1-90 的整数") from exc
        if days < 1 or days > 90:
            raise ValueError("retention_days 必须是 1-90 的整数")
        self.path = os.path.abspath(path)
        self._clock = clock or __import__("time").time
        self.weekly_simulation_cap = cap
        self.retention_days = days

    @property
    def local_date(self):
        return _local_date(self._clock()).isoformat()

    @property
    def week_start(self):
        return (
            _local_date(self._clock()) - timedelta(days=self.retention_days - 1)
        ).isoformat()

    def _cleanup_expired_resources(self):
        directory = os.path.dirname(self.path)
        if not os.path.isdir(directory):
            return 0
        prefix = os.path.basename(self.path) + ".tmp."
        cutoff = self._clock() - TEMP_RESOURCE_TTL_SEC
        removed = 0
        try:
            entries = list(os.scandir(directory))
        except OSError:
            return 0
        for entry in entries:
            if not entry.name.startswith(prefix):
                continue
            try:
                if entry.stat().st_mtime < cutoff:
                    os.unlink(entry.path)
                    removed += 1
            except (FileNotFoundError, OSError):
                continue
        return removed

    @staticmethod
    def _records(value):
        if not isinstance(value, list):
            return []
        result = []
        seen = set()
        for item in value:
            if not isinstance(item, dict):
                continue
            identity = item.get("alpha_id") or item.get("id")
            if identity is None or str(identity) in seen:
                continue
            seen.add(str(identity))
            result.append(dict(item))
        return result

    def _normalize_days(self, days, local_day):
        if not isinstance(days, dict):
            raise TypeError("Alpha feed days 必须是对象")
        start = local_day - timedelta(days=self.retention_days - 1)
        normalized = {}
        for raw_day, bucket in days.items():
            try:
                bucket_day = date.fromisoformat(str(raw_day))
            except (TypeError, ValueError):
                continue
            if bucket_day < start or bucket_day > local_day:
                continue
            bucket = bucket if isinstance(bucket, dict) else {}
            simulations = self._records(bucket.get("simulations"))
            submitted = self._records(bucket.get("submitted_alphas"))
            if simulations or submitted:
                normalized[bucket_day.isoformat()] = {
                    "simulations": simulations,
                    "submitted_alphas": submitted,
                }
        return normalized

    @staticmethod
    def _simulation_sort_key(day, row):
        return (
            str(day),
            str(row.get("date_created") or row.get("dateCreated") or ""),
            str(row.get("alpha_id") or row.get("id") or ""),
        )

    def _prune_simulations(self, days, local_day):
        entries = [
            (day, row)
            for day, bucket in days.items()
            for row in bucket["simulations"]
        ]
        excess = max(0, len(entries) - self.weekly_simulation_cap)
        if not excess:
            return 0
        current_key = local_day.isoformat()
        removable = sorted(
            entries,
            key=lambda item: (
                item[0] == current_key,
                self._simulation_sort_key(*item),
            ),
        )
        remove = {
            (day, str(row.get("alpha_id") or row.get("id")))
            for day, row in removable[:excess]
        }
        for day, bucket in days.items():
            bucket["simulations"] = [
                row for row in bucket["simulations"]
                if (day, str(row.get("alpha_id") or row.get("id"))) not in remove
            ]
        for day in list(days):
            if not days[day]["simulations"] and not days[day]["submitted_alphas"]:
                del days[day]
        return len(remove)

    def refresh(self, days):
        now = self._clock()
        local_day = _local_date(now)
        normalized = self._normalize_days(days, local_day)
        pruned = self._prune_simulations(normalized, local_day)
        next_day = local_day + timedelta(days=1)
        expires_at = datetime.combine(
            next_day, datetime.min.time(), tzinfo=NEW_YORK
        ).astimezone(UTC).timestamp()
        payload = {
            "schema_version": SCHEMA_VERSION,
            "timezone": "America/New_York",
            "retention_days": self.retention_days,
            "week_start": (local_day - timedelta(days=self.retention_days - 1)).isoformat(),
            "local_date": local_day.isoformat(),
            "updated_at": _utc_iso(now),
            "expires_at": _utc_iso(expires_at),
            "days": normalized,
        }
        expired_resources = self._cleanup_expired_resources()
        atomic_write_json_if_changed(self.path, payload)
        simulation_count = sum(
            len(bucket["simulations"]) for bucket in normalized.values()
        )
        submitted_count = sum(
            len(bucket["submitted_alphas"]) for bucket in normalized.values()
        )
        return {
            "cache_path": self.path,
            "local_date": payload["local_date"],
            "week_start": payload["week_start"],
            "updated_at": payload["updated_at"],
            "expires_at": payload["expires_at"],
            "simulation_count": simulation_count,
            "submitted_count": submitted_count,
            "pruned_simulation_count": pruned,
            "expired_resource_count": expired_resources,
        }

    def load(self):
        self._cleanup_expired_resources()
        try:
            with open(self.path, encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, ValueError, json.JSONDecodeError):
            return None
        if (
            not isinstance(payload, dict)
            or payload.get("schema_version") != SCHEMA_VERSION
            or payload.get("timezone") != "America/New_York"
            or payload.get("retention_days", 7) != self.retention_days
            or payload.get("week_start") != self.week_start
        ):
            try:
                os.unlink(self.path)
            except (FileNotFoundError, OSError):
                pass
            return None
        return payload

    def freshness_snapshot(self, *, now=None, interval_sec=3 * 3600):
        """Expose cache freshness without exposing research evidence."""
        current = self._clock() if now is None else now
        payload = self.load()
        if not isinstance(payload, dict):
            return {"freshness": "UNKNOWN", "last_success_at": None,
                    "age_sec": None, "next_due_at": None}
        try:
            updated = datetime.fromisoformat(
                str(payload["updated_at"]).replace("Z", "+00:00")
            ).timestamp()
        except (KeyError, TypeError, ValueError, OverflowError):
            return {"freshness": "UNKNOWN", "last_success_at": None,
                    "age_sec": None, "next_due_at": None}
        age = float(current) - updated
        try:
            expires = datetime.fromisoformat(
                str(payload["expires_at"]).replace("Z", "+00:00")
            ).timestamp()
        except (KeyError, TypeError, ValueError, OverflowError):
            expires = None
        stale = refresh_due(current, updated, interval_sec)
        if expires is not None and float(current) >= expires:
            stale = True
        return {
            "freshness": "FRESH" if not stale else "STALE",
            "last_success_at": updated,
            "age_sec": age if age >= 0 else None,
            "next_due_at": updated + float(interval_sec),
        }
