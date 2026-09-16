"""Pure validation gates for public synthetic and local private templates."""

from .model import FASTEXPR_IDENTIFIER_RE, HORIZON_LATTICE, PRIMARY_FIELD_SLOT_ALIASES

SETTINGS_ARMS = {
    "BASE", "UNIVERSE_ARM", "DECAY_DOWN", "DECAY_UP",
    "TRUNCATION_LOW", "TRUNCATION_HIGH",
}
RELATIONSHIP_CONTRACTS = frozenset({
    "SINGLE_FIELD", "COMPARABLE_SPREAD", "DIRECTIONAL_RATIO",
    "CO_MOVEMENT", "MULTI_FIELD_CONFIRMATION", "UNDECLARED",
})
SEMANTIC_CONTRACTS = frozenset({
    "SYNTHETIC_FIXTURE", "DATA_QUALITY", "EVENT_DRIVEN", "RISK_STATE",
    "TIME_SERIES_STATE", "PERSISTENT_STATE", "CROSS_SECTIONAL_STATE",
    "RELATIONAL_PRIMARY", "VECTOR_AGGREGATION", "UNDECLARED",
})


def effective_semantic_contract(template):
    """Return the declared bounded semantic contract without inference."""
    return str(getattr(template, "semantic_contract", "UNDECLARED")
               or "UNDECLARED").upper()


def evaluate_semantic_contract(contract, traits, *, field_type="",
                               uses_vector_operator=False, field_slots=("p",)):
    """Evaluate one bounded primary-field contract using derived traits only."""
    contract = str(contract or "UNDECLARED").upper()
    field_type = str(field_type or "").upper()
    uses_vector_operator = bool(uses_vector_operator)
    known = traits.get("semantic_admission") == "ALLOW"
    concept = traits.get("concept")
    measurement = traits.get("measurement")
    behavior = traits.get("behavior")
    frequency = traits.get("frequency")
    sign_semantics = traits.get("sign_semantics")
    reasons = []

    if contract not in SEMANTIC_CONTRACTS:
        return {"admission": "REVIEW", "score": -15,
                "reasons": ["SEMANTIC_CONTRACT_UNSUPPORTED"]}
    if contract == "UNDECLARED":
        return {"admission": "REVIEW", "score": -15,
                "reasons": ["SEMANTIC_CONTRACT_UNDECLARED"]}
    if uses_vector_operator != (field_type == "VECTOR"):
        return {"admission": "REJECT", "score": -100,
                "reasons": ["VECTOR 类型不匹配"]}
    if field_type == "VECTOR" and contract != "VECTOR_AGGREGATION":
        return {"admission": "REJECT", "score": -100,
                "reasons": ["VECTOR 只能进入向量聚合模板"]}
    if contract == "VECTOR_AGGREGATION":
        if field_type != "VECTOR" or not uses_vector_operator:
            return {"admission": "REJECT", "score": -100,
                    "reasons": ["VECTOR 聚合需要 VECTOR 字段和向量算子"]}
        return {"admission": "ALLOW", "score": 1,
                "reasons": ["向量字段使用结构合法的向量聚合"]}
    if contract == "SYNTHETIC_FIXTURE":
        return {"admission": "ALLOW", "score": 1,
                "reasons": ["synthetic template fixture; no economic evidence"]}

    if contract == "DATA_QUALITY":
        if concept != "data_quality":
            return {"admission": "REJECT", "score": -30,
                    "reasons": ["模板要求 data_quality 语义"]}
        score = 60
        reasons.append("字段语义明确指向数据质量")
    elif contract == "EVENT_DRIVEN":
        if (not known or behavior != "event_driven"
                or frequency in {"weekly", "monthly", "quarterly", "annual"}):
            return {"admission": "REJECT", "score": -30,
                    "reasons": ["缺少事件驱动语义证据"]}
        score = 65
        reasons.append("字段以事件驱动方式更新")
    elif contract == "RISK_STATE":
        if concept not in {"volatility", "market_price", "liquidity", "analyst_revision"}:
            if known:
                return {"admission": "REJECT", "score": -20,
                        "reasons": ["风险模板与字段概念不匹配"]}
        score = 55 if concept == "volatility" else 30
        reasons.append("模板把字段变化解释为风险或异常暴露")
    elif contract in {"TIME_SERIES_STATE", "PERSISTENT_STATE"}:
        if known and concept == "data_quality":
            return {"admission": "REJECT", "score": -20,
                    "reasons": ["数据质量不是该模板的经济输入"]}
        if concept == "analyst_revision":
            score = 65
            reasons.append("分析师修正体现信息更新或扩散过程")
        elif measurement in {"change", "dispersion"}:
            score = 48
            reasons.append("字段提供可观察的变化或离散程度")
        elif measurement == "level" and contract == "PERSISTENT_STATE":
            score = 42
            reasons.append("字段水平适合检验相对状态与持续性")
        else:
            score = 22
            reasons.append("字段可作为有限的时间序列基线")
        if behavior == "slow_moving" and contract == "PERSISTENT_STATE":
            score += 24
            reasons.append("低频字段更适合检验持久状态或历史 regime")
    elif contract in {"CROSS_SECTIONAL_STATE", "RELATIONAL_PRIMARY"}:
        score = 32
        reasons.append("横截面基线不依赖绝对尺度")

    if sign_semantics == "nonnegative_level" and contract == "RISK_STATE":
        score -= 8
        reasons.append("非负水平字段不把符号方向直接解释为反转")
    if not known:
        return {"admission": "REVIEW", "score": score - (15 if len(field_slots) > 1 else 10),
                "reasons": ["语义 UNKNOWN，仅可审阅"]}
    return {"admission": "ALLOW", "score": score, "reasons": reasons}


