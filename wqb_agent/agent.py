"""
ROLE: CORE
AGENT_RELEVANCE: HIGH
PURPOSE: Compose discovery, proposal validation, guarded Simulation execution,
recovery, and evidence updates through the existing runtime.
READ WHEN: changing experiment execution or recovery orchestration.
DO NOT USE FOR: choosing economic hypotheses or bypassing `research_api`.
"""

import json
import os
import time
from contextlib import nullcontext

from .alpha_colors import classify_alpha_color
from .alpha_feed_cache import WEEKLY_SIMULATION_CAP, WeeklyAlphaFeedCache, refresh_due
from .alpha_feed_workflow import AlphaFeedHooks
from .alpha_pool import build_pool_snapshot
from .artifacts import iter_jsonl_objects
from .behavior import extract_behavior_series
from .config import normalize_config
from .daily_cache import DailyResearchCache
from .evidence import (
    has_resolved_self_correlation,
    overlay_cached_checks,
    refresh_self_correlation_cache,
)
from .expression import canonical_expression, submission_fingerprint
from .heartbeat import HeartbeatSink
from .identity import candidate_identity
from .incremental_value import build_incremental_value
from .locking import OwnerBusyError, single_instance_scope
from .metrics import (
    check_pass,
    checks_passed,
    num,
)
from .optimization_decision import optimization_decision_identity
from .optimizer_workflow import OptimizerHooks
from .pre_correlation import (
    delay_metric_thresholds,
    pre_self_correlation_eligibility,
    turnover_bounds,
)
from .proposal_contract import (
    SETTING_OVERRIDES,
    _operator_reference,
)
from .proposal_execution import ProposalExecutionHooks
from .research_evidence import ResearchEvidenceBundle, classify_research
from .runtime_components import build_runtime_components
from .runtime_composition import AgentWorkflowHooks, build_agent_workflows
from .runtime_policy import build_agent_runtime_policy
from .search_outcome import (
    SearchOutcome,
    extract_statistical_decision,
    settle_search_outcome,
)
from .search_snapshot import SearchSnapshot
from .state import (
    UNRESOLVED_STATUSES,
)
from .submission import (
    latest_active_snapshot,
    self_correlation_evidence,
    submission_eligibility,
)
from .suggestion_workflow import SuggestionHooks
from .validation_report import (
    build_validation_report,
    default_validation_plan,
)

SEED_HYPOTHESES = [
    {
        "id": "h-seed-reversal",
        "statement": "Short-term return reversal: stocks that rose sharply over the last 5 days tend to revert in the near term.",
        "tags": ["reversal", "return", "price", "short-term"],
        "direction": "reversal",
        "datasets": ["pv1", "pv13"],
    },
    {
        "id": "h-seed-analyst",
        "statement": "Analyst target-price revisions upward predict short-term outperformance.",
        "tags": ["analyst", "forecast", "revision", "target"],
        "direction": "long",
        "datasets": ["analyst4"],
    },
    {
        "id": "h-seed-option",
        "statement": "Stocks with elevated implied volatility earn lower forward returns.",
        "tags": ["option", "volatility", "implied", "risk"],
        "direction": "reversal",
        "datasets": ["option8", "option9"],
    },
    {
        "id": "h-seed-model",
        "statement": "High model risk scores predict lower forward returns.",
        "tags": ["model", "score", "risk", "composite"],
        "direction": "reversal",
        "datasets": ["model16", "model51"],
    },
    {
        "id": "h-seed-news",
        "statement": "Positive news sentiment predicts short-term positive returns.",
        "tags": ["news", "sentiment", "positive"],
        "direction": "long",
        "datasets": ["news18", "news12"],
    },
    {
        "id": "h-seed-fundamental",
        "statement": "Firms with strong earnings growth continue to outperform.",
        "tags": ["fundamental", "growth", "earning"],
        "direction": "long",
        "datasets": ["fundamental6", "fundamental2"],
    },
]

EXPLORATION_HYPOTHESES = [
    {"id": "h-explore-fund-cashflow", "statement": "Strong operating cash flow quality predicts outperformance.", "tags": ["cashflow", "operating", "quality"], "direction": "long", "datasets": ["fundamental6", "fundamental2"]},
    {"id": "h-explore-fund-leverage", "statement": "High leverage and debt burden predict lower forward returns.", "tags": ["debt", "leverage", "liability"], "direction": "reversal", "datasets": ["fundamental6", "fundamental2"]},
    {"id": "h-explore-fund-value", "statement": "Low valuation relative to book value or enterprise value predicts outperformance.", "tags": ["value", "book", "enterprise"], "direction": "long", "datasets": ["fundamental6", "fundamental2"]},
    {"id": "h-explore-fund-assets", "statement": "Efficient asset utilization and profitability predict outperformance.", "tags": ["asset", "profitability", "efficiency"], "direction": "long", "datasets": ["fundamental6", "fundamental2"]},
    {"id": "h-explore-news-attention", "statement": "Abnormally high news attention is followed by short-term reversal.", "tags": ["attention", "buzz", "count"], "direction": "reversal", "datasets": ["news18", "news12"]},
    {"id": "h-explore-news-novelty", "statement": "Novel company news contains information that persists into future returns.", "tags": ["novelty", "novel", "unique"], "direction": "long", "datasets": ["news18", "news12"]},
    {"id": "h-explore-news-relevance", "statement": "Highly relevant company-specific news predicts short-term returns.", "tags": ["relevance", "relevant", "company"], "direction": "long", "datasets": ["news18", "news12"]},
    {"id": "h-explore-news-volume", "statement": "Extreme news volume reflects overreaction and predicts reversal.", "tags": ["volume", "story", "article"], "direction": "reversal", "datasets": ["news18", "news12"]},
]


