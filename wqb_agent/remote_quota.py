"""Read-only quota projection from remote Alpha metadata and execution guards."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

NEW_YORK = ZoneInfo("America/New_York")
OFFICIAL_SOURCE = "BRAIN_SIMULATION_HEADERS"
ESTIMATE_SOURCE = "ESTIMATE_REMOTE_ALPHA_REPOSITORY+EXECUTION_GUARD"


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

    def __init__(self, repository, guard, *, daily_cap=1600, rolling_cap=11200,
                 local_date=None, official_observation=None):
        self.repository = repository
        self.guard = guard
        self.daily_cap = self._cap(daily_cap, "daily_cap")
        self.rolling_cap = self._cap(rolling_cap, "rolling_cap")
        if self.daily_cap > self.rolling_cap:
            raise ValueError("daily_cap 不得超过 rolling_cap")
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
        today_used = sum(1 for row in rows if str(row.get("local_date")) == today)
        active = len(self.guard.entries())
        rolling_used = len(rows) + active
        estimate = {
            "today_used": today_used + active,
            "rolling_used": rolling_used,
            "today_remaining": max(0, self.daily_cap - today_used - active),
            "rolling_remaining": max(0, self.rolling_cap - rolling_used),
            "daily_cap": self.daily_cap,
            "rolling_cap": self.rolling_cap,
            "active_guard_count": active,
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


# Compatibility name for integrations migrated from the intermediate
# Remote-First implementation.  The production owner is SimulationQuota;
# this alias does not retain any factory/session state.
RemoteSimulationQuota = SimulationQuota
