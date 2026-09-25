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
from .client import (
    REGULAR_SIMULATION_TYPE,
    SUPPORTED_SIMULATION_REQUEST_TYPES,
)
from .expression import (
    analyze_expression,
    expression_field_identifiers,
    submission_fingerprint,
)
from .failures import ResearchReasonError, reason_code_for_failure
from .locking import single_instance_scope
from .simulator import Simulator

_CAPABILITY_UNCHECKED = object()
_CAPABILITY_READER_ABSENT = object()
_REMOTE_ROWS_UNCHECKED = object()
# The write-type whitelist is owned by the client that builds the request body;
# the Gateway only refuses a spec before it can reach that POST.
SUPPORTED_WRITE_SIMULATION_TYPES = SUPPORTED_SIMULATION_REQUEST_TYPES
MULTI_MIN_CHILDREN = 2
MULTI_MAX_CHILDREN = 10
MULTI_DEFAULT_CHILD_BATCH_SIZE = 10
MULTI_DEFAULT_CONCURRENCY = 2
MULTI_MAX_CONCURRENCY = 8

# The preflight exact-duplicate scan reads the remote Alpha library.  BRAIN
# answers one `/users/self/alphas` page in ~6 s and serialises concurrent pages,
# so walking the complete history of a large library needs tens of minutes and
# can never finish inside a usable budget — it used to abort every POST before
# dispatch.  The scan is therefore bounded to the most recent day and reports
# itself as a window rather than as complete history.  One day of this account's
# library holds ~700-900 alphas, which fits the platform's 1000-row query window
# in a single shard; a two-day span already exceeds that window and forces
# bisection, whose extra pages cost more time than the budget allows.
REMOTE_DUPLICATE_LOOKBACK_DAYS = 1
REMOTE_DUPLICATE_SCAN_BUDGET_SEC = 900
REMOTE_DUPLICATE_SCAN_KEY = "__remote_duplicate_scan__"


def _spec_simulation_type(spec):
    return str(getattr(spec, "simulation_type", REGULAR_SIMULATION_TYPE)
               or REGULAR_SIMULATION_TYPE).upper()


