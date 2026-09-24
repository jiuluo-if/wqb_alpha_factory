"""
ROLE: CORE
AGENT_RELEVANCE: HIGH

PURPOSE:
Provide the small, stable agent-facing interface for discovery, probe
generation, safe Simulation execution, remote Alpha evidence, dedupe, and
metadata tools. It does not implement a research state machine or persist
canonical research results.

READ WHEN:
- starting a research task
- choosing which runtime capability to call
- writing facade-level tests

DO NOT USE FOR:
- deciding economic hypotheses
- bypassing SimulationGateway safety or execution reconciliation
- treating cache or derived output as platform truth
"""

from __future__ import annotations

import json
import os
import tomllib
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from .alpha_factory import AlphaFactory
from .alpha_grouping import (
    find_remote_duplicates,
    find_remote_similar,
    group_remote_evidence,
)
from .alpha_semantics import derive_field_semantic_traits
from .alpha_templates import (
    AlphaTemplate,
    AlphaTemplateRegistry,
    TemplateNumericSlot,
    TemplateOperatorSlot,
    resolve_private_catalog_path,
    validate_template_contract,
)
from .artifacts import _atomic_replace
from .config import MAX_DATAFIELD_PAGES, AppConfig, normalize_config
from .expression import (
    canonical_expression,
    operator_occurrence_count,
    operator_occurrence_signature,
)
from .failures import reason_code_for_failure
from .operator_reference import (
    load_operator_syntax_reference,
    load_packaged_operator_syntax_reference,
)
from .protocol import endpoint_truth
from .remote_alpha_repository import RemoteAlphaRepository
from .remote_colors import preview_remote_colors, sync_remote_colors
from .remote_evidence import RemoteAlphaEvidenceProvider
from .remote_quota import SimulationQuota
from .simulation_gateway import (
    MULTI_DEFAULT_CHILD_BATCH_SIZE,
    MULTI_DEFAULT_CONCURRENCY,
    MULTI_MAX_CHILDREN,
    MULTI_MAX_CONCURRENCY,
    MULTI_MIN_CHILDREN,
    ExecutionGuard,
    SimulationGateway,
    SimulationSpec,
)

MAX_PROBE_FIELDS = 100
MAX_PROBE_TEMPLATES = 100
MAX_PROBE_COUNT = 100

# The research contract version is the compatibility handshake between the
# single research Skill and this runtime.  A Skill that declares a different
# ``compatible_research_contract`` is stale and must be re-read, not reused.
RESEARCH_CONTRACT_VERSION = "2026-09-24"


def _load_config(config: Mapping[str, Any] | str | None) -> dict[str, Any]:
    if config is None:
        path = "config.json"
        if not os.path.exists(path):
            path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config.example.json")
        with open(path, encoding="utf-8-sig") as handle:
            return json.load(handle)
    if isinstance(config, str):
        with open(config, encoding="utf-8-sig") as handle:
            return json.load(handle)
    return dict(config)


def _normalized_config(config=None) -> AppConfig:
    """Normalize every public facade config boundary exactly once."""
    if config is None:
        return normalize_config(_load_config(None))
    return normalize_config(config)


def _state_directory(config: AppConfig, state_dir=None) -> str:
    return state_dir or config.runtime.state_dir


def _client_scope(client):
    return {
        "instrumentType": getattr(client, "instrument_type", "EQUITY"),
        "region": getattr(client, "region", "USA"),
        "universe": getattr(client, "universe", "TOP3000"),
        "delay": getattr(client, "delay", 1),
    }


def list_datasets(*, client=None, config=None):
    """List live datasets for the client's instrument scope."""
    client = _remote_client(client=client)
    reader = getattr(client, "get_datasets", None)
    if not callable(reader):
        raise RuntimeError("LIVE_DATASET_CAPABILITY_REQUIRED")
    return {
        "source": "LIVE",
        "status": "AVAILABLE",
        "evidence_status": "AVAILABLE",
        "scope": _client_scope(client),
        "datasets": list(reader() or ()),
    }


def list_datafields(
    dataset_id, *, client=None, config=None, limit=None, offset=0,
    field_type=None,
):
    """Read one bounded live datafield page without an unbounded retry loop."""
    normalized_id = str(dataset_id or "").strip()
    if not normalized_id:
        raise ValueError("dataset_id must be non-empty")
    typed = _normalized_config(config)
    default_limit = typed.runtime.pagination_limit
    try:
        page_limit = default_limit if limit is None else int(limit)
        page_offset = int(offset)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("limit and offset must be integers") from exc
    if page_limit < 1 or page_limit > 50:
        raise ValueError("limit must be between 1 and 50")
    if page_offset < 0:
        raise ValueError("offset must be non-negative")
    client = _remote_client(client=client)
    reader = getattr(client, "get_datafields", None)
    if not callable(reader):
        raise RuntimeError("LIVE_DATAFIELD_CAPABILITY_REQUIRED")
    fields, count = reader(
        normalized_id,
        limit=page_limit,
        offset=page_offset,
        field_type=field_type,
    )
    return {
        "source": "LIVE",
        "scope": _client_scope(client),
        "dataset_id": normalized_id,
        "field_type": field_type,
        "limit": page_limit,
        "offset": page_offset,
        "count": count,
        "fields": list(fields or ()),
    }


def list_all_datafields(
    dataset_id, *, client=None, config=None, page_limit=None,
    field_type=None, max_pages=None,
):
    """Read every live datafield page within a bounded pagination budget."""
    typed = _normalized_config(config)
    try:
        page_cap = (
            typed.runtime.max_pagination_pages
            if max_pages is None else int(max_pages)
        )
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("max_pages must be an integer") from exc
    if isinstance(max_pages, bool) or not 1 <= page_cap <= MAX_DATAFIELD_PAGES:
        raise ValueError(
            f"max_pages must be between 1 and {MAX_DATAFIELD_PAGES}"
        )

    first = list_datafields(
        dataset_id,
        client=client,
        config=typed,
        limit=page_limit,
        offset=0,
        field_type=field_type,
    )
    fields = list(first["fields"])
    total = first["count"]
    pages = 1
    offset = len(fields)
    while offset < total:
        if pages >= page_cap:
            raise RuntimeError("LIVE_DATAFIELD_PAGINATION_INCOMPLETE")
        page = list_datafields(
            dataset_id,
            client=client,
            config=typed,
            limit=first["limit"],
            offset=offset,
            field_type=field_type,
        )
        chunk = list(page["fields"])
        if not chunk or page["count"] != total:
            raise RuntimeError("LIVE_DATAFIELD_PAGINATION_INCOMPLETE")
        fields.extend(chunk)
        offset += len(chunk)
        pages += 1
    return {
        **first,
        "pages": pages,
        "complete": len(fields) == total,
        "fields": fields,
    }


