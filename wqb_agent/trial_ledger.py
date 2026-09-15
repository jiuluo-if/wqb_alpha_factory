"""ROLE: CORE
AGENT_RELEVANCE: MEDIUM
PURPOSE: Preserve bounded append-only experiment lifecycle and trial counts.
READ WHEN: changing trial accounting or evidence settlement.
DO NOT USE FOR: making platform truth or research-direction decisions.

Append-only, bounded-memory trial lifecycle ledger."""

from __future__ import annotations

import atexit
import hashlib
import json
import os
import sqlite3
import tempfile
import threading
import time
from collections import Counter, defaultdict
from contextlib import nullcontext

from .artifacts import (
    atomic_write_json_if_changed,
    iter_jsonl_objects,
)
from .expression import canonical_expression
from .identity import candidate_identity
from .locking import StateMutationDelegation, single_instance_scope
from .optimization_decision import optimization_decision_identity
from .schema import CREATED_BY_VERSION, TRIAL_LEDGER_VERSION
from .search_evidence import structural_fingerprint

PHASES = {
    "generated", "preflight", "submitted", "completed",
    "candidate_generated", "candidate_rejected", "preflight_accepted",
    "candidate_admitted",
    "simulation_committed", "simulation_submitted", "simulation_settled",
    "research_outcome_settled",
    "optimization_selection",
    "history_completeness",
}

SIMULATION_LIFECYCLE_PHASES = (
    "simulation_committed",
    "simulation_submitted",
    "simulation_settled",
    "research_outcome_settled",
)
LIFECYCLE_PHASE_INDEX = {
    phase: index for index, phase in enumerate(SIMULATION_LIFECYCLE_PHASES)
}

SIMULATION_PHASES = {
    "simulation_committed", "simulation_submitted", "simulation_settled",
    "submitted", "completed",
}


def _text(value, default="unknown"):
    return str(value) if isinstance(value, (str, int, float, bool)) else default


