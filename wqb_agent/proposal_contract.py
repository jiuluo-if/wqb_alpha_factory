"""ROLE: CORE
AGENT_RELEVANCE: HIGH
PURPOSE: Validate agent-authored proposals before execution.
READ WHEN: changing schema, field/operator provenance, or budget gates.
DO NOT USE FOR: selecting hypotheses or bypassing fail-closed validation.

Pure proposal contract and allocation validation.

This module has no transport, persistence, or Agent dependency.  Keeping the
proposal contract here lets the long-running orchestrator compose discovery,
execution, and state updates without owning every validation rule itself.
"""

import hashlib
import os
import re

from .diversity import extract_fields
from .expression import (
    analyze_expression,
    expression_field_identifiers,
    submission_fingerprint,
)
from .research_guard import (
    is_direction_only_change,
    overfit_expression_reason,
    parameter_only_change_reason,
)
from .validation_report import validate_plan

PROPOSAL_EXPERIMENT_QS = {
    "field_understanding": "字段含义及其信息含义（必须基于本轮 discovery）",
    "operator_mapping": "算子如何表达该经济机制",
    "experiment_question": "本次 simulation 要回答的单一问题",
}

RESEARCH_ROLES = {"EXPLORE", "EXPLOIT", "VALIDATION"}
# 18 remains the safe default.  Larger batches are an explicit configuration
# choice for factory runs; the upper bound prevents an accidental unbounded
# inbox from becoming a production batch.
MAX_PROPOSALS_PER_ROUND = 18
MAX_CONFIGURED_PROPOSALS_PER_ROUND = 100
FACTORY_BATCH_SIZE = 100
# Agent authored 的 targeted optimization batch：复用同一个 proposals.json，
# 但边界是 ≤4 CHILD + ≤4 ROBUSTNESS VALIDATE，绝不扩张成第二个 inbox。
TARGETED_BATCH_TYPE = "targeted_optimization"
MAX_TARGETED_CHILDREN = 4
MAX_TARGETED_VALIDATIONS = 4
MAX_TARGETED_PROPOSALS = MAX_TARGETED_CHILDREN + MAX_TARGETED_VALIDATIONS
# 有效期只用于“工厂何时可以重新取得 inbox”的确定性仲裁：到期前 factory 不得
# 用 exploration 100 覆盖一个合法且尚未执行的 Agent batch。
TARGETED_BATCH_TTL_SEC = 6 * 3600
EXPERIMENT_STAGES = {"BASELINE", "CHILD", "ROBUSTNESS"}
CHILD_CHANGE_TYPES = {
    "field_swap", "window_change", "operator_variant", "smoothing",
    "neutralization", "decay", "window_locality", "semantic_field_swap",
    "universe", "universe_robustness", "decay_truncation",
}
SETTING_OVERRIDES = {"universe", "truncation", "decay"}
_VEC_INPUT_RE = re.compile(
    r"\b(vec_avg|vec_sum)\s*\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*\)",
    re.IGNORECASE,
)


def proposal_budget_cap(candidates_per_round, allocation_cap, hard_cap=18):
    """Return the effective cap under config and an explicit bounded limit."""
    try:
        hard_cap = max(0, min(int(hard_cap), MAX_CONFIGURED_PROPOSALS_PER_ROUND))
    except (TypeError, ValueError):
        hard_cap = 18
    return max(
        0, min(int(candidates_per_round), int(allocation_cap), hard_cap),
    )


