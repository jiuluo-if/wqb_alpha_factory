"""Pure screening and CHILD proposal projection for optimization evidence."""

from __future__ import annotations

from .expression import analyze_expression, canonical_expression
from .pre_correlation import optimization_parent_admission
from .proposal_contract import CHILD_CHANGE_TYPES
from .research_guard import overfit_expression_reason, parameter_only_change_reason


def screen_optimization_parents(
    parents, *, excluded_expressions=None, min_sharpe=0.9, min_fitness=0.6,
    min_turnover=0.01, max_turnover=0.7,
):
    """Return only DONE parents with observable, bounded optimization evidence."""
    if not isinstance(parents, (list, tuple)):
        return []
    try:
        thresholds = tuple(float(value) for value in (
            min_sharpe, min_fitness, min_turnover, max_turnover,
        ))
    except (TypeError, ValueError):
        return []
    excluded = {
        canonical_expression(value) for value in (excluded_expressions or [])
        if isinstance(value, str) and value.strip()
    }
    screened, seen = [], set()
    for parent in parents:
        if not isinstance(parent, dict) or str(parent.get("status") or "").upper() != "DONE":
            continue
        expression = parent.get("expression")
        identity = canonical_expression(expression) if isinstance(expression, str) else ""
        if not identity or identity in seen or identity in excluded:
            continue
        admission = optimization_parent_admission(
            parent, min_sharpe=thresholds[0], min_fitness=thresholds[1],
            min_turnover=thresholds[2], max_turnover=thresholds[3],
        )
        if admission["admitted"]:
            seen.add(identity)
            screened.append(parent)
    return screened


def build_optimization_proposals(
    parents, operator_reference, *, max_candidates=4,
    excluded_expressions=None, min_sharpe=0.9, min_fitness=0.6,
    min_turnover=0.01, max_turnover=0.7,
):
    """Build bounded CHILD records from agent-authored economic hypotheses."""
    if not isinstance(parents, (list, tuple)) or not isinstance(operator_reference, dict):
        return []
    try:
        limit = max(0, int(max_candidates))
        thresholds = tuple(float(value) for value in (
            min_sharpe, min_fitness, min_turnover, max_turnover,
        ))
    except (TypeError, ValueError):
        return []
    excluded = {
        canonical_expression(value) for value in (excluded_expressions or [])
        if isinstance(value, str) and value.strip()
    }
    allowed = {str(value) for value in (operator_reference.get("operators") or [])}
    screened = screen_optimization_parents(
        parents, excluded_expressions=excluded_expressions,
        min_sharpe=thresholds[0], min_fitness=thresholds[1],
        min_turnover=thresholds[2], max_turnover=thresholds[3],
    )
    out, seen_parents = [], set()
    for parent in screened:
        if len(out) >= limit or not isinstance(parent, dict):
            break
        child = parent.get("child_economic_hypothesis")
        base = parent.get("expression")
        if not isinstance(child, dict) or not isinstance(base, str) or not base.strip():
            continue
        identity = canonical_expression(base)
        if identity in seen_parents:
            continue
        seen_parents.add(identity)
        fields, datasets = parent.get("fields_used") or [], parent.get("datasets") or []
        common = {key: parent.get(key) for key in (
            "field_understanding", "field_analysis", "field_source",
            "field_hypothesis_basis", "hypothesis_outcome", "confirmation_status",
            "mechanism_learning", "unresolved_question", "next_discriminating_question",
        )}
        if not fields or not datasets or any(not common[key] for key in (
            "field_understanding", "field_analysis", "field_source", "field_hypothesis_basis",
        )):
            continue
        expression = child.get("expression")
        mechanism, change_type = child.get("economic_mechanism"), child.get("change_type")
        if not all(isinstance(value, str) and value.strip() for value in (
            expression, mechanism, change_type,
        )) or change_type not in CHILD_CHANGE_TYPES:
            continue
        if parameter_only_change_reason(base, expression) or overfit_expression_reason(expression):
            continue
        transform = child.get("direction_transform")
        if transform is not None and not isinstance(transform, dict):
            continue
        changed_variable = child.get("changed_variable")
        if not isinstance(changed_variable, str) or not changed_variable.strip():
            changed_variable = change_type
        normalized = canonical_expression(expression)
        if normalized in excluded:
            continue
        actual_ops = list(analyze_expression(expression).operators)
        if not set(actual_ops).issubset(allowed):
            continue
        proposal = {
            "fields": list(fields), "datasets": list(datasets), **common,
            "expression": expression, "operator_mapping": mechanism,
            "economic_mechanism": mechanism,
            "operator_evidence": {
                "sha256": operator_reference.get("capability_fingerprint") or operator_reference.get("sha256"),
                "operators": actual_ops, "rationale": mechanism,
            },
            "experiment_question": child.get(
                "experiment_question", "新的经济机制是否在独立证据上改善净收益与稳定性？",
            ),
            "expected_failure_modes": ["平滑过度导致信号衰减或延迟", "优化后换手、相关性或健康检查恶化"],
            "tuning_risk": bool(child.get("tuning_risk", False)), "experiment_stage": "CHILD",
            "change_type": change_type, "parent_expression": base,
            "parent_id": parent.get("id") or parent.get("proposal_id"),
            "changed_variable": changed_variable, "research_role": "EXPLOIT",
            "lineage_id": parent.get("lineage_id") or parent.get("hypothesis_id"),
            "template_id": f"auto_opt_{change_type}", "template_family": "autonomous_optimization",
            "template_stage_path": "L0:completed signal -> L1:one-variable optimization",
            "template_ref": {"source": "newwqb_autonomous_optimizer", "parent": identity},
            "template_slots": {"parent": base}, "rationale": child.get("rationale") or mechanism,
            "direction": parent.get("direction") or "long",
            "expected_horizon": parent.get("expected_horizon") or "short-term",
            "falsification": child.get("falsification", "若独立样本、健康检查或自相关证据恶化，则关闭该优化分支。"),
            "direction_transform": transform or {"applied": False, "reason": "沿用 parent 的方向，不把方向翻转当作新机制。"},
            "self_correlation_impact": child.get("self_correlation_impact", {
                "expected_effect": "UNKNOWN", "basis": "pre_simulation_structural_forecast",
                "rationale": "优化前没有平台结算序列，不把结构差异冒充为低自相关。", "admission": "REVIEW",
            }),
            "proposal_origin": "agent_optimizer", "research_layer": "optimization",
            "optimization_source": parent.get("optimization_source", "current_run"),
        }
        decision = parent.get("optimization_decision")
        if isinstance(decision, dict) and decision:
            proposal["optimization_decision"] = dict(decision)
        if parent.get("optimization_decision_id"):
            proposal["optimization_decision_id"] = parent["optimization_decision_id"]
        out.append(proposal)
        excluded.add(normalized)
    return out
