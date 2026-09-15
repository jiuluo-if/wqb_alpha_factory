"""已有研究证据驱动的优化候选工作流。

这个模块只编排已有证据、代码初筛和 Agent 已提供的 child hypothesis。
它不生成经济机制，不执行参数扫描，也不拥有 Simulation 或研究状态写入。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, TypedDict

from .experiment import Experiment
from .expression import canonical_expression
from .optimization_decision import (
    OPPORTUNITY_CATEGORIES,
    VALID_DECISIONS,
    VALIDATION_VARIABLES,
    OptimizationDecision,
    decision_rejections,
    numeric_variant_provenance,
    optimization_decision_identity,
    parent_setting_value,
    summarize_parent,
    validation_candidate_values,
    validation_rejections,
)
from .optimizer_selection import (
    OptimizerLocalEvidenceView,
    decision_for_parent,
    next_action,
    optimization_priority,
    parent_rejections,
    same_value,
)
from .pre_correlation import READINESS_BANDS, failing_check_names
from .research_guard import overfit_expression_reason, parameter_only_change_reason
from .research_yield import ResearchYieldFunnel, child_generation_bound


@dataclass(frozen=True)
class OptimizerHooks:
    """Optimizer 所需的窄操作回调。"""

    ensure_loaded: Callable[[], None]
    terminal_expressions: Callable[[], set]
    # universe 的合法候选池由配置提供；缺省时 universe VALIDATE 仍需真实
    # LOW_SUB_UNIVERSE_SHARPE 证据才会被接受（fail-closed）。
    allowed_universes: Callable[[], tuple] | None = None
    # 当前 Simulation delay：pre-correlation 门槛按 0/1 分开，未知即 fail-closed。
    simulation_delay: Callable[[], Any] | None = None
    # 同进程已解析的 SELF_CORRELATION（transient、可重新获取、只读）。跨进程
    # 仍可重新 GET；这条 hook 只避免同进程重复 GET，不写 canonical trajectory。
    resolved_self_correlation: Callable[[str], Any] | None = None


class OptimizerGateReport(TypedDict):
    parent_count: int
    done_parent_count: int
    evidence_eligible_parent_count: int
    evidence_rejected_parent_count: int
    agent_reviewed_parent_count: int
    agent_decision_child_count: int
    agent_decision_validate_count: int
    agent_decision_reroute_count: int
    agent_decision_stop_count: int
    child_generated_count: int
    child_done_count: int
    incremental_pass_count: int
    incremental_fail_count: int
    incremental_unknown_count: int
    ready_parent_count: int
    blocked_reasons: dict[str, int]
    simulation_done: int
    trajectory_recorded: int
    optimizer_candidates: int
    optimizer_rejected: int
    child_generated: int


# Optimization readiness band 的确定性优先级（§41/§42）：先 readiness，再可修
# blocker，最后才是过线距离；绝不退化成 ORDER BY sharpe DESC。
_READINESS_PRIORITY = {band: index for index, band in enumerate(READINESS_BANDS)}

# 已结算的相关性结果会改变下一步：FAIL 需要新的经济结构，PASS 可以推进。
_CORRELATION_FAIL_STATES = frozenset({"FAIL", "FAILED", "BLOCK", "HIGHER"})

# 公开的取值集合：Agent 只需消费 hint，最终仍由 Agent author 决策。
NEXT_ACTIONS = (
    "CHECK_SELF_CORRELATION",
    "CONSIDER_CHILD",
    "CONSIDER_VALIDATE",
    "CONSIDER_CORRELATION_REPAIR",
    "READY_TO_ADVANCE",
    "REROUTE_OR_STOP",
    "STOP",
)


def optimizer_conversions(report):
    """Agent Optimization Yield：derived 转化率；分母 0 一律 None。"""
    def ratio(numerator, denominator):
        if not denominator:
            return None
        return numerator / denominator

    return {
        "done_to_evidence_parent": ratio(
            report["evidence_eligible_parent_count"], report["done_parent_count"]
        ),
        "evidence_parent_to_agent_review": ratio(
            report["agent_reviewed_parent_count"],
            report["evidence_eligible_parent_count"],
        ),
        "agent_review_to_child_decision": ratio(
            report["agent_decision_child_count"], report["agent_reviewed_parent_count"]
        ),
        "child_decision_to_child_generated": ratio(
            report["child_generated_count"], report["agent_decision_child_count"]
        ),
        "child_to_done": ratio(
            report["child_done_count"], report["child_generated_count"]
        ),
        "child_to_incremental_pass": ratio(
            report["incremental_pass_count"], report["child_done_count"]
        ),
    }


def optimization_eligibility_map(parents):
    """Per-parent optimizer stage map for the ResearchYield funnel.

    Keeps the two optimizer stages distinct: ``eligible`` is the Python
    evidence gate, ``reviewed`` / ``decision`` is the Agent
    ``OptimizationDecision``.  Only existing gate helpers are reused, so a
    parent that was never reviewed simply has no Agent stage.
    """
    result = {}
    for parent in parents or ():
        record = (
            parent if isinstance(parent, Mapping)
            else getattr(parent, "to_dict", lambda: {})()
        )
        if not isinstance(record, Mapping):
            continue
        key = (
            record.get("proposal_id") or record.get("id")
            or record.get("submission_fingerprint")
        )
        if key in (None, ""):
            continue
        plain = dict(record)
        reasons = list(parent_rejections(plain))
        decision = decision_for_parent(plain)
        result[str(key)] = {
            "eligible": not reasons,
            "reasons": reasons,
            "reviewed": decision is not None,
            "decision": decision.decision if decision is not None else None,
        }
    return result


class OptimizerWorkflow:
    """编排证据驱动的 CHILD 候选，不决定经济含义。

    Optimization evidence is process-local to the supplied trajectory
    object.  The persisted Alpha Feed contains only remote ID/status/time
    metadata and can change priority for an already-present trajectory row;
    it can never reconstruct a DONE parent or its metrics.
    """

    def __init__(
        self,
        *,
        trajectory,
        alpha_feed_cache,
        alpha_factory,
        quality_policy,
        operator_reference,
        hooks: OptimizerHooks,
    ):
        self.trajectory = trajectory
        self.alpha_feed_cache = alpha_feed_cache
        self.alpha_factory = alpha_factory
        self.quality_policy = quality_policy
        self.operator_reference = operator_reference
        self.hooks = hooks
        self.last_handoff_report: dict[str, int] = {}

    def _local_evidence_view(self, limit=256):
        """Build one immutable trajectory projection for the current request."""
        iterate = getattr(self.trajectory, "iter_canonical_rows", None)
        if callable(iterate):
            rows = list(iterate())
            return OptimizerLocalEvidenceView.from_rows(rows[-max(1, int(limit or 256)):])
        return OptimizerLocalEvidenceView.from_rows(
            self.trajectory.recent(max(1, int(limit or 256)))
        )

    def optimizable_signal_records(self, limit=128):
        """返回 trajectory 中已有 DONE 证据，并按 cloud metadata 排序。"""
        records = []
        rejected = 0
        cloud_ids = self._cloud_alpha_ids()
        view = self._local_evidence_view(limit)
        experiments = list(view.records)
        for position, exp in enumerate(reversed(experiments)):
            record = dict(exp)
            reasons = parent_rejections(record)
            if reasons:
                rejected += 1
                continue
            # Keep evidence ownership separate from the compatibility source
            # label consumed by existing proposal/batch statistics.
            record["evidence_source"] = "local_trajectory"
            record["priority_source"] = (
                "cloud_metadata"
                if str(record.get("alpha_id") or "") in cloud_ids
                else "trajectory_recency"
            )
            record["optimization_source"] = (
                "cloud" if str(record.get("alpha_id") or "") in cloud_ids else "current_run"
            )
            record["optimization_recency"] = position
            records.append(record)
        records.sort(
            key=lambda item: (
                item.get("optimization_source") != "cloud",
                item.get("optimization_recency", 0),
            )
        )
        self.last_handoff_report = {
            "simulation_done": sum(
                1 for exp in experiments
                if str(getattr(exp, "status", "")).upper() == "DONE"
            ),
            "trajectory_recorded": len(records),
            "optimizer_candidates": len(records),
            "optimizer_rejected": rejected,
            "child_generated": 0,
        }
        return records

    def _cloud_alpha_ids(self):
        """读取 weekly cache 中的 Alpha ID，仅作为优先级提示。

        This method intentionally returns IDs only; it never creates a local
        Experiment, imports remote metrics, or changes the trajectory.
        """
        payload = self.alpha_feed_cache.load()
        if not isinstance(payload, dict):
            return set()
        ids = set()
        for bucket in (payload.get("days") or {}).values():
            if not isinstance(bucket, dict):
                continue
            for key in ("simulations", "submitted_alphas"):
                for row in bucket.get(key) or ():
                    if not isinstance(row, dict):
                        continue
                    alpha_id = row.get("alpha_id") or row.get("id")
                    if alpha_id is not None and str(alpha_id).strip():
                        ids.add(str(alpha_id))
        return ids

    def _agent_screen_optimization_parents(self, parents):
        """验证 Agent 已提供的经济机制、单一变化与反过拟合约束。"""
        selected = []
        for parent in parents or ():
            if not isinstance(parent, dict):
                continue
            if isinstance(parent.get("optimization_decision"), Mapping):
                # 正式 OptimizationDecision 契约：完整字段 + 单一变化 +
                # self-correlation 准入；不再依赖隐式 dict mutation。
                decision = decision_for_parent(parent)
                if decision is None or not decision.is_child:
                    continue
                if decision_rejections(decision, parent):
                    continue
                selected.append(parent)
                continue
            child = parent.get("child_economic_hypothesis")
            if not isinstance(child, dict):
                continue
            required = ("expression", "economic_mechanism", "change_type")
            if not all(
                isinstance(child.get(key), str) and child[key].strip()
                for key in required
            ):
                continue
            if parameter_only_change_reason(
                parent.get("expression"), child.get("expression")
            ) or overfit_expression_reason(child.get("expression")):
                continue
            selected.append(parent)
        return selected

    def inspect_optimizer_parents(self, limit=8):
        """有限、只读的 evidence-eligible parent summary（不泄漏无限历史）。"""
        return [summary for _, summary in self._bounded_parent_summaries(limit)]

    def _metric_policy(self):
        """当前 simulation delay + quality policy（delay 未知保持 None）。"""
        hook = getattr(self.hooks, "simulation_delay", None)
        delay = hook() if callable(hook) else None
        return delay, self.quality_policy

    def _bounded_parent_summaries(self, limit, *, view=None):
        """有限、只读的 (record, summary) 对；不泄漏无限历史。"""
        self.hooks.ensure_loaded()
        limit = max(1, int(limit or 8))
        delay, quality_policy = self._metric_policy()
        rows = []
        view = view or self._local_evidence_view(max(limit * 8, 64))
        for record in reversed(view.records):
            if not isinstance(record, Mapping):
                continue
            record = dict(record)
            if parent_rejections(record):
                continue
            record = self._overlay_resolved_correlation(record)
            summary = summarize_parent(
                record, delay=delay, quality_policy=quality_policy
            )
            rows.append((record, summary))
        rows.sort(key=lambda row: optimization_priority(row[1], _READINESS_PRIORITY))
        return rows[:limit]

    def _overlay_resolved_correlation(self, record):
        """只读叠加同进程已结算的 SELF_CORRELATION（不写 trajectory）。"""
        hook = getattr(self.hooks, "resolved_self_correlation", None)
        if not callable(hook):
            return record
        alpha_id = record.get("alpha_id")
        if not alpha_id:
            return record
        evidence = hook(str(alpha_id))
        if not isinstance(evidence, Mapping):
            return record
        status = str(evidence.get("status") or "").upper()
        if status not in {"PASS", "FAIL"}:
            return record
        merged = dict(record)
        merged["self_correlation"] = dict(evidence)
        return merged

    def _numeric_variants_for(self, record):
        """§30/§39：已声明的模板 slot 与 settings 有界池，只给候选不提交。"""
        settings = record.get("settings")
        settings = settings if isinstance(settings, Mapping) else {}
        registry = getattr(self.alpha_factory, "registry", None)
        template_id = record.get("template_id")
        template = (
            registry.get(template_id)
            if registry is not None and template_id else None
        )
        template_slots = [
            {
                "slot": slot.name,
                "kind": slot.kind,
                "default": slot.default,
                "allowed_values": list(slot.allowed_values or ()),
                "economic_role": slot.economic_role,
            }
            for slot in getattr(template, "numeric_slots", ()) or ()
        ]
        universes = self._allowed_universes()
        settings_pools = {}
        for variable in VALIDATION_VARIABLES:
            if variable == "template_window":
                continue
            values = validation_candidate_values(
                variable, current=settings.get(variable),
                allowed_universes=universes,
            )
            settings_pools[variable] = list(values or ())
        return {
            "parent_id": record.get("id"),
            "template_id": template_id,
            "template_slots": template_slots,
            "settings_pools": settings_pools,
        }

    def _generation_bound(self, *, view=None):
        """复用唯一 ResearchYield 有界多代策略，不新建第二套规则。

        父本 gate 不携带 children，所以多代边界必须从 canonical trajectory 的
        真实 CHILD 证据派生（``experiment_stage == "CHILD"``）；ROBUSTNESS /
        VALIDATE 不是新一代 CHILD，不能用来解锁 C2。
        """
        children = self._trajectory_child_records(view=view)
        report = self.gate_report([], children=children)
        funnel = ResearchYieldFunnel(
            mechanism_key="optimizer",
            children_done=report["child_done_count"],
            incremental_pass=report["incremental_pass_count"],
            incremental_available=bool(
                report["incremental_pass_count"]
                + report["incremental_fail_count"]
                + report["incremental_unknown_count"]
            ),
        )
        bound = child_generation_bound(funnel)
        bound.update({
            "children_generated": len(children),
            "children_done": report["child_done_count"],
            "incremental_pass": report["incremental_pass_count"],
            "incremental_fail": report["incremental_fail_count"],
            "incremental_unknown": report["incremental_unknown_count"],
        })
        return bound

    @staticmethod
    def _is_child_generation_record(record):
        """只把明确的 ``experiment_stage == CHILD`` 视为新一代。"""
        if not isinstance(record, Mapping):
            return False
        return str(record.get("experiment_stage") or "").upper() == "CHILD"

    def _canonical_trajectory_rows(self, *, view=None):
        """canonical trajectory 视图：同一 Experiment 只取最新合法 revision。"""
        if view is not None:
            return list(view.records)
        iterate = getattr(self.trajectory, "iter_canonical_rows", None)
        if callable(iterate):
            return [row for row in iterate() if isinstance(row, Mapping)]
        recent = getattr(self.trajectory, "recent", None)
        if not callable(recent):
            return []
        limit = getattr(self.trajectory, "max_len", 256) or 256
        merged = {}
        for row in recent(limit * 4) or ():
            record = row.to_dict() if hasattr(row, "to_dict") else row
            if isinstance(record, Mapping) and record.get("id"):
                merged[record["id"]] = record
        return list(merged.values())

    def _trajectory_child_records(self, *, view=None):
        """canonical trajectory 中真实的 CHILD 记录（fail-closed 识别）。"""
        return [
            row for row in self._canonical_trajectory_rows(view=view)
            if self._is_child_generation_record(row)
        ]

    def optimizer_context(self, *, limit=8):
        """§39-§42：bounded、只读的 metric-aware optimizer context。

        只读取已有 trajectory 证据与配置池：不写状态、不发 POST、不生成经济机制，
        也不会把全部 DONE 历史塞进 Agent context。
        """
        self.hooks.ensure_loaded()
        limit = max(1, min(int(limit or 8), 8))
        view = self._local_evidence_view(max(256, limit * 8))
        rows = self._bounded_parent_summaries(limit, view=view)
        parents = []
        blocker_counts: dict[str, int] = {}
        readiness_counts = {band: 0 for band in READINESS_BANDS}
        self_correlation_counts: dict[str, int] = {}
        eligibility = []
        variants = []
        generation_bound = self._generation_bound(view=view)
        for record, summary in rows:
            correlation = str(
                summary.get("self_correlation_status") or "UNKNOWN"
            ).upper()
            context = dict(summary.get("metric_optimization_context") or {})
            if correlation in _CORRELATION_FAIL_STATES:
                # 已结算 FAIL 既不是“等待查询”，也不是参数验证问题：它需要新的
                # 经济结构（或换路），所以不得继续显示 PRE_CORRELATION_READY。
                context["readiness"] = "STRUCTURAL_REPAIR_REQUIRED"
                context["pre_correlation_eligible"] = False
                opportunities = list(context.get("opportunities") or ())
                if "SELF_CORRELATION_REPAIR" not in opportunities:
                    opportunities.insert(0, "SELF_CORRELATION_REPAIR")
                context["opportunities"] = opportunities
            summary = dict(
                summary,
                metric_optimization_context=context,
                next_action=next_action(
                    context.get("readiness"),
                    self_correlation_status=correlation,
                    generation_allowed=bool(generation_bound.get("allowed", True)),
                ),
            )
            for name in context.get("blocking_checks") or ():
                blocker_counts[name] = blocker_counts.get(name, 0) + 1
            band = str(context.get("readiness") or "LOW_INFORMATION")
            readiness_counts[band] = readiness_counts.get(band, 0) + 1
            status = str(summary.get("self_correlation_status") or "UNKNOWN")
            self_correlation_counts[status] = (
                self_correlation_counts.get(status, 0) + 1
            )
            eligibility.append({
                "parent_id": summary.get("parent_id"),
                "eligible": bool(context.get("pre_correlation_eligible")),
                "readiness": band,
                "reasons": list(context.get("reasons") or ()),
                "opportunities": list(context.get("opportunities") or ()),
                "next_action": summary.get("next_action"),
            })
            candidate = self._numeric_variants_for(record)
            if candidate["template_slots"] or any(
                candidate["settings_pools"].values()
            ):
                variants.append(candidate)
            parents.append(summary)
        gate = self.gate_report(list(view.records))
        return {
            "parent_limit": limit,
            "parent_count": len(parents),
            "eligible_parents": parents,
            "failure_blocker_summary": dict(sorted(blocker_counts.items())),
            "readiness_counts": readiness_counts,
            "self_correlation_counts": self_correlation_counts,
            "numeric_variants_available": variants,
            "pre_correlation_eligibility": eligibility,
            "generation_bound": generation_bound,
            "next_action": (
                parents[0]["next_action"] if parents
                else next_action(
                    None,
                    generation_allowed=bool(generation_bound.get("allowed", True)),
                )
            ),
            "decision_contract": {
                "decisions": list(VALID_DECISIONS),
                "validation_variables": list(VALIDATION_VARIABLES),
                "opportunities": list(OPPORTUNITY_CATEGORIES),
                "readiness_bands": list(READINESS_BANDS),
                "next_actions": list(NEXT_ACTIONS),
            },
            "ranking": (
                "readiness_band_then_structural_blockers_then_repairable_"
                "blockers_then_metric_distance"
            ),
            "blocked_reasons": gate["blocked_reasons"],
            "gate": gate,
        }

    @staticmethod
    def _optimizer_value(parent, key, default=None):
        if isinstance(parent, dict):
            return parent.get(key, default)
        return getattr(parent, key, default)

    def _gate_records(self, limit=128):
        """把 trajectory 行规范化为 mapping（不假设一定是 Experiment）。"""
        records = []
        for row in self.trajectory.recent(limit):
            record = row.to_dict() if hasattr(row, "to_dict") else row
            if isinstance(record, Mapping):
                records.append(record)
        return records

    def gate_report(self, parents=None, *, children=None):
        """返回纯计数 gate：evidence eligibility 与 Agent decision 分离。"""
        if parents is None:
            parents = self._gate_records()
        report: OptimizerGateReport = {
            "parent_count": 0,
            "done_parent_count": 0,
            "evidence_eligible_parent_count": 0,
            "evidence_rejected_parent_count": 0,
            "agent_reviewed_parent_count": 0,
            "agent_decision_child_count": 0,
            "agent_decision_validate_count": 0,
            "agent_decision_reroute_count": 0,
            "agent_decision_stop_count": 0,
            "child_generated_count": len(list(children or ())),
            "child_done_count": 0,
            "incremental_pass_count": 0,
            "incremental_fail_count": 0,
            "incremental_unknown_count": 0,
            "ready_parent_count": 0,
            "blocked_reasons": {},
            "simulation_done": 0, "trajectory_recorded": 0,
            "optimizer_candidates": 0, "optimizer_rejected": 0,
            "child_generated": 0,
        }
        def block(reason):
            blocked = report["blocked_reasons"]
            blocked[reason] = blocked.get(reason, 0) + 1

        for parent in parents or []:
            report["parent_count"] += 1
            if not isinstance(parent, (dict, Experiment)):
                block("INVALID_PARENT")
                continue
            if str(self._optimizer_value(parent, "status", "")).upper() != "DONE":
                block("PARENT_NOT_DONE")
                continue
            report["done_parent_count"] += 1
            record = parent if isinstance(parent, dict) else parent.to_dict()
            evidence = list(parent_rejections(record))
            missing = list(evidence)
            decision = decision_for_parent(record)
            # Readiness needs an Agent-authored *CHILD* decision: either the
            # legacy ``child_economic_hypothesis`` dict or the formal
            # ``OptimizationDecision`` contract.  VALIDATE / REROUTE / STOP are
            # explicit decisions not to derive a child.  This stays separate
            # from the Python evidence gate counted above.
            if decision is None:
                missing.append("PARENT_INCREMENTAL_EVIDENCE_INSUFFICIENT")
            if evidence:
                report["evidence_rejected_parent_count"] += 1
            else:
                report["evidence_eligible_parent_count"] += 1
                if decision is not None:
                    report["agent_reviewed_parent_count"] += 1
                    if decision.decision == "CHILD":
                        report["agent_decision_child_count"] += 1
                    elif decision.decision == "VALIDATE":
                        report["agent_decision_validate_count"] += 1
                    elif decision.decision == "REROUTE":
                        report["agent_decision_reroute_count"] += 1
                    else:
                        report["agent_decision_stop_count"] += 1
            if missing:
                for reason in missing:
                    block(reason)
            if missing or decision is None or not decision.is_child:
                continue
            report["ready_parent_count"] += 1
        for child in children or ():
            record = (
                child if isinstance(child, dict)
                else getattr(child, "to_dict", lambda: {})()
            )
            if not isinstance(record, Mapping):
                continue
            if str(record.get("status") or "").upper() == "DONE":
                report["child_done_count"] += 1
            incremental_row = record.get("incremental_evidence")
            verdict = ""
            if isinstance(incremental_row, Mapping):
                verdict = str(incremental_row.get("decision") or "").upper()
            elif isinstance(record.get("final_outcome"), Mapping):
                verdict = str(
                    record["final_outcome"].get("incremental_decision") or ""
                ).upper()
            if verdict == "PASS":
                report["incremental_pass_count"] += 1
            elif verdict == "FAIL":
                report["incremental_fail_count"] += 1
            elif verdict:
                report["incremental_unknown_count"] += 1
        report["simulation_done"] = report["done_parent_count"]
        report["trajectory_recorded"] = report["evidence_eligible_parent_count"]
        report["optimizer_candidates"] = report["evidence_eligible_parent_count"]
        report["optimizer_rejected"] = max(
            0, report["parent_count"] - report["ready_parent_count"]
        )
        return report

    def _optimization_exclusions(self, parents):
        """终态表达式只排除“新提案”，不排除被优化的 parent 本身。

        ``terminal_expressions`` 同时包含已达终态的 parent 表达式；原样当成初筛
        排除项会让每个已完成 parent 都被判为“已终结”，Agent authored 的
        CHILD/VALIDATE 永远生成不出来。这里只保留真正的非 parent 终态表达式。
        """
        excluded = set()
        for value in self.hooks.terminal_expressions() or ():
            if isinstance(value, str) and value.strip():
                excluded.add(canonical_expression(value))
        own = set()
        for record in parents or ():
            if not isinstance(record, Mapping):
                continue
            expression = record.get("expression")
            if isinstance(expression, str) and expression.strip():
                own.add(canonical_expression(expression))
        return excluded - own

    def generate(self, parents=None, *, max_candidates=4):
        """生成受限 CHILD proposal；绝不自行补全 hypothesis。"""
        self.hooks.ensure_loaded()
        parents = (
            self.optimizable_signal_records()
            if parents is None else parents
        )
        quality = self.quality_policy or {}
        code_screened = self.alpha_factory.screen_optimization_parents(
            parents,
            excluded_expressions=self._optimization_exclusions(parents),
            min_sharpe=quality.get("promising_sharpe", 0.9),
            min_fitness=quality.get("promising_fitness", 0.6),
            min_turnover=quality.get("min_turnover", 0.01),
            max_turnover=quality.get("max_turnover", 0.7),
        )
        agent_screened = self._agent_screen_optimization_parents(code_screened)
        result = self.alpha_factory.optimize_signal_proposals(
            agent_screened,
            self.operator_reference,
            max_candidates=max_candidates,
            excluded_expressions=self._optimization_exclusions(agent_screened),
            min_sharpe=quality.get("promising_sharpe", 0.9),
            min_fitness=quality.get("promising_fitness", 0.6),
            min_turnover=quality.get("min_turnover", 0.01),
            max_turnover=quality.get("max_turnover", 0.7),
        )
        self.last_handoff_report.update({
            "optimizer_candidates": len(agent_screened),
            "optimizer_rejected": max(0, len(parents) - len(agent_screened)),
            "child_generated": len(result or []),
        })
        return result

    def _allowed_universes(self):
        hook = getattr(self.hooks, "allowed_universes", None)
        if not callable(hook):
            return ()
        values = hook()
        if not isinstance(values, (list, tuple, set, frozenset)):
            return ()
        return tuple(str(value) for value in values if str(value).strip())

    def _resolve_template_window(self, decision, parent):
        """从模板显式声明的 numeric slot 解析候选表达式（不猜数字）。"""
        registry = getattr(self.alpha_factory, "registry", None)
        template_id = parent.get("template_id")
        template = (
            registry.get(template_id)
            if registry is not None and template_id else None
        )
        expression = parent.get("expression")
        if template is None or not isinstance(expression, str) or not expression.strip():
            return None
        for slot in getattr(template, "numeric_slots", ()) or ():
            for value in slot.allowed_values or ():
                if value == slot.default or not same_value(value, decision.new_value):
                    continue
                try:
                    rendered = slot.render(expression, value)
                except (KeyError, ValueError):
                    continue
                if rendered == expression:
                    continue
                return {
                    "slot": slot.name,
                    "kind": slot.kind,
                    "candidate_value": value,
                    "parent_default_value": slot.default,
                    "expression": rendered,
                    "change_count": 1,
                    "economic_role": slot.economic_role,
                    "source_template": template.template_id,
                    "reason": decision.reason,
                    "template_variant_id": f"{template.template_id}@{slot.name}={value}",
                }
        return None

    def _resolve_validation_request(self, decision, parent):
        """Python 解析合法有界候选值；Agent 只选择"验证哪个变量"。"""
        variable = str(getattr(decision, "validation_variable", "") or "")
        settings = parent.get("settings")
        settings = settings if isinstance(settings, Mapping) else {}
        metrics = parent.get("metrics")
        blockers = failing_check_names(metrics if isinstance(metrics, Mapping) else {})
        allow_universe = any(
            "SUB_UNIVERSE" in name or "SUBUNIVERSE" in name for name in blockers
        )
        expression = parent.get("expression")
        numeric_variant = None
        if variable == "template_window":
            variant = self._resolve_template_window(decision, parent)
            if variant is None:
                return None, ["VALIDATION_TEMPLATE_SLOT_UNRESOLVED"]
            expression = variant["expression"]
            numeric_variant = dict(variant)
            numeric_variant.pop("expression", None)
            pool = (variant["candidate_value"],)
        else:
            pool = validation_candidate_values(
                variable, current=settings.get(variable),
                allowed_universes=self._allowed_universes(),
            )
            if variable == "universe" and not pool:
                # 没有经过配置/授权解析的 universe 池时不允许自由字符串：
                # Python 不发明 universe，也不做 round-robin。
                return None, ["VALIDATION_UNIVERSE_POOL_UNAVAILABLE"]
        reasons = list(validation_rejections(
            decision, parent, allowed_values=pool, allow_universe=allow_universe
        ))
        if variable == "universe" and not isinstance(decision.new_value, str):
            reasons.append("VALIDATION_UNIVERSE_INVALID")
        if reasons:
            return None, reasons
        new_value = decision.new_value
        verified_old_value = (
            (numeric_variant or {}).get("parent_default_value")
            if variable == "template_window"
            else parent_setting_value(parent, variable)
        )
        settings_override = (
            {} if variable == "template_window" else {variable: new_value}
        )
        return {
            "parent": dict(parent),
            "optimization_decision_id": optimization_decision_identity(decision),
            "optimization_decision": decision.as_dict(),
            "variable": variable,
            "old_value": verified_old_value,
            "new_value": new_value,
            "expected_effect": decision.expected_effect,
            "falsification": decision.falsification,
            "reason": decision.reason,
            "expression": expression,
            "settings_override": settings_override,
            "numeric_variant": numeric_variant,
            "settings_variant": (
                numeric_variant_provenance(
                    decision,
                    source_template=parent.get("template_id"),
                    slot=variable,
                    parent_default=verified_old_value,
                    candidate=new_value,
                )
                if variable != "template_window" else None
            ),
        }, []

    def _canonical_parents(self, parent_ids):
        """Resolve a decision batch with one canonical trajectory pass."""
        targets = {str(value) for value in parent_ids or () if value}
        if not targets:
            return {}
        finder = getattr(self.trajectory, "find_rows", None)
        if callable(finder):
            rows = finder(sorted(targets))
            if isinstance(rows, Mapping):
                return {
                    target: row for target, row in rows.items()
                    if isinstance(row, Mapping)
                }
            return {}
        view = self._local_evidence_view(256)
        return {
            target: row for target, row in view.by_identity().items()
            if target in targets
        }

    def _canonical_parent(self, parent_id):
        """Compatibility helper for callers resolving one parent."""
        return self._canonical_parents([parent_id]).get(str(parent_id or ""))

    def generate_from_decisions(self, decisions, *, max_candidates=4):
        """Validate Agent OptimizationDecisions, then reuse the one CHILD path.

        Python only checks the decision against canonical evidence and the
        deterministic gates; it never authors a mechanism.  A CHILD decision
        reuses the one child path; a VALIDATE decision only resolves a bounded
        single-variable candidate from the declared pools and emits one
        ROBUSTNESS proposal.  REROUTE/STOP never produce a proposal.
        """
        self.hooks.ensure_loaded()
        decisions = list(decisions or ())
        accepted: list[dict] = []
        validation_queue: list[dict] = []
        rejected: list[dict] = []
        decision_results: list[dict] = []
        pending_results: list[tuple[int, str]] = []
        parent_ids = [
            decision.parent_id for decision in decisions
            if isinstance(decision, OptimizationDecision)
        ]
        parents_by_id = self._canonical_parents(parent_ids)
        for decision_index, decision in enumerate(decisions):
            if not isinstance(decision, OptimizationDecision):
                rejected.append({"parent_id": None, "reasons": ["DECISION_INVALID"]})
                decision_results.append({"parent_id": None, "outcome": "REJECTED", "decision": None,
                                         "decision_id": None, "_optimization_decision_index": decision_index})
                continue
            parent = parents_by_id.get(str(decision.parent_id or ""))
            if parent is None:
                rejected.append(
                    {"parent_id": decision.parent_id, "reasons": ["PARENT_NOT_FOUND"]}
                )
                decision_results.append({"parent_id": decision.parent_id,
                                         "decision": decision.decision, "outcome": "REJECTED",
                                         "decision_id": optimization_decision_identity(decision),
                                         "_optimization_decision_index": decision_index})
                continue
            reasons = list(parent_rejections(parent))
            if not reasons and decision.is_validate:
                request, validation_reasons = self._resolve_validation_request(
                    decision, parent
                )
                if not validation_reasons and request is not None:
                    request["_optimization_decision_index"] = decision_index
                    validation_queue.append(request)
                    pending_results.append((decision_index, decision.parent_id))
                    continue
                reasons = list(validation_reasons)
            elif not reasons:
                reasons = list(decision_rejections(decision, parent))
            if not decision.is_child and not decision.is_validate:
                rejected.append({
                    "parent_id": decision.parent_id,
                    "decision": decision.decision,
                    "reasons": reasons or ["NOT_A_CHILD_DECISION"],
                })
                decision_results.append({"parent_id": decision.parent_id,
                                         "decision": decision.decision,
                                         "outcome": decision.decision if decision.decision in {"STOP", "REROUTE"} else "REJECTED",
                                         "decision_id": optimization_decision_identity(decision),
                                         "_optimization_decision_index": decision_index})
                continue
            if reasons:
                rejected.append({
                    "parent_id": decision.parent_id,
                    "decision": decision.decision,
                    "reasons": reasons,
                })
                decision_results.append({"parent_id": decision.parent_id,
                                         "decision": decision.decision,
                                         "outcome": "PRUNED" if any("PRUNE" in str(reason).upper() for reason in reasons) else "REJECTED",
                                         "decision_id": optimization_decision_identity(decision),
                                         "_optimization_decision_index": decision_index})
                continue
            record = dict(parent)
            record["_optimization_decision_index"] = decision_index
            record["optimization_decision_id"] = optimization_decision_identity(decision)
            record["optimization_decision"] = decision.as_dict()
            record["child_economic_hypothesis"] = decision.to_child_hypothesis()
            accepted.append(record)
            pending_results.append((decision_index, decision.parent_id))
        proposals = (
            self.generate(accepted, max_candidates=max_candidates)
            if accepted else []
        )
        validation_proposals = []
        if validation_queue:
            builder = getattr(self.alpha_factory, "validation_proposals", None)
            if callable(builder):
                try:
                    validation_limit = max(0, int(max_candidates) - len(proposals or []))
                except (TypeError, ValueError):
                    validation_limit = 0
                validation_proposals = builder(
                    validation_queue,
                    self.operator_reference,
                    max_candidates=validation_limit,
                    excluded_expressions=self._optimization_exclusions(
                        [request.get("parent") for request in validation_queue]
                    ),
                )
            else:
                for request in validation_queue:
                    rejected.append({
                        "parent_id": (request.get("parent") or {}).get("id"),
                        "decision": "VALIDATE",
                        "reasons": ["VALIDATION_BUILDER_UNAVAILABLE"],
                    })
                validation_queue = []
        generated_decision_ids = {
            str(proposal.get("optimization_decision_id"))
            for proposal in list(proposals or []) + list(validation_proposals or [])
            if isinstance(proposal, dict) and proposal.get("optimization_decision_id")
        }
        if not generated_decision_ids and (proposals or validation_proposals) and len(pending_results) == 1:
            # A single pending decision is the only safe compatibility mapping
            # for an opaque factory result.
            generated_count = len(list(proposals or [])) + len(list(validation_proposals or []))
            if generated_count:
                decision_index, _ = pending_results[0]
                generated_decision_ids.add(optimization_decision_identity(decisions[decision_index]))
        result_by_index = {item.get("_optimization_decision_index"): item
                           for item in decision_results if "_optimization_decision_index" in item}
        for decision_index, parent_id in pending_results:
            decision = decisions[decision_index]
            result_by_index[decision_index] = {
                "parent_id": parent_id,
                "decision": decision.decision,
                "decision_id": optimization_decision_identity(decision),
                "outcome": "GENERATED" if optimization_decision_identity(decision) in generated_decision_ids else "NO_CANDIDATE",
                "_optimization_decision_index": decision_index,
            }
        decision_results = [
            {key: value for key, value in result_by_index.get(index, {
                "parent_id": getattr(decision, "parent_id", None),
                "decision": getattr(decision, "decision", None),
                "decision_id": optimization_decision_identity(decision) if isinstance(decision, OptimizationDecision) else None,
                "outcome": "REJECTED",
            }).items() if key != "_optimization_decision_index"}
            for index, decision in enumerate(decisions)
        ]
        return {
            "proposals": list(proposals or []) + list(validation_proposals or []),
            "accepted": [
                {"parent_id": record.get("id"),
                 "change_type": record["optimization_decision"].get("change_type")}
                for record in accepted
            ],
            "rejected": rejected,
            "decision_report": {
                "reviewed": len(decisions),
                "accepted": len(accepted),
                "rejected": len(rejected),
                "child_generated": len(proposals or []),
                "validation_requests": len(validation_queue),
                "validation_generated": len(validation_proposals or []),
            },
            "decision_results": decision_results,
        }