def validate_factory_batch(proposals, target=FACTORY_BATCH_SIZE,
                           min_datasets=1, require_cross_dataset_pairs=False):
    """Validate the factory's all-or-nothing batch envelope.

    This check is intentionally independent of proposal preflight.  The
    caller must run normal preflight on every member too; if any member is
    rejected, the complete factory batch is blocked before the first POST.
    """
    errors = []
    if not isinstance(proposals, list):
        return False, ["工厂 proposals 必须是 list"]
    try:
        expected = int(target)
    except (TypeError, ValueError):
        expected = FACTORY_BATCH_SIZE
    if len(proposals) != expected:
        errors.append(f"工厂批次必须恰好包含 {expected} 个题案，实际 {len(proposals)}")
    identities = set()
    batch_datasets = set()
    cross_dataset_pairs = 0
    for index, proposal in enumerate(proposals):
        if not isinstance(proposal, dict):
            errors.append(f"第 {index + 1} 个题案不是对象")
            continue
        expression = str(proposal.get("expression") or "").strip()
        if not expression:
            errors.append(f"第 {index + 1} 个题案缺 expression")
            continue
        origin = str(proposal.get("proposal_origin") or "").strip().lower()
        if origin not in {"factory", "agent_optimizer"}:
            errors.append(f"第 {index + 1} 个题案来源必须是 factory/agent_optimizer")
        settings = proposal.get("settings") or {}
        identity = submission_fingerprint(expression, settings)
        if identity in identities:
            errors.append(f"第 {index + 1} 个题案与批次内其他题案重复")
        identities.add(identity)
        raw_datasets = proposal.get("datasets") or []
        if isinstance(raw_datasets, (str, int)):
            raw_datasets = [raw_datasets]
        proposal_datasets = {
            str(item.get("id") or item.get("name")) if isinstance(item, dict)
            else str(item)
            for item in raw_datasets
            if isinstance(item, (str, int)) or (
                isinstance(item, dict) and (item.get("id") or item.get("name"))
            )
        }
        batch_datasets.update(proposal_datasets)
        field_refs = proposal.get("field_refs") or []
        ref_datasets = {
            str(item.get("dataset")) for item in field_refs
            if isinstance(item, dict) and item.get("dataset") is not None
        }
        if len(proposal_datasets | ref_datasets) > 1:
            cross_dataset_pairs += 1
    try:
        required_datasets = max(0, int(min_datasets))
    except (TypeError, ValueError):
        required_datasets = 1
    if required_datasets > 1 and len(batch_datasets) < required_datasets:
        errors.append(
            f"工厂批次至少覆盖 {required_datasets} 个 dataset，实际 {len(batch_datasets)}"
        )
    if require_cross_dataset_pairs and cross_dataset_pairs < 1:
        errors.append("工厂批次至少包含 1 个跨 dataset 多字段题案")
    return not errors, errors


def validate_targeted_batch(proposals, *, target=None):
    """Validate the Agent-authored ``targeted_optimization`` batch envelope.

    The targeted batch reuses the single ``proposals.json`` inbox, so its
    contract has to be explicit: only ``agent_optimizer`` CHILD/ROBUSTNESS
    entries, at most 4 CHILD + 4 VALIDATE, unique expressions.  It is checked
    before execution exactly like the factory envelope, so an invalid targeted
    batch never reaches the Simulator.
    """
    errors = []
    if not isinstance(proposals, list):
        return False, ["targeted batch proposals 必须是 list"]
    if not proposals:
        errors.append("targeted batch 不能为空")
    if target is not None:
        try:
            expected = int(target)
        except (TypeError, ValueError):
            expected = None
        if expected is not None and len(proposals) != expected:
            errors.append(
                f"targeted batch 必须恰好包含 {expected} 个题案，实际 {len(proposals)}"
            )
    children = 0
    validations = 0
    identities = set()
    for index, proposal in enumerate(proposals):
        if not isinstance(proposal, dict):
            errors.append(f"第 {index + 1} 个题案不是对象")
            continue
        expression = str(proposal.get("expression") or "").strip()
        if not expression:
            errors.append(f"第 {index + 1} 个题案缺 expression")
            continue
        origin = str(proposal.get("proposal_origin") or "").strip().lower()
        if origin != "agent_optimizer":
            errors.append(f"第 {index + 1} 个题案来源必须是 agent_optimizer")
        stage = str(proposal.get("experiment_stage") or "").strip().upper()
        if stage == "CHILD":
            children += 1
        elif stage == "ROBUSTNESS":
            validations += 1
        else:
            errors.append(
                f"第 {index + 1} 个题案 experiment_stage 必须是 CHILD/ROBUSTNESS"
            )
        settings = proposal.get("settings")
        settings = settings if isinstance(settings, dict) else {}
        identity = submission_fingerprint(expression, settings)
        if identity in identities:
            errors.append(f"第 {index + 1} 个题案与批次内其他题案重复")
        identities.add(identity)
    if children > MAX_TARGETED_CHILDREN:
        errors.append(
            f"targeted batch 至多 {MAX_TARGETED_CHILDREN} 个 CHILD，实际 {children}"
        )
    if validations > MAX_TARGETED_VALIDATIONS:
        errors.append(
            "targeted batch 至多 "
            f"{MAX_TARGETED_VALIDATIONS} 个 VALIDATE，实际 {validations}"
        )
    return not errors, errors


