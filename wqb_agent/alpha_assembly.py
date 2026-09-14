"""Pure candidate metadata assembly helpers."""

from __future__ import annotations

from collections.abc import Mapping

from .expression import analyze_expression, canonical_expression

_CANDIDATE_METADATA_KEYS = (
    "mutation", "template_id", "template_mode", "template_family",
    "template_stage_path", "template_ref", "template_slots",
    "relationship_audit", "factory_version", "template_version",
    "template_fingerprint", "template_catalog_source", "template_bindings",
    "template_structural_fingerprint", "template_mechanism_fingerprint",
    "template_role", "template_operator_count", "template_field_roles",
    "template_field_relationship", "template_novelty_family",
    "template_allowed_settings_arms", "template_allowed_horizon_profiles",
)


def assemble_factory_realizations(
    *, hypothesis, field_id, profile, slot_profiles, candidate, actual_ops,
    template, compatibility, relation, selected_mapping, operator_reference,
    field_source, field_mechanism_fn, derive_traits_fn,
    frequency_evidence_fn, operator_mappings_fn, canonical_fn=canonical_expression,
    expression_analysis_fn=analyze_expression,
):
    """Build the auditable proposal records for one selected candidate.

    Selection and generation stay with ``AlphaFactory``; this function owns
    the canonical candidate-to-proposal projection, including evidence,
    lineage, and partial-operator realizations.  It has no runtime owner and
    receives all domain callbacks explicitly so it remains a pure assembler.
    """
    profile_by_id = {
        str(item.get("id")): item for item in slot_profiles
        if isinstance(item, dict) and item.get("id") is not None
    }
    profile_by_key = {
        (str(item.get("dataset")) if item.get("dataset") is not None else None,
         str(item.get("id"))): item
        for item in slot_profiles
        if isinstance(item, dict) and item.get("id") is not None
    }
    used_field_ids = [
        str(item) for item in (candidate.get("fields_used") or [field_id])
        if str(item) in profile_by_id
    ]
    if not used_field_ids:
        return []
    used_profiles = []
    for field_ref in candidate.get("field_refs") or []:
        if not isinstance(field_ref, dict) or not field_ref.get("id"):
            continue
        key = (str(field_ref.get("dataset")) if field_ref.get("dataset") is not None else None,
               str(field_ref["id"]))
        selected = profile_by_key.get(key) or profile_by_id.get(str(field_ref["id"]))
        if selected is not None and selected not in used_profiles:
            used_profiles.append(selected)
    if not used_profiles:
        used_profiles = [profile_by_id[item] for item in used_field_ids]
    used_field_ids = [str(item.get("id")) for item in used_profiles]
    field_understanding = {
        item: f"基于本轮 discovery 原文：{profile_by_id[item].get('description')}"
        for item in used_field_ids
    }
    field_analysis = {
        item: {
            "semantic": profile_by_id[item].get("description"),
            "coverage": profile_by_id[item].get("coverage"),
            "frequency": profile_by_id[item].get("frequency"),
            "frequency_evidence": frequency_evidence_fn(profile_by_id[item]),
            "data_type": profile_by_id[item].get("type"),
            "semantic_traits": derive_traits_fn(profile_by_id[item]),
        }
        for item in used_field_ids
    }
    mechanisms = {
        item: field_mechanism_fn(
            profile_by_id[item], derive_traits_fn(profile_by_id[item]),
            template, relation,
        )
        for item in used_field_ids
    }
    field_hypothesis_basis = {
        item: {
            "description": profile_by_id[item].get("description"),
            "mechanism": mechanisms[item],
            "semantic_traits": derive_traits_fn(profile_by_id[item]),
            "independent_increment": "该 BASELINE 只检验这些字段组合的独立增量信息。",
            "direction": template.direction,
        }
        for item in used_field_ids
    }
    datasets = []
    for item in used_profiles:
        dataset = item.get("dataset")
        if dataset and dataset not in datasets:
            datasets.append(dataset)
    mechanism = mechanisms[used_field_ids[0]]
    proposal = {
        "expression": candidate["expression"], "fields": used_field_ids,
        "field_refs": list(candidate.get("field_refs") or []), "datasets": datasets,
        "field_understanding": field_understanding, "field_analysis": field_analysis,
        "field_source": field_source, "field_hypothesis_basis": field_hypothesis_basis,
        "economic_mechanism": mechanism,
        "semantic_admission": relation["admission"] if relation else compatibility["admission"],
        "direction_transform": {**candidate["direction_transform"], "reason": mechanism},
        "operator_mapping": candidate["rationale"],
        "operator_evidence": {
            "sha256": operator_reference.get("capability_fingerprint") or operator_reference.get("sha256"),
            "operators": actual_ops, "rationale": candidate["rationale"],
        },
        "experiment_question": (
            "在机制、字段关系、horizon 和 settings 保持不变时，"
            f"{candidate.get('operator_role') or 'operator'} 使用 "
            f"{next(iter((candidate.get('operator_role_mapping') or {}).values()), 'REALIZATION')} "
            "是否改变可复现结果？"
            if template.template_mode == "PARTIAL_OPERATOR"
            else f"字段 {field_id} 的 {template.template_id} 结构是否提供可复现的增量信号？"
        ),
        "operator_contrast_question_key": (
            f"{template.branch_of or template.template_id}::{candidate.get('operator_role') or 'operator'}"
            if template.template_mode == "PARTIAL_OPERATOR" else None
        ),
        "expected_failure_modes": ["字段覆盖不足或缺失导致有效持仓减少", "信号集中或换手异常导致健康检查失败"],
        "tuning_risk": False, "experiment_stage": "BASELINE", "change_type": "baseline",
        "research_role": "EXPLORE", "lineage_id": f"{hypothesis.get('id', 'factory')}:field:{field_id}",
        "signal_family": f"{template.family}:{field_id}", "mechanism_family": f"{template.family}:{field_id}",
        "expected_quality": 1.0, "information_gain": 1.0, "novelty": 1.0,
        "simulation_cost": 1.0, "rationale": candidate["rationale"],
        "direction": template.direction, "expected_horizon": template.expected_horizon,
        "falsification": template.falsification,
        "self_correlation_impact": {
            "expected_effect": "UNKNOWN", "basis": "no_live_behavior_series",
            "rationale": "模拟前没有平台结算值，不把结构差异冒充为低自相关。", "admission": "REVIEW",
        },
    }
    proposal.update({key: candidate[key] for key in _CANDIDATE_METADATA_KEYS if key in candidate})
    realizations = [proposal]
    if template.template_mode == "PARTIAL_OPERATOR":
        slot = template.operator_slots[0]
        proposal.update({
            "template_mode": "PARTIAL_OPERATOR", "template_branch_of": template.branch_of,
            "operator_role": slot.role,
            "operator_role_mapping": {slot.role: next(iter((selected_mapping or {}).values()))},
            "operator_realization_fingerprint": candidate.get("operator_realization_fingerprint"),
            "operator_capability_fingerprint": candidate.get("operator_capability_fingerprint"),
        })
        for mapping in operator_mappings_fn(template, operator_reference):
            if mapping == selected_mapping:
                continue
            expression = template.render(candidate["template_bindings"], mapping)
            alternative = dict(proposal)
            alternative["expression"] = expression
            alternative["operator_role_mapping"] = {slot.role: next(iter(mapping.values()))}
            alternative["operator_realization_fingerprint"] = template.operator_realization_fingerprint(mapping)
            alternative["operator_evidence"] = dict(proposal["operator_evidence"])
            alternative["operator_evidence"]["operators"] = list(expression_analysis_fn(expression).operators)
            realizations.append(alternative)
    for realization in realizations:
        realization["proposal_origin"] = "factory"
    return realizations


