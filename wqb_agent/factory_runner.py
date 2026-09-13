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

from .artifacts import atomic_write_json_if_changed
from .diversity import field_concept_keys, semantic_mechanism_key
from .expression import canonical_expression
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


class AIFactoryRunner:
    """Run suggestion -> proposal assembly -> production execution repeatedly."""

    SESSION_FILE = "factory_session.json"
    CHECKPOINT_CACHE_MAX = 512
    BLOCKER_RECHECK_SEC = 3600.0

    @staticmethod
    def _safe_control_token(value, default="UNKNOWN"):
        """Keep blocker fingerprints to bounded public control-plane tokens."""
        if isinstance(value, dict):
            value = value.get("status") or value.get("state") or value.get("availability")
        if not isinstance(value, (str, int, float, bool)):
            return default
        token = str(value).strip().upper()
        return token[:80] if token else default

    @staticmethod
    def _safe_nonnegative_count(value):
        try:
            return max(0, int(value))
        except (TypeError, ValueError, OverflowError):
            return 0

    def _blocker_signature(kind, probe):
        """Hash only bounded control-plane classes, never research payloads."""
        probe = probe if isinstance(probe, dict) else {}
        raw_counts = probe.get("rejection_reason_counts")
        counts = raw_counts if isinstance(raw_counts, dict) else {}
        safe = {
            "kind": AIFactoryRunner._safe_control_token(kind),
            "failure_taxonomy": AIFactoryRunner._safe_control_token(
                probe.get("failure_taxonomy")
            ),
            "frequency_evidence": AIFactoryRunner._safe_control_token(
                probe.get("frequency_evidence")
            ),
            "capability_status": AIFactoryRunner._safe_control_token(
                probe.get("capability_status")
            ),
            "shortage_reason": AIFactoryRunner._safe_control_token(
                probe.get("budget_shortage_reason"), default="NONE"
            ),
            "shortage_count": AIFactoryRunner._safe_nonnegative_count(
                probe.get("budget_shortage_count")
            ),
            "rejection_reason_counts": dict(sorted(
                (AIFactoryRunner._safe_control_token(key),
                 AIFactoryRunner._safe_nonnegative_count(value))
                for key, value in counts.items()
                if AIFactoryRunner._safe_nonnegative_count(value) > 0
            )),
        }
        return hashlib.sha256(
            json.dumps(safe, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    @classmethod
    def _blocker_projection(cls, kind, probe, *, now, previous=None, recheck_sec=None):
        previous = previous if isinstance(previous, dict) else {}
        signature = cls._blocker_signature(kind, probe)
        same = previous.get("signature") == signature and previous.get("kind") == kind
        try:
            consecutive = int(previous.get("consecutive_same_count", 0)) if same else 0
        except (TypeError, ValueError):
            consecutive = 0
        interval = cls.BLOCKER_RECHECK_SEC if recheck_sec is None else float(recheck_sec)
        return {
            "kind": str(kind),
            "signature": signature,
            "consecutive_same_count": consecutive + 1,
            "first_seen_at": previous.get("first_seen_at", now) if same else now,
            "last_seen_at": now,
            "next_recheck_at": now + max(0.0, interval),
            "resume_hint": "等待上游 control-plane evidence 更新后重试一次",
        }

    @staticmethod
    def _control_probe(probe, stats=None):
        result = dict(probe) if isinstance(probe, dict) else {}
        if isinstance(stats, dict):
            result["rejection_reason_counts"] = dict(stats.get("rejection_reason_counts") or {})
            result["failure_taxonomy"] = stats.get("status") or result.get("failure_taxonomy")
        bounded = {
            "failure_taxonomy": AIFactoryRunner._safe_control_token(
                result.get("failure_taxonomy")
            ),
            "frequency_evidence": AIFactoryRunner._safe_control_token(
                result.get("frequency_evidence")
            ),
            "capability_status": AIFactoryRunner._safe_control_token(
                result.get("capability_status")
            ),
            "budget_shortage_reason": AIFactoryRunner._safe_control_token(
                result.get("budget_shortage_reason"), default="NONE"
            ),
            "budget_shortage_count": AIFactoryRunner._safe_nonnegative_count(
                result.get("budget_shortage_count")
            ),
            "rejection_reason_counts": {
                AIFactoryRunner._safe_control_token(key):
                AIFactoryRunner._safe_nonnegative_count(value)
                for key, value in (
                    result.get("rejection_reason_counts")
                    if isinstance(result.get("rejection_reason_counts"), dict)
                    else {}
                ).items()
                if AIFactoryRunner._safe_nonnegative_count(value) > 0
            },
        }
        return {
            key: bounded[key]
            for key in (
                "failure_taxonomy", "frequency_evidence", "capability_status",
                "budget_shortage_reason", "budget_shortage_count",
                "rejection_reason_counts",
            )
            if key in result or key == "failure_taxonomy"
        }

    def _remember_blocker(self, session, kind, probe, now):
        config = getattr(self.agent, "factory_config", {}) or {}
        session["blocker"] = self._blocker_projection(
            kind, probe, now=now, previous=session.get("blocker"),
            recheck_sec=config.get("blocker_recheck_sec", self.BLOCKER_RECHECK_SEC),
        )

    @staticmethod
    def route_decision(previous_probe, current_probe, *, route_attempt,
                       no_gain_attempts, max_route_attempts=3,
                       max_no_gain_attempts=2):
        """Make a bounded route decision from control-plane fingerprints."""
        previous_probe = previous_probe if isinstance(previous_probe, dict) else {}
        current_probe = current_probe if isinstance(current_probe, dict) else {}
        current_taxonomy = str(current_probe.get("failure_taxonomy") or "UNKNOWN")
        candidate_changed = (
            set(current_probe.get("candidate_expression_fingerprints") or ())
            != set(previous_probe.get("candidate_expression_fingerprints") or ())
        )
        new_metadata = any(
            key in previous_probe or key in current_probe
            for key in ("semantic_mechanism_fingerprints", "structural_family_fingerprints",
                        "field_concept_fingerprints", "research_question_fingerprints")
        )
        changes = []
        for name, key in (
            ("semantic_change", "semantic_mechanism_fingerprints"),
            ("relationship_change", "relationship_fingerprints"),
            ("dataset_change", "dataset_route"),
            ("question_change", "research_question_fingerprints"),
        ):
            if set(current_probe.get(key) or ()) != set(previous_probe.get(key) or ()):
                changes.append(name)
        for key in ("failure_taxonomy", "frequency_evidence", "capability_status",
                    "rejection_reason_counts", "budget_shortage_reason", "budget_shortage_count"):
            if key in previous_probe and key in current_probe:
                if current_probe.get(key) != previous_probe.get(key):
                    changes.append(key)
        if candidate_changed:
            changes.insert(0, "candidate_change")
        information_gain = bool(
            any(item in changes for item in
                ("semantic_change", "relationship_change", "dataset_change", "question_change",
                 "failure_taxonomy", "frequency_evidence", "capability_status",
                 "rejection_reason_counts", "budget_shortage_reason", "budget_shortage_count"))
            if new_metadata else changes
        )
        next_no_gain = 0 if information_gain else int(no_gain_attempts) + 1
        if int(route_attempt) >= int(max_route_attempts):
            action, reason = "STOP", "ROUTE_ATTEMPTS_EXHAUSTED"
        elif (not information_gain and
              next_no_gain >= int(max_no_gain_attempts)):
            action, reason = "STOP", "NO_INFORMATION_GAIN"
        else:
            action, reason = "REROUTE", current_taxonomy
        return {
            "action": action, "reason": reason,
            "information_gain": information_gain,
            "information_changes": changes,
            "change_type": (
                "none" if not changes else
                "candidate_change_only" if changes == ["candidate_change"] else
                "research_information_change"
            ),
            "no_gain_attempts": next_no_gain,
            "route_attempt": int(route_attempt),
            "route_index": min(int(route_attempt) + 1, 4),
            "route_name": (
                "current_bundle" if int(route_attempt) == 0 else
                "same_dataset_relationship" if int(route_attempt) == 1 else
                "same_dataset_new_mechanism" if int(route_attempt) == 2 else
                "new_dataset_composition" if int(route_attempt) == 3 else
                "rediscovery"
            ),
        }

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
        probe["research_question_fingerprints"] = sorted({
            str(item.get("experiment_question") or item.get("research_question")).strip().lower()
            for item in items
            if item.get("experiment_question") or item.get("research_question")
        })
        probe["budget_shortage_count"] = int(
            budget_audit.get("shortage_count", 0)
        ) if isinstance(budget_audit, dict) else 0
        probe["budget_shortage_reason"] = (
            budget_audit.get("shortage_reason")
            if isinstance(budget_audit, dict) else None
        )
        return probe

    @classmethod
    def read_session(cls, state_dir):
        """Read the single factory envelope without constructing an Agent."""
        path = os.path.join(state_dir, cls.SESSION_FILE)
        try:
            with open(path, encoding="utf-8") as handle:
                session = json.load(handle)
            if not isinstance(session, dict) or not isinstance(session.get("session_id"), str):
                return None
            return session
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return None

    @classmethod
    def request_stop(cls, state_dir):
        """Set a durable stop request in the canonical session envelope."""
        try:
            with single_instance_scope(state_dir, operation="factory-stop"):
                return cls._request_stop_owned(state_dir)
        except OwnerBusyError:
            return {
                "status": "LOCAL_OWNER_BUSY",
                "last_action": "LOCAL_OWNER_BUSY",
            }

    @classmethod
    def _request_stop_owned(cls, state_dir):
        session = cls.read_session(state_dir)
        if not session or session.get("status") != "RUNNING":
            return None
        try:
            float(session["deadline"])
        except (KeyError, TypeError, ValueError):
            return None
        session["stop_requested"] = True
        session["last_action"] = "STOP_REQUESTED"
        atomic_write_json_if_changed(
            os.path.join(state_dir, cls.SESSION_FILE), session,
            ignored_keys=("updated_at",),
        )
        return session

    @classmethod
    def status_view(cls, state_dir):
        """Return only the stable, decision-useful session fields."""
        session = cls.read_session(state_dir)
        if not session:
            if os.path.exists(os.path.join(state_dir, cls.SESSION_FILE)):
                return {
                    "status": "RECONCILE_REQUIRED",
                    "last_action": "INVALID_SESSION",
                }
            return None
        keys = (
            "schema_version", "session_id", "started_at", "deadline", "status",
            "stop_requested", "rounds_completed", "simulations_reserved",
            "simulation_cap", "quota", "last_round", "last_action", "last_result",
            "blocker",
        )
        view = {key: session[key] for key in keys if key in session}
        if isinstance(view.get("quota"), dict):
            view["quota"] = {
                key: view["quota"][key]
                for key in (
                    "schema_version", "timezone", "local_date", "week_start",
                    "daily_cap", "weekly_cap", "daily_reserved", "weekly_reserved",
                )
                if key in view["quota"]
            }
        if isinstance(view.get("last_result"), dict):
            view["last_result"] = {
                key: view["last_result"][key]
                for key in ("round_no", "proposals", "summary_round", "verdicts", "best",
                            "status", "error_type", "message")
                if key in view["last_result"]
            }
        if isinstance(view.get("blocker"), dict):
            view["blocker"] = {
                key: view["blocker"].get(key)
                for key in (
                    "kind", "consecutive_same_count", "next_recheck_at", "resume_hint",
                )
                if key in view["blocker"]
            }
        return view

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
        raw_state = session.get("quota")
        if raw_state is None:
            # Migrate the legacy aggregate reservation conservatively.  The
            # old envelope had no local-day field, so keeping today's bucket
            # at least as full as the legacy reservation fails closed.
            try:
                legacy_reserved = max(0, int(session.get("simulations_reserved", 0)))
            except (TypeError, ValueError):
                legacy_reserved = 0
            if legacy_reserved > weekly_cap:
                raise ValueError("legacy reservation exceeds weekly quota")
            state = quota.initial_state()
            state["weekly_reserved"] = legacy_reserved
            state["daily_reserved"] = min(legacy_reserved, daily_cap)
        else:
            state = quota.normalize_state(raw_state)
        session["quota"] = state
        # Keep the legacy field as a compatibility projection for existing
        # status consumers; quota.remaining() is the admission source of truth.
        session["simulation_cap"] = weekly_cap
        session["simulations_reserved"] = state["weekly_reserved"]
        return quota

    @staticmethod
    def _quota_remaining(session, quota):
        state = quota.normalize_state(session.get("quota"))
        session["quota"] = state
        session["simulations_reserved"] = state["weekly_reserved"]
        return quota.remaining(state)

    @staticmethod
    def _quota_reserve(session, quota, count):
        state = quota.reserve(session.get("quota"), count)
        session["quota"] = state
        session["simulations_reserved"] = state["weekly_reserved"]

    @staticmethod
    def _quota_release(session, quota, count):
        state = quota.release(session.get("quota"), count)
        session["quota"] = state
        session["simulations_reserved"] = state["weekly_reserved"]

    def run(self, duration_sec=86400, max_rounds=0, idle_sleep_sec=30,
            max_simulations=240, daily_simulation_cap=None,
            weekly_simulation_cap=None):
        try:
            with single_instance_scope(self.state_dir, operation="factory-run"):
                return self._run_locked(
                    duration_sec=duration_sec,
                    max_rounds=max_rounds,
                    idle_sleep_sec=idle_sleep_sec,
                    max_simulations=max_simulations,
                    daily_simulation_cap=daily_simulation_cap,
                    weekly_simulation_cap=weekly_simulation_cap,
                )
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
                session["last_action"] = f"STOP_{blocker.get('kind', 'BLOCKER')}"
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
                return session
        # A zero-duration probe must never overwrite or stop a live session.
        # The explicit --factory-stop command is the only control-plane action
        # allowed to request a running factory to stop.
        if duration <= 0 and session and session.get("status") == "RUNNING":
            return session
        if duration <= 0 or not session or session.get("status") != "RUNNING" or session.get("deadline", 0) <= now:
            session = {
                "schema_version": CHECKPOINT_VERSION,
                "created_by_version": CREATED_BY_VERSION,
                "session_id": uuid.uuid4().hex[:16],
                "started_at": now,
                "deadline": now + duration,
                "status": "RUNNING",
                "rounds_completed": 0,
                "simulations_reserved": 0,
                "simulation_cap": weekly_cap,
                "quota": quota.initial_state(),
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
        session.setdefault("last_feasibility_probe", None)
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
                optimized = []
                signal_records = []
                if hasattr(self.agent, "optimizable_signal_records"):
                    signal_records = self.agent.optimizable_signal_records()
                if signal_records:
                    optimized = self.agent.generate_optimized_proposals(
                        signal_records, max_candidates=min(4, batch_size - 1)
                    )
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
                        decision = self.route_decision(
                            session.get("last_feasibility_probe"), feasibility,
                            route_attempt=session.get("route_attempt", 0),
                            no_gain_attempts=session.get("no_gain_attempts", 0),
                            max_route_attempts=config.get("max_route_attempts", 3),
                            max_no_gain_attempts=config.get("max_no_gain_attempts", 2),
                        )
                        session["last_feasibility_probe"] = feasibility
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
                            "status": "FACTORY_FEASIBILITY_BLOCKED",
                            "failure_taxonomy": feasibility.get("failure_taxonomy"),
                            "feasibility_probe": feasibility,
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
                    hypothesis,
                    bundle.get("fields") or [],
                    bundle.get("operator_reference") or {},
                    target=batch_size,
                    optimized=optimized,
                    excluded_expressions=self._known_expressions(),
                    research_context=bundle.get("context"),
                    # A rejected batch must not be regenerated byte-for-byte:
                    # probe_offset is the bounded exploration retry identity.
                    seed=f"{hypothesis['id']}:round:{round_no}:probe:{probe_offset}",
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
                "layer_policy": {
                    "optimization": "cloud_priority_then_current_run",
                    "exploration": "seeded_factory_signal_discovery",
                    "code_screen": "required_before_agent_screen",
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
            if isinstance(checkpoint, dict) and checkpoint.get("complete"):
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
        if checkpoint and checkpoint.get("complete"):
            return []
        if checkpoint and not checkpoint.get("complete"):
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
                if record.get("malformed") or not record.get("checkpoint", {}).get("complete"):
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
        current = self.read_session(self.state_dir)
        if (
            current
            and current.get("session_id") == session.get("session_id")
            and current.get("stop_requested")
        ):
            session["stop_requested"] = True
        atomic_write_json_if_changed(
            self.session_path, session, ignored_keys=("updated_at",)
        )

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
            "message": str(exc)[:200],
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
        session["last_action"] = f"{action}_RECONCILE_REQUIRED"
        session["last_result"] = {
            "status": "RECONCILE_REQUIRED",
            "error_type": error_name,
            "message": str(exc)[:200],
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
            "best": (result.get("best") or {}).get("expression")
                    if isinstance(result.get("best"), dict) else None,
        }