def targeted_batch_state(payload, *, now=None, ttl_sec=TARGETED_BATCH_TTL_SEC):
    """Return the deterministic arbitration state of the canonical inbox.

    ``blocking`` is what the factory must honour: a present, not-yet-expired
    Agent-authored batch owns the single proposals inbox, so exploration must
    not silently overwrite it.  An invalid targeted envelope stays blocking on
    purpose -- discarding it would be the same silent loss this contract exists
    to prevent -- and is reported instead as ``TARGETED_BATCH_INVALID``.
    """
    state = {
        "present": False,
        "valid": False,
        "expired": False,
        "blocking": False,
        "status": "NOT_TARGETED_BATCH",
        "errors": [],
        "proposal_count": 0,
        "created_at": None,
        "expires_at": None,
    }
    if not isinstance(payload, dict):
        return state
    if str(payload.get("batch_type") or "").strip() != TARGETED_BATCH_TYPE:
        return state
    proposals = payload.get("proposals")
    proposals = proposals if isinstance(proposals, list) else []
    valid, errors = validate_targeted_batch(proposals)
    created_at = payload.get("created_at")
    expires_at = payload.get("expires_at")
    state.update({
        "present": True,
        "valid": valid,
        "errors": list(errors),
        "proposal_count": len(proposals),
        "created_at": created_at,
        "expires_at": expires_at,
    })
    current = now if isinstance(now, (int, float)) and not isinstance(now, bool) else None
    if current is not None:
        if isinstance(expires_at, (int, float)) and not isinstance(expires_at, bool):
            state["expired"] = float(current) >= float(expires_at)
        elif isinstance(created_at, (int, float)) and not isinstance(created_at, bool):
            try:
                window = max(0.0, float(ttl_sec))
            except (TypeError, ValueError):
                window = float(TARGETED_BATCH_TTL_SEC)
            state["expired"] = float(current) >= float(created_at) + window
    state["blocking"] = not state["expired"]
    state["status"] = (
        "TARGETED_BATCH_EXPIRED" if state["expired"]
        else ("TARGETED_OPTIMIZATION_PENDING" if valid else "TARGETED_BATCH_INVALID")
    )
    return state


