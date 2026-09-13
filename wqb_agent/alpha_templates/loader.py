"""Fail-closed loading for synthetic package examples and private catalogs."""

import importlib.resources as resources
import io
import os
import tomllib
from pathlib import Path

from .model import (
    FASTEXPR_IDENTIFIER_RE,
    FIXED_NUMERICS,
    HORIZON_LATTICE,
    NUMBER_TOKEN_RE,
    AlphaTemplate,
    TemplateNumericSlot,
    TemplateOperatorSlot,
)
from .validation import validate_template_contract

_KINDS = {"baseline", "economic"}
_DIRECTIONS = {"long", "reversal"}
_SLOTS = {"p", "s", "t", "g", "data_field"}
_GROUPS = {"default", "factory_default", "reversal", "relationship", "momentum", "candidate_scratch", "economic", "vector"}
_REQUIRED = (
    "id", "version", "kind", "family", "expression", "required_slots",
    "stage_path", "economic_mechanism", "direction", "direction_transform",
    "expected_horizon", "falsification", "self_correlation_impact",
    "selection_groups",
)


class PrivateTemplateCatalogError(FileNotFoundError):
    """Raised when a private catalog is not explicitly available."""

    code = "PRIVATE_TEMPLATE_CATALOG_MISSING"


def _text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"template {name}: {name} must be a non-empty string")
    return value.strip()


def _slot(raw, template_id):
    if not isinstance(raw, dict):
        raise ValueError(f"{template_id}: numeric slot must be a table")
    name = _text(raw.get("name"), "numeric slot name")
    allowed = raw.get("allowed_values")
    if not isinstance(allowed, list) or not allowed:
        raise ValueError(f"{template_id}: slot {name} missing allowed_values")
    token = raw.get("token")
    token = str(token) if token is not None else str(raw.get("default"))
    kind = _text(raw.get("kind", "window"), "numeric slot kind")
    if kind == "RESEARCH_HORIZON" and any(value not in HORIZON_LATTICE for value in allowed):
        raise ValueError(f"{template_id}: horizon slot must use the lattice")
    return TemplateNumericSlot(
        name=name,
        kind=kind,
        default=raw.get("default"),
        allowed_values=tuple(allowed),
        economic_role=_text(raw.get("economic_role"), "economic_role"),
        token=token,
        occurrence=raw.get("occurrence", 0),
    )


def _operator_slot(raw, template_id):
    if not isinstance(raw, dict):
        raise ValueError(f"{template_id}: operator slot must be a table")
    values = {}
    for key in ("name", "role", "placeholder", "baseline_operator", "semantic_contract"):
        values[key] = _text(raw.get(key), f"operator slot {key}")
    if not values["placeholder"].startswith("{") or not values["placeholder"].endswith("}"):
        raise ValueError(f"{template_id}: operator placeholder must be explicit")
    allowed = raw.get("allowed_operators")
    if not isinstance(allowed, list) or not 2 <= len(allowed) <= 3:
        raise ValueError(f"{template_id}: allowed operators must contain 2-3 items")
    allowed = tuple(_text(item, "allowed operator") for item in allowed)
    if len(set(allowed)) != len(allowed) or any(
        not FASTEXPR_IDENTIFIER_RE.fullmatch(item) for item in allowed
    ):
        raise ValueError(f"{template_id}: invalid allowed operator identifier")
    if values["baseline_operator"] not in allowed:
        raise ValueError(f"{template_id}: baseline operator is not allowed")
    return TemplateOperatorSlot(allowed_operators=allowed, **values)


