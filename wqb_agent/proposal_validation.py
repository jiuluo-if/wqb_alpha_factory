"""Canonical single-proposal validation and static operator reference contract."""

import hashlib
import os
import re
from collections.abc import Mapping
from importlib import resources

from .diversity import extract_fields
from .expression import (
    analyze_expression,
    expression_field_identifiers,
)
from .field_metadata import profile_frequency_evidence
from .proposal_schema import (
    CHILD_CHANGE_TYPES,
    EXPERIMENT_STAGES,
    PROPOSAL_EXPERIMENT_QS,
)
from .research_guard import (
    is_direction_only_change,
    overfit_expression_reason,
    parameter_only_change_reason,
)
from .validation_report import validate_plan

_VEC_INPUT_RE = re.compile(
    r"\b(vec_avg|vec_sum)\s*\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*\)",
    re.IGNORECASE,
)

def load_operator_syntax_reference(path):
    """Load evergreen syntax hints; never assert current availability."""
    with open(path, encoding="utf-8") as handle:
        text = handle.read()
    operators = sorted(set(re.findall(r"`([a-z][a-z0-9_]*)\s*\(", text)))
    return {
        "path": os.path.abspath(path),
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "operators": operators,
        "source": "STATIC_SYNTAX_REFERENCE",
        "availability": "UNKNOWN",
        "status": "UNKNOWN",
        "evidence_status": "UNAVAILABLE",
    }


def load_packaged_operator_syntax_reference():
    """Load the runtime syntax reference from the installed package."""
    resource = resources.files("wqb_agent.reference").joinpath(
        "OPERATORS_CHEATSHEET.md"
    )
    text = resource.read_text(encoding="utf-8")
    operators = sorted(set(re.findall(r"`([a-z][a-z0-9_]*)\s*\(", text)))
    return {
        "path": "wqb_agent.reference/OPERATORS_CHEATSHEET.md",
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "operators": operators,
        "source": "STATIC_SYNTAX_REFERENCE",
        "availability": "UNKNOWN",
        "status": "UNKNOWN",
        "evidence_status": "UNAVAILABLE",
    }


def _operator_reference(path):
    """Compatibility alias for the static syntax reference loader."""
    return load_operator_syntax_reference(path)


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
                    if item.get("frequency") != profile.get("frequency"):
                        problems.append(
                            f"FIELD_FREQUENCY_PROVENANCE_MISMATCH: field_analysis 的 {field_id} frequency 与 discovery 不一致"
                        )
                    if "frequency_evidence" in item:
                        expected = profile_frequency_evidence(profile)
                        actual = profile_frequency_evidence(item)
                        if any(actual.get(key) != expected.get(key)
                               for key in ("source", "status", "frequency")):
                            problems.append(
                                f"FIELD_FREQUENCY_PROVENANCE_MISMATCH: field_analysis 的 {field_id} frequency_evidence 与 discovery 不一致"
                            )
            identifiers = set(expression_field_identifiers(analyze_expression(expression)))
            unknown = sorted(
                ident for ident in identifiers
                if ident not in profiles_by_id
            )
            if unknown:
                problems.append(f"表达式含本轮 discovery 未确认的字段/标识符: {unknown}")
            if require_research_evidence:
                if isinstance(operator_reference, dict) and "source" in operator_reference:
                    if not (
                        operator_reference.get("status") == "LIVE_VERIFIED"
                        and operator_reference.get("availability") == "AVAILABLE"
                        and operator_reference.get("source") == "BRAIN_LIVE_ONLY"
                    ):
                        problems.append(
                            "operator_reference 必须来自当前 BRAIN LIVE capability，静态/fixture evidence 不足"
                        )
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
    if not isinstance(field_types, Mapping):
        return False, ["field_types 必须是对象"]
    problems = []
    for operator, field_id in _VEC_INPUT_RE.findall(p.get("expression") or ""):
        field_type = (field_types.get(field_id) or "").upper()
        if not field_type:
            problems.append(f"{operator} 输入字段 {field_id} 类型未知；须先查平台真实字段类型")
        elif field_type != "VECTOR":
            problems.append(f"{operator} 只能作用于 VECTOR；{field_id} 实际类型为 {field_type}")
    return not problems, problems