def factory_batch_stats(proposals, feasibility=None, budget=None):
    """Return auditable composition counts without retaining result payloads."""
    from .diversity import diversity_audit

    stats = {
        "proposal_count": len(proposals) if isinstance(proposals, list) else 0,
        "layer_counts": {"optimization": 0, "exploration": 0, "unknown": 0},
        "optimization_source_counts": {"cloud": 0, "current_run": 0, "unknown": 0},
        "exploration_objective_counts": {},
        "dataset_counts": {},
        "field_dataset_counts": {},
        "template_counts": {},
        "dual_or_multi_field_count": 0,
        "cross_dataset_pair_count": 0,
    }
    diversity = diversity_audit(proposals or [])
    stats["diversity"] = diversity
    stats["diversity_layers"] = diversity["layers"]
    if isinstance(budget, dict):
        stats["budget"] = dict(budget)
    if isinstance(feasibility, dict):
        stats["feasibility_probe"] = dict(feasibility)
    for proposal in proposals or []:
        if not isinstance(proposal, dict):
            continue
        layer = str(proposal.get("research_layer") or "").strip().lower()
        if layer not in stats["layer_counts"]:
            layer = "unknown"
        stats["layer_counts"][layer] += 1
        if layer == "optimization":
            source = str(proposal.get("optimization_source") or "").strip().lower()
            if source not in stats["optimization_source_counts"]:
                source = "unknown"
            stats["optimization_source_counts"][source] += 1
        if layer == "exploration":
            objective = str(proposal.get("exploration_objective") or "").strip()
            if objective:
                stats["exploration_objective_counts"][objective] = (
                    stats["exploration_objective_counts"].get(objective, 0) + 1
                )
        datasets = []
        for value in proposal.get("datasets") or []:
            value = value.get("id") or value.get("name") if isinstance(value, dict) else value
            if value is not None and str(value) not in datasets:
                datasets.append(str(value))
        refs = proposal.get("field_refs") or []
        ref_datasets = []
        for ref in refs:
            if not isinstance(ref, dict) or ref.get("dataset") is None:
                continue
            dataset = str(ref["dataset"])
            if dataset not in ref_datasets:
                ref_datasets.append(dataset)
        effective_datasets = ref_datasets or datasets
        primary_dataset = effective_datasets[0] if effective_datasets else None
        if primary_dataset:
            stats["dataset_counts"][primary_dataset] = (
                stats["dataset_counts"].get(primary_dataset, 0) + 1
            )
        for dataset in effective_datasets:
            stats["field_dataset_counts"][dataset] = (
                stats["field_dataset_counts"].get(dataset, 0) + 1
            )
        fields = proposal.get("fields") or proposal.get("fields_used") or []
        if len(fields) > 1:
            stats["dual_or_multi_field_count"] += 1
        if len(set(effective_datasets)) > 1:
            stats["cross_dataset_pair_count"] += 1
        template = proposal.get("template_id") or proposal.get("template_family")
        if template:
            template = str(template)
            stats["template_counts"][template] = (
                stats["template_counts"].get(template, 0) + 1
            )
    return stats


def _operator_reference(path):
    """Read the checked-in operator table and produce proposal evidence."""
    with open(path, encoding="utf-8") as handle:
        text = handle.read()
    operators = sorted(set(re.findall(r"`([a-z][a-z0-9_]*)\s*\(", text)))
    return {
        "path": os.path.abspath(path),
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "operators": operators,
    }


def _expression_operators(expression):
    return list(analyze_expression(expression).operators)