def generate_probes(*, template_ids, count, fields, dataset_id=None,
                    client=None, config=None):
    """Render only Agent-selected BRAIN fields and templates into specs."""
    if not isinstance(fields, (list, tuple)) or not fields:
        raise ValueError("fields must be a non-empty Agent-selected list")
    if len(fields) > MAX_PROBE_FIELDS:
        raise ValueError(f"fields cannot exceed {MAX_PROBE_FIELDS} entries")
    if isinstance(template_ids, str):
        template_ids = [template_ids]
    if not isinstance(template_ids, (list, tuple)) or not template_ids or any(
        not isinstance(item, str) or not item.strip() for item in template_ids
    ):
        raise ValueError("template_ids must be a non-empty explicit list")
    template_ids = [item.strip() for item in template_ids]
    if len(template_ids) > MAX_PROBE_TEMPLATES:
        raise ValueError(
            f"template_ids cannot exceed {MAX_PROBE_TEMPLATES} entries"
        )
    if len(set(template_ids)) != len(template_ids):
        raise ValueError("template_ids must not contain duplicates")
    try:
        target = int(count)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("count must be an explicit integer") from exc
    if isinstance(count, bool) or str(target) != str(count).strip():
        raise ValueError("count must be an explicit integer")
    if not 0 <= target <= MAX_PROBE_COUNT:
        raise ValueError(f"count must be between 0 and {MAX_PROBE_COUNT}")

    selected_fields = []
    for item in fields:
        if isinstance(item, Mapping):
            field = dict(item)
            field_id = field.get("id") or field.get("field_id")
            dataset = field.get("dataset_id") or field.get("dataset")
            if isinstance(dataset, Mapping):
                dataset = dataset.get("id") or dataset.get("name")
            dataset = dataset or dataset_id
        else:
            field_id = item
            dataset = dataset_id
            field = {"id": field_id}
        if field_id is None or dataset is None or not str(dataset).strip():
            raise ValueError("each selected field requires a BRAIN dataset id")
        field["id"] = str(field_id).strip()
        field["dataset"] = str(dataset).strip()
        selected_fields.append(field)
    if target == 0:
        return []
    if client is None:
        from .client import WQBClient
        client = WQBClient()
    typed = _normalized_config(config)
    factory = AlphaFactory(
        neutralization=typed.simulation_config.settings["neutralization"],
        catalog_path=typed.runtime.alpha_template_catalog,
        require_private=True,
    )
    reference = get_operator_reference(client=client, config=config)
    if callable(reference):
        reference = reference()
    return factory.generate_probe_specs(
        {"template_ids": template_ids}, selected_fields, reference,
        target=target, simulation_settings=typed.simulation_config.settings,
    )


def get_operator_syntax_reference(path=None) -> dict[str, Any]:
    """Return evergreen syntax hints, never current capability truth."""
    if path is None:
        return load_packaged_operator_syntax_reference()
    return load_operator_syntax_reference(path)


def get_operator_reference(*, client=None, config=None) -> dict[str, Any]:
    """Return a bounded current operator view from the existing BRAIN client."""
    if client is None:
        raise RuntimeError("LIVE_OPERATOR_CAPABILITY_REQUIRED")
    capability = None
    if capability is None:
        reader = getattr(client, "get_operator_capability", None)
        if not callable(reader):
            raise RuntimeError("LIVE_OPERATOR_CAPABILITY_REQUIRED")
        capability = reader()
    return {
        key: capability[key]
        for key in (
            "key", "status", "availability", "source", "valid", "errors",
            "operators", "capability_fingerprint", "endpoint",
        )
        if key in capability
    }


def get_capabilities(*, client=None, config=None) -> dict[str, Any]:
    """Return a bounded live platform capability view for the AI tools."""
    reference = get_operator_reference(client=client, config=config)
    return {
        "source": "BRAIN_LIVE_ONLY",
        "status": reference.get("status", "UNKNOWN"),
        "evidence_status": "AVAILABLE" if reference.get("operators") else "UNAVAILABLE",
        "operators": list(reference.get("operators") or []),
        "operator_capability": reference,
    }


def get_operators(*, client=None, config=None) -> list[str]:
    """Return only the live verified operator names."""
    return list(get_operator_reference(
        client=client, config=config
    ).get("operators") or [])


def list_templates(*, catalog_path=None, require_private=False):
    """List validated template metadata without constructing research state."""
    registry = (
        AlphaTemplateRegistry.from_private(catalog_path)
        if require_private else AlphaTemplateRegistry(private_catalog=catalog_path)
    )
    rows = registry.catalog()
    if require_private:
        for row in rows:
            row["source"] = "private_catalog"
    return rows


def inspect_template(template_id, *, catalog_path=None, require_private=False):
    registry = (
        AlphaTemplateRegistry.from_private(catalog_path)
        if require_private else AlphaTemplateRegistry(private_catalog=catalog_path)
    )
    template = registry.get(str(template_id))
    if template is None:
        raise KeyError(f"template not found: {template_id}")
    entry = template.catalog_entry()
    if require_private:
        entry["source"] = "private_catalog"
    return entry


def classify_fields(fields):
    """Return conservative, derived field type/dataset/semantic classifications."""
    rows = []
    for field in fields or ():
        if not isinstance(field, Mapping):
            continue
        traits = derive_field_semantic_traits(field)
        dataset = field.get("dataset") or field.get("dataset_id")
        if isinstance(dataset, Mapping):
            dataset = dataset.get("id") or dataset.get("name")
        rows.append({
            "id": str(field.get("id") or ""),
            "type": str(field.get("type") or "UNKNOWN").upper(),
            "dataset": str(dataset or "UNKNOWN"),
            "economic_meaning": traits.get("direction_meaning", "unknown"),
            "availability": (
                "AVAILABLE" if traits.get("metadata_semantics") == "AVAILABLE"
                else "UNKNOWN"
            ),
            "semantic_status": traits.get("status", "UNKNOWN"),
            "semantic_evidence": traits,
        })
    return {
        "source": "DERIVED_METADATA",
        "status": "CLASSIFIED",
        "evidence_status": "INCONCLUSIVE" if rows else "UNAVAILABLE",
        "fields": rows,
    }


def get_simulation_config(*, config=None):
    """Return normalized simulation defaults without performing a remote write."""
    typed = _normalized_config(config)
    return {
        "source": "CONFIG",
        "status": "CONFIGURED",
        "evidence_status": "NOT_APPLICABLE",
        "settings": dict(typed.simulation_config.settings),
        "runtime": {
            "max_concurrent_sims": typed.runtime.max_concurrent_sims,
            "poll_timeout_sec": typed.runtime.poll_timeout_sec,
        },
    }


def validate_simulation_settings(settings, *, client=None, config=None, capability=None):
    """Compatibility facade for the canonical Gateway-owned validator."""
    return SimulationGateway.validate_simulation_settings(
        settings, client=client, capability=capability
    )


