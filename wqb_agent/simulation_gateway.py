"""Remote-first Simulation tools with a deliberately tiny write guard."""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .artifacts import atomic_write_json_if_changed
from .expression import analyze_expression, submission_fingerprint
from .simulator import Simulator
from .state import Experiment


@dataclass(frozen=True)
class SimulationSpec:
    """The only research-authored input required to start a Simulation."""

    expression: str
    settings: Mapping[str, Any] = field(default_factory=dict)
    fields: tuple[str, ...] = field(default_factory=tuple)
    note: str | None = None
    template_id: str | None = None

    def __post_init__(self):
        expression = str(self.expression or "").strip()
        if not expression:
            raise ValueError("expression must be non-empty")
        if not isinstance(self.settings, Mapping):
            raise TypeError("settings must be an object")
        if expression.count("(") != expression.count(")"):
            raise ValueError("expression has unbalanced parentheses")
        object.__setattr__(self, "expression", expression)
        object.__setattr__(self, "settings", dict(self.settings))
        object.__setattr__(
            self, "fields", tuple(str(item) for item in (self.fields or ())
                                   if str(item).strip())
        )


class ExecutionGuard:
    """Persist only unresolved remote-write identities."""

    STATUSES = frozenset({"SUBMITTING", "RUNNING", "SUBMIT_UNKNOWN"})

    def __init__(self, state_dir):
        self.state_dir = os.path.abspath(str(state_dir))
        self.path = os.path.join(self.state_dir, "execution_guard.json")
        self._lock = threading.RLock()

    @staticmethod
    def fingerprint(expression, settings):
        return submission_fingerprint(expression, settings)

    def _read(self):
        try:
            import json
            with open(self.path, encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, ValueError, TypeError):
            payload = {}
        rows = payload.get("entries", []) if isinstance(payload, dict) else []
        return [row for row in rows if isinstance(row, dict) and row.get("execution_fingerprint")]

    def _write(self, rows):
        os.makedirs(self.state_dir, exist_ok=True)
        atomic_write_json_if_changed(
            self.path, {"schema_version": 1, "entries": rows}, sort_keys=True
        )

    def entries(self):
        with self._lock:
            return list(self._read())

    def find(self, fingerprint):
        return next((row for row in self.entries()
                     if row.get("execution_fingerprint") == fingerprint), None)

    def register(self, fingerprint, *, progress_url=None, status="SUBMITTING"):
        if status not in self.STATUSES:
            raise ValueError("invalid execution guard status")
        now = time.time()
        with self._lock:
            rows = self._read()
            existing = next((row for row in rows
                             if row.get("execution_fingerprint") == fingerprint), None)
            if existing is not None:
                return False
            rows.append({
                "execution_fingerprint": str(fingerprint), "status": status,
                "progress_url": progress_url, "created_at": now, "updated_at": now,
            })
            self._write(rows)
            return True

    def update(self, fingerprint, *, status, progress_url=None):
        if status not in self.STATUSES:
            raise ValueError("invalid execution guard status")
        with self._lock:
            rows = self._read()
            for row in rows:
                if row.get("execution_fingerprint") == fingerprint:
                    row["status"] = status
                    if progress_url is not None:
                        row["progress_url"] = progress_url
                    row["updated_at"] = time.time()
                    self._write(rows)
                    return True
        return False

    def remove(self, fingerprint):
        with self._lock:
            rows = self._read()
            kept = [row for row in rows
                    if row.get("execution_fingerprint") != fingerprint]
            if len(kept) == len(rows):
                return False
            self._write(kept)
            return True


class SimulationGateway:
    """Single public Simulation write path, independent of research state."""

    def __init__(self, client, *, state_dir=".wqb_state", max_concurrent=3,
                 poll_timeout_sec=1500, replace_attempts=3):
        self.client = client
        self.guard = ExecutionGuard(state_dir)
        self.simulator = Simulator(
            client, max_concurrent=max_concurrent, poll_timeout_sec=poll_timeout_sec,
            replace_attempts=replace_attempts,
        )

    @staticmethod
    def validate_simulation_spec(spec):
        spec = spec if isinstance(spec, SimulationSpec) else SimulationSpec(**dict(spec))
        analysis = analyze_expression(spec.expression)
        if not analysis.identifiers:
            raise ValueError("expression must contain an identifier")
        return {"valid": True, "expression": spec.expression,
                "settings": dict(spec.settings), "operators": list(analysis.operators)}

    def execution_fingerprint(self, spec):
        spec = spec if isinstance(spec, SimulationSpec) else SimulationSpec(**dict(spec))
        self.validate_simulation_spec(spec)
        return self.guard.fingerprint(spec.expression, spec.settings)

    def simulate(self, spec):
        spec = spec if isinstance(spec, SimulationSpec) else SimulationSpec(**dict(spec))
        fingerprint = self.execution_fingerprint(spec)
        existing = self.guard.find(fingerprint)
        if existing is not None:
            if existing.get("status") == "SUBMIT_UNKNOWN":
                return {"status": "SUBMIT_UNKNOWN", "fingerprint": fingerprint,
                        "progress_url": existing.get("progress_url")}
            return {"status": "EXACT_DUPLICATE", "fingerprint": fingerprint}
        self.guard.register(fingerprint)
        experiment = Experiment(
            1, "agent-authored", spec.expression, dict(spec.settings), list(spec.fields)
        )
        experiment.submission_fingerprint = fingerprint

        def on_update(item):
            status = "SUBMIT_UNKNOWN" if item.status == "UNKNOWN" else item.status
            if status in ExecutionGuard.STATUSES:
                self.guard.update(fingerprint, status=status,
                                  progress_url=item.progress_url)

        completed = self.simulator.run([experiment], on_update=on_update)
        item = completed[0]
        if item.status in {"DONE", "FAILED"}:
            self.guard.remove(fingerprint)
        return self._result(item, fingerprint)

    def resume_execution(self, fingerprint):
        row = self.guard.find(str(fingerprint))
        if row is None:
            raise KeyError("execution fingerprint not found")
        progress_url = row.get("progress_url")
        if not progress_url:
            return {"status": "SUBMIT_UNKNOWN", "fingerprint": str(fingerprint)}
        try:
            alpha_id = self.client.poll_progress(progress_url)
            payload = self.client.get_alpha(alpha_id)
        except Exception as exc:
            return {"status": row.get("status", "SUBMIT_UNKNOWN"),
                    "fingerprint": str(fingerprint), "error": str(exc),
                    "progress_url": progress_url}
        self.guard.remove(str(fingerprint))
        return {"status": "DONE", "fingerprint": str(fingerprint),
                "progress_url": progress_url, "alpha_id": alpha_id,
                "evidence": payload}

    @staticmethod
    def _result(item, fingerprint):
        return {
            "status": item.status, "fingerprint": fingerprint,
            "alpha_id": getattr(item, "alpha_id", None),
            "progress_url": getattr(item, "progress_url", None),
            "evidence": getattr(item, "metrics", None),
            "error": getattr(item, "error", None),
        }