def validate_proposal(p, discovered_fields=None, strict_experiment=False,
                      operator_reference=None, require_research_evidence=False,
                      max_alpha_count=None, require_economic_integrity=False,
                      require_platform_alpha_count=False):
    """Validate proposal metadata and field/operator provenance."""
    if not isinstance(p, dict):
        return False, ["proposal 必须是对象"]
    problems = []
    expression = (p.get("expression") or "").strip()
    if require_economic_integrity:
        mechanism = p.get("economic_mechanism")
        if not isinstance(mechanism, str) or len(mechanism.strip()) < 12:
            problems.append("economic_mechanism 必须明确说明字段与收益机制")
        direction = str(p.get("direction") or "").lower()
        if direction not in {"long", "short", "reversal", "neutral", "conditional"}:
            problems.append("direction 必须明确为 long/short/reversal/neutral/conditional")
        transform = p.get("direction_transform")
        if (not isinstance(transform, dict)
                or not isinstance(transform.get("applied"), bool)
                or not isinstance(transform.get("reason"), str)
                or not transform.get("reason", "").strip()):
            problems.append("direction_transform 必须说明是否转换方向及原因")
        impact = p.get("self_correlation_impact")
        impact_errors = validate_self_correlation_impact(impact)
        problems.extend(impact_errors)
        overfit_reason = overfit_expression_reason(expression)
        if overfit_reason:
            problems.append(overfit_reason)
        parent = p.get("parent_expression")
        if isinstance(parent, str) and is_direction_only_change(parent, expression):
            problems.append("候选只改变 parent 方向，不能作为新的研究实验")
        parameter_reason = parameter_only_change_reason(parent, expression)
        if parameter_reason and p.get("experiment_stage") != "ROBUSTNESS":
            problems.append(parameter_reason)
    declared_fields = p.get("fields")
    if not isinstance(declared_fields, list) or not declared_fields:
        problems.append("fields 必须是非空数组")
    elif expression and not extract_fields(expression, [str(f) for f in declared_fields]):
        problems.append("fields 必须至少包含一个实际出现在 expression 中的真实字段")
    if strict_experiment:
        for key, meaning in PROPOSAL_EXPERIMENT_QS.items():
            value = p.get(key)
            valid = (
                isinstance(value, dict) and bool(value)
                if key == "field_understanding"
                else isinstance(value, str) and bool(value.strip())
            )
            if not valid:
                problems.append(f"缺 {key}（{meaning}）")
        if not isinstance(p.get("datasets"), list) or not p.get("datasets"):
            problems.append("datasets 必须是非空数组")
        failure_modes = p.get("expected_failure_modes")
        if not (isinstance(failure_modes, list) and failure_modes
                and all(isinstance(x, str) and x.strip() for x in failure_modes)):
            problems.append("expected_failure_modes 必须是非空字符串数组")
        if not isinstance(p.get("tuning_risk"), bool):
            problems.append("tuning_risk 必须显式为 true 或 false")
        stage = p.get("experiment_stage")
        change_type = p.get("change_type")
        if stage not in EXPERIMENT_STAGES:
            problems.append(f"experiment_stage 必须是 {sorted(EXPERIMENT_STAGES)} 之一")
        elif stage == "BASELINE":
            if change_type not in (None, "baseline"):
                problems.append("BASELINE 的 change_type 只能是 baseline 或省略")
        else:
            if change_type not in CHILD_CHANGE_TYPES:
                problems.append(
                    "Child/ROBUSTNESS 必须声明单一 change_type: "
                    + ", ".join(sorted(CHILD_CHANGE_TYPES))
                )
            if not isinstance(p.get("parent_expression"), str) or not p["parent_expression"].strip():
                problems.append("Child/ROBUSTNESS 必须声明已完成 baseline 的 parent_expression")
            if not isinstance(p.get("changed_variable"), str) or not p["changed_variable"].strip():
                problems.append("Child/ROBUSTNESS 必须声明唯一 changed_variable")
            if stage == "ROBUSTNESS":
                plan_ok, plan_errors = validate_plan(p.get("validation_plan"))
                if not plan_ok:
                    problems.extend(["ROBUSTNESS 必须先注册 ValidationPlan: " + error
                                     for error in plan_errors])
        profiles_by_id = {}
        profiles_by_key = {}
        for field in discovered_fields or []:
            if not isinstance(field, dict) or not field.get("id"):
                continue
            field_id = str(field["id"])
            profiles_by_id.setdefault(field_id, []).append(field)
            dataset = field.get("dataset")
            if dataset is not None:
                profiles_by_key[(str(dataset), field_id)] = field

        def profile_for(field_id):
            candidates = profiles_by_id.get(field_id) or []
            refs = p.get("field_refs")
            if isinstance(refs, list):
                for ref in refs:
                    if not isinstance(ref, dict) or str(ref.get("id")) != field_id:
                        continue
                    dataset = ref.get("dataset")
                    if dataset is not None:
                        profile = profiles_by_key.get((str(dataset), field_id))
                        if profile is not None:
                            return profile
            return candidates[0] if len(candidates) == 1 else None

        if not profiles_by_id:
            problems.append("缺少本轮 discovery 字段画像；请先运行 python main.py suggest")
        else:
            used = extract_fields(expression, [str(f) for f in declared_fields or []])
            understanding = p.get("field_understanding")
            if not isinstance(understanding, dict):
                problems.append("field_understanding 必须是以 field id 为键的对象")
            for field_id in used:
                profile = profile_for(field_id)
                if profile is None:
                    if len(profiles_by_id.get(field_id) or []) > 1:
                        problems.append(
                            f"字段 {field_id} 在多个 dataset 中存在，必须提供准确 field_refs"
                        )
                    else:
                        problems.append(f"字段 {field_id} 不在本轮真实 discovery 中")
                    continue
                if profile.get("semantic_status") == "UNKNOWN" or not profile.get("description"):
                    problems.append(f"字段 {field_id} 缺平台语义 metadata，状态为 UNKNOWN")
                entry = understanding.get(field_id) if isinstance(understanding, dict) else None
                if not isinstance(entry, str) or not entry.strip():
                    problems.append(f"field_understanding 缺 {field_id} 的解释")
                count = profile.get("alpha_count", profile.get("alphaCount"))
                platform_dedupe = profile.get("platform_dedupe") or {}
                if (
                    require_platform_alpha_count
                    and str(platform_dedupe.get("status") or "UNKNOWN").upper()
                    != "KNOWN"
                ):
                    problems.append(
                        f"字段 {field_id} 平台 alphaCount 状态为 "
                        f"{platform_dedupe.get('status') or 'UNKNOWN'}，不能准入"
                    )
                if max_alpha_count is not None:
                    try:
                        if count is not None and float(count) > float(max_alpha_count):
                            problems.append(f"字段 {field_id} alphaCount={count} 超过上限 {max_alpha_count}")
                    except (TypeError, ValueError):
                        problems.append(f"字段 {field_id} alphaCount 无法判读")
            analysis = p.get("field_analysis")
            if not isinstance(analysis, dict):
                problems.append("field_analysis 必须逐字段记录 semantic/coverage/frequency/data_type")
            else:
                for field_id in used:
                    item = analysis.get(field_id)
                    if not isinstance(item, dict):
                        problems.append(f"field_analysis 缺 {field_id} 的字段画像")
                        continue
                    required = {"semantic", "coverage", "frequency", "data_type"}
                    if not required.issubset(item):
                        problems.append(f"field_analysis 缺 {field_id} 的 semantic/coverage/frequency/data_type")
                        continue
                    profile = profile_for(field_id)
                    if profile is None:
                        continue
                    if item.get("data_type") != profile.get("type"):
                        problems.append(f"field_analysis 的 {field_id} data_type 必须与 BRAIN discovery 一致")
            identifiers = set(expression_field_identifiers(analyze_expression(expression)))
            unknown = sorted(
                ident for ident in identifiers
                if ident not in profiles_by_id
            )
            if unknown:
                problems.append(f"表达式含本轮 discovery 未确认的字段/标识符: {unknown}")
            if require_research_evidence:
                basis = p.get("field_hypothesis_basis")
                if not isinstance(basis, dict):
                    problems.append("field_hypothesis_basis 必须逐字段引用真实 description 并说明机制")
                else:
                    for field_id in used:
                        item = basis.get(field_id)
                        profile = profile_for(field_id) or {}
                        if not isinstance(item, dict) or not isinstance(item.get("mechanism"), str) or not item["mechanism"].strip():
                            problems.append(f"field_hypothesis_basis 缺 {field_id} 的机制说明")
                        elif item.get("description") != profile.get("description"):
                            problems.append(f"field_hypothesis_basis 的 {field_id} 必须逐字引用 discovery description")
                evidence = p.get("operator_evidence")
                actual_ops = set(_expression_operators(expression))
                if not isinstance(evidence, dict):
                    problems.append("operator_evidence 必须记录算子表 hash、实际算子及理由")
                elif evidence.get("sha256") != (operator_reference or {}).get("sha256"):
                    problems.append("operator_evidence 不匹配当前 OPERATORS_CHEATSHEET 快照")
                else:
                    declared_ops = set(evidence.get("operators") or [])
                    allowed_ops = set((operator_reference or {}).get("operators") or [])
                    if not actual_ops.issubset(declared_ops):
                        problems.append("operator_evidence 必须覆盖表达式实际使用的全部算子")
                    if not actual_ops.issubset(allowed_ops):
                        problems.append("表达式含不在 OPERATORS_CHEATSHEET 的算子")
                    if not isinstance(evidence.get("rationale"), str) or not evidence["rationale"].strip():
                        problems.append("operator_evidence 缺少算子选择理由")
    return not problems, problems


