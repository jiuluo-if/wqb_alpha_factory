"""One-command, read-only summaries of local workspace evidence."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass

from .artifacts import iter_jsonl_objects
from .checkpoints import CheckpointStore
from .expression import canonical_expression
from .schema import TRAJECTORY_VERSION, TRIAL_LEDGER_VERSION, VALIDATION_VERSION
from .state import Trajectory, same_execution_identity
from .trial_ledger import LIFECYCLE_PHASE_INDEX, PHASES, TrialLedger

_CHECKPOINT_NAME = re.compile(r"round_\d+\.checkpoint\.json")
_FIXED_ARTIFACT_NAMES = (
    "trajectory.jsonl", "trial_ledger.jsonl", "submission_pool.json",
    "fields_cache.json", "evidence_cache.json", "factory_session.json",
    "sims_results.json", "validation_reports.jsonl", "proposals.json",
    "experience.json",
)
_DOCTOR_ARTIFACT_NAMES = frozenset(_FIXED_ARTIFACT_NAMES[:8])


def is_doctor_artifact(name):
    return name in _DOCTOR_ARTIFACT_NAMES or bool(
        re.fullmatch(r"round_\d+\.checkpoint\.json", name)
        or re.fullmatch(r"active_alphas_\d{8}\.json", name)
    )


@dataclass(frozen=True)
class ArtifactInfo:
    name: str
    exists: bool = False
    readable: bool = False
    kind: str = "other"
    schema_version: int | str | None = None


@dataclass(frozen=True)
class ArtifactInventory:
    """Ephemeral file metadata; no artifact payload is retained here."""

    entries: tuple = ()

    def info(self, name):
        for entry in self.entries:
            if entry.name == name:
                return entry
        return ArtifactInfo(name)


@dataclass(frozen=True)
class LedgerSummary:
    """Lifecycle evidence owned by TrialLedger, not the trajectory projection."""

    committed: frozenset = frozenset()
    submitted: frozenset = frozenset()
    simulation_settled: frozenset = frozenset()
    research_settled: frozenset = frozenset()
    settlement_ids: frozenset = frozenset()
    duplicate_settlements: int = 0
    invalid_rows: int = 0
    unknown_phase_rows: int = 0
    incomplete_rows: int = 0
    duplicate_lifecycle_phases: int = 0
    phase_order_violations: tuple = ()
    missing_alpha_id_rows: int = 0
    simulation_submitted: frozenset = frozenset()
    unsupported_schema_rows: int = 0
    lifecycle_identity_drift: dict = None
    proposal_id_execution_rebind: int = 0


@dataclass(frozen=True)
class ValidationSummary:
    parent_ids: frozenset = frozenset()
    plan_pairs: tuple = ()
    invalid_rows: int = 0
    unsupported_schema_rows: int = 0


@dataclass(frozen=True)
class SubmissionPoolSummary:
    candidate_identities: tuple = ()
    unverifiable_candidates: int = 0
    candidate_identity_sources: tuple = ()


@dataclass(frozen=True)
class TrajectorySummary:
    """Experiment/result/dedupe projection; TrialLedger owns lifecycle facts."""

    records: int = 0
    latest_round: int | None = None
    submit_unknown_count: int = 0
    pending_validation_count: int = 0
    trajectory_ids: frozenset = frozenset()
    observed_settlement_ids: frozenset = frozenset()
    duplicate_observed_settlements: int = 0
    observed_committed: frozenset = frozenset()
    observed_submitted: frozenset = frozenset()
    observed_simulation_settled: frozenset = frozenset()
    observed_research_settled: frozenset = frozenset()
    invalid_rows: int = 0
    unknown_phase_rows: int = 0
    incomplete_rows: int = 0
    duplicate_lifecycle_phases: int = 0
    phase_order_violations: tuple = ()
    missing_alpha_id_rows: int = 0
    trajectory_alpha_ids: frozenset = frozenset()
    trajectory_proposal_ids: frozenset = frozenset()
    observed_simulation_submitted: frozenset = frozenset()
    unsupported_schema_rows: int = 0
    identity_mismatch_rows: int = 0
    parent_identity_issues: dict = None
    duplicate_remote_execution_projection: int = 0
    duplicate_remote_execution_projection_rounds: tuple = ()


@dataclass(frozen=True)
class WorkspaceSnapshot:
    """Ephemeral facts only; it is never persisted or treated as state truth."""

    state_dir: str
    checkpoint_records: tuple
    trajectory: TrajectorySummary
    inventory: ArtifactInventory = ArtifactInventory()
    ledger: LedgerSummary = LedgerSummary()
    validation: ValidationSummary = ValidationSummary()
    submission_pool: SubmissionPoolSummary = SubmissionPoolSummary()
    proposal_round: int | None = None
    proposal_count: int = 0
    current_best: object = None
    evidence_cache_entries: int = 0
    unresolved_submission_identity_collisions: int = 0

    @property
    def unfinished_checkpoint_paths(self):
        return tuple(
            sorted(
                os.path.basename(record["path"])
                for record in self.checkpoint_records
                if record["malformed"]
                or record["checkpoint"].get("complete") is not True
            )
        )


def _new_lifecycle_stats():
    return {
        "unknown_phase_rows": 0,
        "incomplete_rows": 0,
        "duplicate_lifecycle_phases": 0,
        "phase_order_violations": [],
        "missing_alpha_id_rows": 0,
        "unsupported_schema_rows": 0,
        "seen_phases": set(),
        "last_phase": {},
    }


def _observe_schema(row, stats, expected_schema_version):
    version = row.get("schema_version")
    if (
        isinstance(version, int)
        and not isinstance(version, bool)
        and version > expected_schema_version
    ):
        stats["unsupported_schema_rows"] += 1


def _observe_lifecycle(row, stats, *, require_phase, expected_schema_version):
    _observe_schema(row, stats, expected_schema_version)
    phase = row.get("phase")
    if not isinstance(phase, str) or phase not in PHASES:
        if require_phase or phase is not None:
            stats["unknown_phase_rows"] += 1
        return
    if phase not in LIFECYCLE_PHASE_INDEX:
        return
    proposal_id = row.get("proposal_id")
    if proposal_id in (None, ""):
        stats["incomplete_rows"] += 1
        return
    proposal_id = str(proposal_id)
    phase_key = (proposal_id, phase)
    if phase_key in stats["seen_phases"]:
        stats["duplicate_lifecycle_phases"] += 1
    stats["seen_phases"].add(phase_key)
    previous = stats["last_phase"].get(proposal_id)
    if previous is not None and LIFECYCLE_PHASE_INDEX[phase] < LIFECYCLE_PHASE_INDEX[previous]:
        stats["phase_order_violations"].append((proposal_id, previous, phase))
    stats["last_phase"][proposal_id] = phase
    if phase == "research_outcome_settled":
        settlement = row.get("settlement")
        if not isinstance(settlement, dict) or settlement.get("settlement_id") in (None, ""):
            stats["incomplete_rows"] += 1
    if (
        phase == "simulation_settled"
        and str(row.get("outcome") or row.get("status") or "").upper() == "DONE"
        and row.get("alpha_id") in (None, "")
    ):
        stats["missing_alpha_id_rows"] += 1


def _trajectory_summary(state_dir):
    trajectory = Trajectory(path=os.path.join(state_dir, "trajectory.jsonl"))
    lifecycle_stats = _new_lifecycle_stats()
    read_stats = {}
    trajectory_ids = set()
    trajectory_alpha_ids = set()
    trajectory_proposal_ids = set()
    observed_settlement_ids = set()
    observed_committed = set()
    observed_submitted = set()
    observed_simulation_submitted = set()
    observed_simulation_settled = set()
    observed_research_settled = set()
    identity_references = {}
    canonical_rows = {}
    identity_mismatch_rows = 0
    summary = {
        "records": 0,
        "latest_round": None,
        "submit_unknown_count": 0,
        "pending_validation_count": 0,
        "duplicate_observed_settlements": 0,
    }
    for row in trajectory.iter_rows(stats=read_stats) or ():
        summary["records"] += 1
        if isinstance(row.get("round"), int):
            summary["latest_round"] = max(
                summary["latest_round"] or row["round"], row["round"]
            )
        if row.get("status") == "SUBMIT_UNKNOWN":
            summary["submit_unknown_count"] += 1
        if row.get("validation_status") in {"PENDING", "UNVALIDATED"}:
            summary["pending_validation_count"] += 1
        for key in (row.get("id"), row.get("alpha_id"), row.get("proposal_id")):
            if key:
                trajectory_ids.add(str(key))
        if row.get("alpha_id"):
            trajectory_alpha_ids.add(str(row["alpha_id"]))
        if row.get("proposal_id"):
            trajectory_proposal_ids.add(str(row["proposal_id"]))
        row_id = row.get("id")
        if row_id not in (None, ""):
            reference = identity_references.get(row_id)
            if reference is not None and not same_execution_identity(reference, row):
                identity_mismatch_rows += 1
            else:
                identity_references.setdefault(row_id, row)
                canonical_rows[row_id] = row
        proposal_id = row.get("proposal_id")
        phase = row.get("phase")
        _observe_lifecycle(
            row, lifecycle_stats, require_phase=False,
            expected_schema_version=TRAJECTORY_VERSION,
        )
        if proposal_id:
            if phase == "simulation_committed":
                observed_committed.add(str(proposal_id))
            elif phase == "simulation_submitted":
                observed_simulation_submitted.add(str(proposal_id))
                observed_submitted.add(str(proposal_id))
            elif phase == "simulation_settled":
                observed_submitted.add(str(proposal_id))
            if phase == "simulation_settled":
                observed_simulation_settled.add(str(proposal_id))
            elif phase == "research_outcome_settled":
                observed_research_settled.add(str(proposal_id))
        if phase == "research_outcome_settled":
            settlement_id = (row.get("settlement") or {}).get("settlement_id")
            if settlement_id and str(settlement_id) in observed_settlement_ids:
                summary["duplicate_observed_settlements"] += 1
            elif settlement_id:
                observed_settlement_ids.add(str(settlement_id))
    parent_issues = {}
    done_by_expression = {}
    for row in canonical_rows.values():
        if row.get("status") == "DONE" and isinstance(row.get("metrics"), dict) and row.get("metrics"):
            done_by_expression.setdefault(
                canonical_expression(row.get("expression", "")), []
            ).append(row)
    for row in canonical_rows.values():
        stage = str(row.get("experiment_stage") or "").upper()
        if stage not in {"CHILD", "ROBUSTNESS"} and not row.get("parent_id"):
            continue
        parent_id = row.get("parent_id")
        parent = canonical_rows.get(str(parent_id)) if parent_id not in (None, "") else None
        if parent_id not in (None, ""):
            if parent is None:
                parent_issues["PARENT_NOT_FOUND"] = parent_issues.get("PARENT_NOT_FOUND", 0) + 1
                continue
            if parent.get("status") != "DONE" or not isinstance(parent.get("metrics"), dict) or not parent.get("metrics"):
                parent_issues["PARENT_NOT_DONE"] = parent_issues.get("PARENT_NOT_DONE", 0) + 1
            if canonical_expression(row.get("parent_expression", "")) != canonical_expression(parent.get("expression", "")):
                parent_issues["PARENT_EXPRESSION_MISMATCH"] = parent_issues.get("PARENT_EXPRESSION_MISMATCH", 0) + 1
        elif stage in {"CHILD", "ROBUSTNESS"}:
            candidates = done_by_expression.get(canonical_expression(row.get("parent_expression", "")), [])
            if len(candidates) > 1:
                parent_issues["PARENT_REFERENCE_AMBIGUOUS"] = parent_issues.get("PARENT_REFERENCE_AMBIGUOUS", 0) + 1
            elif not candidates:
                parent_issues["PARENT_NOT_FOUND"] = parent_issues.get("PARENT_NOT_FOUND", 0) + 1
    remote_groups = {}
    for criterion in ("progress_url", "alpha_id"):
        grouped = {}
        for row in canonical_rows.values():
            fingerprint = row.get("submission_fingerprint")
            value = row.get(criterion)
            if fingerprint in (None, "") or value in (None, ""):
                continue
            grouped.setdefault((str(fingerprint), str(value)), set()).add(str(row.get("id")))
        for key, ids in grouped.items():
            if len(ids) > 1:
                remote_groups[frozenset(ids)] = key
    remote_rounds = set()
    for ids in remote_groups:
        for row in canonical_rows.values():
            if str(row.get("id")) in ids and row.get("round") not in (None, ""):
                remote_rounds.add(str(row["round"]))
    return TrajectorySummary(
        records=summary["records"],
        latest_round=summary["latest_round"],
        submit_unknown_count=summary["submit_unknown_count"],
        pending_validation_count=summary["pending_validation_count"],
        trajectory_ids=frozenset(trajectory_ids),
        trajectory_alpha_ids=frozenset(trajectory_alpha_ids),
        trajectory_proposal_ids=frozenset(trajectory_proposal_ids),
        observed_settlement_ids=frozenset(observed_settlement_ids),
        duplicate_observed_settlements=summary["duplicate_observed_settlements"],
        observed_committed=frozenset(observed_committed),
        observed_submitted=frozenset(observed_submitted),
        observed_simulation_settled=frozenset(observed_simulation_settled),
        observed_research_settled=frozenset(observed_research_settled),
        invalid_rows=read_stats.get("invalid_rows", 0),
        unknown_phase_rows=lifecycle_stats["unknown_phase_rows"],
        incomplete_rows=lifecycle_stats["incomplete_rows"],
        duplicate_lifecycle_phases=lifecycle_stats["duplicate_lifecycle_phases"],
        phase_order_violations=tuple(lifecycle_stats["phase_order_violations"]),
        missing_alpha_id_rows=lifecycle_stats["missing_alpha_id_rows"],
        observed_simulation_submitted=frozenset(observed_simulation_submitted),
        unsupported_schema_rows=lifecycle_stats["unsupported_schema_rows"],
        identity_mismatch_rows=identity_mismatch_rows,
        parent_identity_issues=parent_issues,
        duplicate_remote_execution_projection=len(remote_groups),
        duplicate_remote_execution_projection_rounds=tuple(sorted(remote_rounds)),
    )


def _load_json(path):
    try:
        with open(path, encoding="utf-8-sig") as handle:
            return True, json.load(handle)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return False, None


def _kind(name):
    if name.endswith(".jsonl"):
        return "jsonl"
    if name.endswith(".json"):
        return "json"
    return "other"


def _safe_schema_version(value):
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value
    return "INVALID"


def _safe_proposal_round(value):
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def _inventory(state_dir, names, checkpoint_records):
    names = set(names)
    checkpoint_by_name = {
        os.path.basename(record["path"]): record
        for record in checkpoint_records
    }
    all_names = names | set(_FIXED_ARTIFACT_NAMES)
    entries = []
    payloads = {}
    for name in sorted(all_names):
        path = os.path.join(state_dir, name)
        exists = name in names
        readable = exists and os.path.isfile(path) and os.access(path, os.R_OK)
        kind = _kind(name)
        schema_version = None
        if readable and _CHECKPOINT_NAME.fullmatch(name):
            record = checkpoint_by_name.get(name)
            if record is None or record["malformed"]:
                schema_version = "UNREADABLE"
            else:
                schema_version = _safe_schema_version(
                    record["checkpoint"].get("schema_version", "LEGACY")
                )
        elif readable and kind == "json" and (
            name in _FIXED_ARTIFACT_NAMES or is_doctor_artifact(name)
        ):
            valid, payload = _load_json(path)
            if not valid:
                schema_version = "UNREADABLE"
            elif isinstance(payload, dict):
                schema_version = _safe_schema_version(
                    payload.get("schema_version", "LEGACY")
                )
                payloads[name] = payload
            else:
                schema_version = "INVALID"
                payloads[name] = payload
        elif readable and kind == "jsonl":
            schema_version = "JSONL_PRESENT"
        elif readable:
            schema_version = "PRESENT"
        entries.append(ArtifactInfo(name, exists, readable, kind, schema_version))
    return ArtifactInventory(tuple(entries)), payloads


def _ledger_summary(path):
    committed = set()
    submitted = set()
    simulation_submitted = set()
    simulation_settled = set()
    research_settled = set()
    settlement_ids = set()
    duplicate_settlements = 0
    lifecycle_stats = _new_lifecycle_stats()
    lifecycle_rows = {}
    proposal_id_execution_rebind = 0
    read_stats = {}
    for row in iter_jsonl_objects(path, stats=read_stats):
        if row.get("reason_code") == "PROPOSAL_ID_REBIND":
            proposal_id_execution_rebind += 1
        _observe_lifecycle(
            row, lifecycle_stats, require_phase=True,
            expected_schema_version=TRIAL_LEDGER_VERSION,
        )
        proposal_id = row.get("proposal_id")
        phase = row.get("phase")
        if proposal_id and phase == "simulation_committed":
            committed.add(str(proposal_id))
        elif proposal_id and phase == "simulation_submitted":
            simulation_submitted.add(str(proposal_id))
            submitted.add(str(proposal_id))
        elif proposal_id and phase == "simulation_settled":
            submitted.add(str(proposal_id))
        if proposal_id and phase == "simulation_settled":
            simulation_settled.add(str(proposal_id))
        if proposal_id and phase == "research_outcome_settled":
            research_settled.add(str(proposal_id))
        if proposal_id and phase in {"simulation_committed", "simulation_submitted",
                                     "simulation_settled", "submitted", "completed"}:
            lifecycle_rows.setdefault(str(proposal_id), []).append(row)
        if phase != "research_outcome_settled":
            continue
        settlement_id = (row.get("settlement") or {}).get("settlement_id")
        if not settlement_id:
            continue
        settlement_id = str(settlement_id)
        if settlement_id in settlement_ids:
            duplicate_settlements += 1
        else:
            settlement_ids.add(settlement_id)
    drift = {}
    for rows in lifecycle_rows.values():
        for code in TrialLedger._lifecycle_identity(rows).get("drift") or ():
            drift[code] = drift.get(code, 0) + 1
    return LedgerSummary(
        committed=frozenset(committed),
        submitted=frozenset(submitted),
        simulation_settled=frozenset(simulation_settled),
        research_settled=frozenset(research_settled),
        settlement_ids=frozenset(settlement_ids),
        duplicate_settlements=duplicate_settlements,
        invalid_rows=read_stats.get("invalid_rows", 0),
        unknown_phase_rows=lifecycle_stats["unknown_phase_rows"],
        incomplete_rows=lifecycle_stats["incomplete_rows"],
        duplicate_lifecycle_phases=lifecycle_stats["duplicate_lifecycle_phases"],
        phase_order_violations=tuple(lifecycle_stats["phase_order_violations"]),
        missing_alpha_id_rows=lifecycle_stats["missing_alpha_id_rows"],
        simulation_submitted=frozenset(simulation_submitted),
        unsupported_schema_rows=lifecycle_stats["unsupported_schema_rows"],
        lifecycle_identity_drift=drift,
        proposal_id_execution_rebind=proposal_id_execution_rebind,
    )


def _validation_summary(path):
    parent_ids = set()
    plan_pairs = []
    read_stats = {}
    for row in iter_jsonl_objects(path, stats=read_stats):
        _observe_schema(row, read_stats, VALIDATION_VERSION)
        if row.get("parent_id"):
            parent_ids.add(str(row["parent_id"]))
        nested = row.get("report")
        if isinstance(nested, dict):
            plan_pairs.append((row.get("plan_id"), nested.get("plan_id")))
    return ValidationSummary(
        frozenset(parent_ids), tuple(plan_pairs), read_stats.get("invalid_rows", 0),
        read_stats.get("unsupported_schema_rows", 0),
    )


def _submission_pool_summary(payload):
    identities = []
    identity_sources = []
    unverifiable = 0
    rows = payload.get("candidates") if isinstance(payload, dict) else []
    for row in rows or []:
        if isinstance(row, dict):
            identity = frozenset(
                str(row.get(key))
                for key in ("alpha_id", "proposal_id")
                if row.get(key) not in (None, "")
            )
            sources = frozenset(
                (key, str(row.get(key)))
                for key in ("alpha_id", "proposal_id")
                if row.get(key) not in (None, "")
            )
            if not identity:
                unverifiable += 1
            identities.append(identity)
            identity_sources.append(sources)
    return SubmissionPoolSummary(tuple(identities), unverifiable, tuple(identity_sources))


def read_workspace_snapshot(state_dir):
    """Read checkpoint and trajectory facts once for one command lifecycle."""
    state_dir = os.fspath(state_dir)
    try:
        names = os.listdir(state_dir)
    except OSError:
        names = []
    store = CheckpointStore(state_dir)
    checkpoint_records = tuple(store.scan(names=names))
    inventory, payloads = _inventory(state_dir, names, checkpoint_records)
    proposals = payloads.get("proposals.json")
    experience = payloads.get("experience.json")
    evidence_cache = payloads.get("evidence_cache.json")
    unresolved_identities = store.unresolved_submission_identities(
        records=checkpoint_records
    )
    return WorkspaceSnapshot(
        state_dir=state_dir,
        checkpoint_records=checkpoint_records,
        trajectory=_trajectory_summary(state_dir),
        inventory=inventory,
        ledger=_ledger_summary(os.path.join(state_dir, "trial_ledger.jsonl")),
        validation=_validation_summary(os.path.join(state_dir, "validation_reports.jsonl")),
        submission_pool=_submission_pool_summary(payloads.get("submission_pool.json")),
        proposal_round=_safe_proposal_round(
            proposals.get("round_no") if isinstance(proposals, dict) else None
        ),
        proposal_count=(len(proposals.get("proposals") or [])
                        if isinstance(proposals, dict) else 0),
        current_best=(experience.get("current_best")
                      if isinstance(experience, dict) else None),
        evidence_cache_entries=(len(evidence_cache)
                               if isinstance(evidence_cache, dict) else 0),
        unresolved_submission_identity_collisions=sum(
            1 for entries in unresolved_identities.values() if len(entries) > 1
        ),
    )
