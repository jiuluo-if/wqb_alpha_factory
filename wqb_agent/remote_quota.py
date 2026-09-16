"""Read-only quota projection from remote Alpha metadata and execution guards."""

from __future__ import annotations

from datetime import UTC, datetime


class SimulationQuota:
    """Project remote usage without creating a local quota state machine."""

    def __init__(self, repository, guard, *, daily_cap=1600, rolling_cap=11200,
                 local_date=None):
        self.repository = repository
        self.guard = guard
        self.daily_cap = self._cap(daily_cap, "daily_cap")
        self.rolling_cap = self._cap(rolling_cap, "rolling_cap")
        if self.daily_cap > self.rolling_cap:
            raise ValueError("daily_cap 不得超过 rolling_cap")
        self._local_date = local_date or self._today

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
    def _today():
        return datetime.now(UTC).astimezone().date().isoformat()

    def snapshot(self):
        today = str(self._local_date())
        rows = [row for row in self.repository.list_remote_alphas()
                if isinstance(row, dict)]
        today_used = sum(1 for row in rows if str(row.get("local_date")) == today)
        active = len(self.guard.entries())
        rolling_used = len(rows) + active
        return {
            "today_used": today_used + active,
            "rolling_used": rolling_used,
            "today_remaining": max(0, self.daily_cap - today_used - active),
            "rolling_remaining": max(0, self.rolling_cap - rolling_used),
            "daily_cap": self.daily_cap,
            "rolling_cap": self.rolling_cap,
            "active_guard_count": active,
            "source": "REMOTE_ALPHA_REPOSITORY+EXECUTION_GUARD",
            "persisted_quota_state": False,
        }


# Compatibility name for integrations migrated from the intermediate
# Remote-First implementation.  The production owner is SimulationQuota;
# this alias does not retain any factory/session state.
RemoteSimulationQuota = SimulationQuota