def field_mechanism(
    profile: Mapping[str, object],
    traits: Mapping[str, object],
    template: Mapping[str, object],
    relation: Mapping[str, object] | None = None,
) -> str:
    field_id = str(profile.get("id"))
    admission = traits.get("semantic_admission", "UNKNOWN")
    family = template.get("family", "unknown")
    if admission != "ALLOW":
        return (
            f"字段 {field_id} 的语义准入为 {admission}；"
            f"当前 profile 只能支持 {family} 的语法审阅，不能证明该字段具备该经济机制。"
        )
    fit_reason = {
        "analyst_revision": "修正值直接承载分析师预期更新，适合检验变化、持续性或滞后确认",
        "option_relative": "put-call/skew 字段表达期权分布的相对位置，适合离散或相对关系检验",
        "liquidity": "交易活跃度或未平仓量描述参与程度，适合流动性与活动强度检验",
        "volatility": "波动率是风险暴露或状态变量，适合风险调整、regime 或相对关系",
        "fundamental": "低频基本面水平代表经济规模，适合持久性和相对状态检验",
        "earnings": "盈利相关字段承载经营预期，适合变化与信息扩散检验",
        "event_count": "事件计数代表注意力事件强度，适合事件发生后的变化检验",
        "data_quality": "数据质量字段描述可用性风险，只进入缺失或陈旧信息机制",
    }.get(
        traits.get("concept"),
        f"该字段的 {traits.get('measurement')} 测量与 {family} 的有限结构相容",
    )
    mechanism = (
        f"字段 {field_id} 被识别为 {traits.get('concept')}，测量为 {traits.get('measurement')}，"
        f"频率为 {traits.get('frequency')}，符号语义为 {traits.get('sign_semantics')}，"
        f"行为为 {traits.get('behavior')}；{fit_reason}。"
        "该机制仍需用独立样本和平台 checks 证伪。"
    )
    if relation and relation.get("labels"):
        mechanism += f" 槽位关系证据为：{', '.join(relation['labels'])}。"
    return mechanism
