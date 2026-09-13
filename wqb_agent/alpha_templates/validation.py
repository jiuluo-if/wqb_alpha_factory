"""Pure validation gates for public synthetic and local private templates."""

from .model import FASTEXPR_IDENTIFIER_RE, HORIZON_LATTICE, PRIMARY_FIELD_SLOT_ALIASES

SETTINGS_ARMS = {
    "BASE", "UNIVERSE_ARM", "DECAY_DOWN", "DECAY_UP",
    "TRUNCATION_LOW", "TRUNCATION_HIGH",
}
RESEARCH_VARIABLES = {
    "HORIZON", "FIELD", "MECHANISM", "UNIVERSE", "DECAY", "TRUNCATION",
}


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
        if not getattr(template, "branch_of", None):
            errors.append("ABSTRACT_BRANCH_PARENT_MISSING")
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
    return {"ok": not errors, "errors": errors, "role": role}


def validate_single_variable_change(changed_variable):
    """Validate the research-policy arm without evaluating its performance."""
    value = str(changed_variable or "").upper()
    return {"ok": value in RESEARCH_VARIABLES,
            "changed_variable": value,
            "reason": "one major research variable per experiment"}