def effective_relationship_contract(template):
    """Return the bounded machine contract without inferring legacy semantics."""
    model_contract = getattr(template, "effective_relationship_contract", None)
    if model_contract is not None:
        return model_contract
    field_count = getattr(template, "economic_field_count", None)
    if field_count is None:
        slots = tuple(getattr(template, "required_slots", ()) or ())
        field_count = len({"primary" if slot in {"p", "data_field"} else slot
                           for slot in slots if slot in {"p", "data_field", "s", "t"}})
    if field_count == 1:
        return "SINGLE_FIELD"
    return str(getattr(template, "relationship_contract", "UNDECLARED")
               or "UNDECLARED").upper()


def validate_template_contract(template):
    """Return an auditable gate report; never infer missing research semantics."""
    errors = []
    mode = str(getattr(template, "template_mode", "CONCRETE") or "CONCRETE").upper()
    slots = tuple(getattr(template, "operator_slots", ()) or ())
    if mode not in {"CONCRETE", "PARTIAL_OPERATOR"}:
        errors.append("INVALID_TEMPLATE_MODE")
    if mode == "CONCRETE" and slots:
        errors.append("CONCRETE_OPERATOR_SLOT")
    if mode == "PARTIAL_OPERATOR":
        if len(slots) != 1:
            errors.append("OPERATOR_SLOT_COUNT")
        elif (
            len(slots[0].allowed_operators) < 2
            or len(slots[0].allowed_operators) > 3
            or slots[0].baseline_operator not in slots[0].allowed_operators
            or any(not FASTEXPR_IDENTIFIER_RE.fullmatch(name)
                   for name in slots[0].allowed_operators)
        ):
            errors.append("INVALID_OPERATOR_SLOT")
    role = str(template.role or "")
    contract = str(getattr(template, "relationship_contract", "UNDECLARED")
                   or "UNDECLARED").upper()
    semantic_contract = effective_semantic_contract(template)
    if semantic_contract not in SEMANTIC_CONTRACTS:
        errors.append("UNSUPPORTED_SEMANTIC_CONTRACT")
    if contract not in RELATIONSHIP_CONTRACTS:
        errors.append("UNSUPPORTED_RELATIONSHIP_CONTRACT")
    if template.economic_field_count == 1 and contract not in {"SINGLE_FIELD", "UNDECLARED"}:
        errors.append("SINGLE_FIELD_RELATIONSHIP_CONTRACT")
    if template.economic_field_count > 1 and contract == "SINGLE_FIELD":
        errors.append("MULTI_FIELD_SINGLE_FIELD_CONTRACT")
    if PRIMARY_FIELD_SLOT_ALIASES.issubset(template.field_slots):
        errors.append("PRIMARY_FIELD_SLOT_ALIAS_CONFLICT")
    if role == "CONTROL_ALPHA":
        if not 1 <= template.operator_count <= 3:
            errors.append("CONTROL_OPERATOR_COUNT")
        if template.economic_field_count != 1:
            errors.append("CONTROL_ECONOMIC_FIELD_COUNT")
    elif role == "PROBE_ALPHA":
        if not 4 <= template.operator_count <= 6:
            errors.append("PROBE_OPERATOR_COUNT")
        if not 2 <= template.economic_field_count <= 4:
            errors.append("PROBE_ECONOMIC_FIELD_COUNT")
    else:
        errors.append("UNKNOWN_TEMPLATE_ROLE")
    if not template.economic_mechanism.strip():
        errors.append("MISSING_ECONOMIC_MECHANISM")
    if not template.field_relationship.strip():
        errors.append("MISSING_FIELD_RELATIONSHIP")
    if not template.direction_reason.strip():
        errors.append("MISSING_DIRECTION_REASON")
    if not template.expected_horizon.strip():
        errors.append("MISSING_EXPECTED_HORIZON")
    if not template.falsification.strip():
        errors.append("MISSING_FALSIFICATION")
    if not template.novelty_family.strip():
        errors.append("MISSING_NOVELTY_FAMILY")
    if any(arm not in SETTINGS_ARMS for arm in template.allowed_settings_arms):
        errors.append("INVALID_SETTINGS_ARM")
    for profile in template.allowed_horizon_profiles:
        if any(value not in HORIZON_LATTICE for value in profile):
            errors.append("NON_LATTICE_HORIZON")
    return {"ok": not errors, "errors": errors, "role": role,
            "relationship_contract": effective_relationship_contract(template),
            "relationship_contract_status": (
                "DECLARED" if contract != "UNDECLARED" else "LEGACY_UNDECLARED"
            ), "semantic_contract": semantic_contract,
            "semantic_contract_status": (
                "DECLARED" if semantic_contract != "UNDECLARED"
                else "LEGACY_UNDECLARED"
            )}
