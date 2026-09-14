"""Factory control-plane quota adapters using the canonical quota owner."""

from __future__ import annotations

from collections.abc import Mapping


def prepare_quota(session: dict, quota):
    raw_state = session.get("quota")
    if raw_state is None:
        try:
            legacy_reserved = max(0, int(session.get("simulations_reserved", 0)))
        except (TypeError, ValueError):
            legacy_reserved = 0
        if legacy_reserved > quota.weekly_cap:
            raise ValueError("legacy reservation exceeds weekly quota")
        state = quota.initial_state()
        state["weekly_reserved"] = legacy_reserved
        state["daily_reserved"] = min(legacy_reserved, quota.daily_cap)
    else:
        state = quota.normalize_state(raw_state)
    session["quota"] = state
    session["simulation_cap"] = quota.weekly_cap
    session["simulations_reserved"] = state["weekly_reserved"]
    return quota


def carry_forward_quota(previous_session: Mapping[str, object] | None, quota):
    if not previous_session:
        return quota.initial_state()
    raw_state = previous_session.get("quota")
    if raw_state is None:
        legacy_reserved = previous_session.get("simulations_reserved", 0)
        if isinstance(legacy_reserved, bool):
            raise ValueError("legacy reservation must be an integer")
        try:
            legacy_reserved = int(legacy_reserved)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("legacy reservation must be an integer") from exc
        if legacy_reserved < 0:
            raise ValueError("legacy reservation must be non-negative")
        raw_state = quota.initial_state()
        raw_state["daily_reserved"] = min(legacy_reserved, quota.daily_cap)
        raw_state["weekly_reserved"] = legacy_reserved
    return quota.normalize_state(raw_state)


def quota_remaining(session: dict, quota):
    state = quota.normalize_state(session.get("quota"))
    session["quota"] = state
    session["simulations_reserved"] = state["weekly_reserved"]
    return quota.remaining(state)


def quota_reserve(session: dict, quota, count):
    state = quota.reserve(session.get("quota"), count)
    session["quota"] = state
    session["simulations_reserved"] = state["weekly_reserved"]


def quota_release(session: dict, quota, count):
    state = quota.release(session.get("quota"), count)
    session["quota"] = state
    session["simulations_reserved"] = state["weekly_reserved"]
