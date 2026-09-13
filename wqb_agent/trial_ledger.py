"""ROLE: CORE
AGENT_RELEVANCE: MEDIUM
PURPOSE: Preserve bounded append-only experiment lifecycle and trial counts.
READ WHEN: changing trial accounting or evidence settlement.
DO NOT USE FOR: making platform truth or research-direction decisions.

Append-only, bounded-memory trial lifecycle ledger."""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from collections import Counter, defaultdict
from contextlib import nullcontext

from .artifacts import (
    append_jsonl_if_unique,
    atomic_write_json_if_changed,
    iter_jsonl_objects,
)
from .expression import canonical_expression
from .identity import candidate_identity
from .locking import StateMutationDelegation, single_instance_scope
from .optimization_decision import optimization_decision_identity
from .schema import CREATED_BY_VERSION, TRIAL_LEDGER_VERSION
from .search_policy import structural_fingerprint

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

    def _durable_scope(self, operation, delegation=None):
        if not self.persist or not self.path:
            return nullcontext()
        state_dir = os.path.dirname(os.path.abspath(self.path))
        if delegation is not None:
            if not isinstance(delegation, StateMutationDelegation):
                raise TypeError("delegation must be a StateMutationDelegation")
            return delegation.authorization(state_dir)
        return single_instance_scope(state_dir, operation)

    def _history_completeness_unlocked(self, trajectory_path):
        existing = list(iter_jsonl_objects(self.path)) if os.path.exists(self.path) else []
        for row in existing:
            if row.get("phase") == "history_completeness":
                return str(row.get("outcome") or "UNKNOWN"), True
        trajectory_has_rows = False
        if trajectory_path and os.path.exists(trajectory_path):
            try:
                with open(trajectory_path, encoding="utf-8") as handle:
                    trajectory_has_rows = any(line.strip() for line in handle)
            except OSError:
                trajectory_has_rows = True
        outcome = "INCOMPLETE_LEGACY" if trajectory_has_rows or existing else "COMPLETE_FROM_START"
        return outcome, False

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
        append_jsonl_if_unique(self.path, row, ("event_id",), lock=self._append_lock)
        return outcome

    def initialize_history_completeness(self, trajectory_path):
        """Record the one-time completeness boundary in this ledger owner."""
        if not self.path or not self.persist:
            return "COMPLETE_FROM_START"
        with self._durable_scope("trial-ledger-history"):
            outcome, exists = self._history_completeness_unlocked(trajectory_path)
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
        candidate_id = self._value(trial, "candidate_id") or candidate_identity(trial, round_no=self._value(trial, "round"))
        trial_id = self._trial_id(trial) or candidate_id
        expression = _text(self._value(trial, "expression", ""), "")
        fingerprint = _text(
            self._value(trial, "submission_fingerprint", "")
            or hashlib.sha256(canonical_expression(expression).encode()).hexdigest(),
            "",
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
            "template_family": self._value(trial, "template_family") or "unknown",
            "research_role": self._value(trial, "research_role") or "EXPLORE",
            "experiment_stage": self._value(trial, "experiment_stage") or "BASELINE",
            "lineage_id": self._value(trial, "lineage_id") or "unknown",
            "template_id": self._value(trial, "template_id") or "unknown",
            "dataset_family": self._value(trial, "dataset_family") or self._value(trial, "datasets") or "unknown",
            "structural_fingerprint": structural_fingerprint(expression, fields),
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
            if any(item.get("event_id") == row["event_id"] for item in self._events):
                return False
            self._events.append(row)
            return True
        with self._durable_scope("trial-ledger-record", delegation=delegation):
            if self.trajectory_path:
                outcome, exists = self._history_completeness_unlocked(self.trajectory_path)
            else:
                outcome, exists = None, True
            written = append_jsonl_if_unique(
                self.path, row, ("event_id",), lock=self._append_lock
            )
            if self.trajectory_path and not exists:
                self._write_history_completeness_unlocked(outcome)
            return written

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

    def summarize(self):
        phase_counts = Counter()
        status_counts = Counter()
        groups = {key: defaultdict(Counter) for key in
                  ("template_family", "lineage_id", "template_id", "field")}
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
            for field in row.get("fields") or []:
                groups["field"][field][row.get("phase", "unknown")] += 1
        lifecycle_proposals = self._lifecycle_proposals(lifecycle_rows)
        lifecycle_arm_counts = self._lifecycle_arm_counts(lifecycle_rows)
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
                result[arm]["evaluated"] += 1
                result[arm]["reward_count"] += 1
                try:
                    reward = float(row.get("fitness", 0.0) or 0.0)
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
            elif status == "FAILED":
                result[arm]["completed"] += 1
                if str(row.get("reason_code") or row.get("reason") or "").upper() in {"INFRA", "RATE_LIMIT", "AUTH", "TIMEOUT"}:
                    result[arm]["failed_infra"] += 1
                else:
                    result[arm]["failed_research"] += 1
        return dict(result)

    @staticmethod
    def _arm_for_row(row):
        datasets = row.get("dataset_family") or "unknown-dataset"
        if isinstance(datasets, list):
            datasets = "+".join(sorted(str(item) for item in datasets))
        return f"{datasets}::{row.get('template_family') or 'unknown-mechanism'}"

    @staticmethod
    def _lifecycle_status(rows):
        latest = rows[-1]
        phase = latest.get("phase")
        if phase == "simulation_settled":
            outcome = str(latest.get("outcome") or latest.get("status") or "").upper()
            if outcome in {"DONE", "FAILED", "SKIPPED", "SKIPPED_LOCAL"}:
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
            result[proposal_id] = {
                "arm": cls._arm_for_row(rows[-1]),
                "status": cls._lifecycle_status(rows),
                "committed": any(row.get("phase") == "simulation_committed" for row in rows),
                "research_role": rows[-1].get("research_role") or "EXPLORE",
                "reward": rows[-1].get("reward"),
            }
        return result

    @classmethod
    def _lifecycle_arm_counts(cls, lifecycle_rows):
        result = defaultdict(lambda: {
            "admitted": 0, "submitted": 0, "evaluated": 0, "done": 0,
            "failed_research": 0, "failed_infra": 0, "skipped_local": 0,
            "completed": 0, "pending": 0, "running": 0, "unknown": 0,
            "reserved": 0, "reward_sum": 0.0, "reward_count": 0, "reward": 0.0,
        })
        for rows in lifecycle_rows.values():
            if not rows:
                continue
            state = result[cls._arm_for_row(rows[-1])]
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
                state["evaluated"] += 1
                state["reward_count"] += 1
                try:
                    reward = float(rows[-1].get("reward"))
                except (TypeError, ValueError):
                    reward = 0.0
                state["reward"] += reward
                state["reward_sum"] += reward
            elif status == "SKIPPED_LOCAL":
                state["completed"] += 1
                state["skipped_local"] += 1
            elif status == "FAILED":
                state["completed"] += 1
                category = str(rows[-1].get("reason_code") or rows[-1].get("reason") or "").upper()
                if category in {"INFRA", "RATE_LIMIT", "AUTH", "TIMEOUT", "SUBMIT_UNKNOWN"}:
                    state["failed_infra"] += 1
                else:
                    state["failed_research"] += 1
        return dict(result)