class Agent:
    def __init__(self, client, config):
        self.client = client
        app_config = normalize_config(config)
        policy = build_agent_runtime_policy(app_config)
        components = build_runtime_components(self.client, app_config)
        self.runtime_policy = policy
        self.runtime_components = components
        self._install_policy(policy)
        self._install_components(components)
        self._init_iteration_state()
        self._init_caches()
        self._init_heartbeat()
        operator_path = os.path.abspath(
            os.path.join(
                os.path.dirname(__file__), "..", "docs", "reference",
                "OPERATORS_CHEATSHEET.md",
            )
        )
        self.operator_reference = _operator_reference(operator_path)
        self._init_workflows()

    def _mutation_scope(self, operation):
        state_dir = getattr(self, "state_dir", None)
        if not state_dir:
            return nullcontext()
        return single_instance_scope(state_dir, operation=operation)

    def _local_owner_busy(self):
        self.last_run_stats = {
            **getattr(self, "last_run_stats", {}),
            "status": "LOCAL_OWNER_BUSY",
        }
        return None

    def _install_policy(self, policy):
        """将 resolved policy 集中投影为旧 Agent 兼容属性。"""
        self.simulation_settings = policy.simulation_settings
        self.factory_config = policy.factory_config
        self.state_dir = policy.state_dir
        self.max_rounds = policy.max_rounds
        self.candidates_per_round = policy.candidates_per_round
        self.max_proposals_per_round = policy.max_proposals_per_round
        self.factory_batch_size = policy.factory_batch_size
        self.research_allocation = policy.research_allocation
        self.research_integrity = policy.research_integrity
        self.fields_per_discovery = policy.fields_per_discovery
        self.pagination_limit = policy.pagination_limit
        self.max_pagination_pages = policy.max_pagination_pages
        self.poll_timeout_sec = policy.poll_timeout_sec
        self.context_experiments = policy.context_experiments
        self.correlation_refresh_window = policy.correlation_refresh_window
        self.quality_policy = policy.quality_policy
        self.statistical_policy = policy.statistical_policy
        self.robustness_policy = policy.robustness_policy
        self.incremental_policy = policy.incremental_policy
        self.max_field_alpha_count = policy.max_field_alpha_count
        self.require_platform_alpha_count = policy.require_platform_alpha_count
        self.min_factory_datasets = policy.min_factory_datasets
        self.min_cross_dataset_pairs = policy.min_cross_dataset_pairs
        self.dataset_pool = list(policy.dataset_pool)
        self.alpha_feed_refresh_interval_sec = policy.alpha_feed_refresh_interval_sec
        self.heartbeat_interval_sec = policy.heartbeat_interval_sec

    def _install_components(self, components):
        """将唯一的基础组件集中投影为现有 Agent 属性。"""
        self.search_policy = components.search_policy
        self.memory = components.memory
        self.trajectory = components.trajectory
        self.trial_ledger = components.trial_ledger
        self.builder = components.builder
        self.alpha_factory = components.builder.factory
        self.discovery = components.discovery
        self.simulator = components.simulator
        self.reflector = components.reflector
        self.checkpoints = components.checkpoints
        self.submission_pool = components.submission_pool

    def _init_iteration_state(self):
        """初始化构造期状态，确保 hooks 使用前已有完整状态。"""
        self._loaded = False
        self._last_round_skipped = False
        self.last_run_stats = {
            "accepted": 0, "rejected": 0, "skipped": 0,
            "status": "NOT_STARTED",
        }

    def _init_caches(self):
        """创建本进程缓存；缓存不承载 trajectory、指标或证据。"""
        self.daily_cache = DailyResearchCache()
        feed_cache_path = os.path.join(
            os.path.dirname(os.path.abspath(self.state_dir)),
            ".alpha_feed_cache", "weekly.json",
        )
        self.alpha_feed_cache = WeeklyAlphaFeedCache(
            feed_cache_path,
            weekly_simulation_cap=WEEKLY_SIMULATION_CAP,
        )

    def _init_heartbeat(self):
        """Create one transient observer and share it with read workflows."""
        self.heartbeat = HeartbeatSink(interval_sec=self.heartbeat_interval_sec)
        self.discovery.heartbeat = self.heartbeat

    def _build_workflow_hooks(self):
        """集中绑定 Agent-owned 操作，不把 Agent 对象泄漏给 workflow。"""
        return AgentWorkflowHooks(
            suggestion=SuggestionHooks(
                ensure_loaded=self._ensure_loaded,
                next_round_no=self.next_round_no,
                epoch_label=self.epoch_label,
                form_research_space=self._form_research_space,
                trusted_current_best=self._trusted_current_best,
                ensure_best_field=self._ensure_best_field,
                optimizer_gate_report=self.optimizer_gate_report,
                optimizer_context=self.optimizer_context,
                fallback_templates=lambda: list(EXPLORATION_HYPOTHESES) + list(SEED_HYPOTHESES),
            ),
            proposal_execution=ProposalExecutionHooks(
                ensure_loaded=self._ensure_loaded,
                next_round_no=self.next_round_no,
                terminal_identities=self._terminal_identities,
                refresh_platform_field_usage=self._refresh_platform_field_usage,
                read_field_cache=self._read_field_cache,
                known_field_types=self._known_field_types,
                proposal_settings=self._proposal_settings,
                completed_parent=self._completed_parent,
                record_trial_phase=self._record_trial_phase,
                record_candidate_rejection=self._record_candidate_rejection,
                on_simulation_update=self._on_simulation_update,
                record_live_result=self._record_live_result,
                refresh_self_correlation_evidence=self._refresh_self_correlation_evidence,
                mark_robustness_stability=self._mark_robustness_stability,
                sync_submission_pool=self._sync_submission_pool,
                save_state=self._save_state,
                write_context=self._write_context,
                print_summary=self._print_summary,
                write_sims_results=self._write_sims_results,
                validation_candidates=lambda: getattr(
                    self, "_validation_candidates", None
                ),
                set_last_round_skipped=lambda value: setattr(
                    self, "_last_round_skipped", value
                ),
                reset_best_exhausted=lambda: setattr(
                    self.memory, "best_exhausted", False
                ),
            ),
            alpha_feed=AlphaFeedHooks(
                get_all_user_alphas=getattr(
                    self.client, "get_all_user_alphas", None
                ),
            ),
            optimizer=OptimizerHooks(
                ensure_loaded=self._ensure_loaded,
                terminal_expressions=self._terminal_expressions,
                simulation_delay=lambda: (
                    self.simulation_settings or {}
                ).get("delay"),
                resolved_self_correlation=self.resolved_self_correlation,
            ),
        )

    def _init_workflows(self):
        """用已存在的组件和 hooks 完成四个 workflow 的唯一装配。"""
        workflows = build_agent_workflows(
            components=self.runtime_components,
            policy=self.runtime_policy,
            operator_reference=self.operator_reference,
            hooks=self._build_workflow_hooks(),
            daily_cache=self.daily_cache,
            weekly_cache=self.alpha_feed_cache,
            heartbeat=self.heartbeat,
        )
        self.suggestion_workflow = workflows.suggestion
        self.proposal_execution = workflows.proposal_execution
        self.alpha_feed_workflow = workflows.alpha_feed
        self.optimizer_workflow = workflows.optimizer

    # ------------------------------------------------------------ running

    def run(self, max_rounds=None):
        raise RuntimeError(
            "自动候选路径已退役：生产研究只能使用 python main.py suggest 后再 python main.py run-proposals。"
        )

    def next_round_no(self):
        """Continue from the last completed round (never restart from zero)."""
        self._ensure_loaded()
        rounds = [e.round for e in self.trajectory.experiments]
        rounds.extend(
            record["round_no"] for record in self.checkpoints.scan()
            if not record["malformed"]
        )
        return (max(rounds) + 1) if rounds else 1

    EPOCH_START = 619  # r619 起启用新纪元标签（用户 2026-08-22 政策）

    def epoch_label(self, round_no):
        """New-era round label: 'rb' + hexadecimal sequence.
        r619 -> rb0, r620 -> rb1, ... r634 -> rbf, r635 -> rb10."""
        seq = max(int(round_no) - self.EPOCH_START, 0)
        return f"rb{format(seq, 'x')}"

    # ------------------------------------------------- LLM-driven research

    def run_suggestion_round(self, round_no=None):
        """Compatibility facade for read-only suggestion generation."""
        return self.suggestion_workflow.run(round_no=round_no)

    def optimizable_signal_records(self, limit=128):
        """兼容 facade：返回已有证据驱动的优化记录。"""
        return self.optimizer_workflow.optimizable_signal_records(limit=limit)

    def optimizer_gate_report(self, parents=None):
        """兼容 facade：返回不含证据细节的优化 gate 计数。"""
        return self.optimizer_workflow.gate_report(parents)

    def inspect_optimizer_parents(self, limit=8):
        """Agent-facing：有限、只读的 evidence-eligible parent summaries。"""
        return self.optimizer_workflow.inspect_optimizer_parents(limit=limit)

    def optimizer_context(self, *, limit=8):
        """Agent-facing：bounded、只读的 metric-aware optimizer context。"""
        return self.optimizer_workflow.optimizer_context(limit=limit)

    def propose_optimization(self, decisions, *, max_candidates=4):
        """Agent-facing：校验 OptimizationDecision 后走唯一 CHILD 生成路径。"""
        try:
            with self._mutation_scope("propose-optimization"):
                authored = list(decisions or ())
                result = self.optimizer_workflow.generate_from_decisions(
                    authored, max_candidates=max_candidates
                )
                self._record_optimization_selection(authored, result)
                return result
        except OwnerBusyError:
            return {
                "status": "LOCAL_OWNER_BUSY",
                "proposals": [],
                "accepted": [],
                "rejected": [],
                "decision_results": [],
            }

    def _record_optimization_selection(self, decisions, result):
        """Bridge finalized decisions to the sole TrialLedger owner."""
        accepted = {item.get("parent_id") for item in (result or {}).get("accepted", [])}
        rejected = {
            item.get("parent_id"): item.get("reasons") or []
            for item in (result or {}).get("rejected", [])
            if item.get("parent_id")
        }
        decision_results = (result or {}).get("decision_results") or []
        authored = list(decisions or ())
        if decision_results and len(decision_results) != len(authored):
            raise ValueError("optimization decision result count mismatch")
        if decision_results and any(
            not isinstance(item, dict) or not item.get("decision_id")
            for item in decision_results
        ):
            raise ValueError("optimization decision result identity missing")
        results_by_id = {
            item.get("decision_id"): item for item in decision_results
            if isinstance(item, dict) and item.get("decision_id")
        }
        for index, decision in enumerate(authored):
            if not hasattr(decision, "parent_id") or not hasattr(decision, "decision"):
                continue
            expected_id = optimization_decision_identity(decision)
            metadata = results_by_id.get(expected_id)
            if decision_results and metadata is None:
                raise ValueError("optimization decision result identity mismatch")
            metadata = metadata or (decision_results[index] if index < len(decision_results) else {})
            outcome = metadata.get("outcome") if isinstance(metadata, dict) else None
            if outcome:
                pass
            elif decision.decision in {"STOP", "REROUTE"}:
                outcome = decision.decision
            elif decision.parent_id in rejected:
                outcome = "PRUNED" if any(
                    "PRUNE" in str(reason).upper() for reason in rejected[decision.parent_id]
                ) else "REJECTED"
            elif decision.parent_id in accepted:
                outcome = "ACCEPTED"
            else:
                outcome = "PRUNED" if any(
                    "PRUNE" in str(reason).upper() for reason in rejected.get(decision.parent_id, ())
                ) else "REJECTED"
            self.trial_ledger.record_optimization_selection(
                decision, outcome=outcome, emitted=outcome == "GENERATED"
            )

    def refresh_remote_alpha_feed(self, *, limit=100):
        """兼容 facade：执行 Alpha Feed 的只读同步。"""
        return self.alpha_feed_workflow.refresh(limit=limit)

    def refresh_remote_alpha_feed_if_due(self, *, limit=100):
        """Refresh Feed in-process at a lifecycle boundary, never as a scheduler."""
        now = time.time()
        cache_exists = os.path.exists(self.alpha_feed_cache.path)
        freshness = self.alpha_feed_cache.freshness_snapshot(
            now=now, interval_sec=self.alpha_feed_refresh_interval_sec
        )
        last_attempt = getattr(self, "_feed_last_attempt_at", None)
        if not refresh_due(now, freshness.get("last_success_at"),
                           self.alpha_feed_refresh_interval_sec):
            result = {"status": "FEED_REFRESH_NOT_DUE", **freshness,
                      "last_attempt_at": last_attempt,
                      "last_attempt_status": getattr(self, "_feed_last_attempt_status", None)}
            return result
        self._feed_last_attempt_at = now
        try:
            snapshot = self.refresh_remote_alpha_feed(limit=limit)
        except Exception as exc:
            status = "FEED_REFRESH_INVALID_CACHE" if (
                cache_exists and freshness.get("freshness") == "UNKNOWN"
            ) else (
                "FEED_REFRESH_QUERY_TOO_BROAD"
                if exc.__class__.__name__ == "WQBQueryTooBroadError"
                else "FEED_REFRESH_TRANSPORT_ERROR"
            )
            self._feed_last_attempt_status = status
            return {"status": status, **self.alpha_feed_cache.freshness_snapshot(
                now=now, interval_sec=self.alpha_feed_refresh_interval_sec
            ), "last_attempt_at": now, "last_attempt_status": status}
        self._feed_last_attempt_status = "FEED_REFRESH_OK"
        return {"status": "FEED_REFRESH_OK", **snapshot,
                **self.alpha_feed_cache.freshness_snapshot(
                    now=now, interval_sec=self.alpha_feed_refresh_interval_sec
                ), "last_attempt_at": now,
                "last_attempt_status": "FEED_REFRESH_OK"}

    def generate_optimized_proposals(self, parents=None, *, max_candidates=4):
        """兼容 facade：生成受限的 evidence-backed CHILD proposals。"""
        return self.optimizer_workflow.generate(
            parents, max_candidates=max_candidates
        )

    def run_proposals(self, path=None, allow_unresolved_checkpoint=False):
        """Compatibility facade for the guarded proposal workflow."""
        try:
            with self._mutation_scope("run-proposals"):
                self.proposal_execution.update_agent_config(
                    factory_batch_size=self.factory_batch_size,
                    min_factory_datasets=self.min_factory_datasets,
                    min_cross_dataset_pairs=self.min_cross_dataset_pairs,
                    candidates_per_round=self.candidates_per_round,
                    max_proposals_per_round=self.max_proposals_per_round,
                    research_allocation=self.research_allocation,
                    research_integrity=self.research_integrity,
                    max_field_alpha_count=self.max_field_alpha_count,
                    require_platform_alpha_count=self.require_platform_alpha_count,
                )
                result = self.proposal_execution.run(
                    path=path,
                    allow_unresolved_checkpoint=allow_unresolved_checkpoint,
                )
                self.last_run_stats = dict(self.proposal_execution.last_run_stats)
                return result
        except OwnerBusyError:
            return self._local_owner_busy()

    # ----------------------------------------------------- crash recovery

    def _proposal_checkpoint_path(self, round_no):
        """Compatibility wrapper; checkpoint ownership is the workflow's."""
        return self.proposal_execution._proposal_checkpoint_path(round_no)

    def skip_stale_reconciled(self, round_no, simulation_id, min_attempts=3):
        try:
            with self._mutation_scope("skip-stale"):
                return self.proposal_execution.skip_stale_reconciled(
                    round_no, simulation_id, min_attempts=min_attempts
                )
        except OwnerBusyError:
            return self._local_owner_busy()

    def skip_submit_unknown_authorized(self, round_no, proposal_id):
        try:
            with self._mutation_scope("skip-submit-unknown"):
                return self.proposal_execution.skip_submit_unknown_authorized(
                    round_no, proposal_id
                )
        except OwnerBusyError:
            return self._local_owner_busy()

    def finalize_recorded_round(self, round_no):
        try:
            with self._mutation_scope("finalize-round"):
                return self.proposal_execution.finalize_recorded_round(round_no)
        except OwnerBusyError:
            return self._local_owner_busy()

    def _unfinished_checkpoint_except(self, round_no):
        return self.proposal_execution._unfinished_checkpoint_except(round_no)

    def _write_proposal_checkpoint(self, round_no, hypothesis, experiments, complete):
        return self.proposal_execution._write_proposal_checkpoint(
            round_no, hypothesis, experiments, complete
        )

    def _load_proposal_checkpoint(self, round_no):
        return self.proposal_execution._load_proposal_checkpoint(round_no)

    def _resume_proposal_checkpoint(self, checkpoint):
        return self.proposal_execution.resume_checkpoint(checkpoint)


    def _read_field_cache(self):
        """Read the on-disk field cache once for types and verified profiles."""
        datasets = None
        # FieldDiscovery already selected the authoritative complete catalog
        # or the legacy TTL cache. Reuse that parsed mapping instead of
        # opening/parsing a second copy during every proposal batch.
        if hasattr(self.discovery, "cached_datasets"):
            datasets = self.discovery.cached_datasets()
        cache_path = os.path.join(self.state_dir, "fields_cache.json")
        field_types = {}
        profiles_by_id = {}
        if datasets is None:
            try:
                with open(cache_path, encoding="utf-8") as f:
                    cache = json.load(f)
                datasets = cache.get("datasets") or {}
            except (OSError, ValueError, json.JSONDecodeError):
                return {}, {}
        try:
            for dataset_id, fields in (datasets or {}).items():
                for field in fields or []:
                    if not isinstance(field, dict):
                        continue
                    field_id = field.get("id")
                    field_type = field.get("type")
                    if field_id and field_type:
                        field_types.setdefault(str(field_id), set()).add(str(field_type))
                    if not field_id or not field_type or not field.get("description"):
                        continue
                    profile = dict(field)
                    profile["dataset"] = self._field_dataset_id(field, dataset_id)
                    profile.setdefault("semantic_status", "KNOWN")
                    profiles_by_id.setdefault(str(field_id), []).append(profile)
        except (AttributeError, TypeError):
            return {}, {}
        for field_id, types in field_types.items():
            field_types[field_id] = (
                next(iter(types)) if len(types) == 1 else "AMBIGUOUS"
            )
        profiles = {}
        for field_id, items in profiles_by_id.items():
            if len(items) == 1:
                profiles[field_id] = items[0]
                continue
            for profile in items:
                dataset = profile.get("dataset")
                key = f"{dataset}::{field_id}" if dataset is not None else field_id
                profiles[key] = profile
        return field_types, profiles

    @staticmethod
    def _field_dataset_id(field, fallback=None):
        """Normalize BRAIN's string-or-object dataset field to a stable id."""
        raw = field.get("dataset") if isinstance(field, dict) else None
        if isinstance(raw, dict):
            raw = raw.get("id") or raw.get("name")
        if raw is None:
            raw = fallback
            if isinstance(raw, dict):
                raw = raw.get("id") or raw.get("name")
        return str(raw) if isinstance(raw, (str, int)) and str(raw).strip() else None

    def _refresh_platform_field_usage(self, payload, proposal_list):
        """Recheck field usage on BRAIN without retaining result payloads."""
        discovery = self.discovery
        if not getattr(discovery, "platform_usage_refresh", False):
            return {}
        dataset_ids = []
        research_space = payload.get("research_space") or {}
        dataset_ids.extend(research_space.get("datasets") or [])
        for field in payload.get("fields") or payload.get("suggestion_fields") or []:
            if isinstance(field, dict) and field.get("dataset"):
                dataset_ids.append(field["dataset"])
        for proposal in proposal_list:
            if isinstance(proposal, dict):
                dataset_ids.extend(proposal.get("datasets") or [])
        discovery.refresh_platform_usage(dataset_ids)
        return discovery.platform_usage_by_field(dataset_ids)

    def _known_field_types(self, payload, cached_field_types=None):
        """Build a field-id -> type map from real discovery artifacts."""
        # The ``suggest`` command may carry an explicit field-type manifest assembled
        # from the platform response.  Prefer it as the authoritative source
        # so manually/externally verified MATRIX fields are not lost when the
        # on-disk discovery cache is stale or incomplete.
        # Cache/catalog is the fallback layer; the current suggestion bundle
        # is newer evidence and must overlay it, never the reverse.
        if cached_field_types is None:
            cached_field_types, _ = self._read_field_cache()
        field_types = {
            str(field_id): str(field_type)
            for field_id, field_type in (cached_field_types or {}).items()
            if field_id and field_type
        }
        for field_id, field_type in (payload.get("field_types") or {}).items():
            if field_id and field_type:
                field_types[str(field_id)] = str(field_type)
        for field in (payload.get("suggestion_fields") or payload.get("fields") or []):
            if isinstance(field, dict) and field.get("id") and field.get("type"):
                field_types[str(field["id"])] = str(field["type"])
        return field_types

    def _verified_field_profiles(self):
        """Load known field metadata from the TTL-bounded discovery cache."""
        _, profiles = self._read_field_cache()
        return profiles

    def _proposal_settings(self, overrides=None):
        """Merge the allowed proposal overrides onto complete defaults.

        BRAIN requires every simulation setting. A partial ``settings`` object
        must therefore never replace the full config, and settings outside the
        user-authorized universe/truncation/decay whitelist are rejected.
        """
        if overrides is None:
            return dict(self.simulation_settings)
        if not isinstance(overrides, dict):
            raise ValueError("settings 必须是对象")
        unknown = sorted(set(overrides) - SETTING_OVERRIDES)
        if unknown:
            raise ValueError(f"settings 包含未授权参数: {unknown}")
        merged = dict(self.simulation_settings)
        merged.update(overrides)
        if "universe" in overrides:
            value = overrides["universe"]
            if not isinstance(value, str) or not value.strip():
                raise ValueError("universe 必须是非空字符串")
        if "truncation" in overrides:
            try:
                value = float(overrides["truncation"])
            except (TypeError, ValueError):
                raise ValueError("truncation 必须是数值") from None
            if not 0.02 <= value <= 0.15:
                raise ValueError("truncation 必须在 0.02 至 0.15 之间")
            merged["truncation"] = value
        if "decay" in overrides:
            value = overrides["decay"]
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 10:
                raise ValueError("decay 必须是 0 至 10 的整数")
        return merged

    def run_one_round(self, round_no):
        """Reject the retired single-round shortcut.

        Production research must pass through ``python main.py suggest`` and
        ``python main.py run-proposals`` so discovery, preflight, checkpoint
        recovery, and
        exactly-once submission rules cannot be bypassed.
        """
        raise RuntimeError(
            "run_one_round 会绕过 proposal 预检与 checkpoint，已禁止用于生产。"
        )

    # ------------------------------------------------------- hypothesis

    def _form_research_space(self, round_no):
        """Choose only a dataset/question before field discovery.

        The returned statement deliberately does not assert an economic
        effect.  The proposal's economic hypothesis is formed later from the
        selected field descriptions and must cite them verbatim.
        """
        seed = self._form_hypothesis(round_no)
        datasets = list(seed.get("datasets") or seed.get("dataset_hints") or [])
        # The configured pool is a real sampling scope, not merely a hint in
        # the prompt.  Include the complete pool while preserving the seed's
        # research direction; discovery will stratify it and record failures.
        for dataset_id in self.dataset_pool:
            if dataset_id not in datasets:
                datasets.append(dataset_id)
        if not datasets:
            datasets = list(self.dataset_pool)
        return {
            "id": seed.get("id", f"space-r{round_no}"),
            "statement": "Which low-usage, semantically documented fields can test a new mechanism?",
            "tags": list(seed.get("tags") or []),
            "datasets": datasets,
            "parent_best": seed.get("parent_best"),
        }

    def _form_hypothesis(self, round_no):
        """Prefer iterating on current best; when that branch is exhausted
        (last round skipped), switch to a next-idea with genuinely different
        fields, else to an unused seed hypothesis (new research branch)."""
        trusted_best = self._trusted_current_best()
        if (
            trusted_best
            and not (self.memory.best_exhausted or self._last_round_skipped)
            and not self._best_family_is_occupied()
        ):
            return self._iterate_best_hypothesis(round_no, trusted_best)

        if trusted_best and self._best_family_is_occupied():
            return self._exploration_seed(round_no)

        best_fields = set((trusted_best or {}).get("fields_used") or [])
        idea = self.memory.next_with_fields(round_no)
        idea_fields = set(idea.get("fields") or []) if idea else set()
        if idea and (not best_fields or not (idea_fields & best_fields)):
            datasets = idea.get("datasets") or []
            tags = list(idea_fields)[:1] + ["next", "memory"]
            return {
                "id": f"h-next-r{round_no}",
                "statement": idea["idea"],
                "tags": tags,
                "direction": "long",
                "datasets": datasets,
            }

        for seed in SEED_HYPOTHESES:
            if seed["id"] not in self.memory.used_hypotheses:
                return dict(seed)
        # all seeds used: cycle deterministically to the least-recently failed
        return dict(SEED_HYPOTHESES[round_no % len(SEED_HYPOTHESES)])

    def _best_family_is_occupied(self):
        """Prefer an unused cross-family seed when best is analyst4-based."""
        best = self._trusted_current_best() or {}
        if "analyst4" not in set(best.get("datasets") or []):
            return False
        return True

    def _exploration_seed(self, round_no):
        """Rotate through non-analyst4 families once best is submit-blocked."""
        return dict(EXPLORATION_HYPOTHESES[round_no % len(EXPLORATION_HYPOTHESES)])

    def _iterate_best_hypothesis(self, round_no, best=None):
        best = best or self._trusted_current_best()
        metrics = best.get("metrics") or {}
        sharpe = metrics.get("sharpe")
        direction = "reversal" if (sharpe is not None and sharpe < 0) else "long"
        fields = best.get("fields_used") or []
        datasets = best.get("datasets") or []
        tags = ["iterate", "best"]
        if fields:
            tags.append(fields[0])

        idea = self.memory.next_with_fields(round_no)
        if idea:
            statement = (
                f"{idea['idea']} (iterating on current best: {best['expression']})"
            )
            if idea.get("datasets"):
                datasets = sorted(set(datasets) | set(idea["datasets"]))
        else:
            statement = (
                f"Iterate on current best {best['expression']} with bounded "
                f"single-variable mutations."
            )
        return {
            "id": f"h-iter-r{round_no}",
            "statement": statement,
            "tags": tags,
            "direction": direction,
            "datasets": datasets,
            "parent_best": best.get("id"),
        }

    def _ensure_best_field(self, fields):
        best = self._trusted_current_best()
        best_fields = best.get("fields_used") or []
        known = {f["id"] for f in fields}
        datasets = best.get("datasets") or []
        merged = list(fields)
        for fid in best_fields:
            if fid not in known:
                merged.insert(
                    0,
                    {
                        "id": fid,
                        "name": fid,
                        "category": "preferred",
                        "dataset": datasets[0] if datasets else None,
                        "match_score": 0,
                    },
                )
        return merged

    def _trusted_current_best(self):
        """Only a stability-validated, healthy record may steer research."""
        best = self.memory.current_best or {}
        metrics = best.get("metrics") or {}
        if checks_passed(metrics) is not True:
            return None
        if best.get("validation_status") != "STABLE":
            return None
        report = best.get("validation_report") or {}
        if report.get("status") != "PASS" or report.get("candidate") != "parent":
            return None
        health = best.get("health") or {}
        if health and not health.get("ok", False):
            return None
        return best

    def _filter_unseen(self, candidates):
        seen = self._terminal_expressions()
        fresh = []
        for c in candidates:
            if canonical_expression(c["expression"]) not in seen:
                fresh.append(c)
        return fresh

    def _terminal_identities(self, expressions=None):
        """视为"已模拟过"的表达式集合（去重依据）。

        只把已定论的表达式计入：DONE 与研究级 FAILED（SYNTAX/DATA）不重复
        提交；UNKNOWN / PENDING / 系统级失败（限流、超时、网络、认证）不
        能证明方向结论——POST 可能未发生或结果未知，剔除去重以便重试
        （2026-08-19 r256 实测：2 条提交 429 耗尽 + 15 条 PENDING 曾因去重
        被永久锁死，无法重跑）。
        """
        from .failures import classify_experiment, is_research_relevant

        scoped = expressions is not None
        target_expressions = {
            canonical_expression(expression)
            for expression in (expressions or [])
            if isinstance(expression, str) and expression.strip()
        }
        if scoped and not target_expressions:
            return set(), set()
        memory_terminal = {
            canonical_expression(expression)
            for expression in self.memory.seen_expressions
            if isinstance(expression, str) and expression.strip()
            and (not scoped
                 or canonical_expression(expression) in target_expressions)
        }
        terminal = set()
        fingerprints = set()

        def apply(row):
            if not isinstance(row, dict):
                return
            expression = row.get("expression")
            if not isinstance(expression, str) or not expression.strip():
                return
            canonical = canonical_expression(expression)
            if scoped and canonical not in target_expressions:
                return
            status = row.get("status")
            fingerprint = row.get("submission_fingerprint")
            if not isinstance(fingerprint, str) or not fingerprint:
                settings = row.get("settings")
                if isinstance(settings, dict):
                    fingerprint = submission_fingerprint(expression, settings)
            fingerprint_key = (
                "settings::" + fingerprint
                if isinstance(fingerprint, str) and fingerprint
                else None
            )
            if status in UNRESOLVED_STATUSES:
                terminal.discard(canonical)
                if fingerprint_key:
                    fingerprints.discard(fingerprint_key)
                return
            if status in ("SKIPPED_STALE", "SKIPPED_UNKNOWN", "DONE"):
                terminal.add(canonical)
            elif status == "FAILED":
                kind = classify_experiment(
                    type("HistoricalExperiment", (), {
                        "status": status, "error": row.get("error")
                    })()
                )
                if kind is None or not is_research_relevant(kind):
                    terminal.discard(canonical)
                    if fingerprint_key:
                        fingerprints.discard(fingerprint_key)
                else:
                    terminal.add(canonical)
            if status in ("DONE", "SKIPPED_STALE", "SKIPPED_UNKNOWN") and fingerprint_key:
                fingerprints.add(fingerprint_key)

        # trajectory is the authoritative exact-dedupe source when present;
        # the memory set is only a fallback for in-memory/unit-test callers
        # without a durable trajectory.  Filtering before status/fingerprint
        # work keeps the returned sets bounded by this proposal batch while
        # preserving the one streaming pass over the append-only source.
        trajectory_path = getattr(self.trajectory, "path", None)
        if (
            getattr(self.trajectory, "persist", True)
            and trajectory_path
            and os.path.exists(trajectory_path)
        ):
            for row in self.trajectory.iter_rows() or ():
                apply(row)
        else:
            terminal.update(memory_terminal)
            for e in self.trajectory.experiments:
                apply(e.to_dict())
        # Completed checkpoints are the only cross-process research identity
        # retained by the new runtime.  Their compact rows are sufficient for
        # exactly-once and expression/fingerprint dedupe without restoring
        # local metrics or Alpha payloads.
        for record in self.checkpoints.scan():
            if record["malformed"] or not record["checkpoint"].get("complete"):
                continue
            for row in record["checkpoint"].get("experiments") or []:
                apply(row)
        return terminal, fingerprints

    def _terminal_expressions(self):
        """Compatibility view containing only canonical expressions."""
        return self._terminal_identities()[0]

    def _completed_parent(self, expression, resolved=None):
        """Return the resolved parent evidence for an evidence-dependent role."""
        if not isinstance(expression, str) or not expression.strip():
            return None
        target = canonical_expression(expression)
        for exp in reversed(self.trajectory.experiments):
            if canonical_expression(exp.expression) == target and exp.status == "DONE" and exp.metrics:
                return exp
        if resolved is not None and target in resolved:
            return resolved[target]
        return self.trajectory.find_completed_expression(expression)

    # ------------------------------------------------------------ helpers

    def _record_trial_phase(self, experiment, phase, outcome=None, reason=None, reason_code=None):
        """Best-effort audit only; never changes Simulation safety semantics."""
        try:
            self.trial_ledger.record(
                experiment, phase, outcome=outcome, reason=reason,
                reason_code=reason_code,
            )
        except Exception as exc:
            print(f"[TRIAL_LEDGER_WARN] {type(exc).__name__}: {exc}")

    def _record_candidate_rejection(self, candidate, stage, reason_code, reason):
        """Record every local rejection against its stable candidate identity."""
        try:
            if isinstance(candidate, dict):
                candidate = dict(candidate)
                candidate.setdefault("candidate_id", candidate_identity(candidate, round_no=candidate.get("round")))
            self.trial_ledger.record(
                candidate,
                "candidate_rejected",
                outcome="REJECTED",
                reason=reason,
                reason_code=reason_code,
                stage=stage,
            )
        except Exception as exc:
            print(f"[TRIAL_LEDGER_WARN] {type(exc).__name__}: {exc}")

    def _update_search_lifecycle(self, experiment, outcome=None):
        """Mirror transport state into the in-memory allocator idempotently."""
        if not hasattr(self, "search_policy"):
            return
        status = str(getattr(experiment, "status", "UNKNOWN") or "UNKNOWN").upper()
        if status == "SUBMIT_UNKNOWN":
            status = "UNKNOWN"
        if status in {"SKIPPED_STALE", "SKIPPED_UNKNOWN"}:
            status = "SKIPPED"
        if status not in {"RUNNING", "PENDING", "DONE", "FAILED", "UNKNOWN", "SKIPPED"}:
            return
        proposal = {
            "proposal_id": getattr(experiment, "allocation_key", None) or getattr(experiment, "proposal_id", None),
            "expression": getattr(experiment, "expression", ""),
            "dataset_family": getattr(experiment, "datasets", []),
            "template_family": getattr(experiment, "template_family", None),
        }
        reward = outcome.reward if outcome is not None else None
        error_text = str(getattr(experiment, "error", "") or "").upper()
        failure_category = "INFRA" if status == "FAILED" and any(token in error_text for token in (
            "TIMEOUT", "RATE_LIMIT", "AUTH", "INFRA", "NETWORK", "HTTP"
        )) else "RESEARCH"
        try:
            self.search_policy.release(
                proposal, status=status, reward=reward,
                outcome=outcome or failure_category,
            )
        except (TypeError, ValueError):
            pass

    def _on_simulation_update(self, experiment, round_no, hypothesis, experiments):
        if hasattr(self, "heartbeat"):
            statuses = [str(item.status).upper() for item in experiments]
            self.heartbeat.emit_stage(
                "SIMULATION_SETTLEMENT",
                done=statuses.count("DONE"), failed=statuses.count("FAILED"),
                running=statuses.count("RUNNING"), pending=statuses.count("PENDING"),
                unknown=statuses.count("UNKNOWN"),
                submit_unknown=statuses.count("SUBMIT_UNKNOWN"),
                known_progress_url=sum(bool(item.progress_url) for item in experiments),
                last_settlement_progress=experiment.status,
            )
        if experiment.status in {"RUNNING", "SUBMIT_UNKNOWN"}:
            self._record_trial_phase(
                experiment, "submitted", outcome=experiment.status
            )
            self._record_trial_phase(
                experiment, "simulation_submitted", outcome=experiment.status
            )
            self._update_search_lifecycle(experiment)
        self._write_proposal_checkpoint(
            round_no, hypothesis, experiments, complete=False
        )

    def emit_heartbeat(self, stage, **metadata):
        """Narrow transient diagnostic hook for compatibility orchestration."""
        if hasattr(self, "heartbeat"):
            return self.heartbeat.emit_stage(stage, **metadata)
        return False

    def _attach_candidate_meta(self, experiment, candidate):
        experiment.hypothesis_id = candidate.get("parent") or experiment.hypothesis_id
        experiment.mutation = candidate.get("mutation")
        experiment.rationale = candidate.get("rationale")

    def _print_experiment(self, exp):
        if exp.metrics:
            m = exp.metrics
            # 同时给出本地 id 与平台真实 alpha_id：本地 id 用于查
            # trajectory，alpha_id 用于 check_health/check_correlation/提交。
            tag = f"[{exp.id}]"
            if exp.alpha_id:
                tag += f" alpha={exp.alpha_id}"
            elapsed = (
                f" elapsed={exp.elapsed_sec:.0f}s" if exp.elapsed_sec is not None else ""
            )
            print(
                f"  {tag}{elapsed} {exp.expression[:60]} "
                f"sharpe={m.get('sharpe')} fitness={m.get('fitness')} "
                f"turnover={m.get('turnover')} returns={m.get('returns')} "
                f"drawdown={m.get('drawdown')} margin={m.get('margin')} "
                f"passed={m.get('passed')}"
            )
        else:
            elapsed = (
                f" elapsed={exp.elapsed_sec:.0f}s" if exp.elapsed_sec is not None else ""
            )
            print(f"  [{exp.id}]{elapsed} {exp.expression[:60]} {exp.status}: {exp.error}")

    def _record_live_result(self, exp):
        """Persist and analyze one result as soon as BRAIN returns it."""
        # Complete the local evidence envelope before the first append.  The
        # trajectory is append-only; mutating ``exp`` after ``add`` would not
        # update the already-written JSONL row and would silently lose the
        # correlation snapshot for crash recovery/reporting.
        exp.self_correlation = self_correlation_evidence(exp.metrics)
        verdict = self.reflector._classify(exp)
        outcome = SearchOutcome.from_experiment(
            exp,
            validation=getattr(exp, "validation_report", None),
            parent=self._completed_parent(getattr(exp, "parent_expression", None)),
            quality_label=verdict.get("label"),
        )
        exp.provisional_outcome = outcome.as_dict()
        exp.search_outcome = exp.provisional_outcome
        self._update_search_lifecycle(exp, outcome=outcome)
        self._record_trial_phase(exp, "completed", outcome=exp.status)
        self._record_trial_phase(
            exp, "simulation_settled", outcome=exp.status,
            reason=exp.error,
            reason_code=("INFRA" if exp.status == "FAILED" and
                         any(token in str(exp.error or "").upper() for token in
                             ("TIMEOUT", "RATE_LIMIT", "AUTH", "INFRA", "NETWORK", "HTTP"))
                         else ("RESEARCH" if exp.status == "FAILED" else None)),
        )
        self.trajectory.add(exp)
        # Classify at settlement time so a mixed batch or an interrupted
        # factory cannot hide completed color transitions until batch close.
        self._cache_color_result(exp)
        self._print_experiment(exp)
        metrics = exp.metrics or {}
        failed_checks = []
        for check in metrics.get("checks") or []:
            if check_pass(check) is not False:
                continue
            check = check if isinstance(check, dict) else {}
            detail = check.get("name") or "UNKNOWN_CHECK"
            if check.get("value") is not None:
                detail += f"={check.get('value')}"
            if check.get("limit") is not None:
                detail += f"(limit={check.get('limit')})"
            failed_checks.append(detail)
        health = exp.health
        health_text = "unknown"
        if health is not None:
            health_text = "OK" if health.get("ok") else (
                "FAIL:" + ";".join(health.get("reasons") or [])
            )
        rating = self._alpha_rating(metrics)
        print(
            f"[LIVE_ANALYSIS] alpha={exp.alpha_id or '-'} "
            f"verdict={verdict.get('label')} rating={rating} "
            f"self_correlation={exp.self_correlation['status']} "
            f"failed_checks={failed_checks or 'none'} health={health_text} "
            f"reason={verdict.get('reason')}"
        )
        if getattr(exp, "falsification", None):
            print(
                f"[FALSIFICATION] alpha={exp.alpha_id or '-'} "
                f"criterion={exp.falsification}"
            )

    def _cache_color_result(self, experiment):
        """Refresh the active day's color view for one settled experiment."""
        alpha_id = (
            experiment.get("alpha_id")
            if isinstance(experiment, dict)
            else getattr(experiment, "alpha_id", None)
        )
        if not alpha_id:
            return
        self.daily_cache.put_colors([{
            "alpha_id": str(alpha_id),
            "classification": classify_alpha_color(experiment),
        }])

    def resolved_self_correlation(self, alpha_id):
        """只读返回同进程 evidence cache 中已解析的 SELF_CORRELATION。

        ``None`` 表示尚未结算；跨进程仍然可以重新 GET，因为该 side-car 是
        re-fetchable 的 transient evidence，而不是 canonical trajectory。
        """
        cache = self.reflector.evidence_cache
        entry = cache.get(str(alpha_id)) if isinstance(cache, dict) else None
        if not has_resolved_self_correlation(entry):
            return None
        for check in (entry or {}).get("checks") or ():
            if str(check.get("name") or "").upper() != "SELF_CORRELATION":
                continue
            result = str(check.get("result") or "").upper()
            if result in {"PASS", "FAIL"}:
                return {"status": result, "check": dict(check)}
        passed = (entry or {}).get("passed")
        if isinstance(passed, bool):
            return {"status": "PASS" if passed else "FAIL", "check": {}}
        return None

    def _settled_self_correlation(self, exp):
        """Return the SELF_CORRELATION evidence for a DONE experiment, with the
        asynchronously-settled platform check (SELF_CORRELATION is PENDING at
        completion) overlaid from the evidence side-car.  This lets the
        submission gate read the *resolved* correlation value instead of the
        raw PENDING placeholder captured at simulation end."""
        metrics = exp.metrics or {}
        cached = (self.reflector.evidence_cache or {}).get(getattr(exp, "alpha_id", None))
        if cached:
            metrics = overlay_cached_checks(
                metrics, cached,
                (self.quality_policy or {}).get("max_self_correlation", 0.5),
            )
        return self_correlation_evidence(metrics)

    def _refresh_self_correlation_evidence(self, experiments):
        """Resolve SELF_CORRELATION only for submit-capable candidates.

        Exploratory and clearly sub-threshold results cannot enter the manual
        submission pool, so querying their asynchronous correlation endpoint
        only adds latency and cannot change a decision.  The admission rule is
        the shared ``pre_self_correlation_eligibility`` policy, so the Agent and
        ``scripts/refresh_self_correlation.py`` can never select different rows.
        """
        alpha_ids = self._pre_correlation_candidates(experiments)
        # Unit-test/fallback clients intentionally do not expose the live HTTP
        # session; leave their synthetic metrics untouched.
        if not alpha_ids or not hasattr(self.client, "_session"):
            return
        # P0-C：把既有 evidence side-car 直接作为 transient、可重新获取的
        # evidence view 复用。每次新建空 dict 会让同进程第二次 refresh 看不到
        # 已解析结果，从而重复 GET；canonical trajectory 与 Simulation 事实
        # 仍不被这条缓存改写。
        cache = self.reflector.evidence_cache
        if not isinstance(cache, dict):
            cache = {}
            self.reflector.evidence_cache = cache
        refreshed = refresh_self_correlation_cache(
            self.client, self.state_dir, alpha_ids,
            correlation_limit=(self.quality_policy or {}).get(
                "max_self_correlation", 0.5
            ),
            persist=False,
            cache=cache,
        )
        print(f"[EVIDENCE] SELF_CORRELATION refreshed {refreshed}/{len(alpha_ids)}")

    def _pre_correlation_candidates(self, experiments=None):
        """Return DONE alpha ids passing the shared pre-correlation gate.

        This is the single Agent-side selector: it reuses the same pure policy as
        the read-only backfill script, so both surfaces always agree.
        """
        alpha_ids = []
        candidates = list(experiments or [])
        candidates.extend(self.trajectory.recent(self.correlation_refresh_window))
        for parent, _report in getattr(self, "_validation_candidates", []) or []:
            candidates.append(parent)
        seen = set()
        delay = (self.simulation_settings or {}).get("delay")
        for exp in candidates:
            if exp.status != "DONE" or not exp.alpha_id:
                continue
            if exp.alpha_id in seen:
                continue
            seen.add(exp.alpha_id)
            eligibility = pre_self_correlation_eligibility(
                exp.metrics or {},
                delay=delay,
                quality_policy=self.quality_policy,
                health=exp.health,
            )
            if eligibility["eligible"]:
                alpha_ids.append(exp.alpha_id)
        return alpha_ids

    @staticmethod
    def _correlation_under(evidence, max_corr):
        """Strict correlation gate: PASS status AND a resolved numeric value
        strictly below ``max_corr``.  A missing/resolved-null value cannot be
        claimed to satisfy a numeric cap, so it fails closed."""
        if not evidence or evidence.get("status") != "PASS":
            return False
        check = evidence.get("check") or {}
        value = check.get("value")
        if value is None:
            return False
        try:
            return float(value) < float(max_corr)
        except (TypeError, ValueError):
            return False

    def _sync_submission_pool(self, experiments):
        """Queue only fully validated candidates under the platform
        SELF_CORRELATION cap set by the user (default strict <0.5).

        There is intentionally no submission call here: every record remains
        ``MANUAL_REQUIRED`` for the user to submit on BRAIN.
        """
        active_snapshot = latest_active_snapshot(self.state_dir)
        corr_cap = num((self.quality_policy or {}).get("max_self_correlation", 0.5))
        if corr_cap is None:
            corr_cap = 0.5
        eligible_records = []
        candidates = list(experiments)
        for parent, _report in getattr(self, "_validation_candidates", []) or []:
            if all(existing.id != parent.id for existing in candidates):
                candidates.append(parent)
        for exp in candidates:
            if exp.status != "DONE" or not exp.alpha_id:
                continue
            exp.self_correlation = self._settled_self_correlation(exp)
            effective_metrics = exp.metrics or {}
            cached = (self.reflector.evidence_cache or {}).get(exp.alpha_id)
            if cached:
                effective_metrics = overlay_cached_checks(
                    effective_metrics, cached, corr_cap
                )
            rating = self._alpha_rating(effective_metrics)
            healthy = bool((exp.health or {}).get("ok"))
            yearly = exp.yearly_evidence or {}
            # If the platform supplied annual aggregates, an unstable annual
            # profile is a hard promotion blocker. Legacy rows without this
            # optional evidence remain readable and are not rewritten.
            yearly_ok = (
                yearly.get("status") != "VERIFIED"
                or yearly.get("stable") is True
            )
            eligible = (
                rating in {"EXCELLENT", "SPECTACULAR"}
                and checks_passed(effective_metrics) is True
                and healthy
                and exp.validation_status == "STABLE"
                and isinstance(exp.validation_report, dict)
                and exp.validation_report.get("status") == "PASS"
                and exp.validation_report.get("candidate") == "parent"
                and yearly_ok
                and self._correlation_under(exp.self_correlation, corr_cap)
                and exp.self_correlation["status"] == "PASS"
            )
            eligibility = submission_eligibility(
                platform_pass=(exp.self_correlation["status"] == "PASS"),
                health=healthy,
                validation=(exp.validation_status == "STABLE" and
                            isinstance(exp.validation_report, dict) and
                            exp.validation_report.get("status") == "PASS"),
                yearly=yearly_ok,
                incremental=getattr(exp, "incremental_evidence", None),
                incremental_mode=self.incremental_policy.mode,
            )
            exp.submission_eligibility = eligibility
            eligible = eligible and eligibility["eligible"]
            if eligible:
                eligible_records.append((
                    exp, rating, exp.self_correlation, active_snapshot
                ))
        color_records = [
            {
                "alpha_id": exp.alpha_id,
                "classification": classify_alpha_color(exp),
            }
            for exp in candidates if exp.alpha_id
        ]
        if color_records:
            self.daily_cache.put_colors(color_records)
        cached_records = [
            {
                "alpha_id": exp.alpha_id,
                "expression": exp.expression,
                "rating": rating,
                "self_correlation": correlation,
                "submission": "MANUAL_REQUIRED",
            }
            for exp, rating, correlation, _active_snapshot in eligible_records
        ]
        if cached_records:
            self.daily_cache.put_submitted_alphas(cached_records)
        added = len(eligible_records)
        if added:
            print(
                f"[SUBMISSION CACHE] {added} 个候选进入 "
                f"America/New_York:{self.daily_cache.local_date}（仅内存）；"
                "仅供人工提交，程序不会 POST Alpha。"
            )

    def _mark_robustness_stability(self, experiments):
        """Apply the one canonical STABLE gate to parent candidates.

        A single SUCCESS/ROBUSTNESS row is evidence only.  The parent is
        promoted only after all pre-registered dimensions have been
        aggregated into one passing ValidationReport.
        """
        self._validation_candidates = []
        all_rows = list(self.trajectory.experiments or [])
        for exp in experiments:
            if all(existing.id != exp.id for existing in all_rows):
                all_rows.append(exp)
        for row in all_rows:
            report = getattr(row, "validation_report", None) or {}
            if (getattr(row, "validation_status", None) == "STABLE"
                    and not (report.get("status") == "PASS" and report.get("candidate") == "parent")):
                row.validation_status = "UNVALIDATED"
        children_by_parent = {}
        plans = {}
        for child in all_rows:
            if child.experiment_stage != "ROBUSTNESS":
                continue
            parent_expression = child.parent_expression
            if not isinstance(parent_expression, str) or not parent_expression.strip():
                continue
            key = canonical_expression(parent_expression)
            children_by_parent.setdefault(key, []).append(child)
            if isinstance(child.validation_plan, dict) and key not in plans:
                plans[key] = child.validation_plan

        trial_summary = self.trial_ledger.summarize()
        for key, children in children_by_parent.items():
            parent = self._completed_parent(children[0].parent_expression)
            if parent is None:
                continue
            plan = plans.get(key)
            if not isinstance(plan, dict):
                plan = default_validation_plan(
                    parent, statistical_policy=self.statistical_policy,
                    robustness_policy=self.robustness_policy,
                )
            parent.self_correlation = self._settled_self_correlation(parent)
            platform_evidence = {
                "parent": {
                    "health": parent.health,
                    "correlation": parent.self_correlation,
                },
                "children": [
                    {
                        "health": child.health,
                        "correlation": self._settled_self_correlation(child),
                    }
                    for child in children
                ],
            }
            report = build_validation_report(
                parent,
                children,
                plan,
                yearly_evidence=parent.yearly_evidence,
                trial_summary=trial_summary,
                platform_evidence=platform_evidence,
            )
            parent.validation_report = report
            for child in children:
                child.validation_report = report
                child.validation_status = "VALIDATED" if report["status"] == "PASS" else "FAILED"
            parent.validation_status = "STABLE" if report["status"] == "PASS" else "UNVALIDATED"
            self._settle_research_outcome(parent, report)
            if report["status"] == "PASS":
                self._validation_candidates.append((parent, report))

    def _settle_research_outcome(self, experiment, report):
        """Append one final replacement observation after aggregate evidence."""
        provisional = getattr(experiment, "provisional_outcome", None) or getattr(experiment, "search_outcome", None)
        if not isinstance(provisional, dict) or not isinstance(report, dict):
            return None
        incremental = self._settle_incremental_evidence(experiment)
        incremental_decision = incremental.get("decision", "UNAVAILABLE") if isinstance(incremental, dict) else "UNAVAILABLE"
        platform = (report.get("dimensions") or {}).get("platform_quality") or {}
        platform_pass = platform.get("status") == "PASS"
        final = settle_search_outcome(
            provisional,
            validation_report=report,
            incremental_decision=incremental_decision,
            yearly_evidence=getattr(experiment, "yearly_evidence", None),
            platform_pass=platform_pass,
        )
        quality = "STABLE" if report.get("status") == "PASS" else provisional.get("base_quality")
        robustness = "PASS" if report.get("status") == "PASS" else "FAIL"
        statistical = extract_statistical_decision(report)
        experiment.final_outcome = final
        experiment.search_outcome = final
        experiment.research_classification = classify_research(
            quality, robustness, statistical, incremental_decision,
            "PASS" if platform_pass else "FAIL",
        )
        experiment.research_evidence_bundle = ResearchEvidenceBundle.from_parts(
            {"label": quality, "effective_trial_count": self.trial_ledger.summarize_cached(
                os.path.join(self.state_dir, "trial_ledger.summary.json")
            ).get("effective_trial_count")},
            report.get("dimensions", {}).get("robustness") or report,
            report.get("statistical_evidence") or {},
            incremental,
            getattr(experiment, "yearly_evidence", None) or {},
            platform,
        ).as_dict()
        self.trial_ledger.record_outcome_settled(
            experiment, reward=final.get("reward"),
            reward_version=final.get("reward_version", "reward_v1"),
            reward_quality=final.get("reward_quality", "FINAL_EVIDENCE"),
            base_quality=quality, robustness=robustness,
            statistical_decision=statistical,
            incremental_decision=incremental_decision,
            research_classification=experiment.research_classification,
            incremental_evidence=incremental,
            research_evidence_bundle=experiment.research_evidence_bundle,
            timestamp=final.get("settled_at") or time.time(),
        )
        try:
            self.search_policy.replace_reward(experiment, final.get("reward"))
        except (TypeError, ValueError):
            pass
        # Persist the settled research evidence as a legal trajectory revision
        # so a later process rehydrates FINAL evidence instead of the early DONE
        # snapshot.  Refusal is fail-closed and audited, never silent.
        try:
            self.trajectory.settle(experiment)
        except ValueError as exc:
            self._record_trial_phase(
                experiment, "settlement_revision_rejected",
                outcome="REJECTED", reason=str(exc),
                reason_code="SETTLEMENT_REVISION_REJECTED",
            )
            print(f"[SETTLEMENT] revision rejected id={experiment.id}: {exc}")
        return final

    def _settle_incremental_evidence(self, experiment):
        """Create production evidence from the only accepted behavior source.

        The current client has no LIVE_VERIFIED PnL capability, so normal
        candidates settle explicitly as UNAVAILABLE rather than receiving a
        correlation fabricated from aggregate metrics.
        """
        behavior = extract_behavior_series(experiment)
        members = []
        for row in list(self.trajectory.experiments or []):
            series = extract_behavior_series(row)
            members.append({
                "alpha_id": getattr(row, "alpha_id", None),
                "candidate_id": getattr(row, "candidate_id", None),
                "status": getattr(row, "status", None),
                "checks_passed": checks_passed(getattr(row, "metrics", None)),
                "identity": getattr(row, "submission_fingerprint", None),
                "behavior_series": series.get("series"),
                "pool_entered_at": getattr(row, "created_at", None),
            })
        snapshot = build_pool_snapshot(members, as_of=time.time())
        evidence = build_incremental_value(
            getattr(experiment, "candidate_id", None) or getattr(experiment, "alpha_id", None) or experiment.id,
            behavior.get("series"), snapshot.members,
            min_overlap=self.incremental_policy.min_overlap,
            max_abs_correlation=self.incremental_policy.max_abs_correlation,
            as_of=snapshot.as_of,
        )
        result = evidence.as_dict() if hasattr(evidence, "as_dict") else dict(evidence)
        result.update({
            "availability": behavior.get("availability"),
            "capability": behavior.get("capability"),
            "source": behavior.get("source"),
            "snapshot_id": snapshot.snapshot_id,
            "pool_size": len(snapshot.members),
            "policy": self.incremental_policy.mode,
        })
        experiment.incremental_evidence = result
        return result

    def _alpha_rating(self, metrics):
        """Internal Excellent/Spectacular discipline from AGENTS.md."""
        required = ("sharpe", "turnover", "fitness", "margin")
        if any(metrics.get(key) is None for key in required):
            return "UNRATED"
        sharpe = num(metrics["sharpe"])
        turnover = num(metrics["turnover"])
        fitness = num(metrics["fitness"])
        margin = num(metrics["margin"])
        if any(value is None for value in (sharpe, turnover, fitness, margin)):
            return "UNRATED"
        excellent = self.quality_policy.get("excellent", {})
        spectacular = self.quality_policy.get("spectacular", {})
        if not isinstance(excellent, dict):
            excellent = {}
        if not isinstance(spectacular, dict):
            spectacular = {}
        def threshold(policy, name, default):
            value = num(policy.get(name, default))
            return default if value is None else value
        # Platform margins are fractional values; thresholds live in config.
        if (sharpe > threshold(spectacular, "min_sharpe", 2.0)
                and threshold(spectacular, "min_turnover", 0.10) <= turnover <= threshold(spectacular, "max_turnover", 0.20)
                and fitness > threshold(spectacular, "min_fitness", 2.5)
                and margin > threshold(spectacular, "min_margin", 0.0006)):
            return "SPECTACULAR"
        if (sharpe > threshold(excellent, "min_sharpe", 1.58)
                and threshold(excellent, "min_turnover", 0.049) <= turnover <= threshold(excellent, "max_turnover", 0.30)
                and fitness > threshold(excellent, "min_fitness", 1.5)
                and margin > threshold(excellent, "min_margin", 0.0004)):
            return "EXCELLENT"
        # Delay-aware 过线纪律：delay 0 与 delay 1 的门槛不同，未知 delay 不晋级。
        thresholds = delay_metric_thresholds(
            (self.simulation_settings or {}).get("delay")
        )
        min_turnover, max_turnover = turnover_bounds(self.quality_policy)
        if (thresholds is not None
                and sharpe > thresholds["sharpe"]
                and min_turnover <= turnover <= max_turnover
                and fitness > thresholds["fitness"]):
            return "GOOD"
        return "BELOW_GOOD"

    def _print_summary(self, summary, elapsed=None):
        print(
            f"Round {summary['round']} verdicts: {summary['verdicts']}"
        )
        if summary["best"]:
            b = summary["best"]
            best_id = f" alpha_id={b.get('alpha_id')}" if b.get("alpha_id") else ""
            print(
                f"  best={b['expression'][:70]} sharpe={b.get('sharpe')} "
                f"fitness={b.get('fitness')}{best_id}"
            )
        if elapsed:
            print(f"  elapsed={elapsed:.1f}s")

    # ----------------------------------------------------------- state I/O

    def _load_state(self):
        self.memory.load()
        self.trajectory.load()
        checkpoint_rows = self._search_checkpoint_rows()
        snapshot = SearchSnapshot.from_sources(
            self.trajectory.experiments,
            self.trial_ledger.summarize_cached(
                os.path.join(self.state_dir, "trial_ledger.summary.json")
            ),
            checkpoint_rows,
        )
        self.search_policy.restore(snapshot.allocator_state(
            self.search_policy.allocator.total_budget
        ))
        # In-memory window from previous sessions is authoritative for dedupe
        for exp in self.trajectory.experiments:
            self.memory.remember_expression(exp.expression)

    def search_calibration_report(self):
        """Return a read-only historical Search Calibration report."""
        from .search_calibration import build_search_calibration

        events = (
            list(iter_jsonl_objects(self.trial_ledger.path))
            if getattr(self.trial_ledger, "persist", True)
            else list(getattr(self.trial_ledger, "_events", []))
        )
        outcomes = []
        trajectory_path = getattr(self.trajectory, "path", None)
        if (
            getattr(self.trajectory, "persist", True)
            and trajectory_path
            and os.path.exists(trajectory_path)
        ):
            for row in iter_jsonl_objects(trajectory_path):
                stored = row.get("search_outcome")
                if isinstance(stored, dict):
                    outcomes.append(stored)
        summary = self.trial_ledger.summarize_cached(
            os.path.join(self.state_dir, "trial_ledger.summary.json")
        )
        summary["committed_simulations"] = sum(
            1 for row in events if row.get("phase") == "simulation_committed"
        )
        final_by_proposal = {
            str(row.get("proposal_id")): dict(row.get("settlement") or {})
            for row in events
            if row.get("phase") == "research_outcome_settled" and row.get("proposal_id")
        }
        if final_by_proposal:
            merged = []
            for row in outcomes:
                replacement = final_by_proposal.get(str(row.get("proposal_id")))
                if replacement:
                    merged.append(dict(row, **replacement, outcome_kind="FINAL"))
                else:
                    merged.append(row)
            outcomes = merged
        return build_search_calibration(summary, outcomes=outcomes, events=events)

    def _search_checkpoint_rows(self):
        """Read-only projection of unfinished checkpoints for allocator restore."""
        rows = []
        for record in self.checkpoints.scan():
            if not record["malformed"] and not record["checkpoint"].get("complete"):
                rows.extend(record["checkpoint"].get("experiments") or [])
        return rows

    def _ensure_loaded(self):
        if not self._loaded:
            self._load_state()
            self._loaded = True

    def _write_sims_results(self, round_no, experiments, total_elapsed_sec=None):
        """Put today's result view in the New York-day memory cache only."""
        results = []
        for e in experiments:
            m = e.metrics or {}
            results.append(
                {
                    "id": e.id,
                    "expression": e.expression,
                    "status": e.status,
                    "alpha_id": e.alpha_id,
                    "error": e.error,
                    "elapsed_sec": e.elapsed_sec,
                    "sharpe": m.get("sharpe"),
                    "fitness": m.get("fitness"),
                    "turnover": m.get("turnover"),
                    "returns": m.get("returns"),
                    "drawdown": m.get("drawdown"),
                    "margin": m.get("margin"),
                    "passed": m.get("passed"),
                    "checks": m.get("checks"),
                    "health": e.health,
                    "self_correlation": e.self_correlation or self_correlation_evidence(m),
                    "yearly_evidence": e.yearly_evidence,
                }
            )
        self.daily_cache.put_simulations(results)
        print(
            f"[RESULTS CACHE] {len(results)} 个模拟结果 -> "
            f"America/New_York:{self.daily_cache.local_date}（仅内存）"
        )

    def _save_state(self, state):
        """Compatibility hook; completed round summaries are not persisted."""
        return None

    def _write_context(self):
        """Keep compressed context in memory; do not create a result file."""
        return None
