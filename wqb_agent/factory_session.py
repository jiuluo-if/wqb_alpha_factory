"""Canonical bounded factory-session file operations."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from copy import deepcopy

from .artifacts import atomic_write_json_if_changed
from .locking import OwnerBusyError, single_instance_scope


def read_session(state_dir, session_file="factory_session.json"):
    path = os.path.join(state_dir, session_file)
    try:
        with open(path, encoding="utf-8") as handle:
            session = json.load(handle)
        if not isinstance(session, dict) or not isinstance(
            session.get("session_id"), str
        ):
            return None
        return session
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


def request_stop(
    state_dir,
    *,
    session_file="factory_session.json",
    validate: Callable[[dict], None],
    project: Callable[[dict], dict],
):
    try:
        with single_instance_scope(state_dir, operation="factory-stop"):
            session = read_session(state_dir, session_file)
            if not session or session.get("status") != "RUNNING":
                return None
            try:
                float(session["deadline"])
            except (KeyError, TypeError, ValueError):
                return None
            source = deepcopy(session)
            source["stop_requested"] = True
            source["last_action"] = "STOP_REQUESTED"
            validate(source)
            projected = project(source)
            atomic_write_json_if_changed(
                os.path.join(state_dir, session_file),
                projected,
                ignored_keys=("updated_at",),
            )
            return projected
    except OwnerBusyError:
        return {"status": "LOCAL_OWNER_BUSY", "last_action": "LOCAL_OWNER_BUSY"}


def status_view(
    state_dir,
    *,
    session_file="factory_session.json",
    project: Callable[[dict], dict],
):
    raw_session = read_session(state_dir, session_file)
    path = os.path.join(state_dir, session_file)
    if not raw_session:
        if os.path.exists(path):
            return {"status": "RECONCILE_REQUIRED", "last_action": "INVALID_SESSION"}
        return None
    session = project(raw_session)
    keys = (
        "schema_version",
        "session_id",
        "started_at",
        "deadline",
        "status",
        "stop_requested",
        "rounds_completed",
        "simulations_reserved",
        "simulation_cap",
        "quota",
        "last_round",
        "last_action",
        "last_result",
        "blocker",
    )
    view = {key: session[key] for key in keys if key in session}
    if isinstance(view.get("quota"), dict):
        view["quota"] = {
            key: view["quota"][key]
            for key in (
                "schema_version",
                "timezone",
                "local_date",
                "week_start",
                "daily_cap",
                "weekly_cap",
                "daily_reserved",
                "weekly_reserved",
            )
            if key in view["quota"]
        }
    if isinstance(view.get("last_result"), dict):
        view["last_result"] = {
            key: view["last_result"][key]
            for key in (
                "round_no",
                "proposals",
                "summary_round",
                "verdict_count",
                "verdict_class_counts",
                "best_present",
                "status",
                "error_type",
            )
            if key in view["last_result"]
        }
    if isinstance(view.get("blocker"), dict):
        view["blocker"] = {
            key: view["blocker"].get(key)
            for key in (
                "kind",
                "consecutive_same_count",
                "next_recheck_at",
                "resume_hint",
            )
            if key in view["blocker"]
        }
    return view
