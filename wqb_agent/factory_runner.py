"""ROLE: LEGACY
AGENT_RELEVANCE: LOW
PURPOSE: Preserve compatibility for the existing bounded factory lifecycle.
READ WHEN: a factory command or legacy session must be maintained.
DO NOT USE FOR: the default agent-facing research model or new experiments.

Unattended, bounded Alpha-factory orchestration.

The runner is intentionally thin: Agent remains the only owner of BRAIN
submission, checkpoint recovery, reflection, and research state.  This module
adds a session envelope and a deterministic template proposal adapter, so a
day-long process reuses the same canonical artifacts instead of creating one
control file per pass.
"""

import hashlib
import json
import os
import re
import time
import uuid
from contextlib import redirect_stdout
from copy import deepcopy

from .artifacts import atomic_write_json_if_changed
from .diversity import field_concept_keys, semantic_mechanism_key
from .expression import canonical_expression
from .factory_blocker import (
    blocker_projection,
    blocker_signature,
    control_probe,
    safe_control_token,
    safe_nonnegative_count,
)
from .factory_quota import (
    carry_forward_quota,
    prepare_quota,
    quota_release,
    quota_remaining,
    quota_reserve,
)
from .factory_route import route_decision
from .factory_session import (
    read_session as read_session_projection,
)
from .factory_session import (
    request_stop as request_stop_projection,
)
from .factory_session import (
    status_view as status_view_projection,
)
from .locking import OwnerBusyError, single_instance_scope
from .proposal_contract import (
    FACTORY_BATCH_SIZE,
    factory_batch_stats,
    targeted_batch_state,
    validate_factory_batch,
)
from .schema import CHECKPOINT_VERSION, CREATED_BY_VERSION
from .weekly_quota import QuotaExceeded, WeeklySimulationQuota


class _DiscardWriter:
    """Drop quiet factory output without retaining a round-sized buffer."""

    def write(self, value):
        return len(value)

    def flush(self):
        return None


_DISCARD_STDOUT = _DiscardWriter()


class FactoryControlStateError(ValueError):
    """A runtime control token was not declared in the closed vocabulary."""


