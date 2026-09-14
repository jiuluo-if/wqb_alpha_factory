"""Canonical batch-envelope contracts for proposal execution."""

from __future__ import annotations

from .diversity import diversity_audit
from .expression import submission_fingerprint
from .proposal_schema import (
    FACTORY_BATCH_SIZE,
    MAX_TARGETED_CHILDREN,
    MAX_TARGETED_VALIDATIONS,
    TARGETED_BATCH_TTL_SEC,
    TARGETED_BATCH_TYPE,
)


def proposal_budget_cap(candidates_per_round, allocation_cap, hard_cap=18):
    """Return the effective cap under config and an explicit bounded limit."""
    from .proposal_schema import MAX_CONFIGURED_PROPOSALS_PER_ROUND

    try:
        hard_cap = max(0, min(int(hard_cap), MAX_CONFIGURED_PROPOSALS_PER_ROUND))
    except (TypeError, ValueError):
        hard_cap = 18
    return max(0, min(int(candidates_per_round), int(allocation_cap), hard_cap))


def validate_factory_batch(proposals, target=FACTORY_BATCH_SIZE,
                           min_datasets=1, require_cross_dataset_pairs=False):
    """Validate the factory all-or-nothing batch envelope."""
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
        if str(proposal.get("proposal_origin") or "").strip().lower() != "factory":
            errors.append(f"第 {index + 1} 个题案来源必须是 factory")
        if str(proposal.get("research_layer") or "").strip().lower() != "exploration":
            errors.append(f"第 {index + 1} 个题案 research_layer 必须是 exploration")
        if str(proposal.get("research_role") or "").strip().upper() != "EXPLORE":
            errors.append(f"第 {index + 1} 个题案 research_role 必须是 EXPLORE")
        if str(proposal.get("experiment_stage") or "").strip().upper() != "BASELINE":
            errors.append(f"第 {index + 1} 个题案 experiment_stage 必须是 BASELINE")
        if str(proposal.get("exploration_objective") or "").strip().lower() != "signal_discovery":
            errors.append(
                f"第 {index + 1} 个题案 exploration_objective 必须是 signal_discovery"
            )
        identity = submission_fingerprint(expression, proposal.get("settings") or {})
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
        ref_datasets = {
            str(item.get("dataset")) for item in (proposal.get("field_refs") or [])
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
    """Validate the Agent-authored targeted optimization envelope."""
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
            errors.append(f"targeted batch 必须恰好包含 {expected} 个题案，实际 {len(proposals)}")
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
        if str(proposal.get("proposal_origin") or "").strip().lower() != "agent_optimizer":
            errors.append(f"第 {index + 1} 个题案来源必须是 agent_optimizer")
        stage = str(proposal.get("experiment_stage") or "").strip().upper()
        if stage == "CHILD":
            children += 1
        elif stage == "ROBUSTNESS":
            validations += 1
        else:
            errors.append(f"第 {index + 1} 个题案 experiment_stage 必须是 CHILD/ROBUSTNESS")
        identity = submission_fingerprint(expression, proposal.get("settings") or {})
        if identity in identities:
            errors.append(f"第 {index + 1} 个题案与批次内其他题案重复")
        identities.add(identity)
    if children > MAX_TARGETED_CHILDREN:
        errors.append(f"targeted batch 至多 {MAX_TARGETED_CHILDREN} 个 CHILD，实际 {children}")
    if validations > MAX_TARGETED_VALIDATIONS:
        errors.append(
            f"targeted batch 至多 {MAX_TARGETED_VALIDATIONS} 个 VALIDATE，实际 {validations}"
        )
    return not errors, errors


def targeted_batch_state(payload, *, now=None, ttl_sec=TARGETED_BATCH_TTL_SEC):
    """Return deterministic arbitration state for the single proposals inbox."""
    state = {
        "present": False, "valid": False, "expired": False, "blocking": False,
        "status": "NOT_TARGETED_BATCH", "errors": [], "proposal_count": 0,
        "created_at": None, "expires_at": None,
    }
    if not isinstance(payload, dict) or str(payload.get("batch_type") or "").strip() != TARGETED_BATCH_TYPE:
        return state
    proposals = payload.get("proposals")
    proposals = proposals if isinstance(proposals, list) else []
    valid, errors = validate_targeted_batch(proposals)
    created_at = payload.get("created_at")
    expires_at = payload.get("expires_at")
    state.update({
        "present": True, "valid": valid, "errors": list(errors),
        "proposal_count": len(proposals), "created_at": created_at,
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
    stats = {
        "proposal_count": len(proposals) if isinstance(proposals, list) else 0,
        "probe_counts": {"factory": 0, "exploration": 0, "EXPLORE": 0, "BASELINE": 0},
        "exploration_objective_counts": {}, "dataset_counts": {},
        "field_dataset_counts": {}, "template_counts": {},
        "dual_or_multi_field_count": 0, "cross_dataset_pair_count": 0,
    }
    diversity = diversity_audit(proposals or [])
    stats["diversity"] = diversity
    stats["diversity_layers"] = diversity["layers"]
    if isinstance(budget, dict):
        stats["budget"] = dict(budget)
    if isinstance(feasibility, dict):
        stats["feasibility_check"] = dict(feasibility)
    for proposal in proposals or []:
        if not isinstance(proposal, dict):
            continue
        if str(proposal.get("proposal_origin") or "").strip().lower() == "factory":
            stats["probe_counts"]["factory"] += 1
        if str(proposal.get("research_layer") or "").strip().lower() == "exploration":
            stats["probe_counts"]["exploration"] += 1
        if str(proposal.get("research_role") or "").strip().upper() == "EXPLORE":
            stats["probe_counts"]["EXPLORE"] += 1
        if str(proposal.get("experiment_stage") or "").strip().upper() == "BASELINE":
            stats["probe_counts"]["BASELINE"] += 1
        objective = str(proposal.get("exploration_objective") or "").strip()
        if objective:
            stats["exploration_objective_counts"][objective] = stats["exploration_objective_counts"].get(objective, 0) + 1
        datasets = []
        for value in proposal.get("datasets") or []:
            value = value.get("id") or value.get("name") if isinstance(value, dict) else value
            if value is not None and str(value) not in datasets:
                datasets.append(str(value))
        refs = proposal.get("field_refs") or []
        ref_datasets = []
        for ref in refs:
            if isinstance(ref, dict) and ref.get("dataset") is not None and str(ref["dataset"]) not in ref_datasets:
                ref_datasets.append(str(ref["dataset"]))
        effective_datasets = ref_datasets or datasets
        if effective_datasets:
            primary = effective_datasets[0]
            stats["dataset_counts"][primary] = stats["dataset_counts"].get(primary, 0) + 1
        for dataset in effective_datasets:
            stats["field_dataset_counts"][dataset] = stats["field_dataset_counts"].get(dataset, 0) + 1
        fields = proposal.get("fields") or proposal.get("fields_used") or []
        if len(fields) > 1:
            stats["dual_or_multi_field_count"] += 1
        if len(set(effective_datasets)) > 1:
            stats["cross_dataset_pair_count"] += 1
        template = proposal.get("template_id") or proposal.get("template_family")
        if template:
            template = str(template)
            stats["template_counts"][template] = stats["template_counts"].get(template, 0) + 1
    return stats