def build_simulation_spec(expression, *, settings=None, fields=(), field_datasets=None,
                          note=None, template_id=None, proposal_id=None,
                          client=None, config=None, anchor_spec=None,
                          simulation_type="REGULAR"):
    """Build a validated, non-submitting SimulationSpec."""
    effective_fields = tuple(str(item) for item in (fields or ()) if str(item).strip())
    effective_field_datasets = dict(field_datasets or {})
    effective_template_id = template_id
    effective_simulation_type = str(simulation_type or "REGULAR").strip().upper()
    if anchor_spec is not None:
        if not isinstance(anchor_spec, SimulationSpec):
            anchor_spec = SimulationSpec(**dict(anchor_spec))
        SimulationGateway.validate_simulation_spec(anchor_spec)
        if effective_fields and effective_fields != anchor_spec.fields:
            raise ValueError("NEW_PROBE_REQUIRED: fields changed during optimization")
        effective_fields = anchor_spec.fields
        # A settings variant keeps the anchor's live field provenance so the
        # Gateway can still verify the very same fields before any POST.
        if not effective_field_datasets:
            effective_field_datasets = dict(anchor_spec.field_datasets)
        if template_id is not None and template_id != anchor_spec.template_id:
            raise ValueError("NEW_PROBE_REQUIRED: template changed during optimization")
        effective_template_id = anchor_spec.template_id
        if effective_simulation_type != anchor_spec.simulation_type:
            raise ValueError("NEW_PROBE_REQUIRED: simulation type changed during optimization")
        effective_simulation_type = anchor_spec.simulation_type
        if canonical_expression(expression) != canonical_expression(anchor_spec.expression):
            raise ValueError("NEW_PROBE_REQUIRED: settings variant changed expression")
    result = validate_simulation_settings(settings or {}, client=client, config=config)
    if not result["valid"]:
        raise ValueError("invalid simulation settings: " + "; ".join(result["errors"]))
    effective_settings = dict(result["settings"])
    effective_settings.pop("fields", None)
    spec = SimulationSpec(
        expression=expression, settings=effective_settings, fields=effective_fields,
        field_datasets=effective_field_datasets, note=note,
        template_id=effective_template_id, simulation_type=effective_simulation_type,
        proposal_id=proposal_id,
    )
    SimulationGateway.validate_simulation_spec(spec)
    return spec


def _optimization_operator_signatures(template):
    expressions = [template.expression]
    if template.template_mode == "PARTIAL_OPERATOR":
        slot = template.operator_slots[0]
        expressions = [template.expression.replace(slot.placeholder, operator)
                       for operator in slot.allowed_operators]
    if template.direction_transform == "reverse":
        expressions = [f"reverse({expression})" for expression in expressions]
    return {operator_occurrence_signature(expression) for expression in expressions}


def build_simulation_variant(base_spec, template, slot_name, value):
    """Return one bounded numeric variant without mutating or submitting."""
    if not isinstance(base_spec, SimulationSpec):
        base_spec = SimulationSpec(**dict(base_spec))
    SimulationGateway.validate_simulation_spec(base_spec)
    template = _coerce_template(template)
    if base_spec.template_id != template.template_id:
        raise ValueError("template_id does not match base SimulationSpec")
    bounds = {"CONTROL_ALPHA": (1, 3), "PROBE_ALPHA": (4, 6)}.get(template.role)
    anchor_count = operator_occurrence_count(base_spec.expression)
    if bounds is None or not bounds[0] <= anchor_count <= bounds[1]:
        raise ValueError("INVALID_OPTIMIZATION_ANCHOR")
    anchor_signature = operator_occurrence_signature(base_spec.expression)
    if anchor_signature not in _optimization_operator_signatures(template):
        raise ValueError("NEW_PROBE_REQUIRED: operator topology changed")
    slot = next((item for item in template.numeric_slots
                 if item.name == str(slot_name)), None)
    if slot is None:
        raise ValueError(f"undeclared numeric slot: {slot_name}")
    if value not in slot.allowed_values:
        raise ValueError(f"value is not allowed for numeric slot: {slot_name}")
    expression = slot.render(base_spec.expression, value)
    if expression == base_spec.expression:
        raise ValueError("numeric variant is a no-op")
    if operator_occurrence_count(expression) != anchor_count:
        raise ValueError("NEW_PROBE_REQUIRED: operator occurrence count changed")
    if operator_occurrence_signature(expression) != anchor_signature:
        raise ValueError("NEW_PROBE_REQUIRED: operator topology changed")
    result = SimulationSpec(
        expression=expression,
        settings=dict(base_spec.settings), fields=base_spec.fields,
        note=base_spec.note, template_id=base_spec.template_id,
        simulation_type=base_spec.simulation_type,
        field_datasets=dict(base_spec.field_datasets),
        proposal_id=base_spec.proposal_id,
    )
    SimulationGateway.validate_simulation_spec(result)
    return result


def validate_template(template):
    """Validate one template object without reading or writing a catalog."""
    try:
        template = _coerce_template(template)
    except (TypeError, ValueError) as exc:
        return {"ok": False, "errors": [str(exc)]}
    report = validate_template_contract(template)
    return {
        **report,
        "source": "DERIVED_TEMPLATE",
        "status": "VALID" if report["ok"] else "INVALID",
        "evidence_status": "INCONCLUSIVE",
    }


def _coerce_template(template):
    if isinstance(template, AlphaTemplate):
        return template
    if not isinstance(template, Mapping):
        raise TypeError("template must be AlphaTemplate or mapping")
    values = dict(template)
    values["template_id"] = values.pop("template_id", values.pop("id", None))
    numeric = []
    for item in values.pop("numeric_slots", ()) or ():
        numeric.append(item if isinstance(item, TemplateNumericSlot)
                      else TemplateNumericSlot(**dict(item)))
    operators = []
    for item in values.pop("operator_slots", ()) or ():
        operators.append(item if isinstance(item, TemplateOperatorSlot)
                         else TemplateOperatorSlot(**dict(item)))
    values["numeric_slots"] = tuple(numeric)
    values["operator_slots"] = tuple(operators)
    return AlphaTemplate(**values)


def _private_catalog_document(catalog_path):
    if catalog_path is None:
        raise ValueError("private catalog path must be explicit")
    path = resolve_private_catalog_path(catalog_path)
    with path.open("rb") as handle:
        return path, tomllib.load(handle)


