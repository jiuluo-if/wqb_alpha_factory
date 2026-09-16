"""Remote-first Simulation tools with a deliberately tiny write guard."""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from .artifacts import atomic_write_json_if_changed
from .expression import analyze_expression, submission_fingerprint
from .simulator import Simulator


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
        # A process dying after the durable pre-POST marker cannot establish
        # whether the remote POST happened.  Promote it before any new call.
        with self._lock:
            rows = self._read()
            changed = False
            for row in rows:
                if row.get("status") == "SUBMITTING":
                    row["status"] = "SUBMIT_UNKNOWN"
                    row["updated_at"] = time.time()
                    changed = True
            if changed:
                self._write(rows)

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
        normalized = []
        for row in rows:
            if not isinstance(row, dict) or not row.get("execution_fingerprint"):
                continue
            if row.get("status") not in self.STATUSES:
                continue
            item = {
                "execution_fingerprint": str(row["execution_fingerprint"]),
                "status": row["status"],
                "created_at": row.get("created_at"),
                "updated_at": row.get("updated_at"),
            }
            if row.get("progress_url") is not None:
                item["progress_url"] = str(row["progress_url"])
            if row.get("remote_alpha_id") is not None:
                item["remote_alpha_id"] = str(row["remote_alpha_id"])
            normalized.append(item)
        return normalized

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

    def _validate_live_capability(self, spec):
        reader = getattr(self.client, "get_operator_capability", None)
        if callable(reader):
            capability = reader()
            if not isinstance(capability, Mapping) or capability.get("valid") is not True:
                raise ValueError("OPERATOR_CAPABILITY_UNAVAILABLE")
            operators = {str(item).casefold() for item in capability.get("operators", ())}
            requested = set(analyze_expression(spec.expression).operators)
            missing = sorted(requested - operators)
            if missing:
                raise ValueError("OPERATOR_CAPABILITY_UNAVAILABLE: " + ", ".join(missing))
        field_reader = getattr(self.client, "get_field_capability", None)
        if spec.fields and callable(field_reader):
            field_capability = field_reader(list(spec.fields))
            if not isinstance(field_capability, Mapping) or field_capability.get("valid") is not True:
                raise ValueError("FIELD_CAPABILITY_UNAVAILABLE")
            available = {str(item).casefold() for item in field_capability.get("fields", ())}
            missing_fields = sorted({str(item).casefold() for item in spec.fields} - available)
            if missing_fields:
                raise ValueError("FIELD_CAPABILITY_UNAVAILABLE: " + ", ".join(missing_fields))

    def _remote_duplicate(self, spec, fingerprint):
        reader = getattr(self.client, "get_all_user_alphas", None)
        if not callable(reader):
            return None
        try:
            rows = reader(max_results=1000)
        except Exception:
            # Remote duplicate lookup is advisory; transport failure must not
            # be mistaken for proof that a write is safe or unsafe.
            return None
        for row in rows or ():
            if not isinstance(row, Mapping):
                continue
            alpha = row.get("alpha") if isinstance(row.get("alpha"), Mapping) else row
            expression = alpha.get("regular") or alpha.get("expression")
            settings = alpha.get("settings") if isinstance(alpha.get("settings"), Mapping) else {}
            if isinstance(expression, str) and self.guard.fingerprint(expression, settings) == fingerprint:
                return {"alpha_id": row.get("id") or alpha.get("id"), "source": "LIVE"}
        return None

    def execution_fingerprint(self, spec):
        spec = spec if isinstance(spec, SimulationSpec) else SimulationSpec(**dict(spec))
        self.validate_simulation_spec(spec)
        return self.guard.fingerprint(spec.expression, spec.settings)

    def simulate(self, spec):
        spec = spec if isinstance(spec, SimulationSpec) else SimulationSpec(**dict(spec))
        fingerprint = self.execution_fingerprint(spec)
        self._validate_live_capability(spec)
        existing = self.guard.find(fingerprint)
        if existing is not None:
            if existing.get("status") == "SUBMIT_UNKNOWN":
                return {"status": "SUBMIT_UNKNOWN", "fingerprint": fingerprint,
                        "progress_url": existing.get("progress_url")}
            return {"status": "EXACT_DUPLICATE", "fingerprint": fingerprint}
        remote = self._remote_duplicate(spec, fingerprint)
        if remote is not None:
            return {"status": "EXACT_DUPLICATE", "fingerprint": fingerprint, **remote}
        self.guard.register(fingerprint)
        # Simulator only needs a mutable transport record.  Keeping this
        # record local is only a transport record; BRAIN owns the result.
        experiment = SimpleNamespace(
            id=fingerprint[:16], expression=spec.expression,
            settings=dict(spec.settings),
            status="PENDING", alpha_id=None, progress_url=None,
            error=None, evidence=None, elapsed_sec=0.0,
            submission_fingerprint=fingerprint,
        )

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
            "evidence": getattr(item, "evidence", None),
            "error": getattr(item, "error", None),
        }
