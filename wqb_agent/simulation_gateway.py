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
from .locking import single_instance_scope
from .simulator import Simulator

_CAPABILITY_UNCHECKED = object()
_CAPABILITY_READER_ABSENT = object()
_REMOTE_ROWS_UNCHECKED = object()


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

    def __init__(self, state_dir, *, reconcile=True):
        self.state_dir = os.path.abspath(str(state_dir))
        self.path = os.path.join(self.state_dir, "execution_guard.json")
        self._lock = threading.RLock()
        if reconcile:
            self.reconcile()

    def reconcile(self):
        """Promote interrupted writes only while the state owner is held."""
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
            self.path, {"schema_version": 1, "entries": rows}, sort_keys=True,
            private=True,
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

    def __init__(self, client, *, state_dir=".wqb_state", max_concurrent=10,
                 poll_timeout_sec=1500, repoll_attempts=3):
        self.client = client
        self.state_dir = os.path.abspath(str(state_dir))
        # Construction must not reconcile a live SUBMITTING marker before the
        # process owns the whole register -> POST -> outcome transaction.
        self.guard = ExecutionGuard(self.state_dir, reconcile=False)
        self.simulator = Simulator(
            client, max_concurrent=max_concurrent, poll_timeout_sec=poll_timeout_sec,
            repoll_attempts=repoll_attempts,
        )

    @staticmethod
    def validate_simulation_spec(spec):
        spec = spec if isinstance(spec, SimulationSpec) else SimulationSpec(**dict(spec))
        analysis = analyze_expression(spec.expression)
        if not analysis.identifiers:
            raise ValueError("expression must contain an identifier")
        return {"valid": True, "expression": spec.expression,
                "settings": dict(spec.settings), "operators": list(analysis.operators)}

    def _validate_live_capability(
        self, spec, *, operator_capability=_CAPABILITY_UNCHECKED,
        field_capability=_CAPABILITY_UNCHECKED,
    ):
        if operator_capability is _CAPABILITY_UNCHECKED:
            reader = getattr(self.client, "get_operator_capability", None)
            operator_capability = (
                reader() if callable(reader) else _CAPABILITY_READER_ABSENT
            )
        if operator_capability is not _CAPABILITY_READER_ABSENT:
            capability = operator_capability
            if not isinstance(capability, Mapping) or capability.get("valid") is not True:
                raise ValueError("OPERATOR_CAPABILITY_UNAVAILABLE")
            operators = {str(item).casefold() for item in capability.get("operators", ())}
            requested = set(analyze_expression(spec.expression).operators)
            missing = sorted(requested - operators)
            if missing:
                raise ValueError("OPERATOR_CAPABILITY_UNAVAILABLE: " + ", ".join(missing))
        if field_capability is _CAPABILITY_UNCHECKED:
            field_reader = getattr(self.client, "get_field_capability", None)
            field_capability = (
                field_reader(list(spec.fields))
                if spec.fields and callable(field_reader)
                else _CAPABILITY_READER_ABSENT
            )
        if spec.fields and field_capability is not _CAPABILITY_READER_ABSENT:
            if not isinstance(field_capability, Mapping) or field_capability.get("valid") is not True:
                raise ValueError("FIELD_CAPABILITY_UNAVAILABLE")
            available = {str(item).casefold() for item in field_capability.get("fields", ())}
            missing_fields = sorted({str(item).casefold() for item in spec.fields} - available)
            if missing_fields:
                raise ValueError("FIELD_CAPABILITY_UNAVAILABLE: " + ", ".join(missing_fields))

    def _remote_duplicate(
        self, spec, fingerprint, rows=_REMOTE_ROWS_UNCHECKED
    ):
        if rows is _REMOTE_ROWS_UNCHECKED:
            reader = getattr(self.client, "get_all_user_alphas", None)
            if not callable(reader):
                return None
            try:
                rows = reader(max_results=1000)
            except Exception:
                # Remote duplicate lookup is advisory; transport failure must
                # not be mistaken for proof that a write is safe or unsafe.
                return None
        for row in rows or ():
            if not isinstance(row, Mapping):
                continue
            alpha_payload = row.get("alpha")
            alpha: Mapping[str, Any] = (
                alpha_payload if isinstance(alpha_payload, Mapping) else row
            )
            expression = alpha.get("regular") or alpha.get("expression")
            settings = alpha.get("settings") if isinstance(alpha.get("settings"), Mapping) else {}
            if isinstance(expression, str) and self.guard.fingerprint(expression, settings) == fingerprint:
                return {"alpha_id": row.get("id") or alpha.get("id"), "source": "LIVE"}
        return None

    def execution_fingerprint(self, spec):
        spec = spec if isinstance(spec, SimulationSpec) else SimulationSpec(**dict(spec))
        self.validate_simulation_spec(spec)
        return self.guard.fingerprint(spec.expression, spec.settings)

    def _preflight_specs(self, specs):
        normalized = [
            item if isinstance(item, SimulationSpec)
            else SimulationSpec(**dict(item))
            for item in (specs or ())
        ]
        results: list[dict[str, Any] | None] = [None] * len(normalized)
        if not normalized:
            return results, [], ()

        operator_reader = getattr(self.client, "get_operator_capability", None)
        operator_capability = (
            operator_reader() if callable(operator_reader)
            else _CAPABILITY_READER_ABSENT
        )
        field_reader = getattr(self.client, "get_field_capability", None)
        field_capabilities: dict[tuple[str, ...], Any] = {}
        remote_reader = getattr(self.client, "get_all_user_alphas", None)
        remote_rows = _REMOTE_ROWS_UNCHECKED
        if callable(remote_reader):
            try:
                remote_rows = remote_reader(max_results=1000)
            except Exception:
                # Remote history is advisory; continue without a duplicate
                # claim when this read is unavailable.
                remote_rows = None

        validated = []
        for index, spec in enumerate(normalized):
            fingerprint = self.execution_fingerprint(spec)
            field_capability = _CAPABILITY_READER_ABSENT
            if spec.fields and callable(field_reader):
                field_key = tuple(spec.fields)
                if field_key not in field_capabilities:
                    field_capabilities[field_key] = field_reader(list(field_key))
                field_capability = field_capabilities[field_key]
            self._validate_live_capability(
                spec,
                operator_capability=operator_capability,
                field_capability=field_capability,
            )
            validated.append((index, spec, fingerprint))
        return results, validated, remote_rows

    def simulate(self, spec):
        return self.simulate_batch([spec])[0]

    def simulate_batch(self, specs):
        with single_instance_scope(self.state_dir, operation="simulation"):
            self.guard.reconcile()
            return self._simulate_batch(specs)

    def _simulate_batch(self, specs):
        """Execute a batch through one bounded Simulator window.

        The batch remains one guarded execution per spec; only dispatch and
        polling share the existing bounded worker pool.
        """
        results, validated, remote_rows = self._preflight_specs(specs)
        prepared = []
        seen = set()
        for index, spec, fingerprint in validated:
            if fingerprint in seen:
                results[index] = {
                    "status": "EXACT_DUPLICATE", "fingerprint": fingerprint,
                }
                continue
            seen.add(fingerprint)
            existing = self.guard.find(fingerprint)
            if existing is not None:
                if existing.get("status") == "SUBMIT_UNKNOWN":
                    results[index] = {
                        "status": "SUBMIT_UNKNOWN",
                        "fingerprint": fingerprint,
                        "progress_url": existing.get("progress_url"),
                    }
                else:
                    results[index] = {
                        "status": "EXACT_DUPLICATE", "fingerprint": fingerprint,
                    }
                continue
            remote = self._remote_duplicate(spec, fingerprint, rows=remote_rows)
            if remote is not None:
                results[index] = {
                    "status": "EXACT_DUPLICATE",
                    "fingerprint": fingerprint,
                    **remote,
                }
                continue
            if not self.guard.register(fingerprint):
                results[index] = {
                    "status": "EXACT_DUPLICATE", "fingerprint": fingerprint,
                }
                continue
            # Simulator only needs mutable transport records.  BRAIN owns the
            # remote result; these records do not become research state.
            prepared.append((
                index,
                fingerprint,
                SimpleNamespace(
                    id=fingerprint[:16], expression=spec.expression,
                    settings=dict(spec.settings),
                    status="PENDING", alpha_id=None, progress_url=None,
                    error=None, evidence=None, elapsed_sec=0.0,
                    submission_fingerprint=fingerprint,
                ),
            ))

        def on_update(item):
            status = "SUBMIT_UNKNOWN" if item.status == "UNKNOWN" else item.status
            if status in ExecutionGuard.STATUSES:
                self.guard.update(
                    item.submission_fingerprint,
                    status=status,
                    progress_url=item.progress_url,
                )

        completed = self.simulator.run(
            [item for _index, _fingerprint, item in prepared],
            on_update=on_update,
        )
        index_by_fingerprint = {
            fingerprint: index for index, fingerprint, _item in prepared
        }
        completed_fingerprints = set()
        for item in completed:
            fingerprint = item.submission_fingerprint
            completed_fingerprints.add(fingerprint)
            if item.status in {"DONE", "FAILED"}:
                self.guard.remove(fingerprint)
            results[index_by_fingerprint[fingerprint]] = self._result(
                item, fingerprint
            )
        for index, fingerprint, _item in prepared:
            if fingerprint in completed_fingerprints:
                continue
            # Simulator may stop dispatch after an ambiguous/auth failure.
            # These records were never submitted and must not become unknown
            # remote jobs on the next process restart.
            self.guard.remove(fingerprint)
            results[index] = {
                "status": "NOT_DISPATCHED",
                "fingerprint": fingerprint,
                "error": "dispatch paused before submission",
            }
        return results

    def simulate_multi_batch(
        self, specs, *, child_batch_size=10, max_concurrent_multi=8
    ):
        with single_instance_scope(self.state_dir, operation="multi-simulation"):
            self.guard.reconcile()
            return self._simulate_multi_batch(
                specs, child_batch_size=child_batch_size,
                max_concurrent_multi=max_concurrent_multi,
            )

    def _simulate_multi_batch(
        self, specs, *, child_batch_size=10, max_concurrent_multi=8
    ):
        """Execute large probe windows as bounded Multi-Simulation parents."""
        if isinstance(child_batch_size, bool) or not isinstance(child_batch_size, int):
            raise TypeError("child_batch_size must be an integer")
        if child_batch_size < 1 or child_batch_size > 10:
            raise ValueError("child_batch_size must be between 1 and 10")
        if isinstance(max_concurrent_multi, bool) or not isinstance(max_concurrent_multi, int):
            raise TypeError("max_concurrent_multi must be an integer")
        if max_concurrent_multi < 1 or max_concurrent_multi > 8:
            raise ValueError("max_concurrent_multi must be between 1 and 8")

        results, validated, remote_rows = self._preflight_specs(specs)
        eligible = []
        seen = set()
        for index, spec, fingerprint in validated:
            if fingerprint in seen:
                results[index] = {
                    "status": "EXACT_DUPLICATE", "fingerprint": fingerprint,
                }
                continue
            seen.add(fingerprint)
            remote = self._remote_duplicate(spec, fingerprint, rows=remote_rows)
            if remote is not None:
                results[index] = {
                    "status": "EXACT_DUPLICATE",
                    "fingerprint": fingerprint,
                    **remote,
                }
                continue
            eligible.append((index, spec, fingerprint))

        batches = []
        grouped: list[list[tuple[int, SimulationSpec, str]]] = []
        current_key = None
        for row in eligible:
            _index, spec, _fingerprint = row
            key = (
                spec.settings.get("region", getattr(self.client, "region", None)),
                spec.settings.get("delay", getattr(self.client, "delay", None)),
            )
            if not grouped or key != current_key:
                grouped.append([])
                current_key = key
            grouped[-1].append(row)

        for group in grouped:
            for start in range(0, len(group), child_batch_size):
                children = group[start:start + child_batch_size]
                child_fingerprints = [row[2] for row in children]
                batch_fingerprint = self.guard.fingerprint(
                    "MULTI[" + ",".join(child_fingerprints) + "]",
                    {"mode": "MULTI", "children": len(children)},
                )
                existing = self.guard.find(batch_fingerprint)
                if existing is not None:
                    status = (
                        "SUBMIT_UNKNOWN"
                        if existing.get("status") == "SUBMIT_UNKNOWN"
                        else "EXACT_DUPLICATE"
                    )
                    for index, _spec, fingerprint in children:
                        results[index] = {
                            "status": status,
                            "fingerprint": fingerprint,
                            "batch_fingerprint": batch_fingerprint,
                            "progress_url": existing.get("progress_url"),
                        }
                    continue
                if not self.guard.register(batch_fingerprint):
                    for index, _spec, fingerprint in children:
                        results[index] = {
                            "status": "EXACT_DUPLICATE",
                            "fingerprint": fingerprint,
                            "batch_fingerprint": batch_fingerprint,
                        }
                    continue
                child_records = [
                    SimpleNamespace(
                        index=index,
                        submission_fingerprint=fingerprint,
                        expression=spec.expression,
                        settings=dict(spec.settings),
                        status="PENDING",
                        alpha_id=None,
                        progress_url=None,
                        error=None,
                        evidence=None,
                    )
                    for index, spec, fingerprint in children
                ]
                batches.append((
                    batch_fingerprint,
                    SimpleNamespace(
                        id=batch_fingerprint[:16],
                        submission_fingerprint=batch_fingerprint,
                        children=child_records,
                        status="PENDING",
                        progress_url=None,
                        error=None,
                        elapsed_sec=0.0,
                    ),
                ))

        def on_update(batch):
            status = "SUBMIT_UNKNOWN" if batch.status == "UNKNOWN" else batch.status
            if status in ExecutionGuard.STATUSES:
                self.guard.update(
                    batch.submission_fingerprint,
                    status=status,
                    progress_url=batch.progress_url,
                )

        completed = self.simulator.run_multi(
            [batch for _fingerprint, batch in batches],
            on_update=on_update,
            max_concurrent=max_concurrent_multi,
        )
        completed_fingerprints = {
            batch.submission_fingerprint for batch in completed
        }
        for batch in completed:
            batch_fingerprint = batch.submission_fingerprint
            if batch.status in {"DONE", "FAILED"}:
                self.guard.remove(batch_fingerprint)
            for child in batch.children:
                if batch.status == "DONE":
                    results[child.index] = self._result(
                        child, child.submission_fingerprint
                    )
                elif batch.status == "FAILED":
                    results[child.index] = {
                        "status": "FAILED",
                        "fingerprint": child.submission_fingerprint,
                        "batch_fingerprint": batch_fingerprint,
                        "error": batch.error,
                    }
                else:
                    results[child.index] = {
                        "status": "SUBMIT_UNKNOWN",
                        "fingerprint": child.submission_fingerprint,
                        "batch_fingerprint": batch_fingerprint,
                        "error": batch.error,
                    }
        for batch_fingerprint, batch in batches:
            if batch_fingerprint in completed_fingerprints:
                continue
            self.guard.remove(batch_fingerprint)
            for child in batch.children:
                results[child.index] = {
                    "status": "NOT_DISPATCHED",
                    "fingerprint": child.submission_fingerprint,
                    "batch_fingerprint": batch_fingerprint,
                    "error": "dispatch paused before submission",
                }
        return results

    def resume_execution(self, fingerprint):
        with single_instance_scope(self.state_dir, operation="resume-simulation"):
            self.guard.reconcile()
            return self._resume_execution(fingerprint)

    def _resume_execution(self, fingerprint):
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
