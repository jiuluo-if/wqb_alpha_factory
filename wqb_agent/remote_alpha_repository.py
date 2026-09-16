"""Rebuildable remote Alpha repository over the existing feed cache."""

from __future__ import annotations

import os
from collections.abc import Mapping
from datetime import date, timedelta

from .alpha_feed_cache import WEEKLY_SIMULATION_CAP, WeeklyAlphaFeedCache
from .alpha_feed_workflow import AlphaFeedWorkflow
from .daily_cache import DailyResearchCache
from .optimization_interfaces import RemoteAlphaEvidenceProvider


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
        self.daily_cache = DailyResearchCache(clock=clock)
        self.cache = WeeklyAlphaFeedCache(
            cache_path, clock=clock,
            weekly_simulation_cap=days * 1600 if retention_days != 7 else WEEKLY_SIMULATION_CAP,
            retention_days=days,
        )
        self.feed = AlphaFeedWorkflow(
            alpha_reader=alpha_reader, daily_cache=self.daily_cache,
            weekly_cache=self.cache, retention_days=days,
        )
        self.evidence = (
            RemoteAlphaEvidenceProvider(evidence_client)
            if evidence_client is not None else None
        )

    def refresh_remote_alphas(self, *, limit=100):
        if self.feed.alpha_reader is None:
            raise RuntimeError("REMOTE_ALPHA_CLIENT_REQUIRED")
        result = self.feed.refresh(limit=limit)
        return {**result, "retention_days": self.retention_days}

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