def _validate_write_simulation_type(spec):
    simulation_type = _spec_simulation_type(spec)
    if simulation_type not in SUPPORTED_WRITE_SIMULATION_TYPES:
        raise ValueError(
            "UNSUPPORTED_SIMULATION_TYPE: production writer supports "
            + ", ".join(sorted(SUPPORTED_WRITE_SIMULATION_TYPES))
            + f" only (got {simulation_type})"
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
    field_datasets: Mapping[str, str] = field(default_factory=dict)
    proposal_id: str | None = None

    def __post_init__(self):
        expression = str(self.expression or "").strip()
        if not expression:
            raise ValueError("expression must be non-empty")
        if not isinstance(self.settings, Mapping):
            raise TypeError("settings must be an object")
        if not isinstance(self.field_datasets, Mapping):
            raise TypeError("field_datasets must be an object")
        if expression.count("(") != expression.count(")"):
            raise ValueError("expression has unbalanced parentheses")
        simulation_type = str(self.simulation_type or "REGULAR").strip().upper()
        if not simulation_type:
            raise ValueError("simulation_type must be non-empty")
        object.__setattr__(self, "expression", expression)
        object.__setattr__(self, "settings", dict(self.settings))
        object.__setattr__(self, "simulation_type", simulation_type)
        raw_fields = (self.fields,) if isinstance(self.fields, str) else (self.fields or ())
        fields = []
        field_datasets = {
            str(field_id).strip(): str(dataset_id).strip()
            for field_id, dataset_id in self.field_datasets.items()
            if str(field_id).strip() and str(dataset_id).strip()
        }
        for item in raw_fields:
            if isinstance(item, Mapping):
                field_id = item.get("id") or item.get("field_id")
                dataset = item.get("dataset_id") or item.get("dataset")
                if isinstance(dataset, Mapping):
                    dataset = dataset.get("id") or dataset.get("name")
                if field_id is None:
                    raise ResearchReasonError(
                        "field profile requires an id", "CAPABILITY_UNAVAILABLE"
                    )
                normalized_id = str(field_id).strip()
                if dataset is not None and str(dataset).strip():
                    field_datasets[normalized_id] = str(dataset).strip()
            else:
                normalized_id = str(item).strip()
            if normalized_id and normalized_id not in fields:
                fields.append(normalized_id)
        if set(field_datasets) - set(fields):
            raise ResearchReasonError(
                "field_datasets contains an undeclared field", "CAPABILITY_UNAVAILABLE"
            )
        proposal_id = self.proposal_id
        if proposal_id is not None:
            proposal_id = str(proposal_id).strip()
            if not proposal_id or len(proposal_id) > 128:
                raise ValueError("proposal_id must contain 1 to 128 characters")
        object.__setattr__(self, "fields", tuple(fields))
        object.__setattr__(self, "field_datasets", field_datasets)
        object.__setattr__(self, "proposal_id", proposal_id)


class ExecutionGuard:
    """Persist only unresolved remote-write identities."""

    STATUSES = frozenset({"SUBMITTING", "RUNNING", "SUBMIT_UNKNOWN"})
    # ``kind`` is execution-safety metadata, not research state: it records
    # whether an unresolved remote write was one Single POST, a Multi parent
    # POST, or one child carried by such a parent.
    SINGLE = "SINGLE"
    MULTI_PARENT = "MULTI_PARENT"
    MULTI_CHILD = "MULTI_CHILD"
    KINDS = frozenset({SINGLE, MULTI_PARENT, MULTI_CHILD})

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
    def fingerprint(expression, settings, *, simulation_type=REGULAR_SIMULATION_TYPE):
        return submission_fingerprint(
            expression, settings, simulation_type=simulation_type
        )

    @staticmethod
    def _normalize_simulation_count(value):
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 1 <= value <= MULTI_MAX_CHILDREN
        ):
            return 1
        return value

    @classmethod
    def _normalize_kind(cls, value):
        """Unknown or legacy rows stay a plain Single write."""
        kind = str(value or cls.SINGLE).strip().upper()
        return kind if kind in cls.KINDS else cls.SINGLE

    @classmethod
    def _validate_simulation_count(cls, value):
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError("simulation_count must be an integer")
        if not 1 <= value <= MULTI_MAX_CHILDREN:
            raise ValueError(
                f"simulation_count must be between 1 and {MULTI_MAX_CHILDREN}"
            )
        return value

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
                "simulation_count": self._normalize_simulation_count(
                    row.get("simulation_count", 1)
                ),
                "kind": self._normalize_kind(row.get("kind")),
            }
            if row.get("progress_url") is not None:
                item["progress_url"] = str(row["progress_url"])
            if row.get("remote_alpha_id") is not None:
                item["remote_alpha_id"] = str(row["remote_alpha_id"])
            if row.get("parent_fingerprint") is not None:
                item["parent_fingerprint"] = str(row["parent_fingerprint"])
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

    def register(
        self, fingerprint, *, progress_url=None, status="SUBMITTING",
        simulation_count=1, kind=SINGLE, parent_fingerprint=None,
    ):
        if status not in self.STATUSES:
            raise ValueError("invalid execution guard status")
        simulation_count = self._validate_simulation_count(simulation_count)
        kind = self._normalize_kind(kind)
        if kind == self.MULTI_CHILD and not parent_fingerprint:
            raise ValueError("MULTI_CHILD requires a parent fingerprint")
        now = time.time()
        with self._lock:
            rows = self._read()
            existing = next((row for row in rows
                             if row.get("execution_fingerprint") == fingerprint), None)
            if existing is not None:
                return False
            row = {
                "execution_fingerprint": str(fingerprint), "status": status,
                "progress_url": progress_url, "created_at": now, "updated_at": now,
                "simulation_count": simulation_count, "kind": kind,
            }
            if parent_fingerprint:
                row["parent_fingerprint"] = str(parent_fingerprint)
            rows.append(row)
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

    def remove_batch(self, fingerprint):
        """Resolve one unresolved write together with its Multi children."""
        with self._lock:
            rows = self._read()
            kept = [
                row for row in rows
                if row.get("execution_fingerprint") != fingerprint
                and row.get("parent_fingerprint") != fingerprint
            ]
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
        self._validate_operator_capability(spec, operator_capability)
        if field_capability is _CAPABILITY_UNCHECKED:
            field_capability = _CAPABILITY_READER_ABSENT
        if spec.fields and field_capability is _CAPABILITY_READER_ABSENT:
            raise ResearchReasonError(
                "FIELD_CAPABILITY_UNAVAILABLE: no live field reader",
                "CAPABILITY_UNAVAILABLE",
            )
        if spec.fields:
            if not isinstance(field_capability, Mapping) or field_capability.get("valid") is not True:
                raise ResearchReasonError(
                    "FIELD_CAPABILITY_UNAVAILABLE", "CAPABILITY_UNAVAILABLE"
                )
            if field_capability.get("source") != "BRAIN_LIVE_ONLY":
                raise ResearchReasonError(
                    "FIELD_CAPABILITY_UNAVAILABLE: non-live source",
                    "CAPABILITY_UNAVAILABLE",
                )
            available = {str(item).casefold() for item in field_capability.get("fields", ())}
            missing_fields = sorted({str(item).casefold() for item in spec.fields} - available)
            if missing_fields:
                raise ResearchReasonError(
                    "FIELD_CAPABILITY_UNAVAILABLE: " + ", ".join(missing_fields),
                    "CAPABILITY_UNAVAILABLE",
                )

    def _validate_operator_capability(self, spec, operator_capability):
        if operator_capability is _CAPABILITY_UNCHECKED:
            reader = getattr(self.client, "get_operator_capability", None)
            operator_capability = (
                reader() if callable(reader) else _CAPABILITY_READER_ABSENT
            )
        if operator_capability is not _CAPABILITY_READER_ABSENT:
            capability = operator_capability
            if not isinstance(capability, Mapping) or capability.get("valid") is not True:
                raise ResearchReasonError(
                    "OPERATOR_CAPABILITY_UNAVAILABLE", "CAPABILITY_UNAVAILABLE"
                )
            operators = {str(item).casefold() for item in capability.get("operators", ())}
            requested = set(analyze_expression(spec.expression).operators)
            missing = sorted(requested - operators)
            if missing:
                raise ResearchReasonError(
                    "OPERATOR_CAPABILITY_UNAVAILABLE: " + ", ".join(missing),
                    "CAPABILITY_UNAVAILABLE",
                )

    @staticmethod
    def _recent_history_rows(shard_reader):
        """Walk a bounded recent window of the remote Alpha library.

        The scan is limited to the most recent ``REMOTE_DUPLICATE_LOOKBACK_DAYS``
        days.  A reader that predates the window argument keeps its own contract
        instead of failing the whole preflight.
        """
        try:
            return shard_reader(
                lookback_days=REMOTE_DUPLICATE_LOOKBACK_DAYS,
                time_budget_sec=REMOTE_DUPLICATE_SCAN_BUDGET_SEC,
            )
        except TypeError:
            return shard_reader()

    @staticmethod
    def _attach_scan_evidence(results, remote_duplicates):
        """Publish the duplicate-scan window next to every batch result."""
        scan = (remote_duplicates or {}).get(REMOTE_DUPLICATE_SCAN_KEY)
        if not isinstance(scan, Mapping):
            return results
        for result in results:
            if isinstance(result, dict):
                result["remote_duplicate_scan"] = dict(scan)
        return results

    def _remote_history_matches(self, validated):
        targets = {fingerprint for _index, _spec, fingerprint in validated}
        if not targets:
            return {}
        shard_reader = getattr(self.client, "iter_user_alpha_history_shards", None)
        if callable(shard_reader):
            rows = self._recent_history_rows(shard_reader)
        else:
            reader = getattr(self.client, "get_all_user_alphas", None)
            if not callable(reader):
                raise ResearchReasonError(
                    "REMOTE_DUPLICATE_CAPABILITY_UNAVAILABLE",
                    "CAPABILITY_UNAVAILABLE",
                )
            # Compatibility path remains bounded. QueryTooBroad and transport
            # failures propagate; an incomplete scan is never treated as clear.
            rows = reader(max_results=1000)
        try:
            iterator = iter(rows)
        except TypeError as exc:
            raise ResearchReasonError(
                "REMOTE_DUPLICATE_CAPABILITY_UNAVAILABLE",
                "CAPABILITY_UNAVAILABLE",
            ) from exc

        matches = {}
        for row in iterator:
            if not isinstance(row, Mapping):
                continue
            alpha_payload = row.get("alpha")
            alpha = alpha_payload if isinstance(alpha_payload, Mapping) else row
            expression = alpha.get("regular") or alpha.get("expression")
            settings = alpha.get("settings", row.get("settings"))
            settings = settings if isinstance(settings, Mapping) else {}
            if not isinstance(expression, str) or not expression.strip():
                continue
            remote_type = (
                alpha.get("type") or alpha.get("simulation_type")
                or row.get("type") or "REGULAR"
            )
            remote_spec = SimulationSpec(
                expression, settings, simulation_type=str(remote_type)
            )
            remote_fingerprint = self.execution_fingerprint(remote_spec)
            if remote_fingerprint in targets:
                matches.setdefault(remote_fingerprint, {
                    "alpha_id": row.get("id") or alpha.get("id"),
                    "source": "LIVE",
                })
        # The scan evidence travels with the batch results: it states which
        # window was scanned, so no reader can mistake it for complete history.
        matches[REMOTE_DUPLICATE_SCAN_KEY] = {
            "status": "BOUNDED_RECENT_WINDOW",
            "lookback_days": REMOTE_DUPLICATE_LOOKBACK_DAYS,
            "complete": False,
        }
        return matches

    @staticmethod
    def _scope_for_spec(client, spec):
        settings = spec.settings
        return {
            "instrumentType": settings.get(
                "instrumentType", getattr(client, "instrument_type", "EQUITY")
            ),
            "region": settings.get("region", getattr(client, "region", "USA")),
            "delay": settings.get("delay", getattr(client, "delay", 1)),
            "universe": settings.get("universe", getattr(client, "universe", "TOP3000")),
        }


    def execution_fingerprint(self, spec):
        spec = spec if isinstance(spec, SimulationSpec) else SimulationSpec(**dict(spec))
        self.validate_simulation_spec(spec)
        return self.guard.fingerprint(
            spec.expression, spec.settings, simulation_type=spec.simulation_type
        )

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
        validated = []
        field_reader = getattr(self.client, "get_field_capability", None)
        field_groups = {}
        spec_field_groups = {}
        for index, spec in enumerate(normalized):
            self._validate_settings(spec, simulation_capability)
            fingerprint = self.execution_fingerprint(spec)
            self._validate_operator_capability(spec, operator_capability)
            scope = self._scope_for_spec(self.client, spec)
            if spec.fields:
                if not callable(field_reader):
                    raise ResearchReasonError(
                        "FIELD_CAPABILITY_UNAVAILABLE: no live field reader",
                        "CAPABILITY_UNAVAILABLE",
                    )
                groups_for_spec = set()
                for field_id in spec.fields:
                    dataset_id = spec.field_datasets.get(field_id)
                    if not dataset_id:
                        raise ResearchReasonError(
                            f"FIELD_CAPABILITY_UNAVAILABLE: missing dataset for {field_id}",
                            "CAPABILITY_UNAVAILABLE",
                        )
                    scope_key = tuple(sorted((key, str(value)) for key, value in scope.items()))
                    group_key = (scope_key, str(dataset_id))
                    group = field_groups.setdefault(group_key, {
                        "scope": scope, "dataset_id": str(dataset_id), "fields": set(),
                    })
                    group["fields"].add(field_id)
                    groups_for_spec.add(group_key)
                spec_field_groups[index] = groups_for_spec
            validated.append((index, spec, fingerprint))
        capabilities = {}
        for group_key, group in field_groups.items():
            capability = field_reader(
                {group["dataset_id"]: sorted(group["fields"])},
                scope=group["scope"],
            )
            if (not isinstance(capability, Mapping)
                    or capability.get("valid") is not True
                    or capability.get("source") != "BRAIN_LIVE_ONLY"):
                raise ResearchReasonError(
                    "FIELD_CAPABILITY_UNAVAILABLE", "CAPABILITY_UNAVAILABLE"
                )
            available = {str(item).casefold() for item in capability.get("fields", ())}
            missing = {field_id.casefold() for field_id in group["fields"]} - available
            if missing:
                raise ResearchReasonError(
                    "FIELD_CAPABILITY_UNAVAILABLE: " + ", ".join(sorted(missing)),
                    "CAPABILITY_UNAVAILABLE",
                )
            capabilities[group_key] = available
        for index, spec, _fingerprint in validated:
            field_capability = _CAPABILITY_READER_ABSENT
            if spec.fields:
                available = set().union(*(
                    capabilities[group_key] for group_key in spec_field_groups[index]
                ))
                field_capability = {
                    "valid": True, "source": "BRAIN_LIVE_ONLY",
                    "fields": available,
                }
            self._validate_live_capability(
                spec, operator_capability=operator_capability,
                field_capability=field_capability,
            )
        return results, validated, self._remote_history_matches(validated)

    def simulate(self, spec):
        return self.simulate_batch([spec])[0]

    def simulate_batch(self, specs):
        with single_instance_scope(self.state_dir, operation="simulation"):
            self.guard.reconcile()
            return self._simulate_batch(specs)

    def _simulate_batch(self, specs, *, preflight=None):
        """Execute a batch through one bounded Simulator window.

        The batch remains one guarded execution per spec; only dispatch and
        polling share the existing bounded worker pool.
        """
        results, validated, remote_duplicates = (
            preflight if preflight is not None else self._preflight_specs(specs)
        )
        prepared = []
        seen = set()
        for index, spec, fingerprint in validated:
            if fingerprint in seen:
                results[index] = self._labelled_result(
                    spec, "EXACT_DUPLICATE", fingerprint
                )
                continue
            seen.add(fingerprint)
            existing = self.guard.find(fingerprint)
            if existing is not None:
                if existing.get("status") == "SUBMIT_UNKNOWN":
                    results[index] = self._labelled_result(
                        spec, "SUBMIT_UNKNOWN", fingerprint,
                        progress_url=existing.get("progress_url"),
                    )
                else:
                    results[index] = self._labelled_result(
                        spec, "EXACT_DUPLICATE", fingerprint
                    )
                continue
            remote = remote_duplicates.get(fingerprint)
            if remote is not None:
                results[index] = self._labelled_result(
                    spec, "EXACT_DUPLICATE", fingerprint, **remote
                )
                continue
            if not self.guard.register(fingerprint):
                results[index] = self._labelled_result(
                    spec, "EXACT_DUPLICATE", fingerprint
                )
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
                    fields=spec.fields,
                    field_validation=(self._field_validation_label(spec)),
                    proposal_id=spec.proposal_id,
                    note=spec.note,
                    template_id=spec.template_id,
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
            spec = next(spec for candidate_index, spec, _fp in validated
                        if candidate_index == index)
            results[index] = self._labelled_result(
                spec, "NOT_DISPATCHED", fingerprint,
                error="dispatch paused before submission",
            )
        return self._attach_scan_evidence(results, remote_duplicates)

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
        if len(normalized_specs) > 1:
            for spec in normalized_specs:
                simulation_type = _spec_simulation_type(spec)
                if (simulation_type != REGULAR_SIMULATION_TYPE
                        and simulation_type in SUPPORTED_WRITE_SIMULATION_TYPES):
                    raise ResearchReasonError(
                        "Multi-Simulation only accepts REGULAR children",
                        "INVALID_SPEC",
                    )
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

        results, validated, remote_duplicates = self._preflight_specs(
            normalized_specs, simulation_capability=capability
        )
        eligible = []
        seen = set()
        for index, spec, fingerprint in validated:
            if fingerprint in seen:
                results[index] = self._labelled_result(
                    spec, "EXACT_DUPLICATE", fingerprint
                )
                continue
            seen.add(fingerprint)
            existing = self.guard.find(fingerprint)
            if existing is not None:
                # Exact-once is per Simulation, not per parent payload.  A child
                # that already carries an unresolved remote write -- as a Single
                # request, as a child of an earlier parent, or as part of a
                # reordered/split/subset batch -- is never POSTed again.
                existing_status = str(existing.get("status") or "")
                results[index] = self._labelled_result(
                    spec,
                    "SUBMIT_UNKNOWN" if existing_status == "SUBMIT_UNKNOWN"
                    else "EXACT_DUPLICATE",
                    fingerprint,
                    progress_url=existing.get("progress_url"),
                    guard_action="EXISTING_GUARD",
                )
                continue
            remote = remote_duplicates.get(fingerprint)
            if remote is not None:
                results[index] = self._labelled_result(
                    spec, "EXACT_DUPLICATE", fingerprint, **remote
                )
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
                    for index, spec, fingerprint in children:
                        results[index] = self._labelled_result(
                            spec, status, fingerprint,
                            batch_fingerprint=batch_fingerprint,
                            progress_url=existing.get("progress_url"),
                            parent=parent,
                        )
                    continue
                if not self.guard.register(
                    batch_fingerprint, simulation_count=len(children),
                    kind=ExecutionGuard.MULTI_PARENT,
                ):
                    parent = self._parent_projection(
                        fingerprint=batch_fingerprint,
                        status="EXACT_DUPLICATE",
                        progress_url=None,
                        child_count=len(children),
                        error="existing execution guard matched",
                        status_path=["EXACT_DUPLICATE"],
                        guard_action="EXISTING_GUARD",
                    )
                    for index, spec, fingerprint in children:
                        results[index] = self._labelled_result(
                            spec, "EXACT_DUPLICATE", fingerprint,
                            batch_fingerprint=batch_fingerprint, parent=parent,
                        )
                    continue
                # Register every child's own fingerprint before the parent POST
                # so the same Simulation cannot be re-dispatched through a
                # reordered, split, subset or Single retry afterwards.  The
                # parent row stays the quota-counted identity; child rows carry
                # no independent Simulation count.
                for index, spec, fingerprint in children:
                    if not self.guard.register(
                        fingerprint, simulation_count=1,
                        kind=ExecutionGuard.MULTI_CHILD,
                        parent_fingerprint=batch_fingerprint,
                    ):
                        results[index] = self._labelled_result(
                            spec, "EXACT_DUPLICATE", fingerprint,
                            batch_fingerprint=batch_fingerprint,
                        )
                        children = [
                            row for row in children if row[2] != fingerprint
                        ]
                if not children:
                    self.guard.remove_batch(batch_fingerprint)
                    continue
                child_records = [
                    SimpleNamespace(
                        index=index,
                        submission_fingerprint=fingerprint,
                        expression=spec.expression,
                        settings=dict(spec.settings),
                        simulation_type=spec.simulation_type,
                        fields=spec.fields,
                        field_validation=(SimulationGateway._field_validation_label(spec)),
                        proposal_id=spec.proposal_id,
                        note=spec.note,
                        template_id=spec.template_id,
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
            remainder_specs = [spec for _index, spec, _fingerprint in single_remainders]
            remainder_validated = [
                (local_index, spec, fingerprint)
                for local_index, (_index, spec, fingerprint) in enumerate(single_remainders)
            ]
            remainder_preflight = (
                [None] * len(remainder_specs), remainder_validated, remote_duplicates
            )
            single_results = self._simulate_batch(
                remainder_specs, preflight=remainder_preflight
            )
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
                self.guard.remove_batch(batch_fingerprint)
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
                    results[child.index] = self._labelled_result(
                        child, "FAILED", child.submission_fingerprint,
                        batch_fingerprint=batch_fingerprint, error=batch.error,
                        failure_kind=getattr(child, "failure_kind", "FAILED_REMOTE"),
                        remote_status=getattr(child, "remote_status", None),
                        diagnostic=getattr(child, "diagnostic", None),
                        failure_scope=getattr(child, "failure_scope", None)
                        or getattr(batch, "failure_scope", None),
                        parent=parent,
                    )
                else:
                    results[child.index] = self._labelled_result(
                        child,
                        "UNKNOWN" if batch.progress_url else "SUBMIT_UNKNOWN",
                        child.submission_fingerprint,
                        batch_fingerprint=batch_fingerprint, error=batch.error,
                        failure_kind=getattr(child, "failure_kind", None),
                        remote_status=getattr(child, "remote_status", None),
                        diagnostic=getattr(child, "diagnostic", None),
                        failure_scope=getattr(child, "failure_scope", None)
                        or getattr(batch, "failure_scope", None),
                        progress_url=batch.progress_url, parent=parent,
                    )
        for batch_fingerprint, batch in batches:
            if batch_fingerprint in completed_fingerprints:
                continue
            self.guard.remove_batch(batch_fingerprint)
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
                results[child.index] = self._labelled_result(
                    child, "NOT_DISPATCHED", child.submission_fingerprint,
                    batch_fingerprint=batch_fingerprint,
                    error="dispatch paused before submission",
                    failure_scope="PARENT", parent=parent,
                )
        return self._attach_scan_evidence(results, remote_duplicates)

    def resume_execution(self, fingerprint):
        with single_instance_scope(self.state_dir, operation="resume-simulation"):
            self.guard.reconcile()
            return self._resume_execution(fingerprint)

    def _resume_execution(self, fingerprint):
        row = self.guard.find(str(fingerprint))
        if row is None:
            raise KeyError("execution fingerprint not found")
        parent_fingerprint = row.get("parent_fingerprint")
        if parent_fingerprint:
            # A Multi child carries no independent remote identity: recovery
            # must follow the parent that owns the progress URL and every child.
            parent = self.guard.find(parent_fingerprint)
            if parent is not None:
                row = parent
                fingerprint = parent_fingerprint
        progress_url = row.get("progress_url")
        if not progress_url:
            return {"status": "SUBMIT_UNKNOWN", "fingerprint": str(fingerprint)}
        try:
            # ``kind`` is persisted safety metadata, so a known Multi parent is
            # polled as a Multi parent instead of being guessed from a progress
            # payload.  Legacy rows without ``kind`` keep the payload probe:
            # BRAIN returns ``children`` for a Multi parent, and treating that
            # response as Single raises "finished without alpha id" and strands
            # a valid remote job in SUBMIT_UNKNOWN.
            is_multi = str(row.get("kind") or "").upper() == ExecutionGuard.MULTI_PARENT
            remote_terminal_error = False
            if not is_multi:
                snapshot_reader = getattr(self.client, "get_progress_snapshot", None)
                if snapshot_reader is not None:
                    snapshot = snapshot_reader(progress_url, timeout=60)
                    payload = (
                        snapshot.get("payload")
                        if isinstance(snapshot, Mapping) else None
                    )
                    if isinstance(payload, Mapping):
                        remote_status = str(payload.get("status", "")).upper()
                        remote_terminal_error = remote_status in {"ERROR", "FAILED"}
                        if isinstance(payload.get("children"), list):
                            is_multi = True
            poller = (
                self.client.poll_multi_progress if is_multi
                else self.client.poll_progress
            )
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
                self.guard.remove_batch(str(fingerprint))
                return {"status": "FAILED", "fingerprint": str(fingerprint),
                        "error": str(exc), "progress_url": progress_url}
            return {"status": row.get("status", "SUBMIT_UNKNOWN"),
                    "fingerprint": str(fingerprint), "error": str(exc),
                    "progress_url": progress_url}
        self.guard.remove_batch(str(fingerprint))
        return result

    @staticmethod
    def _field_validation_label(spec):
        """Report field truth instead of letting an unchecked field look fine.

        ``LIVE_VERIFIED`` means the declared fields were checked against their
        live BRAIN dataset.  ``UNVERIFIED`` means the expression carries fields
        that no capability read covered.  ``NOT_REQUESTED`` means the expression
        declares no field at all.
        """
        if getattr(spec, "fields", ()):
            return "LIVE_VERIFIED"
        try:
            identifiers = expression_field_identifiers(
                analyze_expression(spec.expression)
            )
        except Exception:
            return "UNVERIFIED"
        return "UNVERIFIED" if identifiers else "NOT_REQUESTED"

    @staticmethod
    def _spec_labels(spec, *, field_validation=None):
        return {
            "proposal_id": spec.proposal_id,
            "note": spec.note,
            "template_id": spec.template_id,
            "field_validation": field_validation
            or SimulationGateway._field_validation_label(spec),
        }

    @classmethod
    def _labelled_result(cls, spec, status, fingerprint, **extra):
        result = {
            "status": status,
            "fingerprint": fingerprint,
            **cls._spec_labels(spec),
            **extra,
        }
        reason_code = reason_code_for_failure(
            status, result.get("error"), progress_url=result.get("progress_url")
        )
        if reason_code:
            result["reason_code"] = reason_code
        return result

    @classmethod
    def _result(cls, item, fingerprint):
        spec = SimpleNamespace(
            proposal_id=getattr(item, "proposal_id", None),
            note=getattr(item, "note", None),
            template_id=getattr(item, "template_id", None),
            fields=getattr(item, "fields", ()),
        )
        return cls._labelled_result(
            spec, item.status, fingerprint,
            alpha_id=getattr(item, "alpha_id", None),
            progress_url=getattr(item, "progress_url", None),
            evidence=getattr(item, "evidence", None),
            error=getattr(item, "error", None),
            failure_kind=getattr(item, "failure_kind", None),
            remote_status=getattr(item, "remote_status", None),
            diagnostic=getattr(item, "diagnostic", None),
            field_validation=getattr(item, "field_validation", None) or (
                cls._field_validation_label(item)
            ),
        )