def _parse(document, *, strict_schema=False):
    if not isinstance(document, dict) or not isinstance(document.get("templates"), list):
        raise ValueError("catalog must contain [[templates]] entries")
    templates = []
    seen = set()
    for raw in document["templates"]:
        if not isinstance(raw, dict):
            raise ValueError("template entry must be a table")
        required = _REQUIRED
        if strict_schema:
            required = (*required, "role", "field_roles", "allowed_field_families",
                        "field_relationship", "direction_reason",
                        "allowed_horizon_profiles", "allowed_settings_arms",
                        "mechanism_tags", "novelty_family")
        missing = [key for key in required if key not in raw]
        if missing:
            raise ValueError(f"template missing required keys: {', '.join(missing)}")
        template_id = _text(raw["id"], "id")
        if template_id in seen:
            raise ValueError(f"duplicate template id: {template_id}")
        seen.add(template_id)
        kind = _text(raw["kind"], "kind")
        if kind not in _KINDS:
            raise ValueError(f"{template_id}: unknown kind {kind}")
        required_slots = raw["required_slots"]
        if (not isinstance(required_slots, list) or not required_slots
                or any(slot not in _SLOTS for slot in required_slots)
                or len(set(required_slots)) != len(required_slots)):
            raise ValueError(f"{template_id}: invalid required_slots")
        groups = raw["selection_groups"]
        if (not isinstance(groups, list) or not groups or
                any(group not in _GROUPS for group in groups)):
            raise ValueError(f"{template_id}: invalid selection_groups")
        direction = _text(raw["direction"], "direction")
        if direction not in _DIRECTIONS:
            raise ValueError(f"{template_id}: invalid direction {direction}")
        numeric_slots = tuple(_slot(item, template_id) for item in raw.get("numeric_slots", []))
        template_mode = str(raw.get("template_mode", "CONCRETE")).upper()
        if template_mode not in {"CONCRETE", "PARTIAL_OPERATOR"}:
            raise ValueError(f"{template_id}: invalid template_mode")
        raw_operator_slots = raw.get("operator_slots", raw.get("operator_slot", []))
        if isinstance(raw_operator_slots, dict):
            raw_operator_slots = [raw_operator_slots]
        operator_slots = tuple(
            _operator_slot(item, template_id) for item in raw_operator_slots
        )
        if template_mode == "CONCRETE" and operator_slots:
            raise ValueError(f"{template_id}: CONCRETE cannot declare operator slots")
        if template_mode == "PARTIAL_OPERATOR":
            if not raw.get("branch_of"):
                raise ValueError(f"{template_id}: PARTIAL_OPERATOR requires branch_of")
            if len(operator_slots) != 1:
                raise ValueError(f"{template_id}: PARTIAL_OPERATOR requires one operator slot")
        if len({slot.name for slot in numeric_slots}) != len(numeric_slots):
            raise ValueError(f"{template_id}: duplicate numeric slot name")
        horizon_profiles = tuple(
            tuple(int(value) for value in profile)
            for profile in raw.get("allowed_horizon_profiles", [])
        )
        lattice_index = {value: index for index, value in enumerate(HORIZON_LATTICE)}
        if any(
            any(value not in lattice_index for value in profile)
            or tuple(sorted(profile)) != profile
            or any(lattice_index[b] != lattice_index[a] + 1 for a, b in zip(profile, profile[1:]))
            for profile in horizon_profiles
        ) or len(set(horizon_profiles)) != len(horizon_profiles):
            raise ValueError(f"{template_id}: invalid horizon profile")
        templates.append(AlphaTemplate(
            template_id=template_id,
            version=_text(raw["version"], "version"),
            kind=kind,
            family=_text(raw["family"], "family"),
            expression=_text(raw["expression"], "expression"),
            required_slots=tuple(required_slots),
            stage_path=_text(raw["stage_path"], "stage_path"),
            economic_mechanism=_text(raw["economic_mechanism"], "economic_mechanism"),
            direction=direction,
            direction_transform=raw["direction_transform"],
            expected_horizon=_text(raw["expected_horizon"], "expected_horizon"),
            falsification=_text(raw["falsification"], "falsification"),
            self_correlation_impact=raw["self_correlation_impact"],
            tags=tuple(str(tag) for tag in raw.get("tags", [])),
            selection_groups=tuple(groups),
            selection_order=raw.get("selection_order", 1000),
            numeric_slots=numeric_slots,
            role=raw.get("role"),
            field_roles=tuple(raw.get("field_roles", [])),
            fixed_field_bindings=tuple(raw.get("fixed_field_bindings", [])),
            allowed_field_families=tuple(raw.get("allowed_field_families", [])),
            field_relationship=_text(raw.get("field_relationship", "synthetic example"), "field_relationship"),
            direction_reason=_text(raw.get("direction_reason", raw["economic_mechanism"]), "direction_reason"),
            allowed_horizon_profiles=horizon_profiles,
            allowed_settings_arms=tuple(raw.get("allowed_settings_arms", ["BASE"])),
            mechanism_tags=tuple(raw.get("mechanism_tags", raw.get("tags", []))),
            novelty_family=_text(raw.get("novelty_family", raw["family"]), "novelty_family"),
            template_mode=template_mode,
            branch_of=raw.get("branch_of"),
            operator_slots=operator_slots,
        ))
    result = tuple(templates)
    if strict_schema:
        for template in result:
            contract = validate_template_contract(template)
            if not contract["ok"]:
                raise ValueError(
                    f"{template.template_id}: invalid template contract: "
                    + ", ".join(contract["errors"])
                )
    for template in result:
        declared = {
            (str(slot.token or slot.default), int(slot.occurrence)): slot
            for slot in template.numeric_slots
        }
        seen = {}
        for match in NUMBER_TOKEN_RE.finditer(template.expression):
            token = match.group(1)
            occurrence = seen.get(token, 0)
            seen[token] = occurrence + 1
            slot = declared.pop((token, occurrence), None)
            if slot is None and token not in FIXED_NUMERICS:
                raise ValueError(
                    f"{template.template_id}: unclassified numeric literal {token}#{occurrence}"
                )
        if declared:
            missing = next(iter(declared.values()))
            raise ValueError(
                f"{template.template_id}: numeric slot {missing.name} not present in expression"
            )
    by_id = {template.template_id: template for template in result}
    for template in result:
        if template.template_mode != "PARTIAL_OPERATOR":
            continue
        parent = by_id.get(template.branch_of)
        if parent is None:
            raise ValueError(f"{template.template_id}: ABSTRACT_BRANCH_PARENT_MISSING")
        if parent.template_mode != "CONCRETE" or parent.role != "PROBE_ALPHA":
            raise ValueError(f"{template.template_id}: ABSTRACT_BRANCH_PARENT_INVALID")
        slot = template.operator_slots[0]
        if template.expression.count(slot.placeholder) != 1:
            raise ValueError(f"{template.template_id}: operator placeholder must occur once")
        try:
            bindings = {name: "{" + name + "}" for name in
                        ("p", "s", "t", "g", "data_field")}
            baseline = template.render(bindings, {slot.name: slot.baseline_operator})
            parent_expr = parent.render(bindings)
        except (KeyError, ValueError):
            baseline = parent_expr = None
        if baseline != parent_expr:
            raise ValueError(f"{template.template_id}: ABSTRACT_BRANCH_BASELINE_MISMATCH")
    return result


