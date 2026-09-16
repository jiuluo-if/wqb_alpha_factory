"""BRAIN 用户 Alpha 轻量元数据的只读同步工作流。"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from .daily_cache import NEW_YORK
from .query_errors import QueryTooBroadError


def remote_local_date(value):
    """把远端时间戳归一化为纽约本地日；无效值保持为空。"""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(NEW_YORK).date().isoformat()


@dataclass(frozen=True)
class AlphaFeedHooks:
    """Alpha Feed 所需的唯一远端只读操作。"""

    get_all_user_alphas: Callable[..., list]


class AlphaFeedWorkflow:
    """同步七个纽约自然日内的轻量 Alpha 元数据。"""

    def __init__(
        self,
        *,
        alpha_reader=None,
        daily_cache,
        weekly_cache,
        local_date_provider=None,
        now=None,
        heartbeat=None,
        retention_days=7,
    ):
        self.alpha_reader = alpha_reader
        self.daily_cache = daily_cache
        self.weekly_cache = weekly_cache
        self._local_date_provider = (
            local_date_provider or (lambda: self.daily_cache.local_date)
        )
        self._now = now or time.time
        self.heartbeat = heartbeat
        try:
            self.retention_days = int(retention_days)
        except (TypeError, ValueError) as exc:
            raise ValueError("retention_days 必须是 1-90 的整数") from exc
        if not 1 <= self.retention_days <= 90:
            raise ValueError("retention_days 必须是 1-90 的整数")

    @staticmethod
    def _remote_local_date(value):
        """保留 workflow 内部可测试的日期归一化入口。"""
        return remote_local_date(value)

    def refresh(self, *, limit=100):
        """只读拉取、分桶并刷新两个轻量缓存。"""
        refreshed_at = self._now()
        local_date = self._local_date_provider()
        current_day = date.fromisoformat(local_date)
        week_start = current_day - timedelta(days=self.retention_days - 1)
        days: dict[str, dict[str, list[dict[str, object]]]] = {}
        windows_completed = {"SUBMITTED": 0, "UNSUBMITTED": 0}
        rows_fetched = {"SUBMITTED": 0, "UNSUBMITTED": 0}
        if self.heartbeat is not None:
            self.heartbeat.emit_stage(
                "ALPHA_FEED_REFRESH", query_kind="SUBMITTED",
                split_depth=0, windows_completed=0, rows_fetched=0,
            )

        def fetch_window(status, field):
            start = datetime.combine(
                week_start, datetime.min.time(), tzinfo=NEW_YORK
            ) - timedelta(seconds=1)
            end = datetime.combine(
                current_day + timedelta(days=1), datetime.min.time(),
                tzinfo=NEW_YORK,
            )

            def fetch(start_at, end_at, depth):
                kwargs = {
                    "status": status,
                    "limit": limit,
                    "max_results": 1000,
                }
                if field == "created":
                    kwargs.update({
                        "date_created_after": start_at.isoformat(),
                        "date_created_before": end_at.isoformat(),
                    })
                else:
                    kwargs.update({
                        "date_submitted_after": start_at.isoformat(),
                        "date_submitted_before": end_at.isoformat(),
                    })
                try:
                    rows = self.alpha_reader(**kwargs)
                    windows_completed[status] += 1
                    rows_fetched[status] += len(rows) if isinstance(rows, list) else 0
                    if self.heartbeat is not None:
                        self.heartbeat.emit_stage(
                            "ALPHA_FEED_REFRESH", query_kind=status,
                            split_depth=depth,
                            windows_completed=windows_completed[status],
                            rows_fetched=rows_fetched[status],
                        )
                    return rows
                except QueryTooBroadError:
                    if depth >= 12 or end_at - start_at <= timedelta(minutes=1):
                        raise
                    midpoint = start_at + (end_at - start_at) / 2
                    return (
                        fetch(start_at, midpoint + timedelta(seconds=1), depth + 1)
                        + fetch(midpoint - timedelta(seconds=1), end_at, depth + 1)
                    )

            return fetch(start, end, 0)

        def unique_rows(rows):
            result = []
            seen = set()
            for row in rows:
                if not isinstance(row, dict):
                    continue
                identity = row.get("id")
                if identity is None or str(identity) in seen:
                    continue
                seen.add(str(identity))
                result.append(row)
            return result

        submitted_rows = unique_rows(fetch_window("SUBMITTED", "submitted"))
        simulated_rows = unique_rows(fetch_window("UNSUBMITTED", "created"))

        def bucket_for(value):
            local_value = self._remote_local_date(value)
            if local_value is None:
                return None
            try:
                parsed = date.fromisoformat(local_value)
            except ValueError:
                return None
            if not (week_start <= parsed <= current_day):
                return None
            key = parsed.isoformat()
            return days.setdefault(key, {"simulations": [], "submitted_alphas": []})

        submitted = []
        for row in submitted_rows:
            if not isinstance(row, dict) or not row.get("id"):
                continue
            record = {
                "alpha_id": str(row["id"]),
                "status": row.get("status"),
                "date_submitted": row.get("dateSubmitted"),
                "local_date": self._remote_local_date(row.get("dateSubmitted")),
                "source": "/users/self/alphas",
            }
            bucket = bucket_for(row.get("dateSubmitted"))
            if bucket is not None:
                bucket["submitted_alphas"].append(record)
                submitted.append(record)
        submitted.sort(
            key=lambda item: str(item.get("date_submitted") or ""),
            reverse=True,
        )
        today_simulated = []
        weekly_simulated = []
        for row in simulated_rows:
            if not isinstance(row, dict) or not row.get("id"):
                continue
            row_local_date = self._remote_local_date(row.get("dateCreated"))
            bucket = bucket_for(row.get("dateCreated"))
            if bucket is None:
                continue
            record = {
                "alpha_id": str(row["id"]),
                "status": row.get("status"),
                "date_created": row.get("dateCreated"),
                "local_date": row_local_date,
                "source": "/users/self/alphas",
            }
            bucket["simulations"].append(record)
            weekly_simulated.append(record)
            if row_local_date == local_date:
                today_simulated.append(record)
        today_simulated.sort(
            key=lambda item: str(item.get("date_created") or ""),
            reverse=True,
        )
        weekly_simulated.sort(
            key=lambda item: str(item.get("date_created") or ""),
            reverse=True,
        )
        self.daily_cache.put_submitted_alphas(submitted)
        self.daily_cache.put_simulations(today_simulated)
        cache_result = self.weekly_cache.refresh(days)
        return {
            "local_date": local_date,
            "refreshed_at": refreshed_at,
            "submitted_count": len(submitted),
            "today_simulated_count": len(today_simulated),
            "weekly_simulated_count": len(weekly_simulated),
            **cache_result,
            "source": "/users/self/alphas",
        }
