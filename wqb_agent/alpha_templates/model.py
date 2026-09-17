"""Stable data model and rendering primitives for Alpha templates."""

import hashlib
import json
import re
from dataclasses import dataclass

from ..expression import (  # noqa: F401  # re-export template contract
    HORIZON_LATTICE,
    operator_occurrence_count,
)

NUMBER_TOKEN_RE = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)(?![\w.])")
OPERATOR_OCCURRENCE_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(")
OPERATOR_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}\s*\(")
FASTEXPR_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

ECONOMIC_FIELD_SLOTS = ("p", "data_field", "s", "t")
CONTROL_BINDING_SLOTS = ("g",)
PRIMARY_FIELD_SLOT_ALIASES = frozenset({"p", "data_field"})
DIRECTION_TRANSFORMS = frozenset({"identity", "reverse"})

FIXED_NUMERICS = {
    "0.001": ("SAFETY_CONSTANT", "divide epsilon；固定数值稳定性常量"),
    "0.2": ("OPERATOR_REQUIRED_CONSTANT", "trade_when 触发下界"),
    "0.8": ("OPERATOR_REQUIRED_CONSTANT", "trade_when 触发上界"),
    "1": ("OPERATOR_REQUIRED_CONSTANT", "算子位置参数或单位偏移"),
    "5": ("RESEARCH_HORIZON", "交易周周期；仅声明为 slot 时允许轮换"),
    "22": ("RESEARCH_HORIZON", "交易月周期；仅声明为 slot 时允许轮换"),
    "66": ("RESEARCH_HORIZON", "交易季度周期；仅声明为 slot 时允许轮换"),
    "120": ("RESEARCH_HORIZON", "半年周期；仅声明为 slot 时允许轮换"),
    "255": ("RESEARCH_HORIZON", "交易年周期；仅声明为 slot 时允许轮换"),
    "10": ("OPERATOR_CONSTANT_REVIEW", "旧窗口禁止未经语义审查继续轮换"),
    "20": ("OPERATOR_CONSTANT_REVIEW", "旧窗口禁止未经语义审查继续轮换"),
    "60": ("OPERATOR_CONSTANT_REVIEW", "旧窗口禁止未经语义审查继续轮换"),
}


def render_numeric_token(expression, token, value, occurrence=0):
    """Replace only the selected occurrence of an explicitly declared token."""
    matches = [
        match for match in NUMBER_TOKEN_RE.finditer(expression)
        if match.group(1) == str(token)
    ]
    index = max(0, int(occurrence))
    if index >= len(matches):
        raise ValueError(f"numeric slot token not found: {token}#{index}")
    match = matches[index]
    return expression[:match.start(1)] + str(value) + expression[match.end(1):]


@dataclass(frozen=True)
class TemplateNumericSlot:
    """研究数值声明；只有声明的数字允许轮换。"""

    name: str
    kind: str = "window"
    default: float = 0.0
    allowed_values: tuple = ()
    economic_role: str = ""
    token: str = ""
    occurrence: int = 0

    def render(self, expression, value):
        return render_numeric_token(
            expression, self.token or str(self.default), value, self.occurrence
        )


@dataclass(frozen=True)
class TemplateOperatorSlot:
    """One explicitly bounded operator-role branch point."""

    name: str
    role: str
    placeholder: str
    baseline_operator: str
    allowed_operators: tuple
    semantic_contract: str


@dataclass(frozen=True, init=False)
class AlphaTemplate:
    """One bounded expression skeleton loaded from the catalog."""

    template_id: str
    family: str
    expression: str
    required_slots: tuple
    economic_mechanism: str
    direction: str
    direction_transform: object
    expected_horizon: str
    falsification: str
    self_correlation_impact: object
    tags: tuple
    selection_groups: tuple
    selection_order: int
    numeric_slots: tuple
    version: str
    kind: str
    role: str
    field_roles: tuple
    fixed_field_bindings: tuple
    allowed_field_families: tuple
    field_relationship: str
    relationship_contract: str
    semantic_contract: str
    direction_reason: str
    allowed_horizon_profiles: tuple
    allowed_settings_arms: tuple
    mechanism_tags: tuple
    novelty_family: str
    template_mode: str
    operator_slots: tuple

    def __init__(self, template_id, family=None, expression=None,
                 required_slots=("p",),
                 rationale="", economic=False, numeric_slots=(), *,
                 version="1", kind=None, economic_mechanism=None,
                 direction="long", direction_transform="identity",
                 expected_horizon="short-term", falsification="",
                 self_correlation_impact="unknown", tags=(),
                 selection_groups=(), selection_order=1000, role=None,
                 field_roles=(), fixed_field_bindings=(),
                 allowed_field_families=(), field_relationship="",
                 relationship_contract="UNDECLARED",
                 semantic_contract="UNDECLARED",
                 direction_reason="", allowed_horizon_profiles=(),
                 allowed_settings_arms=("BASE",), mechanism_tags=(),
                  novelty_family="", template_mode="CONCRETE",
                  operator_slots=()):
        object.__setattr__(self, "template_id", str(template_id))
        object.__setattr__(self, "family", family or "")
        object.__setattr__(self, "expression", expression or "")
        object.__setattr__(self, "required_slots", tuple(required_slots))
        object.__setattr__(self, "economic_mechanism",
                           economic_mechanism if economic_mechanism is not None else rationale)
        object.__setattr__(self, "direction", direction)
        object.__setattr__(self, "direction_transform", direction_transform)
        object.__setattr__(self, "expected_horizon", expected_horizon)
        object.__setattr__(self, "falsification", falsification)
        object.__setattr__(self, "self_correlation_impact", self_correlation_impact)
        object.__setattr__(self, "tags", tuple(tags))
        object.__setattr__(self, "selection_groups", tuple(selection_groups))
        object.__setattr__(self, "selection_order", int(selection_order))
        object.__setattr__(self, "numeric_slots", tuple(numeric_slots))
        object.__setattr__(self, "version", str(version))
        object.__setattr__(self, "kind", kind or ("economic" if economic else "baseline"))
        object.__setattr__(self, "role", role or ("PROBE_ALPHA" if economic else "CONTROL_ALPHA"))
        object.__setattr__(self, "field_roles", tuple(field_roles))
        object.__setattr__(self, "fixed_field_bindings", tuple(fixed_field_bindings))
        object.__setattr__(self, "allowed_field_families", tuple(allowed_field_families))
        object.__setattr__(self, "field_relationship", field_relationship)
        object.__setattr__(self, "relationship_contract",
                           str(relationship_contract or "UNDECLARED").upper())
        object.__setattr__(self, "semantic_contract",
                           str(semantic_contract or "UNDECLARED").upper())
        object.__setattr__(self, "direction_reason", direction_reason or self.economic_mechanism)
        object.__setattr__(self, "allowed_horizon_profiles", tuple(allowed_horizon_profiles))
        object.__setattr__(self, "allowed_settings_arms", tuple(allowed_settings_arms))
        object.__setattr__(self, "mechanism_tags", tuple(mechanism_tags))
        object.__setattr__(self, "novelty_family", novelty_family or self.family)
        object.__setattr__(self, "template_mode", str(template_mode or "CONCRETE").upper())
        object.__setattr__(self, "operator_slots", tuple(operator_slots))

    @property
    def economic(self):
        return self.kind == "economic"

    @property
    def field_slots(self):
        """Binding slots that consume economic field profiles."""
        return tuple(slot for slot in self.required_slots
                     if slot in ECONOMIC_FIELD_SLOTS)

    @property
    def control_slots(self):
        """Binding slots required for rendering but not economic fields."""
        return tuple(slot for slot in self.required_slots
                     if slot in CONTROL_BINDING_SLOTS)

    @property
    def economic_field_count(self):
        """Conceptual economic field count; p and data_field are aliases."""
        return len({"primary" if slot in PRIMARY_FIELD_SLOT_ALIASES else slot
                    for slot in self.field_slots})

    @property
    def companion_field_slots(self):
        """Economic slots after the primary p/data_field alias."""
        return tuple(slot for slot in self.field_slots
                     if slot not in PRIMARY_FIELD_SLOT_ALIASES)

    @property
    def effective_relationship_contract(self):
        """Bounded contract used by admission; unary templates are implicit singletons."""
        if self.economic_field_count == 1:
            return "SINGLE_FIELD"
        return self.relationship_contract

    @property
    def operator_count(self):
        count = operator_occurrence_count(self.expression)
        return count + (1 if self.direction_transform == "reverse" else 0)

    @property
    def raw_operator_count(self):
        """Internal skeleton count before deterministic mechanical transforms."""
        return operator_occurrence_count(self.expression)

    @property
    def operator_names(self):
        names = tuple(OPERATOR_OCCURRENCE_RE.findall(self.expression)) + tuple(
            slot.baseline_operator for slot in self.operator_slots
        )
        return (("reverse",) + names if self.direction_transform == "reverse"
                else names)

    @property
    def fingerprint(self):
        return self.structural_fingerprint

    @staticmethod
    def _digest(payload):
        return hashlib.sha256(json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")).hexdigest()[:16]

    @property
    def structural_fingerprint(self):
        return self._digest({
            "expression": self.expression,
            "required_slots": self.required_slots,
            "horizon_slots": tuple(slot.name for slot in self.numeric_slots),
            "direction_transform": self.direction_transform,
        })

    def _apply_direction_transform(self, expression):
        transform = self.direction_transform
        if transform == "identity":
            return expression
        if transform == "reverse":
            return f"reverse({expression})"
        raise ValueError("INVALID_DIRECTION_TRANSFORM")

    def render(self, bindings, operator_mapping=None):
        """Render one concrete expression through the sole template owner."""
        values = dict(bindings or {})
        expression = self.expression
        if self.template_mode == "PARTIAL_OPERATOR":
            if len(self.operator_slots) != 1:
                raise ValueError("PARTIAL_OPERATOR requires exactly one operator slot")
            slot = self.operator_slots[0]
            mapping = operator_mapping or {}
            chosen = mapping.get(slot.name, mapping.get(slot.role, slot.baseline_operator))
            if chosen not in slot.allowed_operators:
                raise ValueError(f"operator {chosen} is not allowed for {slot.name}")
            expression = expression.replace(slot.placeholder, chosen)
            if OPERATOR_PLACEHOLDER_RE.search(expression):
                raise ValueError("unresolved operator placeholder")
        return self._apply_direction_transform(expression.format(**values))

    def operator_realization_fingerprint(self, operator_mapping):
        if self.template_mode != "PARTIAL_OPERATOR" or len(self.operator_slots) != 1:
            raise ValueError("operator realization requires one partial operator slot")
        slot = self.operator_slots[0]
        chosen = (operator_mapping or {}).get(slot.name,
                                              (operator_mapping or {}).get(slot.role))
        if chosen not in slot.allowed_operators:
            raise ValueError(f"operator {chosen} is not allowed for {slot.name}")
        return self._digest({
            "abstract_structural": self.structural_fingerprint,
            "operator_mapping": {slot.role: chosen},
        })

    @property
    def mechanism_fingerprint(self):
        return self._digest({
            "family": self.family,
            "mechanism": self.economic_mechanism,
            "relationship": self.field_relationship,
            "direction": self.direction,
            "novelty_family": self.novelty_family,
        })

    def instantiation_fingerprint(self, bindings):
        return self._digest({
            "structural": self.structural_fingerprint,
            "mechanism": self.mechanism_fingerprint,
            "bindings": bindings,
        })

    def catalog_entry(self):
        return {
            "template_id": self.template_id,
            "version": self.version,
            "kind": self.kind,
            "family": self.family,
            "expression": self.expression,
            "required_slots": list(self.required_slots),
            "economic_field_slots": list(self.field_slots),
            "control_slots": list(self.control_slots),
            "economic_field_count": self.economic_field_count,
            "fingerprint": self.fingerprint,
            "source": "synthetic_catalog",
            "operator_count": self.operator_count,
            "economic": self.economic,
            "economic_mechanism": self.economic_mechanism,
            "direction": self.direction,
            "direction_transform": self.direction_transform,
            "expected_horizon": self.expected_horizon,
            "falsification": self.falsification,
            "self_correlation_impact": self.self_correlation_impact,
            "tags": list(self.tags),
            "selection_groups": list(self.selection_groups),
            "selection_order": self.selection_order,
            "role": self.role,
            "field_roles": list(self.field_roles),
            "fixed_field_bindings": list(self.fixed_field_bindings),
            "allowed_field_families": list(self.allowed_field_families),
            "field_relationship": self.field_relationship,
            "relationship_contract": self.effective_relationship_contract,
            "relationship_contract_status": (
                "DECLARED" if self.effective_relationship_contract != "UNDECLARED"
                else "LEGACY_UNDECLARED"
            ),
            "semantic_contract": self.semantic_contract,
            "semantic_contract_status": (
                "DECLARED" if self.semantic_contract != "UNDECLARED"
                else "LEGACY_UNDECLARED"
            ),
            "direction_reason": self.direction_reason,
            "allowed_horizon_profiles": [list(v) if isinstance(v, tuple) else v for v in self.allowed_horizon_profiles],
            "allowed_settings_arms": list(self.allowed_settings_arms),
            "numeric_slots": [
                {"name": slot.name, "kind": slot.kind, "default": slot.default,
                 "allowed_values": list(slot.allowed_values),
                 "economic_role": slot.economic_role, "token": slot.token,
                 "occurrence": slot.occurrence}
                for slot in self.numeric_slots
            ],
            "mechanism_tags": list(self.mechanism_tags),
            "novelty_family": self.novelty_family,
            "template_mode": self.template_mode,
            "operator_slots": [
                {"name": slot.name, "role": slot.role,
                 "placeholder": slot.placeholder,
                 "baseline_operator": slot.baseline_operator,
                 "allowed_operators": list(slot.allowed_operators),
                 "semantic_contract": slot.semantic_contract}
                for slot in self.operator_slots
            ],
        }