def _template_raw(template):
    template = _coerce_template(template)
    raw = {
        "id": template.template_id, "version": template.version,
        "kind": template.kind, "family": template.family,
        "expression": template.expression, "required_slots": list(template.required_slots),
        "economic_mechanism": template.economic_mechanism, "direction": template.direction,
        "direction_transform": template.direction_transform,
        "expected_horizon": template.expected_horizon, "falsification": template.falsification,
        "self_correlation_impact": template.self_correlation_impact,
        "tags": list(template.tags), "selection_groups": list(template.selection_groups),
        "selection_order": template.selection_order, "role": template.role,
        "field_roles": list(template.field_roles), "fixed_field_bindings": list(template.fixed_field_bindings),
        "allowed_field_families": list(template.allowed_field_families),
        "field_relationship": template.field_relationship,
        "relationship_contract": template.relationship_contract,
        "semantic_contract": template.semantic_contract,
        "direction_reason": template.direction_reason,
        "allowed_horizon_profiles": [list(item) for item in template.allowed_horizon_profiles],
        "allowed_settings_arms": list(template.allowed_settings_arms),
        "mechanism_tags": list(template.mechanism_tags), "novelty_family": template.novelty_family,
        "template_mode": template.template_mode,
    }
    raw["numeric_slots"] = [{
        "name": slot.name, "kind": slot.kind, "default": slot.default,
        "allowed_values": list(slot.allowed_values), "economic_role": slot.economic_role,
        "token": slot.token, "occurrence": slot.occurrence,
    } for slot in template.numeric_slots]
    raw["operator_slots"] = [{
        "name": slot.name, "role": slot.role, "placeholder": slot.placeholder,
        "baseline_operator": slot.baseline_operator,
        "allowed_operators": list(slot.allowed_operators), "semantic_contract": slot.semantic_contract,
    } for slot in template.operator_slots]
    return raw


