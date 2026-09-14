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
from .execution_recovery import merge_checkpoint_with_trajectory
from .expression import canonical_expression, submission_fingerprint
from .identity import candidate_identity
from .proposal_admission import rejection_reason_counts
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
    OPERATOR_PROVENANCE_FIELDS,
    RECOVERABLE_STATUSES,
    TERMINAL_STATUSES,
    UNKNOWN_STATUSES,
    UNRESOLVED_STATUSES,
    Experiment,
    ResearchState,
    same_execution_identity,
)
from .terminal_evidence import failure_category, has_full_terminal_evidence


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
    on_simulation_update: Callable[..., None]
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
    simulation_delegation: Any = None


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

    @staticmethod
    def _rejection_reason_counts(rejected, skipped, diversity_rejected,
                                 settings_rejected, budget_rejected):
        """Return a bounded transient histogram using existing public categories."""
        return rejection_reason_counts(
            rejected, skipped, diversity_rejected, settings_rejected, budget_rejected
        )

    @property
    def _ctx(self):
        return self.context

    def _proposal_checkpoint_path(self, round_no):
        return self._ctx.checkpoints.path(round_no)

    def _write_proposal_checkpoint(self, round_no, hypothesis, experiments, complete,
                                   delegation=None):
        return self._ctx.checkpoints.write(
            round_no, hypothesis, experiments, complete, delegation=delegation
        )

    def _load_proposal_checkpoint(self, round_no):
        return self._ctx.checkpoints.load(round_no)

    def _unfinished_checkpoint_except(self, round_no):
        return self._ctx.checkpoints.unfinished_except(round_no)

    def _unresolved_submission_identities(self, round_no):
        return self._ctx.checkpoints.unresolved_submission_identities(
            exclude_round=round_no
        )

    def _durable_proposal_bindings(self, checkpoint_records):
        """Read exact proposal-id bindings from existing durable owners."""
        bindings = {}

        def add(proposal_id, fingerprint):
            if proposal_id in (None, "") or fingerprint in (None, ""):
                return
            bindings.setdefault(str(proposal_id), set()).add(str(fingerprint))

        trajectory = self._ctx.trajectory
        if getattr(trajectory, "persist", True) and getattr(trajectory, "path", None):
            rows = trajectory.iter_canonical_rows(strict=True) or ()
        else:
            rows = (experiment.to_dict() for experiment in getattr(trajectory, "experiments", ()))
        for row in rows:
            if isinstance(row, dict):
                add(row.get("proposal_id"), row.get("submission_fingerprint"))

        for record in checkpoint_records:
            if record.get("malformed"):
                continue
            for row in record.get("checkpoint", {}).get("experiments") or ():
                if isinstance(row, dict):
                    add(row.get("proposal_id"), row.get("submission_fingerprint"))

        ledger = self._ctx.trial_ledger
        if hasattr(ledger, "proposal_execution_bindings"):
            for proposal_id, fingerprints in ledger.proposal_execution_bindings().items():
                for fingerprint in fingerprints:
                    add(proposal_id, fingerprint)
        return bindings

    def _on_update(self, experiment, round_no, hypothesis, experiments, delegation):
        self._ctx.hooks.on_simulation_update(
            experiment, round_no, hypothesis, experiments, delegation
        )

    def _run_simulator(self, experiments, round_no, hypothesis, all_experiments):
        self._ctx.trajectory.begin_append_batch(experiments)
        try:
            self._ctx.simulator.run(
                experiments,
                on_complete=self._ctx.hooks.record_live_result,
                on_update=lambda exp: self._on_update(
                    exp, round_no, hypothesis, all_experiments,
                    self._ctx.simulation_delegation,
                ),
            )
        finally:
            self._ctx.trajectory.end_append_batch()

    @staticmethod
    def _has_full_terminal_evidence(experiment):
        """Return whether a terminal row is safe for research consumers."""
        return has_full_terminal_evidence(experiment)

    @staticmethod
    def _failure_category(experiment):
        return failure_category(experiment)

    def _canonical_rows_for(self, experiments):
        ids = [experiment.id for experiment in experiments]
        finder = getattr(self._ctx.trajectory, "find_rows", None)
        if callable(finder):
            return finder(ids)
        return {
            experiment.id: experiment.to_dict()
            for experiment in getattr(self._ctx.trajectory, "experiments", ())
            if experiment.id in ids
        }

    def _require_durable_terminal_evidence(self, experiments, round_no):
        rows = self._canonical_rows_for(experiments)
        round_reader = getattr(self._ctx.trajectory, "iter_canonical_round", None)
        if callable(round_reader):
            canonical_round_ids = {
                str(row.get("id"))
                for row in (round_reader(int(round_no)) or ())
                if isinstance(row, dict) and row.get("id")
            }
            expected_ids = {str(experiment.id) for experiment in experiments}
            if canonical_round_ids != expected_ids:
                raise ValueError(
                    f"FINALIZE_EXECUTION_SET_MISMATCH: round {round_no}"
                )
        for experiment in experiments:
            row = rows.get(experiment.id)
            if row is None or not same_execution_identity(experiment.to_dict(), row):
                raise ValueError(
                    f"TERMINAL_EVIDENCE_UNRECOVERABLE: round {round_no} experiment {experiment.id}"
                )
            durable = Experiment.from_dict(row)
            if not self._has_full_terminal_evidence(durable):
                raise ValueError(
                    f"TERMINAL_EVIDENCE_UNRECOVERABLE: round {round_no} experiment {experiment.id}"
                )

    def _merge_checkpoint_with_trajectory(self, experiments, round_no):
        """Monotonically merge checkpoint execution rows with canonical rows."""
        rows = self._canonical_rows_for(experiments)
        return merge_checkpoint_with_trajectory(
            experiments,
            rows,
            round_no,
            terminal_statuses=TERMINAL_STATUSES,
        )

    def _finalize_round_projection(self, round_no, hypothesis, experiments,
                                   *, total_elapsed_sec=None,
                                   close_checkpoint=False):
        """Apply the one canonical terminal projection for any round source."""
        self._require_durable_terminal_evidence(experiments, round_no)
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
        if close_checkpoint:
            self._write_proposal_checkpoint(round_no, hypothesis, experiments, complete=True)
        self._ctx.hooks.write_context()
        if total_elapsed_sec is not None:
            self._ctx.hooks.write_sims_results(
                round_no, experiments, total_elapsed_sec=total_elapsed_sec
            )
        return summary

    def _settle_complete_round(self, round_no, hypothesis, experiments,
                               total_elapsed_sec=None):
        """Perform the existing terminal projection in its original order."""
        self._ctx.trajectory.add_many(experiments)
        self._require_durable_terminal_evidence(experiments, round_no)
        for exp in experiments:
            reward = None
            if exp.status == "DONE" and isinstance(exp.metrics, dict):
                reward = exp.metrics.get("fitness")
            self._ctx.search_policy.release(
                {
                    "proposal_id": exp.allocation_key,
                    "expression": exp.expression,
                    "dataset_family": exp.datasets,
                    "template_family": exp.template_family,
                },
                status="DONE" if exp.status == "DONE" else exp.status,
                reward=reward,
                outcome=self._failure_category(exp),
            )
        summary = self._finalize_round_projection(
            round_no, hypothesis, experiments,
            total_elapsed_sec=total_elapsed_sec,
            close_checkpoint=True,
        )
        self._ctx.hooks.print_summary(summary)
        if total_elapsed_sec is None:
            self._ctx.hooks.write_sims_results(round_no, experiments)
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
        checkpoint_records = self._ctx.checkpoints.scan()
        foreign_record = self._ctx.checkpoints.unfinished_record_except(
            round_no, records=checkpoint_records
        )
        current_record = next(
            (record for record in checkpoint_records
             if record["round_no"] == round_no),
            None,
        )
        if foreign_record and foreign_record.get("validation_code") not in (None,):
            print(
                f"[CHECKPOINT BLOCKED] {os.path.basename(foreign_record['path'])} "
                f"身份不可验证：{foreign_record.get('validation_code') or 'UNVERIFIABLE_CHECKPOINT_IDENTITY'}；"
                "force-new-round 不能绕过。"
            )
            return None
        if foreign_record and not allow_unresolved_checkpoint:
            print(
                f"[CHECKPOINT BLOCKED] 存在未完成 {os.path.basename(foreign_record['path'])}；"
                "必须先以原 proposals.json 恢复，禁止开启新轮。"
            )
            return None
        if foreign_record and allow_unresolved_checkpoint:
            print(
                f"[FORCE NEW ROUND] 保留未完成 {os.path.basename(foreign_record['path'])} "
                "及其原 progress_url；按用户明确授权开启新轮。"
            )
        checkpoint_path = self._proposal_checkpoint_path(round_no)
        checkpoint = current_record.get("checkpoint") if current_record else None
        if current_record and current_record.get("malformed"):
            print(
                f"[CHECKPOINT ERROR] {checkpoint_path} 无法解析或轮次不匹配；"
                "保留原文件，需先人工对账。"
            )
            return None
        if checkpoint and checkpoint.get("complete") is not True:
            return self.resume_checkpoint(checkpoint)
        if checkpoint and checkpoint.get("complete") is True:
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
                self.last_run_stats = {
                    "accepted": 0,
                    "rejected": len(batch_errors),
                    "skipped": 0,
                    "status": "FACTORY_BATCH_BLOCKED",
                    "rejection_reason_counts": {"FACTORY_BATCH_REJECTED": len(batch_errors)},
                }
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
        research_seen = set(terminal_expressions)
        batch_execution_fingerprints = {
            str(fingerprint).removeprefix("settings::")
            for fingerprint in terminal_fingerprints
        }
        durable_proposal_bindings = self._durable_proposal_bindings(checkpoint_records)
        batch_proposal_bindings = {}
        conflicting_proposal_ids = set()
        unresolved_identities = ctx.checkpoints.unresolved_submission_identities(
            exclude_round=round_no, records=checkpoint_records
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
        parent_ids = {
            str(item.get("parent_id"))
            for item in proposal_list
            if isinstance(item, dict) and item.get("parent_id") not in (None, "")
        }
        parent_rows = (
            ctx.trajectory.find_rows(parent_ids, strict=True)
            if parent_ids else {}
        )
        legacy_parent_candidates = ctx.trajectory.find_completed_parent_candidates(
            [item.get("parent_expression") for item in proposal_list if isinstance(item, dict)]
        )
        effective_settings_by_candidate = {}

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
            try:
                effective_settings = hooks.proposal_settings(proposal.get("settings"))
            except ValueError as exc:
                hooks.record_candidate_rejection(
                    proposal, "settings", "INVALID_SETTINGS", str(exc)
                )
                settings_rejected.append((expression, [str(exc)]))
                continue
            execution_fingerprint = submission_fingerprint(
                expression, effective_settings
            )
            if execution_fingerprint in batch_execution_fingerprints:
                hooks.record_candidate_rejection(
                    proposal, "execution_identity", "DUPLICATE_EFFECTIVE_EXECUTION",
                    "相同的完整 effective Simulation settings 已存在，禁止重复执行",
                )
                skipped.append(expression)
                continue
            settings_override = proposal.get("settings")
            research_key = (
                "settings::" + execution_fingerprint
                if settings_override else canonical_expression(expression)
            )
            if research_key in research_seen:
                hooks.record_candidate_rejection(
                    proposal, "duplicate", "DUPLICATE_LOCAL", "同一研究表达式与设置已在本批出现"
                )
                skipped.append(expression)
                continue
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
            role = proposal.get("research_role")
            parent = None
            if role in {"EXPLOIT", "VALIDATION"}:
                supplied_parent_id = proposal.get("parent_id")
                if supplied_parent_id not in (None, ""):
                    parent = parent_rows.get(str(supplied_parent_id))
                    if parent is None:
                        reason = "PARENT_NOT_FOUND"
                        hooks.record_candidate_rejection(
                            proposal, "parent_identity", reason,
                            "parent_id 未在 durable canonical trajectory 中找到",
                        )
                        rejected.append((expression, [reason]))
                        continue
                else:
                    candidates = legacy_parent_candidates.get(
                        canonical_expression(proposal.get("parent_expression", "")), []
                    )
                    if len(candidates) > 1:
                        reason = "PARENT_REFERENCE_AMBIGUOUS"
                        hooks.record_candidate_rejection(
                            proposal, "parent_identity", reason,
                            "legacy expression-only parent 引用对应多个 Experiment",
                        )
                        rejected.append((expression, [reason]))
                        continue
                    if not candidates:
                        reason = "PARENT_NOT_FOUND"
                        hooks.record_candidate_rejection(
                            proposal, "parent_identity", reason,
                            "legacy parent_expression 没有唯一 DONE Experiment",
                        )
                        rejected.append((expression, [reason]))
                        continue
                    parent = candidates[0].to_dict()
                    proposal["parent_id"] = parent.get("id")
                if str(parent.get("status") or "").upper() != "DONE":
                    reason = "PARENT_NOT_DONE"
                    hooks.record_candidate_rejection(
                        proposal, "parent_identity", reason,
                        "parent_id 对应 Experiment 尚未完成",
                    )
                    rejected.append((expression, [reason]))
                    continue
                if not isinstance(parent.get("metrics"), dict) or not parent.get("metrics"):
                    reason = "PARENT_NOT_DONE"
                    hooks.record_candidate_rejection(
                        proposal, "parent_identity", reason,
                        "parent_id 对应 Experiment 缺少 required evidence",
                    )
                    rejected.append((expression, [reason]))
                    continue
                if canonical_expression(proposal.get("parent_expression", "")) != canonical_expression(
                    parent.get("expression", "")
                ):
                    reason = "PARENT_EXPRESSION_MISMATCH"
                    hooks.record_candidate_rejection(
                        proposal, "parent_identity", reason,
                        "parent_expression 与 parent_id 的 canonical expression 不一致",
                    )
                    rejected.append((expression, [reason]))
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
                if role == "EXPLORE" and proposal.get("experiment_stage") != "BASELINE":
                    reason = "EXPLORE 必须是新的 BASELINE"
                    hooks.record_candidate_rejection(proposal, "research_guard", "EXPLORE_STAGE_MISMATCH", reason)
                    rejected.append((expression, [reason]))
                    continue
                if role == "VALIDATION":
                    parent_verdict = ctx.reflector._classify(
                        Experiment.from_dict(parent)
                    ).get("label")
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
            if execution_fingerprint in unresolved_identities:
                reason = (
                    "同一 submission_fingerprint 仍存在未决远程执行；"
                    "force-new-round 不能绕过 execution identity fence"
                )
                hooks.record_candidate_rejection(
                    proposal, "execution_identity",
                    "UNRESOLVED_SUBMISSION_IDENTITY", reason,
                )
                rejected.append((expression, [reason]))
                continue
            proposal_id = str(
                proposal.get("proposal_id")
                or "p-" + execution_fingerprint[:16]
            )
            durable_fingerprints = durable_proposal_bindings.get(proposal_id, set())
            if durable_fingerprints and execution_fingerprint not in durable_fingerprints:
                reason_code = "PROPOSAL_ID_REBIND"
                hooks.record_candidate_rejection(
                    proposal, "execution_identity", reason_code,
                    "proposal_id 已经绑定其他 durable execution identity",
                )
                rejected.append((expression, [reason_code]))
                continue
            previous_fingerprint = batch_proposal_bindings.get(proposal_id)
            if (
                proposal_id in conflicting_proposal_ids
                or (
                    previous_fingerprint is not None
                    and previous_fingerprint != execution_fingerprint
                )
            ):
                reason_code = "PROPOSAL_ID_EXECUTION_COLLISION"
                if proposal_id not in conflicting_proposal_ids and fresh:
                    previous = next(
                        (item for item in fresh
                         if str(item.get("proposal_id") or
                                "p-" + submission_fingerprint(
                                    item["expression"],
                                    effective_settings_by_candidate[item["candidate_id"]],
                                )[:16]) == proposal_id),
                        None,
                    )
                    if previous is not None:
                        fresh.remove(previous)
                        hooks.record_candidate_rejection(
                            previous, "execution_identity", reason_code,
                            "同一 batch 的 proposal_id 绑定多个 execution identity",
                        )
                        rejected.append((previous["expression"], [reason_code]))
                conflicting_proposal_ids.add(proposal_id)
                hooks.record_candidate_rejection(
                    proposal, "execution_identity", reason_code,
                    "同一 batch 的 proposal_id 绑定多个 execution identity",
                )
                rejected.append((expression, [reason_code]))
                continue
            batch_proposal_bindings[proposal_id] = execution_fingerprint
            batch_execution_fingerprints.add(execution_fingerprint)
            research_seen.add(research_key)
            effective_settings_by_candidate[proposal["candidate_id"]] = effective_settings
            proposal["proposal_id"] = proposal_id
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
                allocator_reason = getattr(allocator, "last_rejection_code", None)
                reason_code = allocator_reason or (
                    "SIMULATION_BUDGET" if budget_exhausted else "ARM_ADMISSION"
                )
                reason = "Simulation budget 已耗尽" if reason_code == "SIMULATION_BUDGET" else (
                    "terminal proposal_id 的 arm 不可重绑定"
                    if reason_code == "TERMINAL_PROPOSAL_KEY_ARM_REBIND"
                    else "同一 dataset/mechanism research arm 已有待定或预留预算"
                )
                hooks.record_candidate_rejection(
                    proposal,
                    "simulation_budget" if reason_code == "SIMULATION_BUDGET" else "arm",
                    reason_code,
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
                "rejection_reason_counts": self._rejection_reason_counts(
                    rejected, skipped, diversity_rejected, settings_rejected, budget_rejected
                ),
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
                "rejection_reason_counts": self._rejection_reason_counts(
                    rejected, skipped, diversity_rejected, settings_rejected, budget_rejected
                ),
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
            if proposal.get("template_mode") == "PARTIAL_OPERATOR":
                missing = [
                    name for name in OPERATOR_PROVENANCE_FIELDS
                    if proposal.get(name) in (None, "", {}, [])
                ]
                if missing:
                    raise ValueError(
                        "PARTIAL_OPERATOR provenance incomplete: "
                        + ", ".join(missing)
                    )
            fields = proposal.get("fields") or known_ids
            if fields:
                fields = extract_fields(proposal["expression"], fields)
            effective_settings = effective_settings_by_candidate.get(proposal.get("candidate_id"))
            if effective_settings is None:
                effective_settings = hooks.proposal_settings(proposal.get("settings"))
            exp = Experiment(
                round_no,
                hypothesis["id"],
                proposal["expression"],
                effective_settings,
                fields,
                datasets=proposal.get("datasets") or [],
            )
            exp.candidate_id = proposal.get("candidate_id") or candidate_identity(proposal, round_no=round_no)
            exp.submission_fingerprint = submission_fingerprint(exp.expression, exp.settings)
            exp.proposal_id = proposal.get("proposal_id") or "p-" + exp.submission_fingerprint[:16]
            exp.optimization_decision_id = proposal.get("optimization_decision_id")
            exp.parent_id = proposal.get("parent_id")
            for name in (
                "field_source", "field_understanding", "field_analysis", "field_hypothesis_basis",
                "operator_evidence", "template_id", "template_family", "template_stage_path",
                "template_ref", "template_slots", "search_evidence", "novelty_score",
                *OPERATOR_PROVENANCE_FIELDS,
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
        experiments = self._merge_checkpoint_with_trajectory(experiments, round_no)
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

    def _record_terminal_skip(self, experiment, outcome, reason):
        """Close an unresolved lifecycle without inventing research evidence."""
        proposal = {
            "proposal_id": experiment.allocation_key or experiment.proposal_id,
            "expression": experiment.expression,
            "dataset_family": experiment.datasets,
            "template_family": experiment.template_family,
        }
        policy = self._ctx.search_policy
        if getattr(policy, "enabled", False):
            key = policy.allocator.proposal_key(proposal)
            known = key in policy.allocator.proposals
            known = known or key in getattr(policy, "validation_proposals", {})
            if not known:
                raise ValueError(
                    "SKIP_SEARCH_ACCOUNTING_GAP: unresolved arm is not durable"
                )
        ledger = self._ctx.trial_ledger
        delegation = self._ctx.simulation_delegation
        ledger.record(
            experiment, "completed", outcome=outcome, reason=reason,
            reason_code=outcome, delegation=delegation,
        )
        ledger.record(
            experiment, "simulation_settled", outcome=outcome, reason=reason,
            reason_code=outcome, delegation=delegation,
        )
        policy.release(proposal, status="SKIPPED", reward=0.0)

    @staticmethod
    def _execution_identity_key(row):
        """Compare checkpoint/trajectory identity using retained fields only."""
        settings = row.get("settings") if isinstance(row.get("settings"), dict) else {}
        fingerprint = submission_fingerprint(row.get("expression", ""), settings)
        return (
            str(row.get("id")),
            row.get("round"),
            row.get("hypothesis_id"),
            canonical_expression(row.get("expression", "")),
            json.dumps(settings, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            tuple(row.get("fields_used") or ()),
            str(fingerprint),
        )

    def _validate_finalize_checkpoint(self, checkpoint, experiments, round_no):
        if checkpoint is None:
            return None
        checkpoint_rows = checkpoint.get("experiments") or []
        durable_keys = {
            self._execution_identity_key(exp.to_dict()) for exp in experiments
        }
        checkpoint_keys = {
            self._execution_identity_key(row)
            for row in checkpoint_rows if isinstance(row, dict)
        }
        if checkpoint_keys != durable_keys or len(checkpoint_rows) != len(experiments):
            raise ValueError(
                f"FINALIZE_EXECUTION_SET_MISMATCH: round {round_no}"
            )
        return checkpoint

    def skip_stale_reconciled(self, round_no, simulation_id, min_attempts=3):
        checkpoint = self._load_proposal_checkpoint(int(round_no))
        if not checkpoint:
            raise ValueError(f"round {round_no} has no legal checkpoint")
        experiments = [Experiment.from_dict(row) for row in checkpoint["experiments"]]
        matches = [
            exp for exp in experiments
            if (exp.progress_url or "").rstrip("/").split("/")[-1] == simulation_id
        ]
        if len(matches) != 1:
            raise ValueError(f"expected one checkpoint experiment for {simulation_id}, found {len(matches)}")
        exp = matches[0]
        if not exp.submission_fingerprint:
            exp.submission_fingerprint = submission_fingerprint(
                exp.expression, exp.settings
            )
        if exp.status == "SKIPPED_STALE":
            return exp
        if checkpoint.get("complete") is True:
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
        self._record_terminal_skip(
            exp, "SKIPPED_STALE", exp.error
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
        print(f"[SKIP] r{round_no} {simulation_id} -> SKIPPED_STALE; attempts={len(attempts)}")
        print(f"[SKIP] audit -> {audit_path}")
        return exp

    def skip_submit_unknown_authorized(self, round_no, proposal_id):
        checkpoint = self._load_proposal_checkpoint(int(round_no))
        if not checkpoint:
            raise ValueError(f"round {round_no} has no legal checkpoint")
        experiments = [Experiment.from_dict(row) for row in checkpoint["experiments"]]
        matches = [exp for exp in experiments if exp.proposal_id == proposal_id]
        if len(matches) != 1:
            raise ValueError(f"expected one checkpoint experiment for {proposal_id}, found {len(matches)}")
        exp = matches[0]
        if not exp.submission_fingerprint:
            exp.submission_fingerprint = submission_fingerprint(
                exp.expression, exp.settings
            )
        if exp.status == "SKIPPED_UNKNOWN":
            return exp
        if checkpoint.get("complete") is True:
            raise ValueError(f"round {round_no} has no unfinished checkpoint")
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
        self._record_terminal_skip(
            exp, "SKIPPED_UNKNOWN", exp.error
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
        read_stats = {}
        rows = [
            Experiment.from_dict(row)
            for row in self._ctx.trajectory.iter_canonical_round(
                int(round_no), stats=read_stats
            )
        ]
        if not rows:
            raise ValueError(f"no trajectory evidence for round {round_no}")
        if read_stats.get("identity_mismatch_rows"):
            raise ValueError(
                f"FINALIZE_EXECUTION_IDENTITY_MISMATCH: round {round_no}"
            )
        experiments = rows
        active = [exp for exp in experiments if exp.status in UNRESOLVED_STATUSES]
        if active:
            raise ValueError(f"FINALIZE_UNRESOLVED_EXECUTION: round {round_no}")
        checkpoint_path = self._proposal_checkpoint_path(int(round_no))
        checkpoint = self._load_proposal_checkpoint(int(round_no))
        if os.path.exists(checkpoint_path) and checkpoint is None:
            raise ValueError(f"FINALIZE_EXECUTION_SET_MISMATCH: round {round_no}")
        checkpoint = self._validate_finalize_checkpoint(
            checkpoint, experiments, int(round_no)
        )
        hypothesis = (checkpoint or {}).get("hypothesis") or {
            "id": f"h-llm-r{round_no}", "_round": int(round_no)
        }
        summary = self._finalize_round_projection(
            int(round_no), hypothesis, experiments,
            close_checkpoint=checkpoint is not None and checkpoint.get("complete") is not True,
        )
        audit_path = os.path.join(self._ctx.state_dir, "round_finalization_log.jsonl")
        append_jsonl_best_effort(
            audit_path,
            {"round_no": int(round_no), "selected": len(experiments),
             "finalized_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
             "source": "trajectory_append_only",
             "checkpoint_status": "CLOSED" if checkpoint is not None else "NO_CHECKPOINT_TO_CLOSE",
             "malformed_rows": read_stats.get("invalid_rows", 0)
             + read_stats.get("malformed_round_rows", 0)},
            ("round_no", "source"),
        )
        print(f"[FINALIZE] r{round_no} diagnosed {len(experiments)} recorded terminal experiments")
        print(f"[FINALIZE] audit -> {audit_path}")
        return summary