class AIFactoryRunner:
    """Run suggestion -> proposal assembly -> production execution repeatedly."""

    SESSION_FILE = "factory_session.json"
    CHECKPOINT_CACHE_MAX = 512
    BLOCKER_RECHECK_SEC = 3600.0
    _ROUTE_DIMENSIONS = {
        "candidate_expression": "candidate_expression_fingerprints",
        "semantic_mechanism": "semantic_mechanism_fingerprints",
        "structural_family": "structural_family_fingerprints",
        "field_concept": "field_concept_fingerprints",
        "relationship": "relationship_fingerprints",
        "dataset_route": "dataset_route",
        "research_question": "research_question_fingerprints",
    }
    _SESSION_STATUSES = {
        "RUNNING", "STOPPED", "RECONCILE_REQUIRED", "SIMULATION_BUDGET_CAP",
        "ROUND_CAP", "DEADLINE", "OPERATOR_CAPABILITY_BLOCKED",
    }
    _SESSION_ACTIONS = {
        "START", "ADVANCE_ROUTE_EPISODE", "STOP_REQUESTED", "BLOCKER_COOLDOWN",
        "STOP_FEASIBILITY_BLOCKER", "STOP_PREFLIGHT_BLOCKER",
        "STOP_OPERATOR_CAPABILITY", "STOP_BUDGET_SHORTAGE",
        "INVALID_SIMULATION_QUOTA", "ZERO_DURATION", "WAIT_AGENT_DECISION",
        "STORAGE_RECONCILE_REQUIRED", "ORPHANED_PROPOSALS_BUDGET_BLOCKED",
        "RECOVER_PROPOSALS", "RECOVER_PROPOSALS_PENDING", "RECOVERED_PROPOSALS",
        "EXECUTION_RECONCILE_REQUIRED", "RECOVERY_BUDGET_BLOCKED",
        "RECOVER_CHECKPOINT", "CHECKPOINT_BLOCKED", "RECOVERED_CHECKPOINT",
        "ROUND_CAP", "BUDGET_BLOCKED", "SUGGEST", "WAIT_NO_SUGGESTION",
        "WAIT_INVALID_SUGGESTION", "FACTORY_BATCH_BUDGET_BLOCKED",
        "STOP_MECHANISM_ROUTE", "REROUTE_FACTORY_FEASIBILITY",
        "STOP_BUDGET_SHORTAGE", "REROUTE_BUDGET_SHORTAGE", "WAIT_FACTORY_BATCH",
        "PROPOSALS_WRITE_ERROR", "WAIT_NO_VALID_PROPOSAL", "RUN_PROPOSALS",
        "DAILY_OR_WEEKLY_BUDGET_BLOCKED", "RUN_PROPOSALS_PENDING",
        "WAIT_RUN_PROPOSALS", "ROUND_COMPLETE", "ASSEMBLE_ERROR",
        "RUN_PROPOSALS_ERROR", "RECOVER_PROPOSALS_ERROR",
        "RECOVER_CHECKPOINT_ERROR", "SUGGEST_ERROR",
        "ASSEMBLE_RECONCILE_REQUIRED", "RUN_PROPOSALS_RECONCILE_REQUIRED",
        "RECOVER_PROPOSALS_RECONCILE_REQUIRED",
        "RECOVER_CHECKPOINT_RECONCILE_REQUIRED",
        "SUGGEST_RECONCILE_REQUIRED",
    }
    _RESULT_STATUSES = {
        "LOCAL_OWNER_BUSY", "RECONCILE_REQUIRED", "INVALID_SIMULATION_QUOTA",
        "INVALID_SESSION", "BLOCKED_RECHECK_NOT_DUE", "BLOCKER_UNCHANGED",
        "ORPHANED_PROPOSALS_BUDGET_BLOCKED", "RECOVERY_BUDGET_BLOCKED",
        "INVALID_RESEARCH_SPACE", "OPERATOR_CAPABILITY_UNKNOWN",
        "FACTORY_BATCH_BUDGET_BLOCKED", "FACTORY_FEASIBILITY_CHECK_BLOCKED",
        "FACTORY_BUDGET_SHORTAGE", "FACTORY_BATCH_NOT_READY",
        "DAILY_OR_WEEKLY_BUDGET_BLOCKED", "RUN_PROPOSALS_NOT_EXECUTED",
        "RETRYING", "ADVANCE_ROUTE_EPISODE",
    }
    _BLOCKER_STOP_ACTIONS = {
        "FEASIBILITY": "STOP_FEASIBILITY_BLOCKER",
        "PREFLIGHT": "STOP_PREFLIGHT_BLOCKER",
        "OPERATOR_CAPABILITY": "STOP_OPERATOR_CAPABILITY",
        "BUDGET_SHORTAGE": "STOP_BUDGET_SHORTAGE",
    }
    _TERMINAL_RECONCILE_ACTIONS = {
        "ASSEMBLE": "ASSEMBLE_RECONCILE_REQUIRED",
        "RUN_PROPOSALS": "RUN_PROPOSALS_RECONCILE_REQUIRED",
        "RECOVER_PROPOSALS": "RECOVER_PROPOSALS_RECONCILE_REQUIRED",
        "RECOVER_CHECKPOINT": "RECOVER_CHECKPOINT_RECONCILE_REQUIRED",
        "SUGGEST": "SUGGEST_RECONCILE_REQUIRED",
    }
    _CONTROL_TOKENS = {
        "UNKNOWN", "NONE", "READY", "RUNNING", "STOPPED", "DEADLINE",
        "ROUND_CAP", "START", "ZERO_DURATION", "RECONCILE_REQUIRED",
        "INVALID_SESSION", "INVALID_SIMULATION_QUOTA", "RETRYING",
        "PREFLIGHT", "FEASIBILITY", "BUDGET_SHORTAGE", "OPERATOR_CAPABILITY",
        "PREFLIGHT_BLOCKED", "FACTORY_BATCH_BLOCKED", "FACTORY_BATCH_NOT_READY",
        "FACTORY_FEASIBILITY_CHECK_BLOCKED", "FACTORY_BUDGET_SHORTAGE",
        "RUN_PROPOSALS_NOT_EXECUTED", "BLOCKER_UNCHANGED", "BLOCKED_RECHECK_NOT_DUE",
        "SIMULATION_BUDGET_CAP", "OPERATOR_CAPABILITY_BLOCKED",
        "OPERATOR_CAPABILITY_UNKNOWN", "STOP_PREFLIGHT_BLOCKER",
        "FACTORY_BATCH_BUDGET_BLOCKED", "DAILY_OR_WEEKLY_BUDGET_BLOCKED",
        "ORPHANED_PROPOSALS_BUDGET_BLOCKED", "RECOVERY_BUDGET_BLOCKED",
        "BUDGET_BLOCKED", "STORAGE_RECONCILE_REQUIRED", "CHECKPOINT_BLOCKED",
        "EXECUTION_RECONCILE_REQUIRED", "STOP_REQUESTED", "WAIT_AGENT_DECISION",
        "WAIT_NO_SUGGESTION", "WAIT_INVALID_SUGGESTION", "WAIT_FACTORY_BATCH",
        "WAIT_NO_VALID_PROPOSAL", "WAIT_RUN_PROPOSALS", "RUN_PROPOSALS_PENDING",
        "RECOVER_PROPOSALS_PENDING",
        "RECOVER_CHECKPOINT", "RECOVERED_PROPOSALS", "RECOVERED_CHECKPOINT",
        "BLOCKER_COOLDOWN", "STOP_OPERATOR_CAPABILITY", "PROPOSALS_WRITE_ERROR",
        "ASSEMBLE_ERROR", "RUN_PROPOSALS_ERROR", "RECOVER_PROPOSALS_ERROR",
        "EXECUTION_RECONCILE_REQUIRED", "FACTORY_BATCH_BUDGET_BLOCKED",
        "TARGETED_OPTIMIZATION_PENDING", "TARGETED_BATCH_INVALID", "TARGETED_BATCH_WRITTEN",
        "TARGETED_BATCH_REJECTED", "NO_TARGETED_PROPOSAL", "TARGETED_BATCH_UNCHANGED",
        "TARGETED_BATCH_CONFLICT", "TARGETED_BATCH_RECOVERY_BLOCKED",
        "ROUND_COMPLETE", "STOP_MECHANISM_ROUTE", "REROUTE_FACTORY_FEASIBILITY",
        "STOP_BUDGET_SHORTAGE", "REROUTE_BUDGET_SHORTAGE", "ADVANCE_ROUTE_EPISODE",
        "REROUTE", "STOP", "CANDIDATE_CHANGE_ONLY", "RESEARCH_INFORMATION_CHANGE",
        "NONE", "CANDIDATE_CHANGE", "SEMANTIC_CHANGE", "RELATIONSHIP_CHANGE",
        "DATASET_CHANGE", "QUESTION_CHANGE", "FAILURE_TAXONOMY", "FREQUENCY_EVIDENCE",
        "CAPABILITY_STATUS", "BUDGET_SHORTAGE_REASON", "BUDGET_SHORTAGE_COUNT",
        "CURRENT_BUNDLE", "SAME_DATASET_RELATIONSHIP", "SAME_DATASET_NEW_MECHANISM",
        "NEW_DATASET_COMPOSITION", "REDISCOVERY",
        "FIELD_SEMANTICS_INSUFFICIENT", "FREQUENCY_EVIDENCE_INSUFFICIENT",
        "FREQUENCY_INCOMPATIBLE", "RELATIONSHIP_REVIEW", "MECHANISM_FAMILY_EXHAUSTED",
        "TEMPLATE_INCOMPATIBLE", "CROSS_DATASET_FEASIBILITY_ZERO",
        "PREFLIGHT_REJECTED", "DUPLICATE_LOCAL", "DIVERSITY_REJECTED",
        "INVALID_SETTINGS", "BATCH_CAP", "ALREADY_SIMULATED", "SUCCESS",
        "FAIL", "FAILED", "SUSPICIOUS_HIGH_SIGNAL", "PASS", "BLOCK",
    } | _SESSION_STATUSES | _SESSION_ACTIONS | _RESULT_STATUSES | set(
        _BLOCKER_STOP_ACTIONS.values()
    )

    @staticmethod
    def _safe_control_token(value, default="UNKNOWN"):
        return safe_control_token(value, AIFactoryRunner._CONTROL_TOKENS, default)

    @classmethod
    def _validate_internal_control_state(cls, session):
        """Reject new runtime state outside the declared durable vocabulary."""
        if not isinstance(session, dict):
            raise FactoryControlStateError("FACTORY_CONTROL_STATE_UNDECLARED: session")
        for field, vocabulary in (("status", cls._SESSION_STATUSES), ("last_action", cls._SESSION_ACTIONS)):
            value = session.get(field)
            if value is not None and (not isinstance(value, str) or value not in vocabulary):
                raise FactoryControlStateError(f"FACTORY_CONTROL_STATE_UNDECLARED: {field}")

    @classmethod
    def _project_runtime_return(cls, value):
        return value if not isinstance(value, dict) else cls._control_plane_session(value)

    @staticmethod
    def _safe_nonnegative_count(value):
        return safe_nonnegative_count(value)

    @staticmethod
    def _safe_timestamp(value):
        try:
            return float(value)
        except (TypeError, ValueError, OverflowError):
            return None

    @classmethod
    def _blocker_signature(cls, kind, probe):
        return blocker_signature(kind, probe, control_tokens=cls._CONTROL_TOKENS)

    @classmethod
    def _blocker_projection(cls, kind, probe, *, now, previous=None, recheck_sec=None):
        interval = cls.BLOCKER_RECHECK_SEC if recheck_sec is None else float(recheck_sec)
        return blocker_projection(kind, probe, now=now, previous=previous, recheck_sec=interval, control_tokens=cls._CONTROL_TOKENS)

    @classmethod
    def _control_probe(cls, probe, stats=None):
        return control_probe(probe, allowed=cls._CONTROL_TOKENS, stats=stats)

    def _remember_blocker(self, session, kind, probe, now):
        config = getattr(self.agent, "factory_config", {}) or {}
        session["blocker"] = self._blocker_projection(
            kind, probe, now=now, previous=session.get("blocker"),
            recheck_sec=config.get("blocker_recheck_sec", self.BLOCKER_RECHECK_SEC),
        )

    @staticmethod
    def _begin_route_episode(session, round_no):
        if session.get("route_episode_round") == round_no:
            return
        session["route_episode_round"] = round_no
        session["route_attempt"] = 0
        session["no_gain_attempts"] = 0
        session["last_feasibility_check"] = None
        session["last_budget_probe"] = None
        session["last_preflight_probe"] = None

    @staticmethod
    def _finish_route_episode(session, round_no):
        session["route_episode_round"] = round_no
        session["route_attempt"] = 0
        session["no_gain_attempts"] = 0
        session["last_feasibility_check"] = None
        session["last_budget_probe"] = None
        session["last_preflight_probe"] = None

    @classmethod
    def _advance_route_episode(cls, session):
        round_no = session.get("last_round")
        cls._begin_route_episode(session, round_no)
        cls._finish_route_episode(session, round_no)
        session["last_action"] = "ADVANCE_ROUTE_EPISODE"
        session["last_result"] = {
            "round_no": round_no,
            "status": "ADVANCE_ROUTE_EPISODE",
            "probe_offset": cls._safe_nonnegative_count(session.get("probe_offset")),
        }

    @staticmethod
    def route_decision(previous_probe, current_probe, *, route_attempt,
                       no_gain_attempts, max_route_attempts=3,
                       max_no_gain_attempts=2):
        """Delegate pure route policy while preserving the public facade."""
        return route_decision(
            previous_probe,
            current_probe,
            route_attempt=route_attempt,
            no_gain_attempts=no_gain_attempts,
            max_route_attempts=max_route_attempts,
            max_no_gain_attempts=max_no_gain_attempts,
        )
    @staticmethod
    def _selection_probe(proposals, feasibility, budget_audit):
        """Project the actually selected batch into the route control plane."""
        probe = dict(feasibility) if isinstance(feasibility, dict) else {}
        items = [item for item in (proposals or []) if isinstance(item, dict)]
        probe["candidate_expression_fingerprints"] = sorted({
            canonical_expression(item.get("expression"))
            for item in items
            if canonical_expression(item.get("expression"))
        })
        probe["semantic_mechanism_fingerprints"] = sorted({
            key for key in (semantic_mechanism_key(item) for item in items)
            if key != "UNKNOWN"
        })
        probe["structural_family_fingerprints"] = sorted({
            str(item.get("template_family") or item.get("template_id"))
            for item in items
            if item.get("template_family") or item.get("template_id")
        })
        probe["field_concept_fingerprints"] = sorted({
            concept for item in items for concept in field_concept_keys(item)
            if concept != "unknown"
        })
        probe["dataset_route"] = sorted({
            str(dataset)
            for item in items
            for dataset in (item.get("datasets") or [])
            if dataset is not None and str(dataset).strip()
        })
        def question_key(item):
            if item.get("template_mode") == "PARTIAL_OPERATOR":
                return item.get("operator_contrast_question_key")
            return item.get("experiment_question") or item.get("research_question")
        probe["research_question_fingerprints"] = sorted({
            str(question_key(item)).strip().lower()
            for item in items
            if question_key(item)
        })
        probe["budget_shortage_count"] = int(
            budget_audit.get("shortage_count", 0)
        ) if isinstance(budget_audit, dict) else 0
        probe["budget_shortage_reason"] = (
            budget_audit.get("shortage_reason")
            if isinstance(budget_audit, dict) else None
        )
        return probe

    @staticmethod
    def _route_set_digest(values, *, session_id, dimension):
        normalized = sorted({str(value) for value in (values or ()) if str(value).strip()})
        canonical = json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(
            f"{session_id}:{dimension}:{canonical}".encode()
        ).hexdigest()

    @classmethod
    def _route_probe_projection(cls, probe, *, session_id):
        """Persist only bounded counts and session-bound route set digests."""
        source = probe if isinstance(probe, dict) else {}
        projection = cls._control_probe(source)
        for dimension, source_key in cls._ROUTE_DIMENSIONS.items():
            digest_key = f"{dimension}_set_digest"
            count_key = f"{dimension}_count"
            if source_key not in source and digest_key in source:
                projection[count_key] = cls._safe_nonnegative_count(source.get(count_key))
                digest = str(source.get(digest_key, "")).lower()
                projection[digest_key] = (
                    digest
                    if re.fullmatch(r"[0-9a-f]{64}", digest)
                    else cls._route_set_digest(
                        (), session_id=session_id, dimension=dimension
                    )
                )
                continue
            values = source.get(source_key)
            normalized = sorted({str(value) for value in (values or ()) if str(value).strip()})
            projection[count_key] = len(normalized)
            projection[digest_key] = cls._route_set_digest(
                normalized, session_id=session_id, dimension=dimension
            )
        for key in ("eligible_count", "selected_count", "shortage_count"):
            if key in source:
                projection[key] = cls._safe_nonnegative_count(source.get(key))
        if "budget_shortage_count" in source:
            projection["shortage_count"] = cls._safe_nonnegative_count(
                source.get("budget_shortage_count")
            )
        if "budget_shortage_reason" in source:
            projection["shortage_reason"] = cls._safe_control_token(
                source.get("budget_shortage_reason"), default="UNKNOWN"
            )
        return projection

    @classmethod
    def read_session(cls, state_dir):
        """Read the single factory envelope without constructing an Agent."""
        return read_session_projection(state_dir, cls.SESSION_FILE)

    @classmethod
    def request_stop(cls, state_dir):
        """Set a durable stop request in the canonical session envelope."""
        return request_stop_projection(
            state_dir,
            session_file=cls.SESSION_FILE,
            validate=cls._validate_internal_control_state,
            project=cls._control_plane_session,
        )

    @classmethod
    def status_view(cls, state_dir):
        """Return only the stable, decision-useful session fields."""
        return status_view_projection(
            state_dir,
            session_file=cls.SESSION_FILE,
            project=cls._control_plane_session,
        )

    def __init__(self, agent, *, factory=None, clock=None, sleeper=None, quiet=True):
        self.agent = agent
        self.factory = factory or agent.alpha_factory
        self._clock = clock or time.time
        self._sleep = sleeper or time.sleep
        self.quiet = bool(quiet)
        self.state_dir = agent.state_dir
        self.session_path = os.path.join(self.state_dir, self.SESSION_FILE)
        self.proposals_path = os.path.join(self.state_dir, "proposals.json")

    def _prepare_quota(self, session, weekly_cap, daily_cap):
        """Attach the local quota control metadata to a factory session."""
        quota = WeeklySimulationQuota(
            weekly_cap=weekly_cap, daily_cap=daily_cap, clock=self._clock
        )
        return prepare_quota(session, quota)

    @staticmethod
    def _carry_forward_quota(previous_session, quota):
        """Carry the canonical quota across a renewed session envelope."""
        return carry_forward_quota(previous_session, quota)

    @staticmethod
    def _quota_remaining(session, quota):
        return quota_remaining(session, quota)

    @staticmethod
    def _quota_reserve(session, quota, count):
        quota_reserve(session, quota, count)

    @staticmethod
    def _quota_release(session, quota, count):
        quota_release(session, quota, count)

    def run(self, duration_sec=86400, max_rounds=0, idle_sleep_sec=30,
            max_simulations=240, daily_simulation_cap=None,
            weekly_simulation_cap=None):
        try:
            with single_instance_scope(self.state_dir, operation="factory-run"):
                result = self._run_locked(
                    duration_sec=duration_sec,
                    max_rounds=max_rounds,
                    idle_sleep_sec=idle_sleep_sec,
                    max_simulations=max_simulations,
                    daily_simulation_cap=daily_simulation_cap,
                    weekly_simulation_cap=weekly_simulation_cap,
                )
                return self._project_runtime_return(result)
        except OwnerBusyError:
            return {
                "schema_version": CHECKPOINT_VERSION,
                "created_by_version": CREATED_BY_VERSION,
                "status": "LOCAL_OWNER_BUSY",
                "last_action": "LOCAL_OWNER_BUSY",
                "last_result": {"status": "LOCAL_OWNER_BUSY"},
            }

    def _run_locked(self, duration_sec=86400, max_rounds=0, idle_sleep_sec=30,
                    max_simulations=240, daily_simulation_cap=None,
                    weekly_simulation_cap=None):
        # RESEARCH_POLICY:
        # This compatibility loop is not the default agent-facing model;
        # safety boundaries inside Agent and Client remain mechanism.
        """Run until the deadline or round cap; return a compact session view.

        ``max_rounds=0`` means duration-only.  A non-positive duration is a
        safe dry start and performs no discovery or POST.
        """
        try:
            duration = max(0.0, float(duration_sec))
        except (TypeError, ValueError):
            duration = 86400.0
        try:
            round_cap = max(0, int(max_rounds))
        except (TypeError, ValueError):
            round_cap = 0
        try:
            idle = max(1.0, min(60.0, float(idle_sleep_sec)))
        except (TypeError, ValueError):
            idle = 30.0
        try:
            simulation_cap = max(0, int(max_simulations))
        except (TypeError, ValueError):
            simulation_cap = 240
        try:
            weekly_cap = max(
                0,
                int(simulation_cap if weekly_simulation_cap is None
                    else weekly_simulation_cap),
            )
            daily_cap = max(
                0,
                int(weekly_cap if daily_simulation_cap is None
                    else daily_simulation_cap),
            )
        except (TypeError, ValueError):
            weekly_cap, daily_cap = simulation_cap, simulation_cap
        if daily_cap > weekly_cap:
            return {
                "schema_version": CHECKPOINT_VERSION,
                "created_by_version": CREATED_BY_VERSION,
                "status": "RECONCILE_REQUIRED",
                "last_action": "INVALID_SIMULATION_QUOTA",
                "last_result": {
                    "status": "INVALID_SIMULATION_QUOTA",
                    "daily_cap": daily_cap,
                    "weekly_cap": weekly_cap,
                },
            }
        try:
            quota = WeeklySimulationQuota(
                weekly_cap=weekly_cap, daily_cap=daily_cap, clock=self._clock
            )
        except ValueError:
            return {
                "schema_version": CHECKPOINT_VERSION,
                "created_by_version": CREATED_BY_VERSION,
                "status": "RECONCILE_REQUIRED",
                "last_action": "INVALID_SIMULATION_QUOTA",
                "last_result": {"status": "INVALID_SIMULATION_QUOTA"},
            }
        now = self._clock()
        session = self._load_session()
        # A present but malformed control envelope is evidence, not an empty
        # workspace. Preserve it for manual repair instead of overwriting it
        # with a fresh session that could lose the last execution boundary.
        if session is None and os.path.exists(self.session_path):
            return {
                "schema_version": CHECKPOINT_VERSION,
                "created_by_version": CREATED_BY_VERSION,
                "status": "RECONCILE_REQUIRED",
                "last_action": "INVALID_SESSION",
                "last_result": {"status": "INVALID_SESSION"},
            }
        if session is not None:
            # Legacy inspection remains tolerant through read_session/status_view,
            # but execution must not replace an undeclared recovery boundary.
            self._validate_internal_control_state(session)
        if session and session.get("status") == "STOPPED" and isinstance(session.get("blocker"), dict):
            blocker = session["blocker"]
            try:
                recheck_at = float(blocker.get("next_recheck_at"))
            except (TypeError, ValueError):
                recheck_at = now
            if now < recheck_at:
                session["last_action"] = "BLOCKER_COOLDOWN"
                session["last_result"] = {
                    "status": "BLOCKED_RECHECK_NOT_DUE",
                    "blocker_kind": blocker.get("kind"),
                    "next_recheck_at": blocker.get("next_recheck_at"),
                    "resume_hint": blocker.get("resume_hint"),
                }
                self._save_session(session)
                return session
            if blocker.get("kind") == "BUDGET_SHORTAGE":
                self._advance_route_episode(session)
                session.pop("blocker", None)
                session["status"] = "RUNNING"
                self._save_session(session)
                recheck = {"changed": True, "probe": {}}
            else:
                recheck = self._recheck_blocker(blocker)
            if not recheck.get("changed"):
                updated = dict(blocker)
                updated["consecutive_same_count"] = int(
                    blocker.get("consecutive_same_count", 0)
                ) + 1
                updated["last_seen_at"] = now
                updated["next_recheck_at"] = now + max(
                    0.0, float((getattr(self.agent, "factory_config", {}) or {}).get(
                        "blocker_recheck_sec", self.BLOCKER_RECHECK_SEC
                    ))
                )
                session["blocker"] = updated
                session["status"] = "STOPPED"
                session["last_action"] = self._BLOCKER_STOP_ACTIONS.get(
                    blocker.get("kind")
                )
                if session["last_action"] is None:
                    raise FactoryControlStateError(
                        "FACTORY_CONTROL_STATE_UNDECLARED: blocker action"
                    )
                session["last_result"] = {
                    "status": "BLOCKER_UNCHANGED",
                    "blocker_kind": updated["kind"],
                    "next_recheck_at": updated["next_recheck_at"],
                }
                self._save_session(session)
                return session
            session.pop("blocker", None)
            session["status"] = "RUNNING"
            session["route_attempt"] = 0
            session["no_gain_attempts"] = 0
        # A transport-reconciliation terminal state is a safety boundary, not
        # an invitation to silently mint a new session. Starting over here
        # could re-POST an operation whose outcome was never reconciled.
        # Budget exhaustion is different: it is a clean session boundary and
        # a later explicit factory invocation may start a new budget window.
        if session and session.get("status") == "RECONCILE_REQUIRED":
            # CHECKPOINT_BLOCKED is a recoverable terminal marker once the
            # checkpoint has been completed by the canonical recovery path.
            # Other reconciliation causes remain fail-closed and require
            # explicit handling before a new session may be minted.
            if not (
                session.get("last_action") == "CHECKPOINT_BLOCKED"
                and self._unfinished_checkpoint() is None
            ):
                self._save_session(session)
                return session
        # A zero-duration probe must never overwrite or stop a live session.
        # The explicit --factory-stop command is the only control-plane action
        # allowed to request a running factory to stop.
        if duration <= 0 and session and session.get("status") == "RUNNING":
            self._save_session(session)
            return session
        previous_session = session
        if duration <= 0 or not session or session.get("status") != "RUNNING" or session.get("deadline", 0) <= now:
            try:
                carried_quota = self._carry_forward_quota(previous_session, quota)
            except ValueError:
                if previous_session is not None:
                    previous_session["status"] = "RECONCILE_REQUIRED"
                    previous_session["last_action"] = "INVALID_SIMULATION_QUOTA"
                    previous_session["last_result"] = {
                        "status": "INVALID_SIMULATION_QUOTA"
                    }
                    self._save_session(previous_session)
                    return previous_session
                raise
            session = {
                "schema_version": CHECKPOINT_VERSION,
                "created_by_version": CREATED_BY_VERSION,
                "session_id": uuid.uuid4().hex[:16],
                "started_at": now,
                "deadline": now + duration,
                "status": "RUNNING",
                "rounds_completed": 0,
                "simulations_reserved": carried_quota["weekly_reserved"],
                "simulation_cap": weekly_cap,
                "quota": carried_quota,
                "last_round": None,
                "last_action": "START",
                "last_result": None,
            }
        else:
            # A restart resumes the existing deadline and budget.  It must not
            # silently grant another full day or another simulation allowance.
            session.setdefault("simulations_reserved", 0)
            session.setdefault("simulation_cap", weekly_cap)
            session.setdefault("rounds_completed", 0)
            session.setdefault("probe_offset", 0)
            session.setdefault("stop_requested", False)
        session.setdefault("route_attempt", 0)
        session.setdefault("no_gain_attempts", 0)
        session.setdefault("route_episode_round", None)
        if "last_feasibility_check" not in session:
            session["last_feasibility_check"] = session.get("last_feasibility_probe")
        session.setdefault("last_feasibility_check", None)
        session.setdefault("last_budget_probe", None)
        try:
            quota = self._prepare_quota(session, weekly_cap, daily_cap)
        except ValueError:
            session["status"] = "RECONCILE_REQUIRED"
            session["last_action"] = "INVALID_SIMULATION_QUOTA"
            session["last_result"] = {"status": "INVALID_SIMULATION_QUOTA"}
            self._save_session(session)
            return session
        self._save_session(session)
        if duration <= 0:
            session["status"] = "STOPPED"
            session["last_action"] = "ZERO_DURATION"
            self._save_session(session)
            return session

        while self._clock() < session["deadline"]:
            # Reload only the small canonical envelope so --factory-stop can
            # control a live process without touching proposals/checkpoints.
            current = self._load_session()
            if current and current.get("session_id") == session.get("session_id"):
                session["stop_requested"] = bool(current.get("stop_requested", False))
            if session.get("stop_requested"):
                session["status"] = "STOPPED"
                session["last_action"] = "STOP_REQUESTED"
                break
            feed_hook = getattr(self.agent, "refresh_remote_alpha_feed_if_due", None)
            if callable(feed_hook):
                feed_result = feed_hook(limit=100)
                if isinstance(feed_result, dict):
                    session["feed_refresh"] = {
                        key: feed_result.get(key)
                        for key in (
                            "status", "last_success_at", "age_sec", "next_due_at",
                            "last_attempt_at", "last_attempt_status",
                        )
                    }
                    self._save_session(session)
            pending_targeted = self._pending_targeted_batch()
            if pending_targeted is not None:
                # Agent authored 的 targeted batch 是当前唯一 canonical inbox
                # owner：factory 只等待，不覆盖、不消耗 quota，也不另开第二条
                # 执行路径。Agent 用 `python main.py run-proposals` 执行后，本
                # 循环会看到该轮 canonical checkpoint 并按其恢复未完成部分。
                session["last_action"] = "WAIT_AGENT_DECISION"
                session["last_result"] = {
                    "round_no": pending_targeted.get("round_no"),
                    "proposals": pending_targeted["proposal_count"],
                    "status": pending_targeted["status"],
                    "errors": list(pending_targeted["errors"])[:5],
                    "expires_at": pending_targeted.get("expires_at"),
                }
                self._save_session(session)
                self._bounded_sleep(idle, session["deadline"])
                continue
            if session.get("last_action") == "PROPOSALS_WRITE_ERROR":
                # The new payload is not durable and the old canonical inbox
                # may belong to another round; do not generate a replacement
                # hypothesis until the storage boundary is reconciled.
                session["status"] = "RECONCILE_REQUIRED"
                session["last_action"] = "STORAGE_RECONCILE_REQUIRED"
                self._save_session(session)
                break
            orphaned_proposals = self._orphaned_canonical_proposals(session)
            if orphaned_proposals:
                proposal_count = len(orphaned_proposals)
                reserved = int(session.get("simulations_reserved", 0))
                gap = max(0, proposal_count - reserved)
                if gap > self._quota_remaining(session, quota):
                    session["status"] = "SIMULATION_BUDGET_CAP"
                    session["last_action"] = "ORPHANED_PROPOSALS_BUDGET_BLOCKED"
                    session["last_result"] = {
                        "round_no": session.get("last_round"),
                        "proposals": proposal_count,
                        "status": "ORPHANED_PROPOSALS_BUDGET_BLOCKED",
                    }
                    self._save_session(session)
                    break
                try:
                    self._quota_reserve(session, quota, gap)
                except QuotaExceeded:
                    session["status"] = "SIMULATION_BUDGET_CAP"
                    session["last_action"] = "ORPHANED_PROPOSALS_BUDGET_BLOCKED"
                    self._save_session(session)
                    break
                session["last_action"] = "RECOVER_PROPOSALS"
                self._save_session(session)
                try:
                    result = self._call(self.agent.run_proposals, self.proposals_path)
                except (KeyboardInterrupt, SystemExit):
                    raise
                except Exception as exc:
                    if self._stop_on_terminal_error(session, "RECOVER_PROPOSALS", exc):
                        break
                    self._record_retry(session, "RECOVER_PROPOSALS_ERROR", exc)
                    self._save_session(session)
                    self._bounded_sleep(self._retry_delay(idle, session), session["deadline"])
                    continue
                session["retry_count"] = 0
                # A checkpoint created by this call is handled by the normal
                # recovery branch on the next loop; keep its accepted slots.
                if self._unfinished_checkpoint():
                    session["last_action"] = "RECOVER_PROPOSALS_PENDING"
                    self._save_session(session)
                    continue
                accepted = self._accepted_count(proposal_count)
                self._quota_release(session, quota, proposal_count - accepted)
                session["rounds_completed"] += 1
                session["last_action"] = "RECOVERED_PROPOSALS"
                session["last_result"] = self._compact_result(
                    session.get("last_round"), result, proposal_count
                )
                self._finish_route_episode(session, session.get("last_round"))
                self._save_session(session)
                continue
            foreign = self._unfinished_checkpoint()
            if (
                session.get("last_action") in {
                    "RUN_PROPOSALS_ERROR", "RECOVER_PROPOSALS_ERROR"
                }
                and not foreign
            ):
                # An exception after the production call has no checkpoint to
                # prove whether a POST happened.  Stop for reconciliation;
                # never re-POST merely because the process is unattended.
                session["status"] = "RECONCILE_REQUIRED"
                session["last_action"] = "EXECUTION_RECONCILE_REQUIRED"
                self._save_session(session)
                break
            if foreign:
                # Recovery is always first.  If the canonical proposal file is
                # absent/mismatched, stop for reconciliation rather than
                # generating a new round that could consume another slot.
                checkpoint_round = self._checkpoint_round(foreign)
                if self._proposal_round() == checkpoint_round:
                    if not self._recovery_already_reserved(session, checkpoint_round):
                        recovery_count = self._checkpoint_experiment_count(foreign)
                        remaining = self._quota_remaining(session, quota)
                        if recovery_count > remaining:
                            session["status"] = "SIMULATION_BUDGET_CAP"
                            session["last_action"] = "RECOVERY_BUDGET_BLOCKED"
                            session["last_result"] = {
                                "round_no": checkpoint_round,
                                "proposals": recovery_count,
                                "status": "RECOVERY_BUDGET_BLOCKED",
                            }
                            self._save_session(session)
                            break
                        try:
                            self._quota_reserve(session, quota, recovery_count)
                        except QuotaExceeded:
                            session["status"] = "SIMULATION_BUDGET_CAP"
                            session["last_action"] = "RECOVERY_BUDGET_BLOCKED"
                            self._save_session(session)
                            break
                    # Keep the control-plane ledger tied to the durable
                    # recovery boundary even when this is a fresh factory
                    # session after a previous deadline/stop.
                    session["last_round"] = checkpoint_round
                    session["last_action"] = "RECOVER_CHECKPOINT"
                    self._save_session(session)
                    try:
                        recovery_result = self._call(
                            self.agent.run_proposals, self.proposals_path
                        )
                    except (KeyboardInterrupt, SystemExit):
                        raise
                    except Exception as exc:
                        if self._stop_on_terminal_error(session, "RECOVER_CHECKPOINT", exc):
                            break
                        # Recovery is itself a durable operation.  Persist the
                        # retry marker before sleeping so a process restart
                        # continues from the same checkpoint and does not
                        # manufacture a replacement round.
                        self._record_retry(session, "RECOVER_CHECKPOINT_ERROR", exc)
                        self._save_session(session)
                        self._bounded_sleep(self._retry_delay(idle, session), session["deadline"])
                        continue
                    session["retry_count"] = 0
                if self._unfinished_checkpoint():
                    session["status"] = "RECONCILE_REQUIRED"
                    session["last_action"] = "CHECKPOINT_BLOCKED"
                    break
                session["rounds_completed"] += 1
                session["last_action"] = "RECOVERED_CHECKPOINT"
                session["last_result"] = self._compact_result(
                    checkpoint_round, recovery_result, self._checkpoint_experiment_count(foreign)
                )
                self._finish_route_episode(session, checkpoint_round)
                self._save_session(session)

            if round_cap and session["rounds_completed"] >= round_cap:
                session["status"] = "ROUND_CAP"
                session["last_action"] = "ROUND_CAP"
                self._save_session(session)
                break
            remaining_budget = self._quota_remaining(session, quota)
            if remaining_budget <= 0:
                session["status"] = "SIMULATION_BUDGET_CAP"
                session["last_action"] = "BUDGET_BLOCKED"
                break

            round_no = self.agent.next_round_no()
            self._begin_route_episode(session, round_no)
            try:
                probe_offset = max(0, int(session.get("probe_offset", 0)))
            except (TypeError, ValueError):
                probe_offset = 0
            probe_round = round_no + probe_offset
            session["last_round"] = round_no
            session["last_action"] = "SUGGEST"
            self._save_session(session)
            try:
                bundle = self._call(self.agent.run_suggestion_round, probe_round)
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception as exc:
                if self._stop_on_terminal_error(session, "SUGGEST", exc):
                    break
                self._record_retry(session, "SUGGEST_ERROR", exc)
                self._save_session(session)
                self._bounded_sleep(self._retry_delay(idle, session), session["deadline"])
                continue
            if not isinstance(bundle, dict):
                session["probe_offset"] = probe_offset + 1
                session["last_action"] = "WAIT_NO_SUGGESTION"
                self._save_session(session)
                self._bounded_sleep(idle, session["deadline"])
                continue
            session["retry_count"] = 0
            research_space = bundle.get("research_space")
            if not isinstance(research_space, dict):
                session["probe_offset"] = probe_offset + 1
                session["last_action"] = "WAIT_INVALID_SUGGESTION"
                session["last_result"] = {
                    "round_no": round_no,
                    "status": "INVALID_RESEARCH_SPACE",
                }
                self._save_session(session)
                self._bounded_sleep(idle, session["deadline"])
                continue
            raw_tags = research_space.get("tags")
            raw_tags = raw_tags if isinstance(raw_tags, (list, tuple, set)) else []
            hypothesis = {
                "id": research_space.get("id") or f"h-factory-r{round_no}",
                "statement": research_space.get(
                    "statement", "AI factory field mechanism baseline"
                ),
                "tags": [str(tag) for tag in raw_tags if tag is not None] + ["factory"],
                "datasets": self._string_ids(research_space.get("datasets")),
                "field_source": bundle.get("field_source"),
                "template_mode": str(
                    getattr(self.agent, "factory_config", {}).get(
                        "template_mode", "legacy"
                    )
                ).lower(),
            }
            operator_reference = bundle.get("operator_reference")
            if not self._operator_capability_ready(operator_reference):
                session["status"] = "OPERATOR_CAPABILITY_BLOCKED"
                session["last_action"] = "STOP_OPERATOR_CAPABILITY"
                session["last_result"] = {
                    "round_no": round_no,
                    "proposals": 0,
                    "status": "OPERATOR_CAPABILITY_UNKNOWN",
                    "operator_capability": self._operator_capability_status(
                        operator_reference
                    ),
                }
                self._remember_blocker(
                    session, "OPERATOR_CAPABILITY", session["last_result"], self._clock()
                )
                self._save_session(session)
                break
            try:
                batch_size = FACTORY_BATCH_SIZE
                if remaining_budget < batch_size:
                    session["status"] = "SIMULATION_BUDGET_CAP"
                    session["last_action"] = "FACTORY_BATCH_BUDGET_BLOCKED"
                    session["last_result"] = {
                        "round_no": round_no,
                        "proposals": 0,
                        "required": batch_size,
                        "status": "FACTORY_BATCH_BUDGET_BLOCKED",
                    }
                    self._save_session(session)
                    break
                feasibility = None
                probe_method = getattr(self.factory, "assess_feasibility", None)
                if callable(probe_method):
                    feasibility = probe_method(
                        hypothesis,
                        bundle.get("fields") or [],
                        bundle.get("operator_reference") or {},
                        excluded_expressions=self._known_expressions(),
                        probe_id=f"{hypothesis['id']}:round:{round_no}:probe:{probe_offset}",
                    )
                    if isinstance(feasibility, dict):
                        emit = getattr(self.agent, "emit_heartbeat", None)
                        if callable(emit):
                            emit(
                                "FEASIBILITY",
                                current_probe_id=feasibility.get("probe_id"),
                                pair_examined=feasibility.get("pair_examined", 0),
                                allow_count=feasibility.get("relationship_allow", 0),
                                post_dedupe_candidates=feasibility.get("candidates_after_dedupe", 0),
                                cross_dataset_candidates=feasibility.get("novel_cross_dataset_relationship_count", 0),
                                route_attempt=session.get("route_attempt", 0),
                                current_taxonomy=feasibility.get("failure_taxonomy"),
                            )
                    if (isinstance(feasibility, dict) and
                        getattr(self.agent, "min_cross_dataset_pairs", 0) > 0 and
                        not feasibility.get("batch_gate", {}).get("feasible", False)):
                        config = getattr(self.agent, "factory_config", {}) or {}
                        feasibility_probe = self._route_probe_projection(
                            feasibility, session_id=session["session_id"]
                        )
                        decision = self.route_decision(
                            session.get("last_feasibility_check"), feasibility_probe,
                            route_attempt=session.get("route_attempt", 0),
                            no_gain_attempts=session.get("no_gain_attempts", 0),
                            max_route_attempts=config.get("max_route_attempts", 3),
                            max_no_gain_attempts=config.get("max_no_gain_attempts", 2),
                        )
                        session["last_feasibility_check"] = feasibility_probe
                        session["route_attempt"] = decision["route_attempt"] + 1
                        session["no_gain_attempts"] = decision["no_gain_attempts"]
                        session["probe_offset"] = probe_offset + 1
                        session["last_action"] = (
                            "STOP_MECHANISM_ROUTE" if decision["action"] == "STOP"
                            else "REROUTE_FACTORY_FEASIBILITY"
                        )
                        session["last_result"] = {
                            "round_no": round_no,
                            "proposals": 0,
                            "status": "FACTORY_FEASIBILITY_CHECK_BLOCKED",
                            "failure_taxonomy": feasibility.get("failure_taxonomy"),
                            "feasibility_check": feasibility,
                            "route_decision": decision,
                        }
                        self._save_session(session)
                        if decision["action"] == "STOP":
                            self._remember_blocker(session, "FEASIBILITY", feasibility, self._clock())
                            session["status"] = "STOPPED"
                            self._save_session(session)
                            break
                        self._bounded_sleep(self._retry_delay(idle, session), session["deadline"])
                        continue
                proposals = self.factory.generate_factory_batch(
                    dict(
                        hypothesis,
                        include_partial_operator_branches=(
                            (getattr(self.agent, "factory_config", {}) or {})
                            .get("include_partial_operator_branches", True)
                        ),
                    ),
                    bundle.get("fields") or [],
                    bundle.get("operator_reference") or {},
                    target=batch_size,
                    excluded_expressions=self._known_expressions(),
                    research_context=bundle.get("context"),
                    # A rejected batch must not be regenerated byte-for-byte:
                    # probe_offset is the bounded exploration retry identity.
                    seed=f"{hypothesis['id']}:round:{round_no}:probe:{probe_offset}",
                    max_pending_per_arm=getattr(
                        getattr(getattr(self.agent, "search_policy", None), "allocator", None),
                        "max_pending_per_arm", 1,
                    ),
                )
                emit = getattr(self.agent, "emit_heartbeat", None)
                if callable(emit):
                    emit("ASSEMBLY", proposals=len(proposals) if isinstance(proposals, list) else 0)
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception as exc:
                if self._stop_on_terminal_error(session, "ASSEMBLE", exc):
                    break
                self._record_retry(session, "ASSEMBLE_ERROR", exc)
                self._save_session(session)
                self._bounded_sleep(self._retry_delay(idle, session), session["deadline"])
                continue
            batch_ok, batch_errors = validate_factory_batch(
                proposals,
                target=batch_size,
                min_datasets=getattr(self.agent, "min_factory_datasets", 1),
                require_cross_dataset_pairs=(
                    getattr(self.agent, "min_cross_dataset_pairs", 0) > 0
                ),
            )
            if not batch_ok:
                emit = getattr(self.agent, "emit_heartbeat", None)
                if callable(emit):
                    emit(
                        "BATCH_GATE", proposals=len(proposals) if isinstance(proposals, list) else 0,
                        valid=False, errors=len(batch_errors),
                    )
                budget_audit = getattr(self.factory, "last_budget_audit", None)
                shortage_count = (
                    int(budget_audit.get("shortage_count", 0))
                    if isinstance(budget_audit, dict) else 0
                )
                if shortage_count > 0:
                    budget_probe = self._selection_probe(
                        proposals, feasibility, budget_audit
                    )
                    budget_probe = self._route_probe_projection(
                        budget_probe, session_id=session["session_id"]
                    )
                    config = getattr(self.agent, "factory_config", {}) or {}
                    decision = self.route_decision(
                        session.get("last_budget_probe"), budget_probe,
                        route_attempt=session.get("route_attempt", 0),
                        no_gain_attempts=session.get("no_gain_attempts", 0),
                        max_route_attempts=config.get("max_route_attempts", 3),
                        max_no_gain_attempts=config.get("max_no_gain_attempts", 2),
                    )
                    session["last_budget_probe"] = budget_probe
                    session["route_attempt"] = decision["route_attempt"] + 1
                    session["no_gain_attempts"] = decision["no_gain_attempts"]
                    session["probe_offset"] = probe_offset + 1
                    session["last_action"] = (
                        "STOP_BUDGET_SHORTAGE"
                        if decision["action"] == "STOP"
                        else "REROUTE_BUDGET_SHORTAGE"
                    )
                    session["last_result"] = {
                        "round_no": round_no,
                        "proposals": len(proposals),
                        "required": batch_size,
                        "status": "FACTORY_BUDGET_SHORTAGE",
                        "errors": batch_errors[:5],
                        "budget_audit": budget_audit,
                        "route_decision": decision,
                    }
                    self._save_session(session)
                    if decision["action"] == "STOP":
                        self._remember_blocker(session, "BUDGET_SHORTAGE", budget_probe, self._clock())
                        session["status"] = "STOPPED"
                        self._save_session(session)
                        break
                    self._bounded_sleep(
                        self._retry_delay(idle, session), session["deadline"]
                    )
                    continue
                session["probe_offset"] = probe_offset + 1
                session["last_action"] = "WAIT_FACTORY_BATCH"
                session["last_result"] = {
                    "round_no": round_no,
                    "proposals": len(proposals),
                    "required": batch_size,
                    "status": "FACTORY_BATCH_NOT_READY",
                    "errors": batch_errors[:5],
                }
                self._save_session(session)
                self._bounded_sleep(idle, session["deadline"])
                continue
            payload = {
                "round_no": round_no,
                "epoch_label": bundle.get("epoch_label"),
                "factory_session_id": session["session_id"],
                "hypothesis": hypothesis,
                "proposals": proposals,
                "batch_type": "factory_100",
                "source": "ai_factory_template_adapter",
                "probe_policy": {
                    "research_layer": "exploration",
                    "research_role": "EXPLORE",
                    "experiment_stage": "BASELINE",
                    "objective": "signal_discovery",
                    "selection": "breadth_mechanism_field_structural_diversity",
                },
                "dataset_selection": bundle.get("dataset_selection") or {},
                "catalog_provenance": bundle.get("catalog_provenance") or bundle.get("field_source"),
                "factory_batch_stats": factory_batch_stats(
                    proposals, feasibility, getattr(self.factory, "last_budget_audit", None)
                ),
            }
            # One canonical proposals inbox is overwritten only when its
            # logical content changes.  It is not a per-round artifact.
            try:
                atomic_write_json_if_changed(self.proposals_path, payload)
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception as exc:
                self._record_retry(session, "PROPOSALS_WRITE_ERROR", exc)
                self._save_session(session)
                self._bounded_sleep(self._retry_delay(idle, session), session["deadline"])
                continue
            if not proposals:
                session["probe_offset"] = probe_offset + 1
                session["last_action"] = "WAIT_NO_VALID_PROPOSAL"
                session["last_result"] = {"round_no": round_no, "proposals": 0}
                self._save_session(session)
                self._bounded_sleep(idle, session["deadline"])
                continue
            session["last_action"] = "RUN_PROPOSALS"
            session["probe_offset"] = 0
            try:
                self._quota_reserve(session, quota, len(proposals))
            except QuotaExceeded:
                session["status"] = "SIMULATION_BUDGET_CAP"
                session["last_action"] = "DAILY_OR_WEEKLY_BUDGET_BLOCKED"
                session["last_result"] = {
                    "round_no": round_no,
                    "proposals": len(proposals),
                    "status": "DAILY_OR_WEEKLY_BUDGET_BLOCKED",
                }
                self._save_session(session)
                break
            self._save_session(session)
            try:
                result = self._call(self.agent.run_proposals, self.proposals_path)
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception as exc:
                if self._stop_on_terminal_error(session, "RUN_PROPOSALS", exc):
                    break
                # The proposal file and checkpoint were persisted before the
                # production call.  Keep the reservation and let the next
                # loop recover the same checkpoint; never create a replacement
                # POST after an ambiguous execution exception.
                self._record_retry(session, "RUN_PROPOSALS_ERROR", exc)
                self._save_session(session)
                self._bounded_sleep(self._retry_delay(idle, session), session["deadline"])
                continue
            session["retry_count"] = 0
            if self._unfinished_checkpoint():
                # The Agent durably handed off an unresolved batch. Keep its
                # reservation and round number; the next loop will recover
                # the same checkpoint instead of accounting it twice.
                session["last_action"] = "RUN_PROPOSALS_PENDING"
                self._save_session(session)
                continue
            accepted = self._accepted_count(len(proposals))
            if result is None and accepted == 0:
                # ``Agent.run_proposals`` returns None for a blocked, already
                # consumed, or otherwise non-executed inbox.  No checkpoint
                # means no remote execution boundary was created: release the
                # conservative reservation, keep the round uncompleted, and
                # advance the probe so the next iteration can discover a new
                # candidate set.  Counting this as a completed round used to
                # busy-loop on the same proposals and falsely consume the
                # factory's round budget.
                stats = getattr(self.agent, "last_run_stats", {}) or {}
                agent_status = stats.get("status") if isinstance(stats, dict) else None
                if agent_status in {"PREFLIGHT_BLOCKED", "FACTORY_BATCH_BLOCKED"}:
                    preflight_probe = self._control_probe({}, stats)
                    preflight_probe = self._route_probe_projection(
                        preflight_probe, session_id=session["session_id"]
                    )
                    config = getattr(self.agent, "factory_config", {}) or {}
                    decision = self.route_decision(
                        session.get("last_preflight_probe"), preflight_probe,
                        route_attempt=session.get("route_attempt", 0),
                        no_gain_attempts=session.get("no_gain_attempts", 0),
                        max_route_attempts=config.get("max_route_attempts", 3),
                        max_no_gain_attempts=config.get("max_no_gain_attempts", 2),
                    )
                    session["last_preflight_probe"] = preflight_probe
                    session["route_attempt"] = decision["route_attempt"] + 1
                    session["no_gain_attempts"] = decision["no_gain_attempts"]
                    if decision["action"] == "STOP":
                        self._quota_release(session, quota, len(proposals))
                        self._remember_blocker(session, "PREFLIGHT", preflight_probe, self._clock())
                        session["status"] = "STOPPED"
                        session["last_action"] = "STOP_PREFLIGHT_BLOCKER"
                        session["last_result"] = {
                            "round_no": round_no,
                            "proposals": len(proposals),
                            "status": agent_status,
                            "agent_status": agent_status,
                            "rejection_reason_counts": preflight_probe.get("rejection_reason_counts", {}),
                            "route_decision": decision,
                        }
                        self._save_session(session)
                        break
                self._quota_release(session, quota, len(proposals))
                session["probe_offset"] = probe_offset + 1
                session["last_action"] = "WAIT_RUN_PROPOSALS"
                session["last_result"] = {
                    "round_no": round_no,
                    "proposals": len(proposals),
                    "status": "RUN_PROPOSALS_NOT_EXECUTED",
                    "agent_status": agent_status,
                    "rejection_counts": (
                        getattr(self.agent, "last_run_stats", {}) or {}
                    ).get("rejection_counts") if isinstance(stats, dict) else None,
                    "rejection_reason_counts": (
                        stats.get("rejection_reason_counts") if isinstance(stats, dict) else None
                    ),
                }
                self._save_session(session)
                self._bounded_sleep(idle, session["deadline"])
                continue
            # Preflight reservation is conservative.  Release only candidates
            # the canonical Agent explicitly rejected/skipped; accepted jobs
            # remain charged even when remote state is unresolved.
            self._quota_release(session, quota, len(proposals) - accepted)
            session["rounds_completed"] += 1
            session["last_action"] = "ROUND_COMPLETE"
            session["last_result"] = self._compact_result(round_no, result, len(proposals))
            self._finish_route_episode(session, round_no)
            self._save_session(session)

        if session["status"] == "RUNNING":
            session["status"] = "DEADLINE"
        session["finished_at"] = self._clock()
        self._save_session(session)
        return session

    def _recheck_blocker(self, blocker):
        """Perform one optional bounded control-plane recheck, never a POST."""
        hook = getattr(self.agent, "recheck_factory_blocker", None)
        if not callable(hook):
            hook = getattr(self.factory, "recheck_blocker", None)
        if not callable(hook):
            return {"changed": False, "probe": {}}
        result = hook({
            "kind": blocker.get("kind"),
            "signature": blocker.get("signature"),
            "last_seen_at": blocker.get("last_seen_at"),
            "field_cache_path": os.path.join(self.state_dir, "fields_cache.json"),
        })
        if not isinstance(result, dict):
            return {"changed": False, "probe": {}}
        return {
            "changed": bool(result.get("changed")),
            "probe": self._control_probe(result.get("probe")),
        }

    @staticmethod
    def _checkpoint_round(path):
        match = re.search(r"round_(\d+)\.checkpoint\.json$", path)
        return int(match.group(1)) if match else None

    def _proposal_round(self):
        try:
            with open(self.proposals_path, encoding="utf-8") as handle:
                payload = json.load(handle)
            value = payload.get("round_no") if isinstance(payload, dict) else None
            return int(value) if value is not None else None
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return None

    def _pending_targeted_batch(self):
        """Detect a legal, current, not-yet-executed Agent targeted batch.

        A ``targeted_optimization`` envelope owns the single canonical inbox
        until it is executed or expires, so exploration must never silently
        overwrite it.  Execution evidence is the canonical checkpoint of the
        batch round (the same owner as every other execution boundary), which
        keeps recovery of an unfinished targeted batch on the normal
        ``Agent.run_proposals`` path instead of a second Simulation path.
        """
        try:
            with open(self.proposals_path, encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        state = targeted_batch_state(payload, now=self._clock())
        if not state["blocking"]:
            return None
        round_no = payload.get("round_no")
        if isinstance(round_no, int) and not isinstance(round_no, bool) and round_no > 0:
            checkpoint = self._load_checkpoint_payload(round_no)
            if isinstance(checkpoint, dict) and checkpoint.get("complete") is True:
                return None
        return {**state, "round_no": payload.get("round_no")}

    def _orphaned_canonical_proposals(self, session):
        """Find a factory inbox left between reservation and checkpoint creation."""
        if session.get("last_action") not in {
            "RUN_PROPOSALS", "RECOVER_PROPOSALS",
        }:
            return []
        try:
            round_no = int(session.get("last_round"))
        except (TypeError, ValueError):
            return []
        if isinstance(session.get("last_result"), dict):
            try:
                if int(session["last_result"].get("round_no")) == round_no:
                    return []
            except (TypeError, ValueError):
                pass
        checkpoint = self._load_checkpoint_payload(round_no)
        if checkpoint and checkpoint.get("complete") is True:
            return []
        if checkpoint and checkpoint.get("complete") is not True:
            return []
        try:
            with open(self.proposals_path, encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return []
        if not isinstance(payload, dict):
            return []
        if payload.get("factory_session_id") != session.get("session_id"):
            return []
        try:
            if int(payload.get("round_no")) != round_no:
                return []
        except (TypeError, ValueError):
            return []
        proposals = payload.get("proposals")
        return proposals if isinstance(proposals, list) and proposals else []

    def _load_checkpoint_payload(self, round_no):
        return self.agent.checkpoints.load(round_no)

    @staticmethod
    def _operator_capability_ready(reference):
        return (
            isinstance(reference, dict)
            and reference.get("status") == "LIVE_VERIFIED"
            and reference.get("availability") == "AVAILABLE"
            and reference.get("source") == "BRAIN_LIVE_ONLY"
            and isinstance(reference.get("operators"), list)
        )

    @staticmethod
    def _operator_capability_status(reference):
        if not isinstance(reference, dict):
            return {"status": "UNKNOWN", "availability": "UNKNOWN"}
        return {
            key: reference.get(key)
            for key in ("status", "availability", "source", "valid", "errors")
            if key in reference
        }

    def _accepted_count(self, proposal_count):
        stats = getattr(self.agent, "last_run_stats", {})
        accepted = stats.get("accepted", proposal_count) if isinstance(stats, dict) else proposal_count
        try:
            return max(0, min(proposal_count, int(accepted)))
        except (TypeError, ValueError):
            return proposal_count

    def _checkpoint_experiment_count(self, path):
        """Count checkpoint slots without materializing experiment objects."""
        checkpoint_round = self._checkpoint_round(path)
        if checkpoint_round is None:
            return 0
        payload = self.agent.checkpoints.load(checkpoint_round)
        experiments = payload.get("experiments") if isinstance(payload, dict) else None
        return len(experiments) if isinstance(experiments, list) else 0

    @staticmethod
    def _recovery_already_reserved(session, checkpoint_round):
        """Avoid double charging after a factory crash at a recovery boundary."""
        try:
            last_round = int(session.get("last_round"))
        except (TypeError, ValueError):
            last_round = None
        return (
            last_round == checkpoint_round
            and session.get("last_action") in {
                "RUN_PROPOSALS", "RUN_PROPOSALS_ERROR",
                "RECOVER_CHECKPOINT", "RECOVER_CHECKPOINT_ERROR",
                "RECOVER_PROPOSALS", "RECOVER_PROPOSALS_ERROR",
                "RECOVER_PROPOSALS_PENDING", "RUN_PROPOSALS_PENDING",
            }
        )

    def _known_expressions(self):
        """Return a bounded advisory prefilter for already seen expressions.

        The production Agent performs the authoritative streaming trajectory
        dedupe.  This small set only avoids obvious recent/memory duplicates
        before writing the canonical proposals inbox.
        """
        values = set()
        memory = getattr(self.agent, "memory", None)
        if memory is not None:
            values.update(
                canonical_expression(value)
                for value in (getattr(memory, "seen_expressions", set()) or set())
                if isinstance(value, (str, int)) and canonical_expression(value)
            )
        trajectory = getattr(self.agent, "trajectory", None)
        if trajectory is not None:
            for experiment in getattr(trajectory, "experiments", []) or []:
                expression = getattr(experiment, "expression", None)
                if isinstance(expression, (str, int)) and expression:
                    values.add(canonical_expression(expression))
        checkpoints = getattr(self.agent, "checkpoints", None)
        if checkpoints is not None:
            for record in checkpoints.scan() or ():
                if record.get("malformed") or record.get("checkpoint", {}).get("complete") is not True:
                    continue
                for row in record.get("checkpoint", {}).get("experiments") or ():
                    expression = row.get("expression") if isinstance(row, dict) else None
                    if isinstance(expression, (str, int)) and expression:
                        values.add(canonical_expression(expression))
        return values

    @staticmethod
    def _string_ids(values):
        """Normalize AI-authored dataset/id lists without inventing values."""
        if isinstance(values, (str, int)):
            values = [values]
        if not isinstance(values, (list, tuple, set)):
            return []
        result = []
        for value in values:
            if isinstance(value, dict):
                value = value.get("id") or value.get("name")
            if isinstance(value, (str, int)) and str(value).strip():
                item = str(value)
                if item not in result:
                    result.append(item)
        return result

    def _save_session(self, session):
        # A concurrent --factory-stop may replace the envelope between two
        # runner writes.  Preserve that control-plane bit for the same
        # session, while allowing a genuinely new session to start cleanly.
        self._validate_internal_control_state(session)
        persistence_source = deepcopy(session)
        current = self.read_session(self.state_dir)
        if (
            current
            and current.get("session_id") == session.get("session_id")
            and current.get("stop_requested")
        ):
            persistence_source["stop_requested"] = True
        projected = self._control_plane_session(persistence_source)
        atomic_write_json_if_changed(
            self.session_path, projected, ignored_keys=("updated_at",)
        )

    @classmethod
    def _project_route_decision(cls, decision):
        if not isinstance(decision, dict):
            return None
        projected = {}
        for key in ("action", "change_type", "route_name"):
            if key in decision:
                projected[key] = cls._safe_control_token(decision[key])
        for key in ("reason",):
            if key in decision:
                projected[key] = cls._safe_control_token(decision[key])
        if "information_gain" in decision:
            projected["information_gain"] = bool(decision["information_gain"])
        if isinstance(decision.get("information_changes"), list):
            projected["information_changes"] = [
                cls._safe_control_token(value)
                for value in decision["information_changes"]
                if cls._safe_control_token(value) != "UNKNOWN"
            ][:8]
        for key in ("no_gain_attempts", "route_attempt", "route_index"):
            if key in decision:
                projected[key] = cls._safe_nonnegative_count(decision[key])
        return projected

    @classmethod
    def _project_last_result(cls, result):
        if not isinstance(result, dict):
            return None
        projected = {}
        for key in ("round_no", "proposals", "required", "summary_round"):
            if key in result:
                projected[key] = cls._safe_nonnegative_count(result[key])
        for key in ("eligible_count", "selected_count", "shortage_count"):
            if key in result:
                projected[key] = cls._safe_nonnegative_count(result[key])
        if "shortage_reason" in result:
            projected["shortage_reason"] = cls._safe_control_token(
                result["shortage_reason"], default="UNKNOWN"
            )
        for key in ("status", "agent_status", "error_type"):
            if key in result:
                projected[key] = cls._safe_control_token(result[key])
        for key in ("rejection_counts", "rejection_reason_counts"):
            counts = result.get(key)
            if isinstance(counts, dict):
                projected[key] = {
                    cls._safe_control_token(name): cls._safe_nonnegative_count(value)
                    for name, value in counts.items()
                    if cls._safe_control_token(name) != "UNKNOWN"
                    and cls._safe_nonnegative_count(value) > 0
                }
        if "errors" in result:
            errors = result.get("errors")
            if isinstance(errors, (list, tuple, set, dict)):
                projected["errors"] = ["PRESENT"] if errors else []
                projected["error_count"] = len(errors)
            elif errors:
                projected["errors"] = ["PRESENT"]
                projected["error_count"] = 1
        audit = result.get("budget_audit")
        if isinstance(audit, dict):
            for source_key, target_key in (
                ("eligible_count", "eligible_count"),
                ("selected_count", "selected_count"),
                ("shortage_count", "shortage_count"),
            ):
                if source_key in audit:
                    projected[target_key] = cls._safe_nonnegative_count(audit[source_key])
            if "shortage_reason" in audit:
                projected["shortage_reason"] = cls._safe_control_token(
                    audit["shortage_reason"], default="UNKNOWN"
                )
        if "best" in result:
            projected["best_present"] = bool(result.get("best"))
        if "verdicts" in result:
            verdicts = result.get("verdicts")
            if isinstance(verdicts, dict):
                projected["verdict_class_counts"] = {
                    cls._safe_control_token(name): cls._safe_nonnegative_count(value)
                    for name, value in verdicts.items()
                    if cls._safe_control_token(name) != "UNKNOWN"
                    and cls._safe_nonnegative_count(value) > 0
                }
                projected["verdict_count"] = sum(projected["verdict_class_counts"].values())
            elif isinstance(verdicts, (list, tuple)):
                projected["verdict_count"] = len(verdicts)
        route_decision = cls._project_route_decision(result.get("route_decision"))
        if route_decision:
            projected["route_decision"] = route_decision
        return projected

    @classmethod
    def _control_plane_session(cls, session):
        source = session if isinstance(session, dict) else {}
        projected = {}
        scalar_keys = (
            "schema_version", "created_by_version", "session_id", "started_at",
            "deadline", "status", "stop_requested", "rounds_completed",
            "simulations_reserved", "simulation_cap", "last_round", "last_action",
            "route_attempt", "no_gain_attempts", "probe_offset", "retry_count",
            "route_episode_round", "finished_at",
        )
        for key in scalar_keys:
            if key not in source:
                continue
            value = source[key]
            if key in {"status", "last_action"}:
                value = cls._safe_control_token(value)
            elif key == "created_by_version":
                value = CREATED_BY_VERSION
            elif key in {"started_at", "deadline", "finished_at"}:
                try:
                    value = float(value)
                except (TypeError, ValueError, OverflowError):
                    continue
            elif key in {
                "rounds_completed", "simulations_reserved", "simulation_cap",
                "last_round", "route_attempt", "no_gain_attempts", "probe_offset",
                "retry_count", "route_episode_round",
            }:
                value = cls._safe_nonnegative_count(value)
            elif key == "stop_requested":
                value = bool(value)
            projected[key] = value
        if isinstance(source.get("quota"), dict):
            quota = source["quota"]
            projected["quota"] = {
                key: quota[key]
                for key in (
                    "schema_version", "timezone", "local_date", "week_start",
                    "daily_cap", "weekly_cap", "daily_reserved", "weekly_reserved",
                )
                if key in quota
            }
            for key in ("schema_version", "daily_cap", "weekly_cap", "daily_reserved", "weekly_reserved"):
                if key in projected["quota"]:
                    projected["quota"][key] = cls._safe_nonnegative_count(projected["quota"][key])
            for key in ("local_date", "week_start"):
                if key in projected["quota"] and not re.fullmatch(
                    r"\d{4}-\d{2}-\d{2}", str(projected["quota"][key])
                ):
                    projected["quota"][key] = "UNKNOWN"
            if projected["quota"].get("timezone") != WeeklySimulationQuota.TIMEZONE:
                projected["quota"]["timezone"] = WeeklySimulationQuota.TIMEZONE
        for key in ("last_feasibility_check", "last_budget_probe", "last_preflight_probe"):
            raw = source.get(key)
            if raw is None and key == "last_feasibility_check":
                raw = source.get("last_feasibility_probe")
            projected[key] = (
                cls._route_probe_projection(raw, session_id=str(source.get("session_id", "UNKNOWN")))
                if isinstance(raw, dict) else None
            )
        if isinstance(source.get("blocker"), dict):
            blocker = source["blocker"]
            signature = str(blocker.get("signature", "")).lower()
            if not re.fullmatch(r"[0-9a-f]{64}", signature):
                signature = ""
            projected["blocker"] = {
                "kind": cls._safe_control_token(blocker.get("kind")),
                "signature": signature,
                "consecutive_same_count": cls._safe_nonnegative_count(
                    blocker.get("consecutive_same_count")
                ),
                "first_seen_at": cls._safe_timestamp(blocker.get("first_seen_at")),
                "last_seen_at": cls._safe_timestamp(blocker.get("last_seen_at")),
                "next_recheck_at": cls._safe_timestamp(blocker.get("next_recheck_at")),
                "resume_hint": "等待上游 control-plane evidence 更新后重试一次",
            }
        if isinstance(source.get("last_result"), dict):
            projected["last_result"] = cls._project_last_result(source["last_result"])
        if isinstance(source.get("feed_refresh"), dict):
            projected["feed_refresh"] = {
                key: cls._safe_control_token(source["feed_refresh"].get(key))
                for key in ("status", "last_attempt_status")
                if source["feed_refresh"].get(key) is not None
            }
        return projected

    def _load_session(self):
        session = self.read_session(self.state_dir)
        try:
            if not session:
                return None
            session["deadline"] = float(session["deadline"])
            session["rounds_completed"] = max(0, int(session.get("rounds_completed", 0)))
            session["simulations_reserved"] = max(
                0, int(session.get("simulations_reserved", 0))
            )
            session["simulation_cap"] = max(0, int(session.get("simulation_cap", 240)))
            if not isinstance(session.get("status"), str):
                return None
            return session
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            pass
        return None

    def _bounded_sleep(self, seconds, deadline):
        remaining = max(0.0, deadline - self._clock())
        if remaining > 0:
            self._sleep(min(seconds, remaining))

    def _call(self, function, *args):
        """Run agent work without turning every internal round into a report."""
        if not self.quiet:
            return function(*args)
        # ``StringIO`` retained every print from a potentially large round
        # until the call returned.  The factory is intentionally quiet, so a
        # sink is sufficient and keeps peak memory independent of log volume.
        with redirect_stdout(_DISCARD_STDOUT):
            return function(*args)

    @staticmethod
    def _retry_delay(base, session):
        """Bound repeated transient failures without busy-looping the API."""
        try:
            count = max(0, int(session.get("retry_count", 0)))
        except (TypeError, ValueError):
            count = 0
        return min(60.0, float(base) * (2 ** min(max(count - 1, 0), 6)))

    @staticmethod
    def _record_retry(session, action, exc):
        session["last_action"] = action
        session["retry_count"] = int(session.get("retry_count", 0)) + 1
        session["last_result"] = {
            "status": "RETRYING",
            "error_type": type(exc).__name__,
        }

    def _stop_on_terminal_error(self, session, action, exc):
        """Stop deterministic/safety errors instead of retrying all day."""
        error_name = type(exc).__name__
        terminal_names = {
            "WQBAuthError", "WQBSubmitUnknownError", "WQBRejectedError",
        }
        if not isinstance(exc, (ValueError, KeyError, TypeError)) and error_name not in terminal_names:
            return False
        session["status"] = "RECONCILE_REQUIRED"
        try:
            session["last_action"] = AIFactoryRunner._TERMINAL_RECONCILE_ACTIONS[action]
        except KeyError as exc:
            raise FactoryControlStateError(
                "FACTORY_CONTROL_STATE_UNDECLARED: terminal action"
            ) from exc
        session["last_result"] = {
            "status": "RECONCILE_REQUIRED",
            "error_type": error_name,
        }
        self._save_session(session)
        return True

    def _unfinished_checkpoint(self):
        return self.agent.checkpoints.unfinished_except(-1)

    @staticmethod
    def _compact_result(round_no, result, proposal_count):
        if not isinstance(result, dict):
            return {"round_no": round_no, "proposals": proposal_count,
                    "result": "none" if result is None else type(result).__name__}
        return {
            "round_no": round_no,
            "proposals": proposal_count,
            "summary_round": result.get("round"),
            "verdicts": result.get("verdicts"),
            "best_present": bool(result.get("best")),
        }
