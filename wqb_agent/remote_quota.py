"""Read-only quota projection from remote Alpha metadata and execution guards."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from .config import DEFAULT_DAILY_SIMULATION_LIMIT
from .simulation_gateway import MULTI_MAX_CHILDREN

NEW_YORK = ZoneInfo("America/New_York")
OFFICIAL_SOURCE = "BRAIN_SIMULATION_HEADERS"
ESTIMATE_SOURCE = "ESTIMATE_REMOTE_ALPHA_REPOSITORY+EXECUTION_GUARD"


def _estimate_window(repository):
    retention_days = getattr(repository, "retention_days", None)
    if (
        isinstance(retention_days, bool)
        or not isinstance(retention_days, int)
        or not 1 <= retention_days <= 90
    ):
        return None, "UNKNOWN"
    return retention_days, "REMOTE_CACHE_RETENTION"


def _valid_local_date(value):
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return date.fromisoformat(value.strip()).isoformat()
    except ValueError:
        return None


def _creation_local_date(value):
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(NEW_YORK).date().isoformat()


def _simulation_local_date(row):
    simulation_day = _valid_local_date(row.get("simulation_local_date"))
    if simulation_day is not None:
        return simulation_day
    simulation_day = _creation_local_date(row.get("date_created"))
    if simulation_day is not None:
        return simulation_day
    status = str(row.get("status") or "").upper()
    if "date_submitted" in row or status == "SUBMITTED":
        return None
    return _valid_local_date(row.get("local_date"))


def _in_estimate_window(simulation_day, today, window_days):
    if window_days is None:
        return True
    try:
        current_day = date.fromisoformat(today)
        candidate = date.fromisoformat(simulation_day)
    except ValueError:
        return False
    window_start = current_day - timedelta(days=window_days - 1)
    return window_start <= candidate <= current_day


def _guard_simulation_count(row):
    value = row.get("simulation_count") if isinstance(row, Mapping) else None
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= MULTI_MAX_CHILDREN
    ):
        return 1
    return value


def _unknown_official_observation():
    return {
        "status": "UNKNOWN",
        "evidence_status": "UNAVAILABLE",
        "source": OFFICIAL_SOURCE,
        "limit": None,
        "remaining": None,
        "reset": None,
    }


def _official_projection(observation):
    if not isinstance(observation, Mapping):
        return _unknown_official_observation()
    if observation.get("source") != OFFICIAL_SOURCE:
        return _unknown_official_observation()
    status = str(observation.get("status") or "UNKNOWN").upper()
    if status not in {"AVAILABLE", "PARTIAL"}:
        return _unknown_official_observation()
    values = {}
    for key in ("limit", "remaining", "reset"):
        value = observation.get(key)
        values[key] = (
            int(value)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0
            else None
        )
    if status == "AVAILABLE" and any(value is None for value in values.values()):
        status = "PARTIAL"
    return {
        "status": status,
        "evidence_status": (
            "AVAILABLE" if status == "AVAILABLE" else "INCONCLUSIVE"
        ),
        "source": OFFICIAL_SOURCE,
        **values,
    }


class SimulationQuota:
    """Project remote usage without creating a local quota state machine."""

    def __init__(self, repository, guard, *, daily_cap=DEFAULT_DAILY_SIMULATION_LIMIT,
                 local_date=None, official_observation=None):
        self.repository = repository
        self.guard = guard
        self.daily_cap = self._cap(daily_cap, "daily_cap")
        self._local_date = local_date or self._today
        self.official_observation = official_observation

    @staticmethod
    def _cap(value, name):
        if isinstance(value, bool):
            raise ValueError(f"{name} 必须是非负整数")
        try:
            result = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name} 必须是非负整数") from exc
        if result < 0:
            raise ValueError(f"{name} 必须是非负整数")
        return result

    @staticmethod
    def _today(now=None):
        current = datetime.now(NEW_YORK) if now is None else now
        if current.tzinfo is None:
            current = current.replace(tzinfo=UTC)
        return current.astimezone(NEW_YORK).date().isoformat()

    def snapshot(self):
        today = str(self._local_date())
        rows = [row for row in self.repository.list_remote_alphas()
                if isinstance(row, dict)]
        window_days, window_source = _estimate_window(self.repository)
        simulation_days = []
        for row in rows:
            simulation_day = _simulation_local_date(row)
            if simulation_day is not None and _in_estimate_window(
                simulation_day, today, window_days
            ):
                simulation_days.append(simulation_day)
        today_used = sum(day == today for day in simulation_days)
        entries = self.guard.entries()
        active_guard_count = len(entries)
        active_guard_simulation_count = sum(
            _guard_simulation_count(row) for row in entries
        )
        window_used = len(simulation_days) + active_guard_simulation_count
        estimate = {
            "today_used": today_used + active_guard_simulation_count,
            "today_remaining": max(
                0, self.daily_cap - today_used - active_guard_simulation_count
            ),
            "daily_cap": self.daily_cap,
            "window_used": window_used,
            "active_guard_count": active_guard_count,
            "active_guard_simulation_count": active_guard_simulation_count,
            "window_days": window_days,
            "window_source": window_source,
            "source": ESTIMATE_SOURCE,
            "persisted_quota_state": False,
            "evidence_status": "APPROXIMATE",
            "approximate": True,
        }
        official = _official_projection(self.official_observation)
        official_available = official["status"] in {"AVAILABLE", "PARTIAL"}
        return {
            **estimate,
            "source": official["source"] if official_available else estimate["source"],
            "evidence_status": (
                official["evidence_status"] if official_available
                else estimate["evidence_status"]
            ),
            "official": official,
            "estimate": estimate,
            "usage_semantics": "APPROXIMATE_ESTIMATE",
            "legacy_fields_are_estimate": True,
        }
