"""Pure learning projections consumed by Reflector's memory effects."""

from .expression import canonical_expression
from .metrics import score_of
from .research_guard import is_direction_only_change, parameter_only_change_reason


def direction_key(experiment):
    return experiment.expression[:80]


def experiment_score(experiment):
    return score_of((experiment.metrics or {}) or None)


def interpretation(hypothesis, outcomes):
    value = hypothesis.get("agent_interpretation") if isinstance(hypothesis, dict) else None
    if not isinstance(value, dict):
        return None
    outcome = str(value.get("outcome") or "").upper()
    learning = value.get("mechanism_learning")
    refs = value.get("evidence_refs")
    if (outcome not in outcomes or not isinstance(learning, str) or not learning.strip()
            or not isinstance(refs, list) or not refs
            or not all(isinstance(ref, (str, int)) and str(ref).strip() for ref in refs)):
        return None
    normalized = dict(value)
    normalized["outcome"] = outcome
    normalized["evidence_refs"] = [str(ref) for ref in refs]
    if "direct_relevance" in normalized and not isinstance(normalized["direct_relevance"], bool):
        return None
    for key in ("unresolved_question", "competing_explanations",
                "next_discriminating_question", "evidence_needed"):
        if key not in normalized:
            continue
        if key in {"competing_explanations", "evidence_needed"}:
            if not isinstance(normalized[key], list) or not all(
                isinstance(item, str) and item.strip() for item in normalized[key]
            ):
                return None
        elif not isinstance(normalized[key], str) or not normalized[key].strip():
            return None
    return normalized


def independence_blocker(first, second):
    first_expression = canonical_expression(first.expression)
    second_expression = canonical_expression(second.expression)
    if first_expression and first_expression == second_expression:
        return "duplicate expression is not independent evidence"
    if parameter_only_change_reason(first.expression, second.expression):
        return "parameter-only expression variants are not independent evidence"
    if is_direction_only_change(first.expression, second.expression):
        return "direction-only expression variants are not independent evidence"
    return None