def _toml_value(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _render_private_catalog(raw_templates):
    lines = []
    for raw in raw_templates:
        lines.append("[[templates]]")
        nested = {"numeric_slots", "operator_slots"}
        for key, value in raw.items():
            if key not in nested:
                lines.append(f"{key} = {_toml_value(value)}")
        for key in ("numeric_slots", "operator_slots"):
            for item in raw.get(key, ()):
                lines.append("")
                lines.append(f"[[templates.{key}]]")
                for item_key, item_value in item.items():
                    lines.append(f"{item_key} = {_toml_value(item_value)}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _write_private_catalog(path, document):
    raw_templates = list(document.get("templates") or [])
    _atomic_replace(str(path), _render_private_catalog(raw_templates).encode("utf-8"),
                    private=True)


def create_template(template, *, catalog_path=None):
    path, document = _private_catalog_document(catalog_path)
    candidate = _template_raw(template)
    if str(candidate.get("semantic_contract", "UNDECLARED")).upper() == "SYNTHETIC_FIXTURE":
        raise ValueError("SYNTHETIC_SEMANTIC_CONTRACT_PRIVATE")
    registry = AlphaTemplateRegistry.from_private(path)
    if registry.get(candidate["id"]) is not None:
        raise ValueError(f"duplicate template_id: {candidate['id']}")
    candidate_template = _coerce_template(candidate)
    report = validate_template(candidate_template)
    if not report.get("ok"):
        raise ValueError("invalid template: " + ", ".join(report["errors"]))
    document["templates"].append(candidate)
    _write_private_catalog(path, document)
    return inspect_template(candidate["id"], catalog_path=path, require_private=True)


def update_template(template_id, template, *, catalog_path=None):
    path, document = _private_catalog_document(catalog_path)
    candidate = _template_raw(template)
    if str(template_id) != candidate["id"]:
        raise ValueError("template_id must match template.id")
    if str(candidate.get("semantic_contract", "UNDECLARED")).upper() == "SYNTHETIC_FIXTURE":
        raise ValueError("SYNTHETIC_SEMANTIC_CONTRACT_PRIVATE")
    report = validate_template(candidate)
    if not report.get("ok"):
        raise ValueError("invalid template: " + ", ".join(report["errors"]))
    rows = document.get("templates") or []
    for index, row in enumerate(rows):
        if str(row.get("id")) == str(template_id):
            rows[index] = candidate
            _write_private_catalog(path, document)
            return inspect_template(template_id, catalog_path=path, require_private=True)
    raise KeyError(f"template not found: {template_id}")


def delete_template(template_id, *, catalog_path=None):
    path, document = _private_catalog_document(catalog_path)
    rows = document.get("templates") or []
    kept = [row for row in rows if str(row.get("id")) != str(template_id)]
    if len(kept) == len(rows):
        raise KeyError(f"template not found: {template_id}")
    document["templates"] = kept
    _write_private_catalog(path, document)
    return {"template_id": str(template_id), "status": "DELETED", "source": "PRIVATE_CATALOG"}


def _simulation_gateway(*, client=None, config=None, state_dir=None):
    """Build the Remote-First gateway without constructing the research Agent."""
    if client is None:
        from .client import WQBClient
        client = WQBClient()
    typed = _normalized_config(config)
    directory = _state_directory(typed, state_dir)
    runtime = typed.runtime
    max_concurrent = runtime.max_concurrent_sims
    poll_timeout = runtime.poll_timeout_sec
    return SimulationGateway(
        client, state_dir=directory, max_concurrent=max_concurrent,
        poll_timeout_sec=poll_timeout,
    )


def validate_simulation_spec(spec, *, client=None, config=None,
                             state_dir=None):
    """Validate only executable request shape and return bounded facts."""
    gateway = _simulation_gateway(
        client=client, config=config, state_dir=state_dir
    )
    result = gateway.validate_simulation_spec(spec)
    return {
        **result, "source": "SimulationGateway", "status": "VALID",
        "evidence_status": "INCONCLUSIVE",
    }


def execution_fingerprint(spec, *, client=None, config=None,
                          state_dir=None):
    return _simulation_gateway(
        client=client, config=config, state_dir=state_dir
    ).execution_fingerprint(spec)


def simulate(spec, *, client=None, config=None, state_dir=None):
    """Start one real Simulation through the only public write gateway."""
    return _simulation_gateway(
        client=client, config=config, state_dir=state_dir
    ).simulate(spec)


def simulate_single(spec, *, client=None, config=None, state_dir=None):
    """Explicitly named single-request facade over the canonical writer."""
    return simulate(spec, client=client, config=config, state_dir=state_dir)


def simulate_batch(specs, *, client=None, config=None, state_dir=None):
    """Execute Single Simulations through the ten-worker window by default."""
    gateway = _simulation_gateway(
        client=client, config=config, state_dir=state_dir
    )
    return gateway.simulate_batch(specs)


def simulate_multi_batch(
    specs, *, client=None, config=None, state_dir=None,
    child_batch_size=MULTI_DEFAULT_CHILD_BATCH_SIZE,
    max_concurrent_multi=MULTI_DEFAULT_CONCURRENCY,
):
    """Execute probe windows as Multi-Simulation parents.

    Each parent contains two to ten children. The safe default dispatches
    two parent jobs concurrently; the supported hard maximum is eight.
    The Gateway remains the only write path.
    """
    gateway = _simulation_gateway(
        client=client, config=config, state_dir=state_dir
    )
    return gateway.simulate_multi_batch(
        specs,
        child_batch_size=child_batch_size,
        max_concurrent_multi=max_concurrent_multi,
    )


def _capability_status(capability):
    if not isinstance(capability, Mapping):
        return "UNKNOWN"
    return str(
        capability.get("capability_status")
        or capability.get("status")
        or "UNKNOWN"
    ).upper()


def _mode_unavailable_reason(
    authentication, *, options_available, regular_available, multi=False,
    permissions=(),
):
    if not bool((authentication or {}).get("authenticated")):
        reason = (authentication or {}).get("reason")
        if isinstance(reason, str) and reason.strip():
            return reason.strip()[:100]
        return "AUTHENTICATION_REQUIRED"
    if not options_available or not regular_available:
        return "CAPABILITY_UNKNOWN"
    if multi and "MULTI_SIMULATION" not in permissions:
        return "PERMISSION_UNAVAILABLE"
    return None


def _simulation_modes_from_capabilities(authentication, simulation_capability):
    permissions = {
        str(item).upper() for item in (authentication or {}).get("permissions", ())
    }
    source = "BRAIN_LIVE" if authentication is not None else "UNKNOWN"
    options_available = _capability_status(simulation_capability) == "AVAILABLE"
    choices = {
        str(item).upper()
        for item in simulation_capability.get("simulation_type_choices", ())
    } if options_available else set()
    regular_child_available = "REGULAR" in choices
    single_reason = _mode_unavailable_reason(
        authentication,
        options_available=options_available,
        regular_available=regular_child_available,
    )
    multi_reason = _mode_unavailable_reason(
        authentication,
        options_available=options_available,
        regular_available=regular_child_available,
        multi=True,
        permissions=permissions,
    )
    single_available = single_reason is None
    multi_available = multi_reason is None
    multi = {
        "name": "Multi-Simulation", "available": multi_available,
        "status": "AVAILABLE" if multi_available else "UNAVAILABLE",
        "source": source, "evidence_status": "INCONCLUSIVE",
        "children_per_job": MULTI_DEFAULT_CHILD_BATCH_SIZE,
        "min_children_per_job": MULTI_MIN_CHILDREN,
        "max_children_per_job": MULTI_MAX_CHILDREN,
        "default_concurrent_jobs": MULTI_DEFAULT_CONCURRENCY,
        "max_concurrent_jobs": MULTI_MAX_CONCURRENCY,
    }
    if multi_reason is not None:
        multi["reason"] = multi_reason
    single = {
        "name": "Single Simulation", "available": single_available,
        "status": "AVAILABLE" if single_available else "UNAVAILABLE",
        "source": source, "evidence_status": "INCONCLUSIVE",
        "max_concurrent": 10,
    }
    if single_reason is not None:
        single["reason"] = single_reason
    region_agnostic = {
        "name": "Region-Agnostic Simulation",
        "available": False,
        "status": "UNAVAILABLE",
        "source": source, "evidence_status": "INCONCLUSIVE",
        "simulation_type": "REGION_AGNOSTIC",
        "platform_advertised": "REGION_AGNOSTIC" in choices,
        "writer_supported": False,
        "reason": "WRITER_UNSUPPORTED",
    }
    return {
        "single": single,
        "multi": multi,
        "region_agnostic": region_agnostic,
    }


def get_simulation_modes(*, client=None, config=None) -> dict[str, dict[str, Any]]:
    """Describe modes from live account permission and OPTIONS capability."""
    if client is None:
        return _simulation_modes_from_capabilities(None, None)
    authentication = client.get_authentication_status()
    capability = client.get_simulation_capability()
    return _simulation_modes_from_capabilities(authentication, capability)


def get_live_preflight(*, client=None, config=None, state_dir=None):
    """Compose bounded read-only account/platform readiness facts."""
    client = _remote_client(client=client)
    authentication = client.get_authentication_status()
    capability = client.get_simulation_capability()
    modes = _simulation_modes_from_capabilities(authentication, capability)
    typed = _normalized_config(config)
    directory = _state_directory(typed, state_dir)
    pending = get_pending_executions(state_dir=directory)["entries"]
    try:
        quota = simulation_quota(client=client, config=typed, state_dir=directory)
    except Exception as exc:
        quota = {"status": "UNKNOWN", "reason": type(exc).__name__}
    auth_output = dict(authentication)
    if auth_output.get("user_id") is not None:
        value = str(auth_output["user_id"])
        auth_output["user_id"] = value if len(value) <= 8 else value[:2] + "***" + value[-2:]
    recordsets = endpoint_truth("recordsets")
    return {
        "network_write": False,
        "authentication": auth_output,
        "simulation_options": capability,
        "simulation_modes": modes,
        "multi_child_range": "2..10",
        "scope": _client_scope(client),
        "recordset_api": {
            "status": recordsets.status.value if recordsets else "UNKNOWN",
            "availability": "NOT_PROBED", "network_write": False,
        },
        "remote_quota": quota,
        "pending_execution_count": len(pending),
    }


def research_status(*, client=None, config=None, state_dir=None):
    """Aggregate the read-only facts an Agent checks before starting research.

    This is the single startup-readiness call: live capability, simulation
    modes, quota with freshness, pending executions, cache freshness and the
    research contract version.  Every section keeps its own source/status, and
    a missing live client degrades those sections to UNKNOWN instead of failing
    the whole call.  Python reports facts only; it never chooses the next
    experiment.
    """
    typed = _normalized_config(config)
    directory = _state_directory(typed, state_dir)
    pending = get_pending_executions(state_dir=directory)["entries"]
    cache = remote_cache_status(config=typed, state_dir=directory)
    try:
        quota = simulation_quota(client=client, config=typed, state_dir=directory)
    except Exception as exc:
        quota = {
            "status": "UNKNOWN", "source": "UNAVAILABLE",
            "reason_code": "CAPABILITY_UNAVAILABLE", "error": type(exc).__name__,
        }
    modes: dict[str, Any]
    if client is None:
        capability = {
            "source": "UNAVAILABLE", "status": "UNKNOWN",
            "evidence_status": "INCONCLUSIVE",
        }
        # Without a live client the mode report is derived from nothing, so it
        # must not keep claiming a BRAIN_LIVE source.
        modes = {
            key: {
                **value, "source": "UNAVAILABLE",
                "evidence_status": "UNAVAILABLE",
            }
            for key, value in _simulation_modes_from_capabilities(None, None).items()
        }
    else:
        try:
            capability = get_capabilities(client=client, config=typed)
        except Exception as exc:
            capability = {
                "source": "UNAVAILABLE", "status": "UNKNOWN",
                "evidence_status": "INCONCLUSIVE", "error": type(exc).__name__,
            }
        try:
            modes = get_simulation_modes(client=client, config=typed)
        except Exception as exc:
            modes = {"error": type(exc).__name__, "status": "UNKNOWN"}
    live = client is not None
    # A Multi child row is not an independent unresolved write: its parent row
    # already represents that remote POST and its Simulation count.
    pending_writes = [
        row for row in pending
        if str(row.get("kind") or "").upper() != "MULTI_CHILD"
    ]
    return {
        "source": "LIVE" if live else "LOCAL_ONLY",
        "status": "AVAILABLE" if live else "PARTIAL",
        "evidence_status": "AVAILABLE" if live else "INCONCLUSIVE",
        "research_contract_version": RESEARCH_CONTRACT_VERSION,
        "capability": capability,
        "simulation_modes": modes,
        "quota": quota,
        "pending_executions": pending,
        "pending_execution_count": len(pending_writes),
        "pending_simulation_count": sum(
            int(row.get("simulation_count") or 1) for row in pending_writes
        ),
        "pending_multi_child_count": len(pending) - len(pending_writes),
        "cache": cache,
    }


def get_pending_executions(*, state_dir=None, config=None):
    """Read unresolved local guard entries from the configured state directory."""
    typed = _normalized_config(config)
    directory = _state_directory(typed, state_dir)
    return {"entries": ExecutionGuard(directory, reconcile=False).entries()}


def resume_execution(fingerprint, *, client=None, config=None,
                     state_dir=None):
    return _simulation_gateway(
        client=client, config=config, state_dir=state_dir
    ).resume_execution(fingerprint)


def reconcile_execution(fingerprint, *, client=None, config=None,
                        state_dir=None):
    """Read-only recovery of one guarded execution; never submits again."""
    return resume_execution(
        fingerprint, client=client, config=config,
        state_dir=state_dir,
    )


def _remote_client(*, client=None):
    if client is not None:
        return client
    from .client import WQBClient
    return WQBClient()


def _evidence_provider(*, client=None):
    return RemoteAlphaEvidenceProvider(_remote_client(client=client))


def get_alpha(alpha_id, *, client=None, config=None):
    return _evidence_provider(client=client).get_alpha(alpha_id)


def get_activity_diversity(*, client=None, config=None, user_id=None,
                           region=None, delay=None, data_category=None):
    """Return live account activity distribution for AI coverage diagnosis only."""
    client = _remote_client(client=client)
    if user_id is None:
        user_id = client.get_authentication_status().get("user_id")
    return {
        "source": "BRAIN_LIVE",
        "status": "AVAILABLE",
        "network_write": False,
        "user_id": user_id,
        "diversity": client.get_activity_diversity(
            user_id, region=region, delay=delay, data_category=data_category,
        ),
    }


def get_alpha_summary(alpha_id, *, client=None, config=None):
    """Read only the cheap BRAIN Alpha detail used for broad first-pass screening."""
    return RemoteAlphaEvidenceProvider(
        _remote_client(client=client)
    ).get_alpha_summary(str(alpha_id).strip())


def get_alpha_evidence(alpha_id, *, client=None, config=None,
                       live=True, recordsets=(), depth="summary"):
    if not live:
        raise ValueError("LIVE_EVIDENCE_REQUIRED")
    return _evidence_provider(client=client).get_alpha_evidence(
        str(alpha_id).strip(), live=True, recordsets=recordsets, depth=depth
    )


def get_alpha_metrics(alpha_id, *, client=None, config=None):
    return _evidence_provider(client=client).get_alpha_metrics(str(alpha_id).strip())


def get_alpha_aggregates(alpha_id, *, client=None, config=None):
    return _evidence_provider(client=client).get_alpha_aggregates(alpha_id)


def get_alpha_pnl(alpha_id, *, client=None, config=None):
    return _evidence_provider(client=client).get_alpha_pnl(alpha_id)


def get_alpha_self_correlation(alpha_id, *, client=None, config=None):
    return _evidence_provider(client=client).get_alpha_self_correlation(alpha_id)


def get_alpha_prod_correlation(alpha_id, *, client=None, config=None):
    """Read PROD correlation only when the Agent requests a finalist check."""
    return RemoteAlphaEvidenceProvider(
        _remote_client(client=client)
    ).get_alpha_prod_correlation(str(alpha_id).strip())


def get_alpha_recordsets(alpha_id, names, *, client=None, config=None):
    """Read only explicitly selected, currently discoverable Alpha recordsets."""
    return _evidence_provider(client=client).get_alpha_recordsets(
        str(alpha_id).strip(), names,
    )


def compare_alphas(alpha_ids, *, client=None, config=None, depth="summary",
                   max_concurrent=4):
    ids = [str(item).strip() for item in (alpha_ids or ()) if str(item).strip()]
    try:
        concurrency = int(max_concurrent)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("max_concurrent must be an integer") from exc
    if isinstance(max_concurrent, bool) or not 1 <= concurrency <= 4:
        raise ValueError("max_concurrent must be between 1 and 4")
    if not ids:
        return {"source": "LIVE", "status": "AVAILABLE",
                "evidence_status": "AVAILABLE", "depth": str(depth).upper(),
                "alphas": []}
    provider = _evidence_provider(client=client)
    with ThreadPoolExecutor(max_workers=min(concurrency, len(ids))) as pool:
        alphas = list(pool.map(
            lambda item: provider.get_alpha_evidence(item, depth=depth), ids
        ))
    return {"source": "LIVE", "status": "AVAILABLE",
            "evidence_status": "AVAILABLE", "depth": str(depth).upper(),
            "alphas": alphas}


def _remote_repository(*, client=None, config=None, state_dir=None,
                       require_client=True):
    if require_client or client is not None:
        client = _remote_client(client=client)
    typed = _normalized_config(config)
    retention = typed.remote_cache.retention_days
    directory = _state_directory(typed, state_dir)
    cache_path = os.path.join(directory, ".alpha_feed_cache", "remote.json")
    return RemoteAlphaRepository(
        client.get_all_user_alphas if client is not None else None,
        cache_path=cache_path, retention_days=retention, evidence_client=client,
    )


def refresh_remote_alphas(*, client=None, config=None, state_dir=None,
    limit=100, days=None):
    if days is not None:
        typed = _normalized_config(config)
        if int(days) != typed.remote_cache.retention_days:
            raise ValueError("days must equal the configured retention window")
        if int(days) < 1 or int(days) > 90:
            raise ValueError("days must be within 1-90")
    return _remote_repository(
        client=client, config=config, state_dir=state_dir,
        require_client=True,
    ).refresh_remote_alphas(limit=limit)


def list_remote_alphas(*, client=None, config=None, state_dir=None,
                       days=None, status=None):
    return _remote_repository(
        client=client, config=config, state_dir=state_dir,
        require_client=False,
    ).list_remote_alphas(days=days, status=status)


def remote_cache_status(*, client=None, config=None, state_dir=None):
    return _remote_repository(
        client=client, config=config, state_dir=state_dir,
        require_client=False,
    ).cache_status()


def purge_remote_cache(*, client=None, config=None, state_dir=None):
    return {"removed": _remote_repository(
        client=client, config=config, state_dir=state_dir,
        require_client=False,
    ).purge_remote_cache()}


def simulation_quota(*, client=None, config=None, state_dir=None,
                     refresh_if_stale=True):
    """Return a source/freshness-labeled quota projection.

    BRAIN's own rate-limit headers stay the fact source; the rebuildable remote
    metadata feed is refreshed once when missing or stale. If that bounded
    GET-only refresh fails, remaining values stay UNKNOWN instead of showing a
    full budget.
    """
    typed = _normalized_config(config)
    directory = _state_directory(typed, state_dir)
    repository = _remote_repository(
        client=client, config=typed, state_dir=directory,
        require_client=False,
    )
    freshness = repository.cache_status().get("freshness", "UNKNOWN")
    refresh_error = None
    if refresh_if_stale and str(freshness).upper() != "FRESH" and client is not None:
        try:
            repository = _remote_repository(
                client=client, config=typed, state_dir=directory,
                require_client=True,
            )
            repository.refresh_remote_alphas()
        except Exception as exc:
            refresh_error = exc
    observation_reader = getattr(client, "get_simulation_quota_observation", None)
    observation = observation_reader() if callable(observation_reader) else None
    snapshot = SimulationQuota(
        repository, ExecutionGuard(directory, reconcile=False),
        daily_cap=typed.quota.daily,
        official_observation=observation,
    ).snapshot()
    if refresh_error is not None and snapshot.get("status") != "LIVE":
        snapshot.update({
            "status": "UNKNOWN",
            "today_remaining": None,
            "reason_code": reason_code_for_failure("UNKNOWN", refresh_error),
            "error": f"bounded live quota refresh failed ({type(refresh_error).__name__})",
        })
    return snapshot


def get_remote_alpha(alpha_id, *, live=False, client=None,
                     config=None, state_dir=None):
    """Read one Alpha from the rebuildable cache or from BRAIN explicitly."""
    return _remote_repository(
        client=client, config=config, state_dir=state_dir,
        require_client=live,
    ).get_remote_alpha(alpha_id, live=live)


def get_remote_alpha_evidence(alpha_id, *, live=True, client=None,
                              config=None, state_dir=None, depth="full"):
    """Return source-labeled evidence at explicit summary/full depth."""
    return _remote_repository(
        client=client, config=config, state_dir=state_dir,
        require_client=True,
    ).get_remote_alpha_evidence(alpha_id, live=live, depth=depth)


def group_alphas(alpha_ids=None, *, rows=None, client=None,
                 config=None, state_dir=None, days=None):
    if rows is None:
        repository = _remote_repository(
            client=client, config=config, state_dir=state_dir
        )
        ids = alpha_ids or [
            item["alpha_id"] for item in repository.list_remote_alphas(days=days)
        ]
        rows = [repository.get_remote_alpha_evidence(item, depth="summary") for item in ids]
    return group_remote_evidence(rows)


def find_duplicate_alphas(alpha_id, *, rows=None, client=None,
                          config=None, state_dir=None):
    if rows is None:
        rows = []
        repository = _remote_repository(
            client=client, config=config, state_dir=state_dir
        )
        for item in repository.list_remote_alphas():
            rows.append(repository.get_remote_alpha_evidence(
                item["alpha_id"], depth="summary"
            ))
    return find_remote_duplicates(rows, alpha_id)


def find_similar_alphas(expression_or_alpha_id, *, rows=None,
                        client=None, config=None, state_dir=None, days=None):
    """Return advisory remote exact/strict/family matches; never blocks a POST."""
    if rows is None:
        repository = _remote_repository(
            client=client, config=config, state_dir=state_dir
        )
        rows = [repository.get_remote_alpha_evidence(item["alpha_id"], depth="summary")
                for item in repository.list_remote_alphas(days=days)]
    return find_remote_similar(rows, expression_or_alpha_id)


def preview_alpha_colors(alpha_ids=None, *, rows=None, assignments=None,
                         client=None, config=None, state_dir=None, days=None,
                         overwrite=False):
    if rows is None:
        repository = _remote_repository(
            client=client, config=config, state_dir=state_dir
        )
        ids = alpha_ids or [
            item["alpha_id"] for item in repository.list_remote_alphas(days=days)
        ]
        rows = [repository.get_remote_alpha_evidence(item) for item in ids]
    return preview_remote_colors(rows, assignments=assignments, overwrite=overwrite)


def sync_alpha_colors(plan=None, *, exact_plan=None, client=None, config=None,
                      state_dir=None, overwrite=False, dry_run=False):
    """Apply a previously reviewed color preview plan only."""
    if plan is not None and exact_plan is not None:
        raise TypeError("provide only one of plan or exact_plan")
    plan = exact_plan if exact_plan is not None else plan
    if plan is None:
        raise ValueError("EXACT_COLOR_PLAN_REQUIRED")
    repository = _remote_repository(
        client=client, config=config, state_dir=state_dir
    )
    return sync_remote_colors(
        plan, get_alpha=repository.evidence.get_alpha,
        set_alpha_color=repository.evidence.client.set_alpha_color,
        overwrite=overwrite, dry_run=dry_run,
    )


def research_tool_manifest(profile="core"):
    """Return a deterministic default CORE surface or the opt-in full catalog."""
    rows: list[dict[str, Any]] = [
        {"name": "research_status", "mode": "READ_ONLY", "owner": "research_api"},
        {"name": "get_capabilities", "mode": "READ_ONLY", "owner": "BRAIN"},
        {"name": "get_operators", "mode": "READ_ONLY", "owner": "BRAIN"},
        {"name": "get_operator_reference", "mode": "READ_ONLY", "owner": "BRAIN"},
        {"name": "get_operator_syntax_reference", "mode": "READ_ONLY", "owner": "operator_reference"},
        {"name": "list_datasets", "mode": "READ_ONLY", "owner": "BRAIN"},
        {"name": "list_datafields", "mode": "READ_ONLY", "owner": "BRAIN"},
        {"name": "list_all_datafields", "mode": "READ_ONLY", "owner": "BRAIN"},
        {"name": "list_templates", "mode": "READ_ONLY", "owner": "AlphaFactory"},
        {"name": "inspect_template", "mode": "READ_ONLY", "owner": "AlphaFactory"},
        {"name": "create_template", "mode": "PRIVATE_CATALOG_WRITE", "owner": "AlphaFactory"},
        {"name": "update_template", "mode": "PRIVATE_CATALOG_WRITE", "owner": "AlphaFactory"},
        {"name": "delete_template", "mode": "PRIVATE_CATALOG_WRITE", "owner": "AlphaFactory"},
        {"name": "validate_template", "mode": "PURE", "owner": "AlphaFactory"},
        {"name": "classify_fields", "mode": "PURE", "owner": "field_metadata"},
        {"name": "get_simulation_config", "mode": "READ_ONLY", "owner": "config"},
        {"name": "validate_simulation_settings", "mode": "READ_ONLY", "owner": "SimulationGateway"},
        {"name": "build_simulation_spec", "mode": "READ_ONLY", "owner": "SimulationGateway"},
        {"name": "build_simulation_variant", "mode": "PURE", "owner": "SimulationGateway"},
        {"name": "generate_probes", "mode": "READ_ONLY", "owner": "AlphaFactory"},
        {"name": "validate_simulation_spec", "mode": "READ_ONLY", "owner": "SimulationGateway"},
        {"name": "execution_fingerprint", "mode": "PURE", "owner": "SimulationGateway"},
        {"name": "simulate", "mode": "SIMULATION_WRITE", "remote_write": True, "owner": "SimulationGateway"},
        {"name": "simulate_single", "mode": "SIMULATION_WRITE", "remote_write": True, "owner": "SimulationGateway"},
        {"name": "simulate_batch", "mode": "SIMULATION_WRITE", "remote_write": True, "owner": "SimulationGateway"},
        {"name": "simulate_multi_batch", "mode": "SIMULATION_WRITE", "remote_write": True, "owner": "SimulationGateway"},
        {"name": "get_simulation_modes", "mode": "READ_ONLY", "owner": "SimulationGateway"},
        {"name": "get_live_preflight", "mode": "READ_ONLY", "owner": "BRAIN"},
        {"name": "get_pending_executions", "mode": "READ_ONLY", "owner": "ExecutionGuard"},
        {"name": "resume_execution", "mode": "READ_ONLY", "remote_write": False, "owner": "ExecutionGuard"},
        {"name": "reconcile_execution", "mode": "READ_ONLY", "remote_write": False, "owner": "ExecutionGuard"},
        {"name": "simulation_quota", "mode": "READ_ONLY", "owner": "RemoteAlphaRepository"},
        {"name": "get_alpha", "mode": "READ_ONLY", "owner": "BRAIN"},
        {"name": "get_alpha_summary", "mode": "READ_ONLY", "owner": "BRAIN"},
        {"name": "get_alpha_evidence", "mode": "READ_ONLY", "owner": "BRAIN"},
        {"name": "get_alpha_metrics", "mode": "READ_ONLY", "owner": "BRAIN"},
        {"name": "get_alpha_aggregates", "mode": "READ_ONLY", "owner": "BRAIN"},
        {"name": "get_alpha_pnl", "mode": "READ_ONLY", "owner": "BRAIN"},
        {"name": "get_alpha_self_correlation", "mode": "READ_ONLY", "owner": "BRAIN"},
        {"name": "get_alpha_prod_correlation", "mode": "READ_ONLY", "owner": "BRAIN"},
        {"name": "get_alpha_recordsets", "mode": "READ_ONLY", "owner": "BRAIN"},
        {"name": "get_activity_diversity", "mode": "READ_ONLY", "owner": "BRAIN"},
        {"name": "compare_alphas", "mode": "READ_ONLY", "owner": "BRAIN"},
        {"name": "refresh_remote_alphas", "mode": "LOCAL_CACHE_WRITE", "local_write": True, "owner": "RemoteAlphaRepository"},
        {"name": "list_remote_alphas", "mode": "READ_ONLY", "owner": "RemoteAlphaRepository"},
        {"name": "get_remote_alpha", "mode": "READ_ONLY", "owner": "RemoteAlphaRepository"},
        {"name": "get_remote_alpha_evidence", "mode": "READ_ONLY", "owner": "RemoteAlphaRepository"},
        {"name": "remote_cache_status", "mode": "READ_ONLY", "owner": "RemoteAlphaRepository"},
        {"name": "purge_remote_cache", "mode": "LOCAL_CACHE_WRITE", "local_write": True, "owner": "RemoteAlphaRepository"},
        {"name": "find_duplicate_alphas", "mode": "READ_ONLY", "owner": "RemoteAlphaRepository"},
        {"name": "find_similar_alphas", "mode": "READ_ONLY", "owner": "RemoteAlphaRepository"},
        {"name": "group_alphas", "mode": "PURE", "owner": "RemoteAlphaRepository"},
        {"name": "preview_alpha_colors", "mode": "PURE", "owner": "RemoteAlphaRepository"},
        {"name": "sync_alpha_colors", "mode": "REMOTE_METADATA_WRITE", "owner": "BRAIN"},
        {"name": "alpha_submission", "mode": "MANUAL_ONLY", "owner": "user"},
    ]
    if profile == "full":
        return rows
    if profile != "core":
        raise ValueError("profile must be 'core' or 'full'")
    core_names = {
        "research_status",
        "list_datasets", "list_datafields",
        "build_simulation_spec", "validate_simulation_spec",
        "simulate", "simulate_batch", "simulate_multi_batch",
        "get_alpha_summary", "get_alpha_evidence",
        "find_duplicate_alphas", "resume_execution",
    }
    return [row for row in rows if row["name"] in core_names]


__all__ = [
    "SimulationSpec", "list_datasets", "list_datafields", "list_all_datafields",
    "generate_probes",
    "get_capabilities", "get_operators", "get_operator_reference", "get_operator_syntax_reference",
    "list_templates", "inspect_template",
    "create_template", "update_template", "delete_template", "validate_template",
    "classify_fields", "get_simulation_config", "validate_simulation_settings",
    "build_simulation_spec", "build_simulation_variant",
    "validate_simulation_spec", "execution_fingerprint",
    "simulate", "simulate_single", "simulate_batch",
    "simulate_multi_batch", "get_simulation_modes", "get_live_preflight",
    "get_pending_executions", "resume_execution",
    "reconcile_execution", "research_status",
    "get_alpha", "get_alpha_summary", "get_alpha_evidence", "get_alpha_metrics",
    "get_alpha_aggregates", "get_alpha_pnl", "get_alpha_self_correlation",
    "get_alpha_prod_correlation",
    "get_alpha_recordsets",
    "compare_alphas", "refresh_remote_alphas", "list_remote_alphas",
    "get_activity_diversity",
    "get_remote_alpha", "get_remote_alpha_evidence", "remote_cache_status",
    "purge_remote_cache", "simulation_quota", "group_alphas",
    "find_duplicate_alphas", "find_similar_alphas",
    "preview_alpha_colors", "sync_alpha_colors",
    "research_tool_manifest",
]
