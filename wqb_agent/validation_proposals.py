"""Pure ROBUSTNESS proposal construction for bounded validation requests."""

from __future__ import annotations

from .expression import analyze_expression, canonical_expression
from .validation_report import default_validation_plan

VALIDATION_CHANGE_TYPES = {
    "decay": "decay", "truncation": "decay_truncation",
    "universe": "universe", "template_window": "window_change",
}


def build_validation_proposals(
    requests, operator_reference, *, max_candidates=4, excluded_expressions=None,
):
    """Resolve agent-selected single-variable VALIDATE requests to ROBUSTNESS records."""
    if not isinstance(requests, (list, tuple)) or not isinstance(operator_reference, dict):
        return []
    try:
        limit = max(0, int(max_candidates))
    except (TypeError, ValueError):
        return []
    excluded = {
        canonical_expression(value) for value in (excluded_expressions or [])
        if isinstance(value, str) and value.strip()
    }
    allowed_ops = {str(value) for value in (operator_reference.get("operators") or [])}
    out = []
    for request in requests:
        if len(out) >= limit or not isinstance(request, dict):
            break
        parent, variable = request.get("parent"), str(request.get("variable") or "")
        if not isinstance(parent, dict) or variable not in VALIDATION_CHANGE_TYPES:
            continue
        base = parent.get("expression")
        if not isinstance(base, str) or not base.strip():
            continue
        expression, normalized = request.get("expression") or base, canonical_expression(request.get("expression") or base)
        if not normalized or normalized in excluded:
            continue
        actual_ops = list(analyze_expression(expression).operators)
        if not set(actual_ops).issubset(allowed_ops):
            continue
        fields, datasets = parent.get("fields_used") or [], parent.get("datasets") or []
        required = ("field_understanding", "field_analysis", "field_source", "field_hypothesis_basis")
        if not fields or not datasets or any(not parent.get(key) for key in required):
            continue
        settings_override = request.get("settings_override")
        expected_keys = set() if variable == "template_window" else {variable}
        if not isinstance(settings_override, dict) or set(settings_override) != expected_keys:
            continue
        mechanism = parent.get("economic_mechanism")
        expected_effect = str(request.get("expected_effect") or "").strip()
        falsification = str(request.get("falsification") or "").strip()
        reason = str(request.get("reason") or "").strip()
        if not isinstance(mechanism, str) or not mechanism.strip() or not all((expected_effect, falsification, reason)):
            continue
        proposal = {
            "expression": expression, "fields": list(fields), "datasets": list(datasets),
            "field_understanding": parent.get("field_understanding"),
            "field_analysis": parent.get("field_analysis"), "field_source": parent.get("field_source"),
            "field_hypothesis_basis": parent.get("field_hypothesis_basis"), "operator_mapping": reason,
            "economic_mechanism": mechanism,
            "operator_evidence": {"sha256": operator_reference.get("capability_fingerprint") or operator_reference.get("sha256"), "operators": actual_ops, "rationale": reason},
            "experiment_question": expected_effect,
            "expected_failure_modes": ["参数邻域内的改善只是噪声或过拟合", "换手、健康或平台 checks 在新取值下恶化"],
            "tuning_risk": False, "experiment_stage": "ROBUSTNESS",
            "change_type": VALIDATION_CHANGE_TYPES[variable], "parent_expression": base,
            "parent_id": parent.get("id") or parent.get("proposal_id"), "changed_variable": variable,
            "research_role": "VALIDATION", "lineage_id": parent.get("lineage_id") or parent.get("hypothesis_id"),
            "template_id": f"validate_{variable}", "template_family": "bounded_validation",
            "template_stage_path": "L0:completed signal -> L1:one-variable validation",
            "template_ref": {"source": "bounded_validation", "parent": canonical_expression(base)},
            "template_slots": {"parent": base}, "rationale": reason,
            "direction": parent.get("direction") or "long", "expected_horizon": parent.get("expected_horizon") or "short-term",
            "falsification": falsification, "direction_transform": parent.get("direction_transform") or {"applied": False, "reason": "沿用 parent 的方向，不把方向翻转当作新机制。"},
            "self_correlation_impact": parent.get("self_correlation_impact") or {"expected_effect": "UNKNOWN", "basis": "pre_simulation_structural_forecast", "rationale": "参数验证不改变经济暴露来源，先不假设相关性。", "admission": "REVIEW"},
            "settings": dict(settings_override), "validation_plan": default_validation_plan(parent),
            "proposal_origin": "agent_optimizer", "research_layer": "optimization",
        }
        for key in ("numeric_variant", "settings_variant"):
            value = request.get(key)
            if isinstance(value, dict) and value:
                proposal[key] = dict(value)
        decision = request.get("optimization_decision") or parent.get("optimization_decision")
        if isinstance(decision, dict) and decision:
            proposal["optimization_decision"] = dict(decision)
        if request.get("optimization_decision_id"):
            proposal["optimization_decision_id"] = request["optimization_decision_id"]
        out.append(proposal)
        excluded.add(normalized)
    return out
