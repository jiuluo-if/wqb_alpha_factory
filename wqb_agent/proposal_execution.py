"""Proposal execution and checkpoint recovery orchestration.

This module owns the guarded proposal execution boundary.  It deliberately
does not import :class:`Agent`: high-level research planning stays in the
agent, remote Simulation lifecycle stays in ``Simulator``, and HTTP transport
stays in ``WQBClient``.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

from .artifacts import append_jsonl_best_effort, iter_jsonl_objects
from .diversity import extract_fields, is_redundant
from .expression import canonical_expression, submission_fingerprint
from .identity import candidate_identity
from .proposal_contract import (
    FACTORY_BATCH_SIZE,
    MAX_TARGETED_PROPOSALS,
    RESEARCH_ROLES,
    TARGETED_BATCH_TYPE,
    proposal_budget_cap,
    proposal_priority,
    validate_factory_batch,
    validate_proposal,
    validate_targeted_batch,
    validate_vector_inputs,
)
from .research_guard import ResearchLoopGuard, structural_family_key
from .state import (
    RECOVERABLE_STATUSES,
    UNKNOWN_STATUSES,
    UNRESOLVED_STATUSES,
    Experiment,
    ResearchState,
)


@dataclass(frozen=True)
class ProposalExecutionHooks:
    """Small callbacks for Agent-owned domain/evidence operations.

    These callbacks are intentionally operation-shaped.  The workflow never
    receives the Agent object and therefore cannot reach arbitrary Agent
    attributes or create a second orchestration path.
    """

    ensure_loaded: Callable[[], None]
    next_round_no: Callable[[], int]
    terminal_identities: Callable[[list[Any]], tuple[set[str], set[str]]]
    refresh_platform_field_usage: Callable[[dict, list[Any]], dict]
    read_field_cache: Callable[[], tuple[dict, dict]]
    known_field_types: Callable[[dict, dict | None], dict]
    proposal_settings: Callable[[dict | None], dict]
    completed_parent: Callable[[str | None, dict | None], Any]
    record_trial_phase: Callable[..., None]
    record_candidate_rejection: Callable[..., None]
    on_simulation_update: Callable[[Experiment, int, dict, list[Experiment]], None]
    record_live_result: Callable[[Experiment], None]
    refresh_self_correlation_evidence: Callable[[list[Experiment]], None]
    mark_robustness_stability: Callable[[list[Experiment]], None]
    sync_submission_pool: Callable[[list[Experiment]], None]
    save_state: Callable[[ResearchState | None], None]
    write_context: Callable[[], None]
    print_summary: Callable[[dict], None]
    write_sims_results: Callable[..., None]
    validation_candidates: Callable[[], Any]
    set_last_round_skipped: Callable[[bool], None]
    reset_best_exhausted: Callable[[], None]


@dataclass(frozen=True)
class ProposalExecutionContext:
    """Explicit execution dependencies resolved by ``Agent`` composition."""

    state_dir: str
    simulator: Any
    trajectory: Any
    trial_ledger: Any
    checkpoints: Any
    memory: Any
    search_policy: Any
    reflector: Any
    hooks: ProposalExecutionHooks
    operator_reference: dict | None = None
    factory_batch_size: int = FACTORY_BATCH_SIZE
    min_factory_datasets: int = 1
    min_cross_dataset_pairs: int = 0
    candidates_per_round: int = 6
    max_proposals_per_round: int = 18
    research_allocation: dict | None = None
    research_integrity: bool = False
    max_field_alpha_count: int | None = None
    require_platform_alpha_count: bool = False


class ProposalExecutionWorkflow:
    """Own proposal-file execution, recovery, and terminal settlement."""

    def __init__(self, context: ProposalExecutionContext):
        self.context = context
        self.last_run_stats = {
            "accepted": 0,
            "rejected": 0,
            "skipped": 0,
            "status": "NOT_STARTED",
        }

    def update_agent_config(self, **values):
        """Refresh compatibility configuration changed after composition."""
        self.context = replace(self.context, **values)

    @property
    def _ctx(self):
        return self.context

    def _proposal_checkpoint_path(self, round_no):
        return self._ctx.checkpoints.path(round_no)

    def _write_proposal_checkpoint(self, round_no, hypothesis, experiments, complete):
        return self._ctx.checkpoints.write(round_no, hypothesis, experiments, complete)

    def _load_proposal_checkpoint(self, round_no):
        return self._ctx.checkpoints.load(round_no)

    def _unfinished_checkpoint_except(self, round_no):
        return self._ctx.checkpoints.unfinished_except(round_no)

    def _on_update(self, experiment, round_no, hypothesis, experiments):
        self._ctx.hooks.on_simulation_update(
            experiment, round_no, hypothesis, experiments
        )

    def _run_simulator(self, experiments, round_no, hypothesis, all_experiments):
        self._ctx.trajectory.begin_append_batch(experiments)
        try:
            self._ctx.simulator.run(
                experiments,
                on_complete=self._ctx.hooks.record_live_result,
                on_update=lambda exp: self._on_update(
                    exp, round_no, hypothesis, all_experiments
                ),
            )
        finally:
            self._ctx.trajectory.end_append_batch()

    def _settle_complete_round(self, round_no, hypothesis, experiments,
                               total_elapsed_sec=None):
        """Perform the existing terminal projection in its original order."""
        self._ctx.trajectory.add_many(experiments)
        for exp in experiments:
            self._ctx.search_policy.release(
                {
                    "proposal_id": exp.allocation_key,
                    "expression": exp.expression,
                    "dataset_family": exp.datasets,
                    "template_family": exp.template_family,
                },
                status="DONE" if exp.status == "DONE" else exp.status,
                reward=(exp.metrics or {}).get("fitness", 0.0)
                if isinstance(exp.metrics, dict) else 0.0,
            )
        self._ctx.hooks.refresh_self_correlation_evidence(experiments)
        self._ctx.hooks.mark_robustness_stability(experiments)
        summary = self._ctx.reflector.reflect(
            round_no,
            hypothesis,
            experiments,
            validation_candidates=self._ctx.hooks.validation_candidates(),
        )
        self._ctx.hooks.sync_submission_pool(experiments)
        state = ResearchState(
            round_no=round_no,
            hypothesis=hypothesis,
            dataset=sorted({d for exp in experiments for d in exp.datasets}),
            fields_used=[f for exp in experiments for f in exp.fields_used],
        )
        self._ctx.hooks.save_state(state)
        self._write_proposal_checkpoint(round_no, hypothesis, experiments, complete=True)
        self._ctx.hooks.write_context()
        self._ctx.hooks.print_summary(summary)
        if total_elapsed_sec is None:
            self._ctx.hooks.write_sims_results(round_no, experiments)
        else:
            self._ctx.hooks.write_sims_results(
                round_no, experiments, total_elapsed_sec=total_elapsed_sec
            )
        return summary

    def run(self, path=None, allow_unresolved_checkpoint=False):
        """Execute one proposals file through the existing guarded path."""
        ctx = self._ctx
        hooks = ctx.hooks
        self.last_run_stats = {"accepted": 0, "rejected": 0, "skipped": 0}
        hooks.ensure_loaded()
        path = path or os.path.join(ctx.state_dir, "proposals.json")
        if not os.path.exists(path):
            print(f"No proposals file at {path}.")
            return None
        try:
            with open(path, encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, ValueError) as exc:
            print(f"[PROPOSALS ERROR] {path} 无法读取或不是有效 JSON：{exc}")
            return None
        if not isinstance(payload, dict):
            print(f"[PROPOSALS ERROR] {path} 顶层必须是对象（含 round_no/proposals）。")
            return None

        raw_round_no = payload.get("round_no")
        if raw_round_no is None:
            round_no = hooks.next_round_no()
        elif isinstance(raw_round_no, bool):
            print("[PROPOSALS ERROR] round_no 必须是正整数；未执行任何提案。")
            return None
        else:
            try:
                round_no = int(raw_round_no)
            except (TypeError, ValueError):
                print("[PROPOSALS ERROR] round_no 必须是正整数；未执行任何提案。")
                return None
            if round_no <= 0:
                print("[PROPOSALS ERROR] round_no 必须是正整数；未执行任何提案。")
                return None
        foreign_checkpoint = self._unfinished_checkpoint_except(round_no)
        if foreign_checkpoint and not allow_unresolved_checkpoint:
            print(
                f"[CHECKPOINT BLOCKED] 存在未完成 {os.path.basename(foreign_checkpoint)}；"
                "必须先以原 proposals.json 恢复，禁止开启新轮。"
            )
            return None
        if foreign_checkpoint and allow_unresolved_checkpoint:
            print(
                f"[FORCE NEW ROUND] 保留未完成 {os.path.basename(foreign_checkpoint)} "
                "及其原 progress_url；按用户明确授权开启新轮。"
            )
        checkpoint_path = self._proposal_checkpoint_path(round_no)
        checkpoint = self._load_proposal_checkpoint(round_no)
        if os.path.exists(checkpoint_path) and checkpoint is None:
            print(
                f"[CHECKPOINT ERROR] {checkpoint_path} 无法解析或轮次不匹配；"
                "保留原文件，需先人工对账。"
            )
            return None
        if checkpoint and not checkpoint.get("complete"):
            return self.resume_checkpoint(checkpoint)
        if checkpoint and checkpoint.get("complete"):
            print(f"[CHECKPOINT COMPLETE] round {round_no} 已完成；不重新派发其中的 proposals。")
            return None

        proposal_list = payload.get("proposals") or []
        if not isinstance(proposal_list, list):
            print("[PROPOSALS ERROR] proposals 必须是数组；未执行任何提案。")
            return None
        if not proposal_list:
            print("No proposals in file; nothing to run.")
            return None
        factory_batch = payload.get("batch_type") == "factory_100"
        if factory_batch:
            batch_ok, batch_errors = validate_factory_batch(
                proposal_list,
                target=ctx.factory_batch_size,
                min_datasets=ctx.min_factory_datasets,
                require_cross_dataset_pairs=ctx.min_cross_dataset_pairs > 0,
            )
            if not batch_ok:
                print("[FACTORY BATCH BLOCKED] 整批不满足 100 题案契约：")
                for problem in batch_errors:
                    print(f"  - {problem}")
                return None
        targeted_batch = payload.get("batch_type") == TARGETED_BATCH_TYPE
        if targeted_batch:
            batch_ok, batch_errors = validate_targeted_batch(proposal_list)
            if not batch_ok:
                print("[TARGETED BATCH BLOCKED] 不满足 targeted optimization 契约：")
                for problem in batch_errors:
                    print(f"  - {problem}")
                self.last_run_stats = {
                    "accepted": 0,
                    "rejected": len(batch_errors),
                    "skipped": 0,
                    "status": "TARGETED_BATCH_BLOCKED",
                }
                return None

        hypothesis = payload.get("hypothesis")
        if hypothesis is None:
            hypothesis = {
                "id": f"h-llm-r{round_no}",
                "statement": payload.get("statement", "LLM-proposed research direction"),
                "tags": ["llm", "proposal"],
                "direction": "long",
                "datasets": [],
            }
        elif not isinstance(hypothesis, dict):
            print("[PROPOSALS ERROR] hypothesis 必须是对象；未执行任何提案。")
            return None
        else:
            hypothesis = dict(hypothesis)
        hypothesis["_round"] = round_no

        terminal_expressions, terminal_fingerprints = hooks.terminal_identities(
            [item.get("expression") for item in proposal_list if isinstance(item, dict)]
        )
        local_seen = set(terminal_expressions)
        local_seen.update(terminal_fingerprints)
        pending = [
            exp for exp in ctx.trajectory.experiments
            if exp.status in ("UNKNOWN", "PENDING")
        ]
        if pending:
            print(
                f"[NOTE] {len(pending)} 个历史实验未完成（UNKNOWN/PENDING），"
                "其表达式已豁免去重，可在本轮重新提交。"
            )
        fresh, skipped, rejected = [], [], []
        diversity_rejected, settings_rejected = [], []
        loop_guard = ResearchLoopGuard(ctx.trajectory.experiments)
        platform_usage = hooks.refresh_platform_field_usage(payload, proposal_list)
        cached_field_types, cached_profiles = hooks.read_field_cache()
        field_types = hooks.known_field_types(
            payload, cached_field_types=cached_field_types
        )
        discovered_profiles = {}
        for field in payload.get("suggestion_fields") or payload.get("fields") or []:
            if isinstance(field, dict) and field.get("id"):
                normalized_field = dict(field)
                dataset = self._field_dataset_id(field)
                if dataset is not None:
                    normalized_field["dataset"] = dataset
                key = f"{dataset}::{field['id']}" if dataset is not None else str(field["id"])
                discovered_profiles[key] = normalized_field
        for field_id, field in cached_profiles.items():
            dataset = self._field_dataset_id(field) if isinstance(field, dict) else None
            key = (
                f"{dataset}::{field.get('id')}"
                if dataset is not None and field.get("id") else field_id
            )
            discovered_profiles.setdefault(key, field)
        for profile_key, profile in list(discovered_profiles.items()):
            if not isinstance(profile, dict):
                continue
            field_id = profile.get("id")
            dataset_id = profile.get("dataset")
            usage = platform_usage.get(str(dataset_id), {}).get(str(field_id))
            if usage is not None:
                profile = dict(profile)
                profile["alpha_count"] = usage.get("alpha_count")
                profile["platform_dedupe"] = usage
                discovered_profiles[profile_key] = profile
        proposal_field_profiles = list(discovered_profiles.values())
        completed_parent_index = ctx.trajectory.find_completed_expressions(
            [item.get("parent_expression") for item in proposal_list if isinstance(item, dict)]
        )

        for raw_proposal in proposal_list:
            if not isinstance(raw_proposal, dict):
                malformed = {"round": round_no, "expression": str(raw_proposal or "")}
                malformed["candidate_id"] = candidate_identity(malformed, round_no=round_no)
                hooks.record_trial_phase(malformed, "candidate_generated", outcome="CONSIDERED")
                hooks.record_candidate_rejection(
                    malformed, "schema", "NOT_OBJECT", "proposal 必须是对象"
                )
                rejected.append((str(raw_proposal), ["proposal 必须是对象"]))
                continue
            proposal = dict(raw_proposal)
            proposal["round"] = round_no
            proposal["candidate_id"] = candidate_identity(proposal, round_no=round_no)
            hooks.record_trial_phase(proposal, "candidate_generated", outcome="CONSIDERED")
            source = proposal.get("field_source") or payload.get("field_source")
            if source is None:
                source = next(
                    (profile.get("field_source") for profile in discovered_profiles.values()
                     if profile.get("field_source")),
                    None,
                )
            if source is not None:
                proposal["field_source"] = source
            expression = (proposal.get("expression") or "").strip()
            if not expression:
                hooks.record_candidate_rejection(
                    proposal, "schema", "MISSING_EXPRESSION", "expression 不能为空"
                )
                continue
            settings_override = proposal.get("settings")
            if settings_override:
                try:
                    effective_settings = hooks.proposal_settings(settings_override)
                except ValueError:
                    effective_settings = settings_override
                if isinstance(effective_settings, dict):
                    dedup_key = "settings::" + submission_fingerprint(expression, effective_settings)
                else:
                    dedup_key = (
                        "settings::" + json.dumps(settings_override, sort_keys=True)
                        + "::" + canonical_expression(expression)
                    )
                if dedup_key in local_seen:
                    hooks.record_candidate_rejection(
                        proposal, "duplicate", "DUPLICATE_LOCAL", "同一表达式与设置已在本批出现"
                    )
                    skipped.append(expression)
                    continue
                local_seen.add(dedup_key)
            elif canonical_expression(expression) in local_seen:
                hooks.record_candidate_rejection(
                    proposal, "duplicate", "DUPLICATE_LOCAL", "同一规范化表达式已在本批出现"
                )
                skipped.append(expression)
                continue
            else:
                local_seen.add(canonical_expression(expression))
            ok, problems = validate_proposal(
                proposal,
                discovered_fields=proposal_field_profiles,
                strict_experiment=True,
                operator_reference=ctx.operator_reference,
                require_research_evidence=bool(ctx.research_allocation),
                require_economic_integrity=ctx.research_integrity,
                max_alpha_count=ctx.max_field_alpha_count,
                require_platform_alpha_count=ctx.require_platform_alpha_count,
            )
            type_ok, type_problems = validate_vector_inputs(proposal, field_types)
            if not type_ok:
                problems.extend(type_problems)
                ok = False
            if not ok:
                hooks.record_candidate_rejection(
                    proposal, "schema", "PREFLIGHT_REJECTED", "; ".join(problems)
                )
                rejected.append((expression, problems))
                continue
            if ctx.research_allocation:
                if not (
                    isinstance(source, dict)
                    and source.get("kind") in {"local_catalog", "brain_api"}
                    and "snapshot_date" in source
                ):
                    reason = "field_source 必须声明 local_catalog/brain_api 及 snapshot_date"
                    hooks.record_candidate_rejection(proposal, "field_source", "INVALID_FIELD_SOURCE", reason)
                    rejected.append((expression, [reason]))
                    continue
                role = proposal.get("research_role")
                if role not in RESEARCH_ROLES:
                    reason = "research_role 必须是 EXPLORE/EXPLOIT/VALIDATION"
                    hooks.record_candidate_rejection(proposal, "research_guard", "INVALID_RESEARCH_ROLE", reason)
                    rejected.append((expression, [reason]))
                    continue
                parent = hooks.completed_parent(
                    proposal.get("parent_expression"), completed_parent_index
                )
                if role == "EXPLORE" and proposal.get("experiment_stage") != "BASELINE":
                    reason = "EXPLORE 必须是新的 BASELINE"
                    hooks.record_candidate_rejection(proposal, "research_guard", "EXPLORE_STAGE_MISMATCH", reason)
                    rejected.append((expression, [reason]))
                    continue
                if role in {"EXPLOIT", "VALIDATION"} and parent is None:
                    reason = f"{role} 只能引用已完成的 parent_expression，不能在同一批提案中预支结果"
                    hooks.record_candidate_rejection(proposal, "research_guard", "PARENT_NOT_DONE", reason)
                    rejected.append((expression, [reason]))
                    continue
                if role == "VALIDATION":
                    parent_verdict = ctx.reflector._classify(parent).get("label")
                    if parent_verdict not in {"SUCCESS", "SUSPICIOUS_HIGH_SIGNAL"}:
                        reason = "VALIDATION 的 parent 必须已通过质量门或为需审计的高信号"
                        hooks.record_candidate_rejection(proposal, "research_guard", "PARENT_QUALITY_FAIL", reason)
                        rejected.append((expression, [reason]))
                        continue
            lineage_id = proposal.get("lineage_id") or proposal.get("parent_expression") or hypothesis.get("id")
            lineage_decision = ctx.memory.lineage_decision(lineage_id)
            blocked = (
                lineage_decision == "KILL"
                or (lineage_decision == "STOP" and proposal.get("experiment_stage") != "ROBUSTNESS")
            )
            if blocked:
                reason = f"lineage {lineage_id!r} 已标记 {lineage_decision}，不再消耗探索预算"
                hooks.record_candidate_rejection(proposal, "lineage", f"LINEAGE_{lineage_decision}", reason)
                diversity_rejected.append((expression, [reason]))
                continue
            guard_ok, guard_reason = loop_guard.check(
                proposal, default_lineage=proposal.get("lineage_id") or hypothesis.get("id")
            )
            if not guard_ok:
                hooks.record_candidate_rejection(proposal, "research_guard", "LOOP_GUARD", guard_reason)
                diversity_rejected.append((expression, [guard_reason]))
                continue
            try:
                effective_settings = hooks.proposal_settings(proposal.get("settings"))
            except ValueError as exc:
                hooks.record_candidate_rejection(proposal, "settings", "INVALID_SETTINGS", str(exc))
                settings_rejected.append((expression, [str(exc)]))
                continue
            proposal.setdefault("proposal_id", "p-" + submission_fingerprint(expression, effective_settings)[:16])
            fresh.append(proposal)

        diverse, diverse_records = [], []
        family_counts = {}
        allocation_counts = {role: 0 for role in RESEARCH_ROLES}
        historical_pool = list(ctx.trajectory.experiments)
        role_max = (ctx.research_allocation.get("maximum", {}) if ctx.research_allocation else {})
        for proposal in fresh:
            ctx.search_policy.annotate(proposal, historical_pool + diverse_records)
        for proposal in sorted(
            fresh,
            key=lambda item: (ctx.search_policy.priority(item), proposal_priority(item)),
            reverse=True,
        ):
            role = proposal.get("research_role")
            if role in role_max and allocation_counts.get(role, 0) >= int(role_max[role]):
                reason = f"{role} 已达到本轮动态上限 {role_max[role]}"
                hooks.record_candidate_rejection(proposal, "diversity", "ARM_ROLE_CAP", reason)
                diversity_rejected.append((proposal["expression"], [reason]))
                continue
            fields = extract_fields(proposal["expression"], proposal.get("fields") or [])
            family = proposal.get("template_family")
            if not isinstance(family, (str, int)) or not str(family).strip():
                family = structural_family_key(proposal["expression"], proposal.get("fields") or fields)
            family = str(family)
            if factory_batch:
                field_scope = ",".join(sorted(set(fields)))
                family = f"{family}:{field_scope}"
            family_cap = 4 if factory_batch else 2
            if family_counts.get(family, 0) >= family_cap:
                reason = f"signal family {family!r} 已达本批上限 {family_cap}"
                hooks.record_candidate_rejection(proposal, "diversity", "DIVERSITY_FAMILY_CAP", reason)
                diversity_rejected.append((proposal["expression"], [reason]))
                continue
            record = {
                "expression": proposal["expression"],
                "fields_used": fields,
                "template_id": proposal.get("template_id"),
            }
            redundant, keeper = is_redundant(record, diverse_records)
            distinct_factory_templates = (
                factory_batch and record.get("template_id") and isinstance(keeper, dict)
                and keeper.get("template_id") and record["template_id"] != keeper["template_id"]
            )
            distinct_factory_field_scope = (
                factory_batch and isinstance(keeper, dict)
                and set(record.get("fields_used") or []) != set(keeper.get("fields_used") or [])
            )
            if redundant and not (distinct_factory_templates or distinct_factory_field_scope):
                reason = "与本轮更高优先级候选近重复"
                hooks.record_candidate_rejection(proposal, "diversity", "DIVERSITY_REDUNDANT", reason)
                diversity_rejected.append((proposal["expression"], [reason]))
                continue
            if not ctx.search_policy.accept(proposal):
                allocator = ctx.search_policy.allocator
                budget_exhausted = allocator.consumed_budget >= allocator.total_budget
                reason = "Simulation budget 已耗尽" if budget_exhausted else "同一 dataset/mechanism research arm 已有待定或预留预算"
                hooks.record_candidate_rejection(
                    proposal,
                    "simulation_budget" if budget_exhausted else "arm",
                    "SIMULATION_BUDGET" if budget_exhausted else "ARM_ADMISSION",
                    reason,
                )
                diversity_rejected.append((proposal["expression"], [reason]))
                continue
            hooks.record_trial_phase(proposal, "candidate_admitted", outcome="ADMITTED")
            family_counts[family] = family_counts.get(family, 0) + 1
            if role in allocation_counts:
                allocation_counts[role] += 1
            diverse.append(proposal)
            diverse_records.append(record)

        if factory_batch:
            allocation_cap = ctx.factory_batch_size
        elif targeted_batch:
            # targeted batch 的边界由批契约本身决定（≤4 CHILD + 4 VALIDATE），
            # 不受无关的 per-round exploration 配置影响。
            allocation_cap = MAX_TARGETED_PROPOSALS
        else:
            try:
                allocation_cap = int(
                    (ctx.research_allocation or {}).get(
                        "max_simulations", ctx.candidates_per_round
                    )
                )
            except (TypeError, ValueError):
                allocation_cap = ctx.candidates_per_round
        if factory_batch:
            candidates_cap = ctx.factory_batch_size
            hard_cap = ctx.factory_batch_size
        elif targeted_batch:
            candidates_cap = MAX_TARGETED_PROPOSALS
            hard_cap = MAX_TARGETED_PROPOSALS
        else:
            candidates_cap = ctx.candidates_per_round
            hard_cap = ctx.max_proposals_per_round
        budget_cap = proposal_budget_cap(candidates_cap, allocation_cap, hard_cap=hard_cap)
        budget_rejected = diverse[budget_cap:]
        fresh = diverse[:budget_cap]
        for proposal in budget_rejected:
            hooks.record_candidate_rejection(proposal, "batch_budget", "BATCH_CAP", "本地 batch cap 淘汰，未承诺 Simulation")
            ctx.search_policy.release(proposal, status="SKIPPED_LOCAL")

        if ctx.research_allocation:
            role_max = ctx.research_allocation.get("maximum", {})
            role_counts = {role: 0 for role in RESEARCH_ROLES}
            for proposal in fresh:
                role = proposal.get("research_role")
                if role in role_counts:
                    role_counts[role] += 1
            role_errors = [
                f"{role} 有 {role_counts.get(role, 0)}，超过动态上限 {maximum}"
                for role, maximum in role_max.items()
                if role_counts.get(role, 0) > int(maximum)
            ]
            if role_errors:
                print("[ALLOCATION BLOCKED] 本轮不提交 Simulation：")
                for problem in role_errors:
                    print(f"  - {problem}")
                return None
            print("[ALLOCATION] 动态分配 " + ", ".join(
                f"{role}={role_counts.get(role, 0)}" for role in sorted(RESEARCH_ROLES)
            ))
        if factory_batch and (
            len(fresh) != ctx.factory_batch_size or rejected or skipped
            or diversity_rejected or settings_rejected or budget_rejected
        ):
            print(
                f"[FACTORY BATCH BLOCKED] factory batch 需要 {ctx.factory_batch_size} 个全部预检通过的题案，"
                f"当前可执行 {len(fresh)} 个；不允许部分提交"
            )
            self.last_run_stats = {
                "accepted": 0,
                "rejected": len(rejected) + len(diversity_rejected) + len(settings_rejected),
                "skipped": len(skipped),
                "status": "FACTORY_BATCH_BLOCKED",
                "rejection_counts": {
                    "preflight": len(rejected), "duplicate": len(skipped),
                    "diversity": len(diversity_rejected), "settings": len(settings_rejected),
                    "budget": len(budget_rejected),
                },
            }
            return None

        committed = []
        for proposal in fresh:
            if ctx.search_policy.commit(proposal):
                committed.append(proposal)
                hooks.record_trial_phase(proposal, "simulation_committed", outcome="COMMITTED")
            else:
                hooks.record_candidate_rejection(
                    proposal, "simulation_budget", "BUDGET_COMMIT_FAILED",
                    "最终执行集合无法承诺 Simulation budget",
                )
                ctx.search_policy.release(proposal, status="SKIPPED_LOCAL")
        fresh = committed
        if diversity_rejected:
            print(f"[DIVERSITY BLOCKED] {len(diversity_rejected)} 个提案未进入模拟：")
            for expr, problems in diversity_rejected:
                print(f"  - {expr[:70]} -> {'; '.join(problems)}")
        if budget_rejected:
            print(f"[BUDGET BLOCKED] 仅执行优先级最高的 {budget_cap} 个提案；{len(budget_rejected)} 个未消耗 simulation。")
        print(f"\n=== Round {round_no} (LLM proposals) ===")
        print(f"Hypothesis: {hypothesis['statement']}")
        if skipped:
            print(f"skipped already-simulated: {len(skipped)}")
        if rejected:
            print(f"[PREFLIGHT BLOCKED] {len(rejected)} 提案未通过生产预检：")
            for expr, problems in rejected:
                print(f"  - {expr[:70]} -> {'; '.join(problems)}")
        if settings_rejected:
            print(f"[SETTINGS BLOCKED] {len(settings_rejected)} 提案设置不合法，拒绝模拟：")
            for expr, problems in settings_rejected:
                print(f"  - {expr[:70]} -> {'; '.join(problems)}")
        if not fresh:
            self.last_run_stats = {
                "accepted": 0,
                "rejected": len(rejected) + len(diversity_rejected) + len(settings_rejected),
                "skipped": len(skipped),
                "status": (
                    "PREFLIGHT_BLOCKED" if rejected or settings_rejected or diversity_rejected
                    else "ALREADY_SIMULATED"
                ),
                "rejection_counts": {
                    "preflight": len(rejected), "duplicate": len(skipped),
                    "diversity": len(diversity_rejected), "settings": len(settings_rejected),
                    "budget": len(budget_rejected),
                },
            }
            if rejected and not skipped:
                print("所有提案均未通过生产预检；请补齐字段、字段画像、算子证据、实验阶段和谱系元数据后重试。")
            else:
                print("All proposals already simulated before; nothing to run.")
            ctx.memory.register_hypothesis(hypothesis)
            ctx.memory.save()
            hooks.set_last_round_skipped(True)
            hooks.save_state(None)
            hooks.write_context()
            return None
        hooks.set_last_round_skipped(False)
        hooks.reset_best_exhausted()
        self.last_run_stats = {
            "accepted": len(fresh),
            "rejected": len(rejected) + len(diversity_rejected) + len(settings_rejected),
            "skipped": len(skipped),
            "status": "READY_TO_SIMULATE",
        }

        experiments = []
        known_ids = list(discovered_profiles)
        for proposal in fresh:
            fields = proposal.get("fields") or known_ids
            if fields:
                fields = extract_fields(proposal["expression"], fields)
            exp = Experiment(
                round_no,
                hypothesis["id"],
                proposal["expression"],
                hooks.proposal_settings(proposal.get("settings")),
                fields,
                datasets=proposal.get("datasets") or [],
            )
            exp.candidate_id = proposal.get("candidate_id") or candidate_identity(proposal, round_no=round_no)
            exp.submission_fingerprint = submission_fingerprint(exp.expression, exp.settings)
            exp.proposal_id = proposal.get("proposal_id") or "p-" + exp.submission_fingerprint[:16]
            for name in (
                "field_source", "field_understanding", "field_analysis", "field_hypothesis_basis",
                "operator_evidence", "template_id", "template_family", "template_stage_path",
                "template_ref", "template_slots", "search_evidence", "novelty_score",
            ):
                setattr(exp, name, proposal.get(name))
            exp.allocation_arm = ctx.search_policy.allocator.arm_key(proposal)
            exp.allocation_key = ctx.search_policy.allocator.proposal_key(proposal)
            exp.factory_session_id = proposal.get("factory_session_id")
            exp.proposal_origin = proposal.get("proposal_origin") or ("factory" if factory_batch else "agent")
            exp.research_layer = proposal.get("research_layer") or {
                "EXPLOIT": "optimization", "EXPLORE": "exploration",
            }.get(proposal.get("research_role"))
            exp.mutation = proposal.get("mutation") or "agent-proposed"
            exp.lineage_id = proposal.get("lineage_id") or proposal.get("parent_expression") or hypothesis["id"]
            for name in (
                "experiment_stage", "research_role", "change_type", "parent_expression",
                "child_economic_hypothesis", "changed_variable", "tuning_risk", "direction",
                "expected_horizon", "falsification", "economic_mechanism", "direction_transform",
                "self_correlation_impact", "validation_plan",
            ):
                setattr(exp, name, proposal.get(name))
            exp.rationale = proposal.get("rationale") or proposal.get("hypothesis") or ""
            exp.expected_failure_modes = list(proposal.get("expected_failure_modes") or [])
            if exp.experiment_stage == "ROBUSTNESS" and exp.validation_plan is None:
                raise ValueError("ROBUSTNESS 缺少预注册 validation_plan")
            experiments.append(exp)
            hooks.record_trial_phase(exp, "generated", outcome="PENDING")
            hooks.record_trial_phase(exp, "preflight", outcome="ACCEPTED")
            hooks.record_trial_phase(exp, "preflight_accepted", outcome="ACCEPTED")
            ctx.memory.remember_expression(exp.expression)

        ctx.memory.register_hypothesis(hypothesis)
        checkpoint_path = self._proposal_checkpoint_path(round_no)
        self._write_proposal_checkpoint(round_no, hypothesis, experiments, complete=False)
        round_start = time.time()
        self._run_simulator(experiments, round_no, hypothesis, experiments)
        elapsed = time.time() - round_start
        unresolved = [exp for exp in experiments if exp.status in UNRESOLVED_STATUSES]
        if unresolved:
            for exp in experiments:
                ctx.search_policy.release(
                    {"proposal_id": exp.allocation_key, "expression": exp.expression,
                     "dataset_family": exp.datasets, "template_family": exp.template_family},
                    status="UNKNOWN" if exp.status in UNKNOWN_STATUSES else "PENDING",
                )
            self._write_proposal_checkpoint(round_no, hypothesis, experiments, complete=False)
            hooks.write_sims_results(round_no, experiments, total_elapsed_sec=elapsed)
            print(
                f"[CHECKPOINT] {len(unresolved)} 个任务未定论，已保留 {checkpoint_path}；"
                "下次运行同一 proposals.json 只会恢复轮询，绝不重复 POST。"
            )
            return None
        summary = self._settle_complete_round(
            round_no, hypothesis, experiments, total_elapsed_sec=elapsed
        )
        print(
            f"[ELAPSED] Round {round_no} 模拟总耗时 {elapsed/60:.1f} 分钟"
            f"（{len(experiments)} 个模拟，3 并发真实计时）"
        )
        return summary

    @staticmethod
    def _field_dataset_id(field, fallback=None):
        raw = field.get("dataset") if isinstance(field, dict) else None
        if isinstance(raw, dict):
            raw = raw.get("id") or raw.get("name")
        if raw is None:
            raw = fallback
            if isinstance(raw, dict):
                raw = raw.get("id") or raw.get("name")
        return str(raw) if raw is not None else None

    def resume_checkpoint(self, checkpoint):
        """Resume known remote jobs while preserving exactly-once semantics."""
        ctx = self._ctx
        round_no = checkpoint["round_no"]
        hypothesis = checkpoint.get("hypothesis") or {"id": f"h-llm-r{round_no}"}
        experiments = [Experiment.from_dict(row) for row in checkpoint["experiments"]]
        runnable = [exp for exp in experiments if exp.status in RECOVERABLE_STATUSES]
        unresolved_unknown = [exp for exp in experiments if exp.status == "SUBMIT_UNKNOWN"]
        print(f"\n=== Round {round_no} checkpoint resume ===")
        if unresolved_unknown:
            print(
                f"[SUBMIT_UNKNOWN] {len(unresolved_unknown)} 个 POST 结果不明，已保留预算槽，"
                "不会重发；需先做平台侧只读对账。"
            )
            runnable = [exp for exp in runnable if exp.progress_url]
        if runnable:
            self._run_simulator(runnable, round_no, hypothesis, experiments)
        unresolved = [exp for exp in experiments if exp.status in UNRESOLVED_STATUSES]
        if unresolved:
            self._write_proposal_checkpoint(round_no, hypothesis, experiments, complete=False)
            ctx.hooks.write_sims_results(round_no, experiments)
            return None
        return self._settle_complete_round(round_no, hypothesis, experiments)

    def skip_stale_reconciled(self, round_no, simulation_id, min_attempts=3):
        checkpoint = self._load_proposal_checkpoint(int(round_no))
        if not checkpoint or checkpoint.get("complete"):
            raise ValueError(f"round {round_no} has no unfinished checkpoint")
        history_path = os.path.join(self._ctx.state_dir, "reconcile_history.jsonl")
        attempts = [
            row for row in iter_jsonl_objects(history_path)
            if row.get("simulation_id") == simulation_id
            and row.get("outcome") in {"STALE", "UNKNOWN"}
        ]
        if len(attempts) < int(min_attempts):
            raise ValueError(
                f"only {len(attempts)} read-only STALE/UNKNOWN reconciliations; need {min_attempts}"
            )
        experiments = [Experiment.from_dict(row) for row in checkpoint["experiments"]]
        matches = [
            exp for exp in experiments
            if (exp.progress_url or "").rstrip("/").split("/")[-1] == simulation_id
        ]
        if len(matches) != 1:
            raise ValueError(f"expected one checkpoint experiment for {simulation_id}, found {len(matches)}")
        exp = matches[0]
        if exp.status not in RECOVERABLE_STATUSES:
            raise ValueError(f"simulation {simulation_id} is already {exp.status}")
        exp.status = "SKIPPED_STALE"
        exp.skip_record = {
            "reason": "repeated_read_only_reconciliation_stale_or_unknown",
            "simulation_id": simulation_id,
            "attempts": len(attempts),
            "outcomes": [row.get("outcome") for row in attempts],
            "last_reconciled_at": attempts[-1].get("reconciled_at"),
            "skipped_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "research_decision": "N/A",
        }
        exp.error = "SKIPPED_AFTER_REPEATED_STALE_RECONCILIATION"
        unresolved = [item for item in experiments if item.status in UNRESOLVED_STATUSES]
        self._write_proposal_checkpoint(
            int(round_no), checkpoint.get("hypothesis") or {}, experiments, complete=not unresolved
        )
        self._ctx.trajectory.add(exp)
        audit_path = os.path.join(self._ctx.state_dir, "stale_skip_log.jsonl")
        append_jsonl_best_effort(
            audit_path,
            {"round_no": int(round_no), "experiment_id": exp.id,
             "expression": exp.expression, "progress_url": exp.progress_url,
             **exp.skip_record},
            ("round_no", "experiment_id", "reason"),
        )
        print(f"[SKIP] r{round_no} {simulation_id} -> SKIPPED_STALE; attempts={len(attempts)}")
        print(f"[SKIP] audit -> {audit_path}")
        return exp

    def skip_submit_unknown_authorized(self, round_no, proposal_id):
        checkpoint = self._load_proposal_checkpoint(int(round_no))
        if not checkpoint or checkpoint.get("complete"):
            raise ValueError(f"round {round_no} has no unfinished checkpoint")
        experiments = [Experiment.from_dict(row) for row in checkpoint["experiments"]]
        matches = [exp for exp in experiments if exp.proposal_id == proposal_id]
        if len(matches) != 1:
            raise ValueError(f"expected one checkpoint experiment for {proposal_id}, found {len(matches)}")
        exp = matches[0]
        # A progress-URL-less UNKNOWN is the same recovery-evidence gap as
        # SUBMIT_UNKNOWN: an ambiguous write result with no safe re-POST and no
        # remote identity to poll.  A URL-bearing UNKNOWN is NOT skippable here
        # — the known remote job is recoverable read-only and skipping would
        # discard live evidence, so it must go through reconciliation first.
        ambiguous_unknown = exp.status == "UNKNOWN" and not (exp.progress_url or "").strip()
        if exp.status != "SUBMIT_UNKNOWN" and not ambiguous_unknown:
            raise ValueError(
                f"proposal {proposal_id} is {exp.status}; only SUBMIT_UNKNOWN or a "
                "progress-URL-less UNKNOWN may be user-authorized skipped"
            )
        exp.status = "SKIPPED_UNKNOWN"
        exp.skip_record = {
            "reason": (
                "user_authorized_skip_unknown_no_progress_url"
                if ambiguous_unknown
                else "user_authorized_skip_after_repeated_read_only_reconciliation"
            ),
            "proposal_id": proposal_id,
            "submission_fingerprint": exp.submission_fingerprint,
            "remote_id": None,
            "read_only_reconciliation": (
                "no_progress_url_no_unique_remote_record"
                if ambiguous_unknown else "no_unique_remote_record"
            ),
            "skipped_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "research_decision": "N/A",
        }
        exp.error = (
            "SKIPPED_AFTER_USER_AUTHORIZED_UNKNOWN_NO_URL"
            if ambiguous_unknown
            else "SKIPPED_AFTER_USER_AUTHORIZED_SUBMIT_UNKNOWN"
        )
        unresolved = [item for item in experiments if item.status in UNRESOLVED_STATUSES]
        self._write_proposal_checkpoint(
            int(round_no), checkpoint.get("hypothesis") or {}, experiments, complete=not unresolved
        )
        self._ctx.trajectory.add(exp)
        audit_path = os.path.join(self._ctx.state_dir, "stale_skip_log.jsonl")
        append_jsonl_best_effort(
            audit_path,
            {"round_no": int(round_no), "experiment_id": exp.id,
             "expression": exp.expression, "progress_url": exp.progress_url,
             **exp.skip_record},
            ("round_no", "experiment_id", "reason"),
        )
        print(f"[SKIP] r{round_no} {proposal_id} -> SKIPPED_UNKNOWN; user-authorized")
        print(f"[SKIP] audit -> {audit_path}")
        return exp

    def finalize_recorded_round(self, round_no):
        self._ctx.hooks.ensure_loaded()
        rows = [exp for exp in self._ctx.trajectory.experiments if exp.round == int(round_no)]
        if not rows:
            raise ValueError(f"no trajectory evidence for round {round_no}")
        selected = {}
        rank = {"UNKNOWN": 0, "PENDING": 0, "SUBMITTING": 0, "RUNNING": 0,
                "SKIPPED_STALE": 1, "SKIPPED_UNKNOWN": 1, "FAILED": 1, "DONE": 2}
        for exp in rows:
            old = selected.get(exp.expression)
            if old is None or rank.get(exp.status, 0) > rank.get(old.status, 0):
                selected[exp.expression] = exp
        experiments = list(selected.values())
        active = [exp for exp in experiments if exp.status in UNRESOLVED_STATUSES]
        if active:
            raise ValueError(f"round {round_no} still has unresolved experiments")
        checkpoint = self._load_proposal_checkpoint(int(round_no)) or {}
        hypothesis = checkpoint.get("hypothesis") or {"id": f"h-llm-r{round_no}", "_round": int(round_no)}
        self._ctx.hooks.refresh_self_correlation_evidence(experiments)
        self._ctx.hooks.mark_robustness_stability(experiments)
        summary = self._ctx.reflector.reflect(
            int(round_no), hypothesis, experiments,
            validation_candidates=self._ctx.hooks.validation_candidates(),
        )
        self._ctx.hooks.sync_submission_pool(experiments)
        state = ResearchState(
            round_no=int(round_no), hypothesis=hypothesis,
            dataset=sorted({d for exp in experiments for d in exp.datasets}),
            fields_used=[f for exp in experiments for f in exp.fields_used],
        )
        self._ctx.hooks.save_state(state)
        self._ctx.hooks.write_context()
        audit_path = os.path.join(self._ctx.state_dir, "round_finalization_log.jsonl")
        append_jsonl_best_effort(
            audit_path,
            {"round_no": int(round_no), "selected": len(experiments),
             "finalized_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
             "source": "trajectory_append_only"},
            ("round_no", "source"),
        )
        print(f"[FINALIZE] r{round_no} diagnosed {len(experiments)} recorded terminal experiments")
        print(f"[FINALIZE] audit -> {audit_path}")
        return summary