class TrialLedger:
    """Record proposal lifecycle facts without replacing trajectory/checkpoint."""

    SCHEMA_VERSION = TRIAL_LEDGER_VERSION

    def __init__(self, path, persist=True, trajectory_path=None):
        self.path = path
        self.persist = bool(persist)
        self.trajectory_path = trajectory_path
        self._events = []
        self._append_lock = threading.Lock()
        # Owner-local, rebuildable exact membership projection.  The SQLite
        # file is process-local and disposable; JSONL remains the only
        # durable source of lifecycle/accounting truth.
        self._membership_db = None
        self._membership_db_path = None
        self._membership_initialized = False
        self._membership_signature = None
        self._history_completeness = None
        self._history_completeness_exists = False
        self._reconciliation_count = 0
        atexit.register(self._close_membership_db_unlocked)

    def _open_membership_db_unlocked(self):
        if self._membership_db is not None:
            return self._membership_db
        if self.persist and self.path:
            fd, db_path = tempfile.mkstemp(prefix="wqb-trial-membership-", suffix=".sqlite3")
            os.close(fd)
            self._membership_db_path = db_path
            target = db_path
        else:
            target = ":memory:"
        try:
            self._membership_db = sqlite3.connect(target, check_same_thread=False)
            self._membership_db.execute(
                "CREATE TABLE IF NOT EXISTS event_ids (event_id TEXT PRIMARY KEY)"
            )
            self._membership_db.commit()
        except (OSError, sqlite3.Error):
            self._close_membership_db_unlocked()
            raise
        return self._membership_db

    def _close_membership_db_unlocked(self):
        if self._membership_db is not None:
            try:
                self._membership_db.close()
            except sqlite3.Error:
                pass
        self._membership_db = None
        if self._membership_db_path:
            try:
                os.remove(self._membership_db_path)
            except OSError:
                pass
        self._membership_db_path = None

    def _invalidate_membership_unlocked(self):
        self._membership_initialized = False
        self._membership_signature = None

    def _membership_contains_unlocked(self, event_id):
        if event_id is None:
            return False
        db = self._open_membership_db_unlocked()
        row = db.execute(
            "SELECT 1 FROM event_ids WHERE event_id = ? LIMIT 1", (str(event_id),)
        ).fetchone()
        return row is not None

    def _durable_scope(self, operation, delegation=None):
        if not self.persist or not self.path:
            return nullcontext()
        state_dir = os.path.dirname(os.path.abspath(self.path))
        if delegation is not None:
            if not isinstance(delegation, StateMutationDelegation):
                raise TypeError("delegation must be a StateMutationDelegation")
            return delegation.authorization(state_dir)
        return single_instance_scope(state_dir, operation)

    @staticmethod
    def _file_signature(path):
        if not path:
            return None
        try:
            stat = os.stat(path)
        except OSError:
            return None
        return stat.st_size, stat.st_mtime_ns

    def _reconcile_membership_unlocked(self, trajectory_path):
        completeness = None
        completeness_exists = False
        has_event_rows = False
        db = self._open_membership_db_unlocked()
        try:
            db.execute("DELETE FROM event_ids")
            source = (
                iter_jsonl_objects(self.path)
                if self.path and os.path.exists(self.path)
                else self._events
            )
            for row in source:
                event_id = row.get("event_id")
                if event_id is not None:
                    has_event_rows = True
                    db.execute(
                        "INSERT OR IGNORE INTO event_ids(event_id) VALUES (?)",
                        (str(event_id),),
                    )
                if row.get("phase") == "history_completeness":
                    completeness = str(row.get("outcome") or "UNKNOWN")
                    completeness_exists = True
            db.commit()
        except sqlite3.Error:
            db.rollback()
            self._invalidate_membership_unlocked()
            raise
        trajectory_has_rows = False
        if trajectory_path and os.path.exists(trajectory_path):
            try:
                with open(trajectory_path, encoding="utf-8") as handle:
                    trajectory_has_rows = any(line.strip() for line in handle)
            except OSError:
                trajectory_has_rows = True
        if completeness is None:
            completeness = (
                "INCOMPLETE_LEGACY"
                if trajectory_has_rows or has_event_rows
                else "COMPLETE_FROM_START"
            )
        self._history_completeness = completeness
        self._history_completeness_exists = completeness_exists
        self._membership_initialized = True
        self._membership_signature = self._file_signature(self.path)
        self._reconciliation_count += 1
        return completeness, completeness_exists

    def _ensure_membership_unlocked(self, trajectory_path):
        signature = self._file_signature(self.path)
        if (
            not self._membership_initialized
            or signature != self._membership_signature
        ):
            return self._reconcile_membership_unlocked(trajectory_path)
        return self._history_completeness, self._history_completeness_exists

    def _append_jsonl_unique_unlocked(self, row):
        event_id = row.get("event_id")
        if self._membership_contains_unlocked(event_id):
            return False
        parent = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(parent, exist_ok=True)
        line = (json.dumps(row, ensure_ascii=False) + "\n").encode("utf-8")
        with open(self.path, "ab") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
        if event_id is not None:
            try:
                self._membership_db.execute(
                    "INSERT INTO event_ids(event_id) VALUES (?)", (str(event_id),)
                )
                self._membership_db.commit()
            except sqlite3.Error:
                # The canonical append already succeeded.  Never write a
                # second durable record; rebuild the disposable index later.
                self._invalidate_membership_unlocked()
                return True
        if self._membership_initialized:
            self._membership_signature = self._file_signature(self.path)
        return True

    def _write_history_completeness_unlocked(self, outcome):
        row = {
            "schema_version": self.SCHEMA_VERSION,
            "created_by_version": CREATED_BY_VERSION,
            "event_id": hashlib.sha256(
                f"history_completeness|{outcome}".encode()
            ).hexdigest(),
            "phase": "history_completeness",
            "event_type": "history_completeness",
            "outcome": outcome,
            "status": outcome,
            "recorded_at": time.time(),
        }
        self._append_jsonl_unique_unlocked(row)
        self._history_completeness = outcome
        self._history_completeness_exists = True
        return outcome

    def initialize_history_completeness(self, trajectory_path):
        """Record the one-time completeness boundary in this ledger owner."""
        if not self.path or not self.persist:
            return "COMPLETE_FROM_START"
        with self._durable_scope("trial-ledger-history"):
            with self._append_lock:
                outcome, exists = self._ensure_membership_unlocked(trajectory_path)
                if not exists:
                    self._write_history_completeness_unlocked(outcome)
                return outcome

    @staticmethod
    def _trial_id(trial):
        if isinstance(trial, dict):
            return _text(trial.get("candidate_id") or trial.get("id") or trial.get("proposal_id"), "")
        return _text(getattr(trial, "candidate_id", None) or getattr(trial, "id", None) or getattr(trial, "proposal_id", None), "")

    @staticmethod
    def _value(trial, key, default=None):
        if isinstance(trial, dict):
            return trial.get(key, default)
        return getattr(trial, key, default)

    def record(self, trial, phase, *, outcome=None, reason=None, reason_code=None,
               stage=None, timestamp=None, reward=None, settlement=None,
               delegation=None):
        if phase not in PHASES:
            raise ValueError(f"未知 trial phase: {phase}")
        candidate_id = self._value(trial, "candidate_id")
        if candidate_id in (None, ""):
            recovery_terminal = phase in {"completed", "simulation_settled"} and str(
                outcome or self._value(trial, "status") or ""
            ).upper() in {"SKIPPED_STALE", "SKIPPED_UNKNOWN"}
            candidate_id = None if recovery_terminal else candidate_identity(
                trial, round_no=self._value(trial, "round")
            )
        trial_id = self._trial_id(trial) or candidate_id
        expression = _text(self._value(trial, "expression", ""), "")
        stored_fingerprint = self._value(trial, "submission_fingerprint", "")
        fingerprint = _text(
            stored_fingerprint
            or hashlib.sha256(canonical_expression(expression).encode()).hexdigest(),
            "",
        )
        fingerprint_source = (
            "submission_fingerprint" if stored_fingerprint not in (None, "")
            else "legacy_derived"
        )
        state = _text(self._value(trial, "status"), "UNKNOWN")
        stable_settlement = (settlement or {}).get("settlement_id") if phase == "research_outcome_settled" else None
        selection_identity = self._value(trial, "selection_identity") if phase == "optimization_selection" else None
        identity = (f"settlement|{stable_settlement}" if stable_settlement else
                    selection_identity or
                    f"{candidate_id}|{self._value(trial, 'proposal_id') or ''}|{phase}|{state}|{outcome or ''}|{reason_code or ''}|{reason or ''}|{reward}")
        event_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        fields = self._value(trial, "fields_used", [])
        if not isinstance(fields, (list, tuple)):
            fields = []
        row = {
            "schema_version": self.SCHEMA_VERSION,
            "created_by_version": CREATED_BY_VERSION,
            "event_id": event_id,
            "trial_id": trial_id,
            "experiment_id": self._value(trial, "id"),
            "candidate_id": candidate_id,
            "proposal_id": self._value(trial, "proposal_id"),
            "phase": phase,
            "event_type": phase,
            "selection_identity": selection_identity,
            "optimization_decision_id": self._value(trial, "optimization_decision_id"),
            "optimization_candidate_emitted": self._value(trial, "optimization_candidate_emitted"),
            "optimization_decision": self._value(trial, "optimization_decision"),
            "outcome": outcome or state,
            "reason": reason,
            "reason_code": reason_code,
            "stage": stage,
            "status": state,
            "sharpe": self._value(self._value(trial, "metrics", {}) or {}, "sharpe"),
            "fitness": self._value(self._value(trial, "metrics", {}) or {}, "fitness"),
            "round": self._value(trial, "round"),
            "expression_fingerprint": fingerprint,
            "execution_fingerprint_source": fingerprint_source,
            "template_family": self._value(trial, "template_family") or "unknown",
            "research_role": self._value(trial, "research_role") or "EXPLORE",
            "experiment_stage": self._value(trial, "experiment_stage") or "BASELINE",
            "lineage_id": self._value(trial, "lineage_id") or "unknown",
            "template_id": self._value(trial, "template_id") or "unknown",
            "dataset_family": self._value(trial, "dataset_family") or self._value(trial, "datasets") or "unknown",
            "structural_fingerprint": structural_fingerprint(expression, fields),
            "template_mode": self._value(trial, "template_mode") or "LEGACY",
            "template_branch_of": self._value(trial, "template_branch_of") or "LEGACY",
            "operator_role": self._value(trial, "operator_role") or "UNKNOWN",
            "operator_realization_fingerprint": self._value(
                trial, "operator_realization_fingerprint"
            ) or "UNKNOWN",
            "operator_realization": self._operator_realization(trial),
            "fields": sorted({_text(field) for field in fields if field is not None}),
            "alpha_id": self._value(trial, "alpha_id"),
            "reward": reward if reward is not None else self._value(trial, "reward") or (
                (self._value(trial, "search_outcome") or {}).get("reward")
                if isinstance(self._value(trial, "search_outcome"), dict) else None
            ),
            "search_outcome": self._value(trial, "search_outcome"),
            "settlement": settlement,
            "recorded_at": timestamp if timestamp is not None else time.time(),
        }
        if not self.path or not self.persist:
            with self._append_lock:
                self._ensure_membership_unlocked(None)
                if self._membership_contains_unlocked(row["event_id"]):
                    return False
                self._events.append(row)
                self._membership_db.execute(
                    "INSERT INTO event_ids(event_id) VALUES (?)", (str(row["event_id"]),)
                )
                self._membership_db.commit()
                return True
        with self._durable_scope("trial-ledger-record", delegation=delegation):
            with self._append_lock:
                if self.trajectory_path:
                    outcome, exists = self._ensure_membership_unlocked(
                        self.trajectory_path
                    )
                else:
                    outcome, exists = self._ensure_membership_unlocked(None)
                    exists = True
                written = self._append_jsonl_unique_unlocked(row)
                if self.trajectory_path and not exists:
                    self._write_history_completeness_unlocked(outcome)
                return written

    @classmethod
    def _operator_realization(cls, trial):
        mapping = cls._value(trial, "operator_role_mapping")
        if not isinstance(mapping, dict) or len(mapping) != 1:
            return "UNKNOWN"
        return _text(next(iter(mapping.values())), "UNKNOWN")

    def record_outcome_settled(self, trial, *, reward, reward_version="reward_v1",
                               base_quality=None, robustness=None,
                               statistical_decision=None, incremental_decision=None,
                               research_classification=None, incremental_evidence=None,
                               reward_quality="FINAL_EVIDENCE",
                               research_evidence_bundle=None,
                               timestamp=None):
        settled_at = timestamp if timestamp is not None else time.time()
        settlement = {
            "proposal_id": self._value(trial, "proposal_id"),
            "optimization_decision_id": self._value(trial, "optimization_decision_id"),
            "reward": reward,
            "reward_version": reward_version,
            "reward_quality": reward_quality,
            "base_quality": base_quality,
            "robustness": robustness,
            "statistical_decision": statistical_decision,
            "incremental_decision": incremental_decision,
            "research_classification": research_classification,
            "incremental_evidence": incremental_evidence,
            "research_evidence_bundle": research_evidence_bundle,
            "settled_at": settled_at,
        }
        semantic = dict(settlement)
        semantic.pop("settled_at", None)
        settlement["settlement_id"] = hashlib.sha256(
            json.dumps(semantic, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
        ).hexdigest()
        return self.record(trial, "research_outcome_settled", outcome="SETTLED",
                           reason_code="FINAL", timestamp=settled_at, reward=reward,
                           settlement=settlement)

    def record_optimization_selection(self, decision, *, outcome=None, reason=None,
                                      emitted=None, timestamp=None):
        """Record one finalized non-Simulation optimization decision.

        The semantic identity intentionally excludes timestamps so retries of the
        same decision cannot inflate selection accounting.
        """
        payload = decision.as_dict() if hasattr(decision, "as_dict") else dict(decision or {})
        selection_identity = optimization_decision_identity(payload)
        trial = {
            "candidate_id": selection_identity,
            "proposal_id": selection_identity,
            "selection_identity": selection_identity,
            "optimization_decision_id": selection_identity,
            "optimization_candidate_emitted": emitted,
            "optimization_decision": str(payload.get("decision") or "STOP").upper(),
            "status": str(outcome or payload.get("decision") or "SELECTED").upper(),
            "round": payload.get("round"),
            "template_family": "optimization_selection",
            "research_role": "EXPLOIT",
            "experiment_stage": "OPTIMIZATION",
        }
        return self.record(trial, "optimization_selection", outcome=outcome or payload.get("decision") or "SELECTED",
                           reason=reason or payload.get("reason"),
                           reason_code="OPTIMIZATION_DECISION",
                           timestamp=timestamp)

    def proposal_execution_bindings(self):
        """Return exact proposal-to-execution bindings from experiment-backed rows.

        Candidate-only legacy rows may contain a derived expression hash, so they
        are deliberately excluded.  Rows carrying an Experiment id and its
        persisted submission fingerprint are the durable evidence required for a
        proposal-id rebind decision.
        """
        bindings = defaultdict(set)
        rows = self._events if not self.path or not self.persist else iter_jsonl_objects(self.path)
        for row in rows:
            proposal_id = row.get("proposal_id")
            experiment_id = row.get("experiment_id")
            fingerprint = row.get("expression_fingerprint")
            if proposal_id in (None, "") or experiment_id in (None, ""):
                continue
            if row.get("execution_fingerprint_source") != "submission_fingerprint":
                continue
            if not isinstance(fingerprint, str) or not fingerprint:
                continue
            bindings[str(proposal_id)].add(fingerprint)
        return {proposal_id: frozenset(values) for proposal_id, values in bindings.items()}

    def summarize(self):
        phase_counts = Counter()
        status_counts = Counter()
        groups = {key: defaultdict(Counter) for key in
                  ("template_family", "lineage_id", "template_id", "field",
                   "operator_role", "operator_realization", "operator_realization_group")}
        events = 0
        trial_ids = set()
        generated_trials = set()
        sharpe_count = 0
        sharpe_mean = 0.0
        sharpe_m2 = 0.0
        event_type_counts = Counter()
        rejected_candidates = set()
        submitted_trials = set()
        accepted_candidates = set()
        completed_trials = set()
        admitted_families = Counter()
        admitted_structures = Counter()
        structural_trials = set()
        family_trials = set()
        latest_by_trial = {}
        unique_candidates = set()
        unique_proposals = set()
        lifecycle_rows = defaultdict(list)
        settlement_rows = {}
        selection_ids = set()
        non_emitted_selection_ids = set()
        rows = self._events if not self.path or not self.persist else iter_jsonl_objects(self.path)
        history_completeness = None
        for row in rows:
            if row.get("phase") == "history_completeness":
                history_completeness = row.get("outcome") or row.get("status")
                continue
            events += 1
            is_selection = row.get("phase") == "optimization_selection"
            if is_selection:
                selection_id = row.get("selection_identity") or row.get("event_id")
                selection_ids.add(selection_id)
                emitted = row.get("optimization_candidate_emitted")
                if emitted is False or (emitted is None and str(row.get("outcome") or "").upper() in {
                    "STOP", "REROUTE", "REJECTED", "PRUNED", "NO_CANDIDATE",
                }):
                    non_emitted_selection_ids.add(selection_id)
            trial_id = row.get("trial_id")
            if trial_id and not is_selection:
                trial_ids.add(trial_id)
            if row.get("structural_fingerprint") and not is_selection:
                structural_trials.add(row.get("structural_fingerprint"))
            if not is_selection:
                family_trials.add((row.get("template_family", "unknown"), row.get("dataset_family", "unknown").__str__()))
            if row.get("phase") in {"generated", "candidate_generated"} and trial_id:
                generated_trials.add(trial_id)
            event_type_counts[row.get("event_type") or row.get("phase", "unknown")] += 1
            if row.get("phase") == "candidate_rejected" and row.get("candidate_id"):
                rejected_candidates.add(row.get("candidate_id"))
            if row.get("candidate_id") and not is_selection:
                unique_candidates.add(row.get("candidate_id"))
            if row.get("proposal_id") and not is_selection:
                unique_proposals.add(row.get("proposal_id"))
            if row.get("phase") == "preflight_accepted" and row.get("candidate_id"):
                accepted_candidates.add(row.get("candidate_id"))
            if row.get("phase") in {"submitted", "simulation_submitted", "simulation_settled"} and row.get("proposal_id"):
                submitted_trials.add(row.get("proposal_id"))
            if row.get("phase") == "candidate_admitted":
                admitted_families[str(row.get("template_family") or "unknown")] += 1
                admitted_structures[str(row.get("structural_fingerprint") or "unknown")] += 1
            if trial_id:
                latest_by_trial[trial_id] = row
            if row.get("phase") in SIMULATION_PHASES and row.get("proposal_id"):
                lifecycle_rows[str(row.get("proposal_id"))].append(row)
            if row.get("phase") == "research_outcome_settled" and row.get("proposal_id"):
                settlement_rows[str(row.get("proposal_id"))] = row
            if row.get("phase") in {"completed", "simulation_settled"}:
                if row.get("proposal_id") or trial_id:
                    completed_trials.add(row.get("proposal_id") or trial_id)
            if row.get("phase") == "completed":
                value = row.get("sharpe")
                try:
                    value = float(value)
                except (TypeError, ValueError):
                    value = None
                if value is not None:
                    sharpe_count += 1
                    delta = value - sharpe_mean
                    sharpe_mean += delta / sharpe_count
                    sharpe_m2 += delta * (value - sharpe_mean)
            phase_counts[row.get("phase", "unknown")] += 1
            status_counts[row.get("status", "UNKNOWN")] += 1
            for key in ("template_family", "lineage_id", "template_id"):
                groups[key][row.get(key, "unknown")][row.get("phase", "unknown")] += 1
            for key in ("operator_role", "operator_realization"):
                groups[key][row.get(key, "UNKNOWN")][row.get("phase", "unknown")] += 1
            realization_group = (
                f"{row.get('operator_role', 'UNKNOWN')}::"
                f"{row.get('operator_realization', 'UNKNOWN')}"
            )
            groups["operator_realization_group"][realization_group][
                row.get("phase", "unknown")
            ] += 1
            for field in row.get("fields") or []:
                groups["field"][field][row.get("phase", "unknown")] += 1
        lifecycle_proposals = self._lifecycle_proposals(lifecycle_rows)
        lifecycle_arm_counts = self._lifecycle_arm_counts(lifecycle_rows)
        lifecycle_identity_drift = Counter(
            code
            for proposal in lifecycle_proposals.values()
            for code in proposal.get("identity_drift") or ()
        )
        for proposal_id, settlement in settlement_rows.items():
            proposal = lifecycle_proposals.get(proposal_id)
            if not isinstance(proposal, dict):
                continue
            arm = proposal.get("arm")
            try:
                final_reward = float(settlement.get("reward"))
            except (TypeError, ValueError):
                continue
            previous_reward = float(proposal.get("reward") or 0.0)
            if arm in lifecycle_arm_counts:
                lifecycle_arm_counts[arm]["reward_sum"] += final_reward - previous_reward
                lifecycle_arm_counts[arm]["reward"] += final_reward - previous_reward
            proposal["reward"] = final_reward
            proposal["final_outcome"] = settlement.get("settlement")
        return {
            "schema_version": self.SCHEMA_VERSION,
            "history_completeness": history_completeness or (
                "COMPLETE_FROM_START" if not self.path or not self.persist else "UNKNOWN"
            ),
            "events": events,
            "trial_count": len(trial_ids),
            "generated_trials": len(generated_trials),
            "candidate_generated_count": len(generated_trials),
            "candidate_count": len(generated_trials),
            "selection_trial_count": len(generated_trials) + len(non_emitted_selection_ids),
            "optimization_selection_count": len(selection_ids),
            "non_emitted_optimization_selection_count": len(non_emitted_selection_ids),
            "candidate_rejected_count": len(rejected_candidates),
            "rejected_candidate_count": len(rejected_candidates),
            "preflight_accepted_count": len(accepted_candidates),
            "submitted_count": len(submitted_trials),
            "completed_count": len(completed_trials),
            "unique_candidate_count": len(unique_candidates),
            "unique_proposal_count": len(unique_proposals),
            "simulation_count": len(submitted_trials),
            "family_counts": dict(admitted_families),
            "structural_family_counts": dict(admitted_structures),
            "structural_trial_count": len(structural_trials),
            "effective_trial_count": max(1, len(structural_trials or family_trials or trial_ids)),
            "arm_counts": self._arm_counts(latest_by_trial),
            "lifecycle_arm_counts": lifecycle_arm_counts,
            "lifecycle_proposals": lifecycle_proposals,
            "lifecycle_identity_drift": dict(lifecycle_identity_drift),
            "settled_observation_count": len(settlement_rows),
            "settled_rewards": {
                proposal_id: row.get("reward")
                for proposal_id, row in settlement_rows.items()
            },
            "event_type_counts": dict(event_type_counts),
            "trial_sharpe_count": sharpe_count,
            "trial_sharpe_mean": sharpe_mean if sharpe_count else None,
            "trial_sharpe_std": (
                (sharpe_m2 / sharpe_count) ** 0.5 if sharpe_count > 1 else None
            ),
            "phase_counts": dict(phase_counts),
            "status_counts": dict(status_counts),
            "invariant": {
                "candidate_generated_ge_preflight_accepted_ge_submitted": (
                    len(generated_trials) >= len(accepted_candidates) >= len(submitted_trials)
                ),
                "note": "append-only recovery may temporarily leave accepted/submitted events incomplete",
            },
            "trial_counts": {
                key: {group: dict(counts) for group, counts in values.items()}
                for key, values in groups.items()
            },
            "operator_realization_accounting": {
                group: dict(counts)
                for group, counts in groups["operator_realization_group"].items()
            },
        }

    def summarize_cached(self, cache_path):
        """Use a bounded disposable summary projection keyed by ledger signature."""
        if not self.path or not self.persist:
            return self.summarize()
        try:
            stat = os.stat(self.path)
            signature = {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
        except OSError:
            signature = {"size": 0, "mtime_ns": 0}
        try:
            with open(cache_path, encoding="utf-8") as handle:
                cached = json.load(handle)
            if isinstance(cached, dict) and cached.get("source_signature") == signature:
                summary = cached.get("summary")
                if isinstance(summary, dict):
                    return summary
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass
        summary = self.summarize()
        atomic_write_json_if_changed(cache_path, {
            "schema_version": self.SCHEMA_VERSION,
            "created_by_version": CREATED_BY_VERSION,
            "source_signature": signature,
            "summary": summary,
        }, sort_keys=True)
        return summary

    @staticmethod
    def _arm_counts(latest_by_trial):
        result = defaultdict(lambda: {
            "admitted": 0, "submitted": 0, "evaluated": 0, "done": 0,
            "failed_research": 0, "failed_infra": 0, "skipped_local": 0,
            "skipped_stale": 0, "skipped_unknown": 0,
            "completed": 0, "pending": 0, "running": 0, "unknown": 0,
            "reserved": 0, "reward_sum": 0.0, "reward_count": 0, "reward": 0.0,
        })
        for row in latest_by_trial.values():
            # Candidate/search-attempt rows are not Simulation lifecycle
            # evidence.  A rejected candidate usually has status UNKNOWN;
            # counting it here would restore a phantom occupied slot.
            if row.get("phase") not in SIMULATION_PHASES:
                continue
            datasets = row.get("dataset_family") or "unknown-dataset"
            if isinstance(datasets, list):
                datasets = "+".join(sorted(str(item) for item in datasets))
            arm = f"{datasets}::{row.get('template_family') or 'unknown-mechanism'}"
            status = str(row.get("status") or "UNKNOWN").upper()
            if row.get("phase") in {"candidate_admitted", "preflight_accepted", "submitted", "completed"}:
                result[arm]["admitted"] += 1
            if row.get("phase") in {"submitted", "completed"}:
                result[arm]["submitted"] += 1
            if status == "DONE":
                result[arm]["completed"] += 1
                result[arm]["done"] += 1
                try:
                    reward = float(row.get("fitness"))
                    result[arm]["evaluated"] += 1
                    result[arm]["reward_count"] += 1
                    result[arm]["reward"] += reward
                    result[arm]["reward_sum"] += reward
                except (TypeError, ValueError):
                    pass
            elif status in {"PENDING", "SUBMITTING"}:
                result[arm]["pending"] += 1
            elif status == "RUNNING":
                result[arm]["running"] += 1
            elif status in {"UNKNOWN", "SUBMIT_UNKNOWN"}:
                result[arm]["unknown"] += 1
            elif status in {"SKIPPED_STALE", "SKIPPED_UNKNOWN", "SKIPPED_LOCAL"}:
                result[arm]["completed"] += 1
                if status == "SKIPPED_STALE":
                    result[arm]["skipped_stale"] += 1
                elif status == "SKIPPED_UNKNOWN":
                    result[arm]["skipped_unknown"] += 1
                else:
                    result[arm]["skipped_local"] += 1
            elif status == "FAILED":
                result[arm]["completed"] += 1
                if str(row.get("reason_code") or row.get("reason") or "").upper() in {"INFRA", "RATE_LIMIT", "AUTH", "TIMEOUT", "NETWORK", "HTTP"}:
                    result[arm]["failed_infra"] += 1
                elif str(row.get("reason_code") or row.get("reason") or "").upper() in {"RESEARCH", "FAIL", "QUALITY_GATE", "SYNTAX", "DATA"}:
                    result[arm]["failed_research"] += 1
        return dict(result)

    @staticmethod
    def _arm_for_row(row):
        datasets = row.get("dataset_family") or "unknown-dataset"
        if isinstance(datasets, list):
            datasets = "+".join(sorted(str(item) for item in datasets))
        return f"{datasets}::{row.get('template_family') or 'unknown-mechanism'}"

    @classmethod
    def _lifecycle_identity(cls, rows):
        """Reduce immutable lifecycle facts without trusting sparse terminal rows."""
        reference = None
        for row in rows:
            if row.get("phase") in {"simulation_committed", "simulation_submitted"}:
                if any(row.get(key) not in (None, "", [], {}) for key in (
                    "candidate_id", "expression_fingerprint", "dataset_family",
                    "template_family", "research_role",
                )):
                    reference = row
                    break
        reference = reference or next((row for row in rows if row), {})
        candidate_id = next(
            (row.get("candidate_id") for row in rows
             if row.get("candidate_id") not in (None, "")),
            None,
        )
        arm = cls._arm_for_row(reference)
        if arm == "unknown-dataset::unknown-mechanism":
            arm = next(
                (cls._arm_for_row(row) for row in rows
                 if row.get("dataset_family") not in (None, "", [], {})
                 or row.get("template_family") not in (None, "", "unknown")),
                arm,
            )
        drift = set()
        for row in rows:
            row_candidate = row.get("candidate_id")
            if candidate_id is not None and row_candidate not in (None, "", candidate_id):
                drift.add("LIFECYCLE_CANDIDATE_ID_DRIFT")
            row_has_arm = (
                row.get("dataset_family") not in (None, "", [], {})
                or row.get("template_family") not in (None, "", "unknown")
            )
            if row_has_arm and cls._arm_for_row(row) != arm:
                drift.add("LIFECYCLE_ARM_DRIFT")
        return {
            "candidate_id": candidate_id,
            "proposal_id": reference.get("proposal_id"),
            "experiment_id": next(
                (row.get("experiment_id") for row in rows
                 if row.get("experiment_id") not in (None, "")),
                None,
            ),
            "expression_fingerprint": next(
                (row.get("expression_fingerprint") for row in rows
                 if row.get("expression_fingerprint") not in (None, "")),
                None,
            ),
            "research_role": next(
                (row.get("research_role") for row in rows
                 if row.get("research_role") not in (None, "", "unknown")),
                "EXPLORE",
            ),
            "arm": arm,
            "drift": tuple(sorted(drift)),
        }

    @staticmethod
    def _lifecycle_status(rows):
        latest = rows[-1]
        phase = latest.get("phase")
        if phase == "simulation_settled":
            outcome = str(latest.get("outcome") or latest.get("status") or "").upper()
            if outcome in {
                "DONE", "FAILED", "SKIPPED", "SKIPPED_LOCAL",
                "SKIPPED_STALE", "SKIPPED_UNKNOWN",
            }:
                return outcome
        if phase == "simulation_submitted":
            outcome = str(latest.get("outcome") or "RUNNING").upper()
            if "UNKNOWN" in outcome:
                return "UNKNOWN"
            if outcome == "PENDING":
                return "PENDING"
            return "RUNNING"
        return "RESERVED"

    @classmethod
    def _lifecycle_proposals(cls, lifecycle_rows):
        result = {}
        for proposal_id, rows in lifecycle_rows.items():
            identity = cls._lifecycle_identity(rows)
            result[proposal_id] = {
                "arm": identity["arm"],
                "status": cls._lifecycle_status(rows),
                "committed": any(row.get("phase") == "simulation_committed" for row in rows),
                "research_role": identity["research_role"],
                "candidate_id": identity["candidate_id"],
                "experiment_id": identity["experiment_id"],
                "expression_fingerprint": identity["expression_fingerprint"],
                "identity_drift": identity["drift"],
                "reward": rows[-1].get("reward"),
            }
        return result

    @classmethod
    def _lifecycle_arm_counts(cls, lifecycle_rows):
        result = defaultdict(lambda: {
            "admitted": 0, "submitted": 0, "evaluated": 0, "done": 0,
            "failed_research": 0, "failed_infra": 0, "skipped_local": 0,
            "skipped_stale": 0, "skipped_unknown": 0,
            "completed": 0, "pending": 0, "running": 0, "unknown": 0,
            "reserved": 0, "reward_sum": 0.0, "reward_count": 0, "reward": 0.0,
        })
        for rows in lifecycle_rows.values():
            if not rows:
                continue
            identity = cls._lifecycle_identity(rows)
            state = result[identity["arm"]]
            state["admitted"] += 1
            state["submitted"] += int(any(
                row.get("phase") in {"simulation_submitted", "simulation_settled", "completed"}
                for row in rows
            ))
            status = cls._lifecycle_status(rows)
            if status == "RESERVED":
                state["reserved"] += 1
            elif status in {"RUNNING", "PENDING", "UNKNOWN"}:
                state[status.lower()] += 1
            elif status == "DONE":
                state["completed"] += 1
                state["done"] += 1
                try:
                    reward = float(rows[-1].get("reward"))
                    state["evaluated"] += 1
                    state["reward_count"] += 1
                except (TypeError, ValueError):
                    reward = None
                if reward is None:
                    continue
                state["reward"] += reward
                state["reward_sum"] += reward
            elif status in {"SKIPPED_LOCAL", "SKIPPED_STALE", "SKIPPED_UNKNOWN"}:
                state["completed"] += 1
                if status == "SKIPPED_STALE":
                    state["skipped_stale"] += 1
                elif status == "SKIPPED_UNKNOWN":
                    state["skipped_unknown"] += 1
                else:
                    state["skipped_local"] += 1
            elif status == "FAILED":
                state["completed"] += 1
                category = str(rows[-1].get("reason_code") or rows[-1].get("reason") or "").upper()
                if category in {"INFRA", "RATE_LIMIT", "AUTH", "TIMEOUT", "SUBMIT_UNKNOWN", "NETWORK", "HTTP"}:
                    state["failed_infra"] += 1
                elif category in {"RESEARCH", "FAIL", "QUALITY_GATE", "SYNTAX", "DATA"}:
                    state["failed_research"] += 1
        return dict(result)