def validate_self_correlation_impact(value):
    """Validate a pre-simulation correlation-impact forecast.

    The forecast is a planning claim only.  ``UNKNOWN/REVIEW`` is allowed for
    exploration, while ``HIGHER/BLOCK`` is rejected.  Actual admission still
    requires behavioral evidence when available and the platform's settled
    SELF_CORRELATION value later in the submission gate.
    """
    problems = []
    if not isinstance(value, dict):
        return ["self_correlation_impact 必须说明提交后的预期影响与准入动作"]
    expected = str(value.get("expected_effect") or "").upper()
    if expected not in {"LOWER", "SIMILAR", "HIGHER", "UNKNOWN"}:
        problems.append("self_correlation_impact.expected_effect 必须是 LOWER/SIMILAR/HIGHER/UNKNOWN")
    if not isinstance(value.get("basis"), str) or not value["basis"].strip():
        problems.append("self_correlation_impact.basis 不能为空")
    if not isinstance(value.get("rationale"), str) or not value["rationale"].strip():
        problems.append("self_correlation_impact.rationale 不能为空")
    admission = str(value.get("admission") or "").upper()
    if admission not in {"ALLOW", "REVIEW", "BLOCK"}:
        problems.append("self_correlation_impact.admission 必须是 ALLOW/REVIEW/BLOCK")
    if expected == "HIGHER" or admission == "BLOCK":
        problems.append("self_correlation_impact 预计提高自相关或已标记 BLOCK，不得进入模拟/提交准入")
    if expected in {"SIMILAR", "UNKNOWN"} and admission == "ALLOW":
        problems.append("self_correlation_impact 为 SIMILAR/UNKNOWN 只能 REVIEW，不能直接 ALLOW")
    return problems


def proposal_priority(proposal):
    """Small, inspectable priority heuristic; it is not a tuning score."""
    if not isinstance(proposal, dict):
        return 0.0

    def value(name, default=1.0):
        try:
            return max(0.1, float(proposal.get(name, default)))
        except (TypeError, ValueError):
            return default
    return value("expected_quality") * value("information_gain") * value("novelty") / value("simulation_cost")


def validate_vector_inputs(p, field_types):
    """Require vec_avg/vec_sum inputs to be verified VECTOR fields."""
    if not isinstance(p, dict):
        return False, ["proposal 必须是对象"]
    if not isinstance(field_types, dict):
        return False, ["field_types 必须是对象"]
    problems = []
    for operator, field_id in _VEC_INPUT_RE.findall(p.get("expression") or ""):
        field_type = (field_types.get(field_id) or "").upper()
        if not field_type:
            problems.append(f"{operator} 输入字段 {field_id} 类型未知；须先查平台真实字段类型")
        elif field_type != "VECTOR":
            problems.append(f"{operator} 只能作用于 VECTOR；{field_id} 实际类型为 {field_type}")
    return not problems, problems
