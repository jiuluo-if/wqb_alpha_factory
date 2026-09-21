"""Remote-first Simulation tools with a deliberately tiny write guard."""

from __future__ import annotations

import math
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
REGULAR_SIMULATION_TYPE = "REGULAR"
SUPPORTED_WRITE_SIMULATION_TYPES = frozenset({REGULAR_SIMULATION_TYPE})
MULTI_MIN_CHILDREN = 2
MULTI_MAX_CHILDREN = 10
MULTI_DEFAULT_CHILD_BATCH_SIZE = 10
MULTI_DEFAULT_CONCURRENCY = 2
MULTI_MAX_CONCURRENCY = 8


def _spec_simulation_type(spec):
    return str(getattr(spec, "simulation_type", REGULAR_SIMULATION_TYPE)
               or REGULAR_SIMULATION_TYPE).upper()


def _validate_write_simulation_type(spec):
    simulation_type = _spec_simulation_type(spec)
    if simulation_type not in SUPPORTED_WRITE_SIMULATION_TYPES:
        raise ValueError(
            "UNSUPPORTED_SIMULATION_TYPE: production writer supports REGULAR only"
        )


@dataclass(frozen=True)
class SimulationSpec:
    """The only research-authored input required to start a Simulation."""

    expression: str
    settings: Mapping[str, Any] = field(default_factory=dict)
    fields: tuple[str, ...] = field(default_factory=tuple)
    note: str | None = None
    template_id: str | None = None
    # OPTIONS may advertise types beyond the writer contract. REGULAR stays
    # the default so existing callers and historical fingerprints are stable.
    simulation_type: str = "REGULAR"

    def __post_init__(self):
        expression = str(self.expression or "").strip()
        if not expression:
            raise ValueError("expression must be non-empty")
        if not isinstance(self.settings, Mapping):
            raise TypeError("settings must be an object")
        if expression.count("(") != expression.count(")"):
            raise ValueError("expression has unbalanced parentheses")
        simulation_type = str(self.simulation_type or "REGULAR").strip().upper()
        if not simulation_type:
            raise ValueError("simulation_type must be non-empty")
        object.__setattr__(self, "expression", expression)
        object.__setattr__(self, "settings", dict(self.settings))
        object.__setattr__(self, "simulation_type", simulation_type)
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

    PARENT_STATUS_PATH_LIMIT = 8
    PARENT_DIAGNOSTIC_KEYS = frozenset({
        "remote_status", "message", "simulation_id", "property",
        "line", "start", "end",
    })

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
    def validate_simulation_settings(settings, *, client=None, capability=None):
        """Return the canonical bounded Simulation settings validation report."""
        errors = []
        if not isinstance(settings, Mapping):
            return {
                "valid": False, "status": "INVALID", "source": "LOCAL_SCHEMA",
                "evidence_status": "UNAVAILABLE", "settings": {},
                "errors": ["settings must be an object"],
            }
        normalized = dict(settings)
        for key in ("region", "universe", "instrumentType", "neutralization",
                    "pasteurization", "unitHandling", "nanHandling", "language"):
            if key in normalized and (
                not isinstance(normalized[key], str) or not normalized[key].strip()
            ):
                errors.append(f"{key} must be a non-empty string")
        if "delay" in normalized:
            value = normalized["delay"]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                errors.append("delay must be a non-negative integer")
        if "decay" in normalized:
            value = normalized["decay"]
            if (
                isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or value < 1
            ):
                errors.append("decay must be a finite number >= 1")
        if "truncation" in normalized:
            value = normalized["truncation"]
            if (
                isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value)
            ):
                errors.append("truncation must be a finite number")
        if "visualization" in normalized and not isinstance(normalized["visualization"], bool):
            errors.append("visualization must be a boolean")
        fields = normalized.get("fields")
        if fields is not None and (
            not isinstance(fields, (list, tuple))
            or not fields
            or any(
                not isinstance(item, (str, int)) or not str(item).strip()
                for item in fields
            )
        ):
            errors.append("fields must be a non-empty list when provided")
        if client is not None:
            for key, attr in (
                ("region", "region"),
                ("universe", "universe"),
                ("instrumentType", "instrument_type"),
            ):
                expected = getattr(client, attr, None)
                if (
                    key in normalized
                    and expected is not None
                    and str(normalized[key]) != str(expected)
                ):
                    errors.append(f"{key} does not match client scope")
        if capability is None and client is not None:
            reader = getattr(client, "get_simulation_capability", None)
            if callable(reader):
                try:
                    capability = reader()
                except Exception:
                    capability = {"status": "UNKNOWN"}
        capability_status = (
            str(
                capability.get("capability_status")
                or capability.get("status")
                or "UNKNOWN"
            ).upper()
            if isinstance(capability, Mapping) else "UNKNOWN"
        )
        validation_source = "LIVE_OPTIONS" if capability_status == "AVAILABLE" else "LOCAL_ONLY"
        if capability_status == "AVAILABLE" and isinstance(capability, Mapping):
            for key in capability.get("required_settings", ()):
                if key not in normalized:
                    errors.append(f"missing required setting: {key}")
            # OPTIONS is resolved for the client's own instrumentType/region.
            # Scope-dependent lists must not reject a request for another scope.
            scope_dependent_applies = True
            if client is not None:
                client_region = getattr(client, "region", None)
                client_type = getattr(client, "instrument_type", None)
                requested_region = normalized.get("region", client_region)
                requested_type = normalized.get("instrumentType", client_type)
                scope_dependent_applies = (
                    (client_region is None or str(requested_region) == str(client_region))
                    and (client_type is None or str(requested_type) == str(client_type))
                )
            for key, spec in (capability.get("settings") or {}).items():
                if key not in normalized or not isinstance(spec, Mapping):
                    continue
                if not scope_dependent_applies and key in (
                    "universe", "delay", "neutralization", "decay",
                    "truncation", "pasteurization", "unitHandling",
                    "nanHandling",
                ):
                    continue
                allowed = spec.get("allowed_values")
                if isinstance(allowed, list) and normalized[key] not in allowed:
                    errors.append(f"{key} is not allowed by live OPTIONS")
        return {
            "valid": not errors, "status": "VALID" if not errors else "INVALID",
            "source": validation_source, "validation_source": validation_source,
            "capability_status": capability_status,
            "evidence_status": "INCONCLUSIVE", "settings": normalized, "errors": errors,
        }

    @staticmethod
    def validate_simulation_spec(spec):
        spec = spec if isinstance(spec, SimulationSpec) else SimulationSpec(**dict(spec))
        _validate_write_simulation_type(spec)
        analysis = analyze_expression(spec.expression)
        if not analysis.identifiers:
            raise ValueError("expression must contain an identifier")
        return {"valid": True, "expression": spec.expression,
                "settings": dict(spec.settings), "operators": list(analysis.operators)}

    @classmethod
    def _parent_projection(
        cls, *, fingerprint, status, progress_url, child_count,
        exception_class=None, error=None, failure_kind=None, http_status=None,
        remote_status=None, diagnostic=None, status_path=(), guard_action=None,
        failure_scope=None,
    ):
        bounded_diagnostic = {}
        if isinstance(diagnostic, Mapping):
            for key in cls.PARENT_DIAGNOSTIC_KEYS:
                if key not in diagnostic or diagnostic[key] is None:
                    continue
                value = diagnostic[key]
                bounded_diagnostic[key] = (
                    str(value)[:500] if key == "message"
                    else str(value)[:200] if key in {"property", "simulation_id"}
                    else value if isinstance(value, (int, float))
                    and not isinstance(value, bool) else str(value)[:200]
                )
        path = [str(item) for item in (status_path or ()) if item]
        path = path[-cls.PARENT_STATUS_PATH_LIMIT:]
        if not path and status:
            path = [str(status)]
        return {
            "fingerprint": str(fingerprint),
            "status": str(status),
            "progress_url": str(progress_url) if progress_url else None,
            "child_count": int(child_count),
            "exception_class": str(exception_class) if exception_class else None,
            "error": str(error)[:500] if error else None,
            "failure_kind": str(failure_kind) if failure_kind else None,
            "http_status": (
                int(http_status)
                if isinstance(http_status, int) and not isinstance(http_status, bool)
                else None
            ),
            "remote_status": str(remote_status) if remote_status else None,
            "diagnostic": bounded_diagnostic,
            "status_path": path,
            "guard_action": guard_action,
            "failure_scope": failure_scope,
        }

    @classmethod
    def _parent_from_batch(cls, batch, *, guard_action, status=None, error=None):
        return cls._parent_projection(
            fingerprint=batch.submission_fingerprint,
            status=status or batch.status,
            progress_url=getattr(batch, "progress_url", None),
            child_count=len(getattr(batch, "children", ()) or ()),
            exception_class=getattr(batch, "exception_class", None),
            error=error if error is not None else getattr(batch, "error", None),
            failure_kind=getattr(batch, "failure_kind", None),
            http_status=getattr(batch, "http_status", None),
            remote_status=getattr(batch, "remote_status", None),
            diagnostic=getattr(batch, "diagnostic", None),
            status_path=getattr(batch, "status_path", ()),
            guard_action=guard_action,
            failure_scope=getattr(batch, "failure_scope", None),
        )

    def _validate_settings(self, spec, capability):
        report = self.validate_simulation_settings(
            spec.settings, client=self.client, capability=capability
        )
        if not report["valid"]:
            raise ValueError("invalid simulation settings: " + "; ".join(report["errors"]))

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

    def _preflight_specs(self, specs, *, simulation_capability=_CAPABILITY_UNCHECKED):
        normalized = [
            item if isinstance(item, SimulationSpec)
            else SimulationSpec(**dict(item))
            for item in (specs or ())
        ]
        results: list[dict[str, Any] | None] = [None] * len(normalized)
        if not normalized:
            return results, [], ()

        # Platform OPTIONS is capability truth, not proof that this writer
        # implements every advertised request schema.  Reject before any
        # ExecutionGuard registration or remote POST.
        for spec in normalized:
            _validate_write_simulation_type(spec)

        if simulation_capability is _CAPABILITY_UNCHECKED:
            capability_reader = getattr(self.client, "get_simulation_capability", None)
            simulation_capability = _CAPABILITY_READER_ABSENT
            if callable(capability_reader):
                try:
                    simulation_capability = capability_reader()
                except Exception:
                    simulation_capability = {"status": "UNKNOWN"}
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
            self._validate_settings(spec, simulation_capability)
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
                    simulation_type=spec.simulation_type,
                    status="PENDING", alpha_id=None, progress_url=None,
                    error=None, evidence=None, elapsed_sec=0.0,
                    submission_fingerprint=fingerprint,
                ),
            ))

        def on_update(item):
            status = (
                "RUNNING" if item.status == "UNKNOWN" and item.progress_url
                else "SUBMIT_UNKNOWN" if item.status == "UNKNOWN" else item.status
            )
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
        self, specs, *, child_batch_size=MULTI_DEFAULT_CHILD_BATCH_SIZE,
        max_concurrent_multi=MULTI_DEFAULT_CONCURRENCY
    ):
        with single_instance_scope(self.state_dir, operation="multi-simulation"):
            self.guard.reconcile()
            return self._simulate_multi_batch(
                specs, child_batch_size=child_batch_size,
                max_concurrent_multi=max_concurrent_multi,
            )

    def _simulate_multi_batch(
        self, specs, *, child_batch_size=MULTI_DEFAULT_CHILD_BATCH_SIZE,
        max_concurrent_multi=MULTI_DEFAULT_CONCURRENCY
    ):
        """Execute large probe windows as bounded Multi-Simulation parents."""
        normalized_specs = [
            item if isinstance(item, SimulationSpec)
            else SimulationSpec(**dict(item))
            for item in (specs or ())
        ]
        if isinstance(child_batch_size, bool) or not isinstance(child_batch_size, int):
            raise TypeError("child_batch_size must be an integer")
        if child_batch_size < MULTI_MIN_CHILDREN or child_batch_size > MULTI_MAX_CHILDREN:
            raise ValueError(
                f"child_batch_size must be between {MULTI_MIN_CHILDREN} "
                f"and {MULTI_MAX_CHILDREN}"
            )
        if isinstance(max_concurrent_multi, bool) or not isinstance(max_concurrent_multi, int):
            raise TypeError("max_concurrent_multi must be an integer")
        if max_concurrent_multi < 1 or max_concurrent_multi > MULTI_MAX_CONCURRENCY:
            raise ValueError(
                f"max_concurrent_multi must be between 1 and {MULTI_MAX_CONCURRENCY}"
            )
        if len(normalized_specs) == 1:
            # A one-child remainder is a valid Single request, never a
            # one-element Multi payload.  The Gateway still owns this split.
            return self._simulate_batch(normalized_specs)

        auth_reader = getattr(self.client, "get_authentication_status", None)
        if not callable(auth_reader):
            raise ValueError("PERMISSION_UNAVAILABLE: live authentication capability required")
        authentication = auth_reader()
        permissions = {
            str(item).upper() for item in (authentication or {}).get("permissions", ())
        }
        if not (authentication or {}).get("authenticated") or "MULTI_SIMULATION" not in permissions:
            raise ValueError("PERMISSION_UNAVAILABLE: MULTI_SIMULATION")
        capability_reader = getattr(self.client, "get_simulation_capability", None)
        capability = _CAPABILITY_READER_ABSENT
        if callable(capability_reader):
            capability = capability_reader()
            choices = {
                str(item).upper()
                for item in (capability or {}).get("simulation_type_choices", ())
            }
            if str((capability or {}).get("status", "")).upper() != "AVAILABLE" or (
                "REGULAR" not in choices
            ):
                raise ValueError("SIMULATION_CAPABILITY_UNAVAILABLE")

        results, validated, remote_rows = self._preflight_specs(
            normalized_specs, simulation_capability=capability
        )
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

        single_remainders = []
        for group in grouped:
            for start in range(0, len(group), child_batch_size):
                children = group[start:start + child_batch_size]
                if len(children) == 1:
                    single_remainders.extend(children)
                    continue
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
                    parent = self._parent_projection(
                        fingerprint=batch_fingerprint,
                        status=existing.get("status") or status,
                        progress_url=existing.get("progress_url"),
                        child_count=len(children),
                        error="existing execution guard matched",
                        status_path=[existing.get("status") or status],
                        guard_action="EXISTING_GUARD",
                    )
                    for index, _spec, fingerprint in children:
                        results[index] = {
                            "status": status,
                            "fingerprint": fingerprint,
                            "batch_fingerprint": batch_fingerprint,
                            "progress_url": existing.get("progress_url"),
                            "parent": parent,
                        }
                    continue
                if not self.guard.register(batch_fingerprint):
                    parent = self._parent_projection(
                        fingerprint=batch_fingerprint,
                        status="EXACT_DUPLICATE",
                        progress_url=None,
                        child_count=len(children),
                        error="existing execution guard matched",
                        status_path=["EXACT_DUPLICATE"],
                        guard_action="EXISTING_GUARD",
                    )
                    for index, _spec, fingerprint in children:
                        results[index] = {
                            "status": "EXACT_DUPLICATE",
                            "fingerprint": fingerprint,
                            "batch_fingerprint": batch_fingerprint,
                            "parent": parent,
                        }
                    continue
                child_records = [
                    SimpleNamespace(
                        index=index,
                        submission_fingerprint=fingerprint,
                        expression=spec.expression,
                        settings=dict(spec.settings),
                        simulation_type=spec.simulation_type,
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
                        status_path=["PENDING"],
                        progress_url=None,
                        error=None,
                        exception_class=None,
                        failure_kind=None,
                        http_status=None,
                        remote_status=None,
                        diagnostic={},
                        failure_scope=None,
                        elapsed_sec=0.0,
                    ),
                ))

        # Keep the same Gateway owner for a remainder of one, but use the
        # Single POST contract instead of inventing an invalid Multi payload.
        if single_remainders:
            single_results = self._simulate_batch([
                spec for _index, spec, _fingerprint in single_remainders
            ])
            for (index, _spec, _fingerprint), result in zip(
                single_remainders, single_results
            ):
                results[index] = result

        def on_update(batch):
            status = (
                "RUNNING" if batch.status == "UNKNOWN" and batch.progress_url
                else "SUBMIT_UNKNOWN" if batch.status == "UNKNOWN" else batch.status
            )
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
            guard_action = (
                "REMOVED_TERMINAL" if batch.status in {"DONE", "FAILED"}
                else "PRESERVED_RUNNING" if batch.progress_url
                else "PRESERVED_SUBMIT_UNKNOWN"
            )
            parent = self._parent_from_batch(batch, guard_action=guard_action)
            for child in batch.children:
                if batch.status == "DONE":
                    results[child.index] = self._result(
                        child, child.submission_fingerprint
                    )
                    results[child.index].update({
                        "batch_fingerprint": batch_fingerprint,
                        "parent": parent,
                    })
                elif batch.status == "FAILED":
                    results[child.index] = {
                        "status": "FAILED",
                        "fingerprint": child.submission_fingerprint,
                        "batch_fingerprint": batch_fingerprint,
                        "error": batch.error,
                        "failure_kind": getattr(child, "failure_kind", "FAILED_REMOTE"),
                        "remote_status": getattr(child, "remote_status", None),
                        "diagnostic": getattr(child, "diagnostic", None),
                        "failure_scope": getattr(child, "failure_scope", None)
                        or getattr(batch, "failure_scope", None),
                        "parent": parent,
                    }
                else:
                    results[child.index] = {
                        "status": "UNKNOWN" if batch.progress_url else "SUBMIT_UNKNOWN",
                        "fingerprint": child.submission_fingerprint,
                        "batch_fingerprint": batch_fingerprint,
                        "error": batch.error,
                        "failure_kind": getattr(child, "failure_kind", None),
                        "remote_status": getattr(child, "remote_status", None),
                        "diagnostic": getattr(child, "diagnostic", None),
                        "failure_scope": getattr(child, "failure_scope", None)
                        or getattr(batch, "failure_scope", None),
                        "parent": parent,
                    }
        for batch_fingerprint, batch in batches:
            if batch_fingerprint in completed_fingerprints:
                continue
            self.guard.remove(batch_fingerprint)
            parent = self._parent_projection(
                fingerprint=batch_fingerprint,
                status="NOT_DISPATCHED",
                progress_url=None,
                child_count=len(batch.children),
                error="dispatch paused before submission",
                status_path=[*(getattr(batch, "status_path", []) or []), "NOT_DISPATCHED"],
                guard_action="REMOVED_NOT_DISPATCHED",
            )
            for child in batch.children:
                results[child.index] = {
                    "status": "NOT_DISPATCHED",
                    "fingerprint": child.submission_fingerprint,
                    "batch_fingerprint": batch_fingerprint,
                    "error": "dispatch paused before submission",
                    "failure_scope": "PARENT",
                    "parent": parent,
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
            # The guard deliberately stores only the durable remote identity
            # (fingerprint/status/progress URL).  Therefore recovery must
            # identify a Multi parent from its known progress response rather
            # than persisting research metadata in the guard.  BRAIN returns
            # ``children`` for a Multi parent; treating that response as a
            # Single result raises "finished without alpha id" and strands a
            # valid remote job in SUBMIT_UNKNOWN.
            poller = self.client.poll_progress
            is_multi = False
            remote_terminal_error = False
            snapshot_reader = getattr(self.client, "get_progress_snapshot", None)
            if snapshot_reader is not None:
                snapshot = snapshot_reader(progress_url, timeout=60)
                payload = snapshot.get("payload") if isinstance(snapshot, Mapping) else None
                if isinstance(payload, Mapping):
                    remote_status = str(payload.get("status", "")).upper()
                    remote_terminal_error = remote_status in {"ERROR", "FAILED"}
                    if isinstance(payload.get("children"), list):
                        poller = self.client.poll_multi_progress
                        is_multi = True
            alpha_ids = poller(progress_url)
            if is_multi:
                if not isinstance(alpha_ids, (list, tuple)) or not alpha_ids:
                    raise ValueError("Multi-Simulation recovery returned no child alpha ids")
                payload = [self.client.get_alpha(alpha_id) for alpha_id in alpha_ids]
                result = {"status": "DONE", "fingerprint": str(fingerprint),
                          "progress_url": progress_url, "alpha_ids": list(alpha_ids),
                          "evidence": payload}
            else:
                payload = self.client.get_alpha(alpha_ids)
                result = {"status": "DONE", "fingerprint": str(fingerprint),
                          "progress_url": progress_url, "alpha_id": alpha_ids,
                          "evidence": payload}
        except Exception as exc:
            if remote_terminal_error:
                self.guard.remove(str(fingerprint))
                return {"status": "FAILED", "fingerprint": str(fingerprint),
                        "error": str(exc), "progress_url": progress_url}
            return {"status": row.get("status", "SUBMIT_UNKNOWN"),
                    "fingerprint": str(fingerprint), "error": str(exc),
                    "progress_url": progress_url}
        self.guard.remove(str(fingerprint))
        return result

    @staticmethod
    def _result(item, fingerprint):
        return {
            "status": item.status, "fingerprint": fingerprint,
            "alpha_id": getattr(item, "alpha_id", None),
            "progress_url": getattr(item, "progress_url", None),
            "evidence": getattr(item, "evidence", None),
            "error": getattr(item, "error", None),
            "failure_kind": getattr(item, "failure_kind", None),
            "remote_status": getattr(item, "remote_status", None),
            "diagnostic": getattr(item, "diagnostic", None),
        }