def load_templates(source):
    """Load templates from a binary/text stream or TOML path."""
    if hasattr(source, "read"):
        content = source.read()
        if isinstance(content, str):
            content = content.encode("utf-8")
    else:
        with open(source, "rb") as handle:
            content = handle.read()
    return _parse(tomllib.load(io.BytesIO(content)))


def load_builtin_templates():
    resource = resources.files("wqb_agent.alpha_templates").joinpath(
        "catalog", "builtin.toml"
    )
    with resource.open("rb") as handle:
        return _parse(tomllib.load(handle))


def resolve_private_catalog_path(explicit=None, *, environ=None, home=None):
    """Resolve only explicit absolute paths; never search the repository."""
    environ = os.environ if environ is None else environ
    candidate = explicit or environ.get("WQB_ALPHA_TEMPLATE_CATALOG")
    if candidate is None:
        base = Path.home() if home is None else Path(home)
        candidate = base / ".wqb_alpha_factory" / "private" / "alpha_templates.toml"
    path = Path(candidate).expanduser()
    if not path.is_absolute():
        raise ValueError("private template catalog path must be absolute")
    if not path.is_file():
        raise PrivateTemplateCatalogError(
            f"{PrivateTemplateCatalogError.code}: {path}"
        )
    return path


def load_private_templates(source=None, *, environ=None, home=None):
    """Load the local private catalog and fail closed when it is absent."""
    path = resolve_private_catalog_path(source, environ=environ, home=home)
    with path.open("rb") as handle:
        return _parse(tomllib.load(handle), strict_schema=True)
