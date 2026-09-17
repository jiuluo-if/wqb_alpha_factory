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
from .config import AppConfig, normalize_config
from .discovery import FieldDiscovery
from .operator_reference import (
    load_operator_syntax_reference,
    load_packaged_operator_syntax_reference,
)
from .remote_alpha_repository import RemoteAlphaRepository
from .remote_colors import preview_remote_colors, sync_remote_colors
from .remote_evidence import RemoteAlphaEvidenceProvider
from .remote_quota import SimulationQuota
from .simulation_gateway import ExecutionGuard, SimulationGateway, SimulationSpec


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


def _remote_research_components(*, client, config=None, state_dir=None,
                                include_factory=False):
    """Build only rebuildable components for public discovery/probe tools."""
    typed = _normalized_config(config)
    runtime = typed.runtime
    directory = _state_directory(typed, state_dir)
    selection = runtime.field_selection
    discovery = FieldDiscovery(
        client,
        pagination_limit=runtime.pagination_limit,
        max_pages=runtime.max_pagination_pages,
        cache_path=os.path.join(directory, "fields_cache.json"),
        cache_ttl_sec=runtime.fields_cache_ttl_sec,
        catalog_root=directory,
        max_alpha_count=runtime.max_field_alpha_count,
        selection_mode=selection["mode"],
        random_fraction=selection["random_fraction"],
        random_seed=selection["random_seed"],
        platform_usage_refresh=selection["platform_usage_refresh"],
        require_platform_alpha_count=selection["require_platform_alpha_count"],
        dataset_sampling=selection["dataset_sampling"],
        min_datasets=selection["min_datasets"],
        dataset_pool=selection["dataset_pool"],
        persist_catalog=selection["persist_catalog"],
    )
    factory = None
    if include_factory:
        factory = AlphaFactory(
            neutralization=typed.simulation_config.settings["neutralization"],
            catalog_path=runtime.alpha_template_catalog,
            require_private=True,
        )
    return typed, discovery, factory


def discover_fields(query, *, client=None, config=None, state_dir=None, limit=None):
    """Discover fields through the existing BRAIN-backed discovery component."""
    if client is None:
        from .client import WQBClient
        client = WQBClient()
    _typed, discovery, _factory = _remote_research_components(
        client=client, config=config, state_dir=state_dir
    )
    default_limit = _typed.runtime.fields_per_discovery
    if isinstance(query, str):
        hypothesis = {"id": "agent-query", "statement": query, "tags": query.split(), "datasets": []}
    elif isinstance(query, Mapping):
        hypothesis = dict(query)
    else:
        raise TypeError("query must be a string or object")
    fields = discovery.discover(
        hypothesis,
        target_count=limit or default_limit,
    )
    return {
        "source": "BRAIN_LIVE_ONLY",
        "status": "AVAILABLE",
        "evidence_status": "AVAILABLE" if fields else "UNAVAILABLE",
        "fields": fields,
        "field_source": discovery.source_provenance(),
        "query": hypothesis,
    }


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
    field_type=None, max_pages=100,
):
    """Read every live datafield page within a bounded pagination budget."""
    try:
        page_cap = int(max_pages)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("max_pages must be an integer") from exc
    if page_cap < 1:
        raise ValueError("max_pages must be positive")

    first = list_datafields(
        dataset_id,
        client=client,
        config=config,
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
            config=config,
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


def generate_probes(query=None, *, template_ids=None, count=None,
                    fields=None, client=None, config=None, state_dir=None):
    """Generate reviewable ``SimulationSpec`` probes without an inbox write."""
    if client is None:
        from .client import WQBClient
        client = WQBClient()
    _typed, discovery, factory = _remote_research_components(
        client=client, config=config, state_dir=state_dir,
        include_factory=True,
    )
    reference = get_operator_reference(client=client, config=config)
    try:
        target = (_typed.factory.default_probe_count if count is None else int(count))
    except (TypeError, ValueError) as exc:
        raise ValueError("count 必须是整数") from exc
    if target < 0:
        raise ValueError("count 必须是非负整数")
    if query is None:
        hypothesis = {"id": "agent-query", "statement": "", "tags": [], "datasets": []}
    elif isinstance(query, str):
        hypothesis = {
            "id": "agent-query", "statement": query,
            "tags": query.split(), "datasets": [],
        }
    elif isinstance(query, Mapping):
        hypothesis = dict(query)
    else:
        raise TypeError("query must be a string, object, or None")
    requested_templates = list(template_ids or [])
    hypothesis["template_ids"] = requested_templates
    fields = fields if fields is not None else discovery.discover(
        hypothesis, target_count=_typed.runtime.fields_per_discovery
    )
    if callable(reference):
        reference = reference()
    return factory.generate_probe_specs(
        hypothesis, fields, reference, target=target,
        simulation_settings=_typed.simulation_config.settings,
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


def validate_simulation_settings(settings, *, client=None, config=None):
    """Validate bounded Simulation settings before Gateway construction."""
    errors = []
    if not isinstance(settings, Mapping):
        return {
            "valid": False, "status": "INVALID", "source": "LOCAL_SCHEMA",
            "evidence_status": "UNAVAILABLE", "settings": {},
            "errors": ["settings must be an object"],
        }
    normalized = dict(settings)
    for key in ("region", "universe", "instrumentType", "neutralization"):
        if key in normalized and (not isinstance(normalized[key], str) or not normalized[key].strip()):
            errors.append(f"{key} must be a non-empty string")
    for key in ("delay", "decay"):
        if key not in normalized:
            continue
        value = normalized[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < (0 if key == "delay" else 1):
            errors.append(f"{key} must be a valid non-negative integer")
        elif key == "delay" and value not in {0, 1}:
            errors.append("delay must be 0 or 1")
        elif key == "decay" and value > 252:
            errors.append("decay must be between 1 and 252")
    fields = normalized.get("fields")
    if fields is not None and (
        not isinstance(fields, (list, tuple))
        or not fields
        or any(not isinstance(item, (str, int)) or not str(item).strip() for item in fields)
    ):
        errors.append("fields must be a non-empty list when provided")
    if client is not None:
        for key, attr in (("region", "region"), ("universe", "universe"), ("instrumentType", "instrument_type")):
            expected = getattr(client, attr, None)
            if key in normalized and expected is not None and str(normalized[key]) != str(expected):
                errors.append(f"{key} does not match client scope")
    return {
        "valid": not errors, "status": "VALID" if not errors else "INVALID",
        "source": "LOCAL_SCHEMA", "evidence_status": "INCONCLUSIVE",
        "settings": normalized, "errors": errors,
    }


def build_simulation_spec(expression, *, settings=None, fields=(), note=None, template_id=None,
                          client=None, config=None):
    """Build a validated, non-submitting SimulationSpec."""
    result = validate_simulation_settings(settings or {}, client=client, config=config)
    if not result["valid"]:
        raise ValueError("invalid simulation settings: " + "; ".join(result["errors"]))
    effective_settings = dict(result["settings"])
    effective_settings.pop("fields", None)
    return SimulationSpec(
        expression=expression, settings=effective_settings, fields=tuple(fields or ()),
        note=note, template_id=template_id,
    )


def suggest_next_specs(evidence, objective, allowed_changes):
    """Return reviewable candidates only; this function never calls Simulation."""
    changes = allowed_changes if isinstance(allowed_changes, Mapping) else {}
    candidates = []
    for key, values in changes.items():
        if not isinstance(values, (list, tuple)):
            values = [values]
        for value in values:
            candidates.append({
                "change": {str(key): value},
                "reason": f"candidate change for objective: {str(objective or '').strip()}",
            })
    return {
        "source": "AI_PROPOSAL", "status": "CANDIDATES_ONLY",
        "evidence_status": "AVAILABLE" if evidence else "UNAVAILABLE",
        "objective": objective, "candidates": candidates, "simulated": False,
    }


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
    """Explicit small-optimization alias for one Single Simulation."""
    return simulate(spec, client=client, config=config, state_dir=state_dir)


def simulate_batch(specs, *, client=None, config=None, state_dir=None):
    """Execute Single Simulations through the ten-worker window by default."""
    gateway = _simulation_gateway(
        client=client, config=config, state_dir=state_dir
    )
    return gateway.simulate_batch(specs)


def simulate_single_batch(specs, *, client=None, config=None, state_dir=None):
    """Explicit small-optimization batch alias for Single Simulation."""
    return simulate_batch(
        specs, client=client, config=config, state_dir=state_dir
    )


def simulate_multi_batch(
    specs, *, client=None, config=None, state_dir=None,
    child_batch_size=10, max_concurrent_multi=8,
):
    """Execute probe windows as Multi-Simulation parents.

    Each parent contains at most ten children and at most eight parent jobs
    are dispatched concurrently.  The Gateway remains the only write path.
    """
    gateway = _simulation_gateway(
        client=client, config=config, state_dir=state_dir
    )
    return gateway.simulate_multi_batch(
        specs,
        child_batch_size=child_batch_size,
        max_concurrent_multi=max_concurrent_multi,
    )


def get_simulation_modes() -> dict[str, dict[str, Any]]:
    """Describe the three UI modes without implying unverified write access."""
    return {
        "single": {
            "name": "Single Simulation",
            "available": True,
            "status": "AVAILABLE",
            "evidence_status": "INCONCLUSIVE",
            "max_concurrent": 10,
        },
        "multi": {
            "name": "Multi-Simulation",
            "available": True,
            "status": "AVAILABLE",
            "evidence_status": "INCONCLUSIVE",
            "children_per_job": 10,
            "max_concurrent_jobs": 8,
        },
        "region_agnostic": {
            "name": "Region-Agnostic Simulation",
            "available": False,
            "status": "UNAVAILABLE",
            "evidence_status": "UNAVAILABLE",
            "reason": "NO_VERIFIED_WRITE_CONTRACT",
        },
    }


def get_pending_executions(*, state_dir=".wqb_state"):
    return {"entries": ExecutionGuard(state_dir, reconcile=False).entries()}


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


def get_alpha(alpha_id, *, client=None, config=None):
    return _remote_client(client=client).get_alpha(str(alpha_id).strip())


def get_alpha_evidence(alpha_id, *, client=None, config=None,
                       live=True):
    if not live:
        raise ValueError("LIVE_EVIDENCE_REQUIRED")
    import time as _time
    snapshot = RemoteAlphaEvidenceProvider(
        _remote_client(client=client)
    ).collect(str(alpha_id).strip())
    return {
        "alpha_id": snapshot.alpha_id, "source": "LIVE",
        "fetched_at": _time.time(), "age_sec": 0.0,
        "alpha": dict(snapshot.alpha_detail),
        "aggregates": snapshot.aggregates, "pnl": snapshot.pnl,
        "self_correlation": snapshot.self_correlation,
        "status": dict(snapshot.status), "availability": dict(snapshot.availability),
    }


def get_alpha_metrics(alpha_id, *, client=None, config=None):
    return get_alpha_evidence(alpha_id, client=client,
                              config=config)["alpha"].get("is", {})


def get_alpha_aggregates(alpha_id, *, client=None, config=None):
    return get_alpha_evidence(alpha_id, client=client,
                              config=config)["aggregates"]


def get_alpha_pnl(alpha_id, *, client=None, config=None):
    return get_alpha_evidence(alpha_id, client=client,
                              config=config)["pnl"]


def get_alpha_self_correlation(alpha_id, *, client=None, config=None):
    return get_alpha_evidence(alpha_id, client=client,
                              config=config)["self_correlation"]


def compare_alphas(alpha_ids, *, client=None, config=None):
    return {"source": "LIVE", "status": "AVAILABLE", "evidence_status": "AVAILABLE",
            "alphas": [
        get_alpha_evidence(item, client=client, config=config)
        for item in (alpha_ids or ())
    ]}


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


def simulation_quota(*, client=None, config=None, state_dir=None):
    """Return a read-only quota projection from remote usage and active guards."""
    repository = _remote_repository(
        client=client, config=config, state_dir=state_dir,
        require_client=False,
    )
    quota = _normalized_config(config).quota
    typed = _normalized_config(config)
    return SimulationQuota(
        repository, ExecutionGuard(_state_directory(typed, state_dir), reconcile=False),
        daily_cap=quota.daily,
        rolling_cap=quota.rolling_limit,
    ).snapshot()


def get_remote_alpha(alpha_id, *, live=False, client=None,
                     config=None, state_dir=None):
    """Read one Alpha from the rebuildable cache or from BRAIN explicitly."""
    return _remote_repository(
        client=client, config=config, state_dir=state_dir,
        require_client=live,
    ).get_remote_alpha(alpha_id, live=live)


def get_remote_alpha_evidence(alpha_id, *, live=True, client=None,
                              config=None, state_dir=None):
    """Return remote evidence; live reads are the default and source-labeled."""
    return _remote_repository(
        client=client, config=config, state_dir=state_dir,
        require_client=True,
    ).get_remote_alpha_evidence(alpha_id, live=live)


def group_alphas(alpha_ids=None, *, rows=None, client=None,
                 config=None, state_dir=None, days=None):
    if rows is None:
        repository = _remote_repository(
            client=client, config=config, state_dir=state_dir
        )
        ids = alpha_ids or [
            item["alpha_id"] for item in repository.list_remote_alphas(days=days)
        ]
        rows = [repository.get_remote_alpha_evidence(item) for item in ids]
    return group_remote_evidence(rows)


def find_alpha_duplicates(alpha_id, *, rows=None, client=None,
                          config=None, state_dir=None):
    if rows is None:
        rows = []
        repository = _remote_repository(
            client=client, config=config, state_dir=state_dir
        )
        for item in repository.list_remote_alphas():
            rows.append(repository.get_remote_alpha_evidence(item["alpha_id"]))
    return find_remote_duplicates(rows, alpha_id)


def find_duplicate_alphas(alpha_id, *, rows=None, client=None,
                          config=None, state_dir=None):
    """Public name for exact execution duplicate lookup."""
    return find_alpha_duplicates(
        alpha_id, rows=rows, client=client,
        config=config, state_dir=state_dir,
    )


def find_similar_alphas(expression_or_alpha_id, *, rows=None,
                        client=None, config=None, state_dir=None, days=None):
    """Return advisory remote exact/structural matches; never blocks a POST."""
    if rows is None:
        repository = _remote_repository(
            client=client, config=config, state_dir=state_dir
        )
        rows = [repository.get_remote_alpha_evidence(item["alpha_id"])
                for item in repository.list_remote_alphas(days=days)]
    return find_remote_similar(rows, expression_or_alpha_id)


def preview_alpha_colors(alpha_ids=None, *, rows=None, client=None,
                         config=None, state_dir=None, days=None):
    if rows is None:
        repository = _remote_repository(
            client=client, config=config, state_dir=state_dir
        )
        ids = alpha_ids or [
            item["alpha_id"] for item in repository.list_remote_alphas(days=days)
        ]
        rows = [repository.get_remote_alpha_evidence(item) for item in ids]
    return preview_remote_colors(rows)


def sync_alpha_colors(alpha_ids=None, *, rows=None, client=None,
                      config=None, state_dir=None, days=None, overwrite=False,
                      dry_run=False):
    repository = _remote_repository(
        client=client, config=config, state_dir=state_dir
    )
    if rows is None:
        ids = alpha_ids or [
            item["alpha_id"] for item in repository.list_remote_alphas(days=days)
        ]
        rows = [repository.get_remote_alpha_evidence(item) for item in ids]
    return sync_remote_colors(
        rows, get_alpha=repository.evidence.get_alpha,
        set_alpha_color=repository.evidence.client.set_alpha_color,
        overwrite=overwrite, dry_run=dry_run,
    )


def research_tool_manifest():
    return [
        {"name": "get_capabilities", "mode": "READ_ONLY", "owner": "BRAIN"},
        {"name": "list_datasets", "mode": "READ_ONLY", "owner": "BRAIN"},
        {"name": "list_datafields", "mode": "READ_ONLY", "owner": "BRAIN"},
        {"name": "list_all_datafields", "mode": "READ_ONLY", "owner": "BRAIN"},
        {"name": "discover_fields", "mode": "READ_ONLY", "owner": "BRAIN"},
        {"name": "get_operators", "mode": "READ_ONLY", "owner": "BRAIN"},
        {"name": "list_templates", "mode": "READ_ONLY", "owner": "AlphaFactory"},
        {"name": "inspect_template", "mode": "READ_ONLY", "owner": "AlphaFactory"},
        {"name": "create_template", "mode": "PRIVATE_CATALOG_WRITE", "owner": "AlphaFactory"},
        {"name": "update_template", "mode": "PRIVATE_CATALOG_WRITE", "owner": "AlphaFactory"},
        {"name": "delete_template", "mode": "PRIVATE_CATALOG_WRITE", "owner": "AlphaFactory"},
        {"name": "validate_template", "mode": "PURE", "owner": "AlphaFactory"},
        {"name": "classify_fields", "mode": "PURE", "owner": "field_metadata"},
        {"name": "get_simulation_config", "mode": "READ_ONLY", "owner": "config"},
        {"name": "validate_simulation_settings", "mode": "PURE", "owner": "SimulationGateway"},
        {"name": "build_simulation_spec", "mode": "PURE", "owner": "SimulationGateway"},
        {"name": "suggest_next_specs", "mode": "PURE", "owner": "AI"},
        {"name": "generate_probes", "mode": "PURE", "owner": "AlphaFactory"},
        {"name": "validate_simulation_spec", "mode": "READ_ONLY", "owner": "SimulationGateway"},
        {"name": "simulate", "mode": "SIMULATION_WRITE", "remote_write": True, "owner": "SimulationGateway"},
        {"name": "simulate_batch", "mode": "SIMULATION_WRITE", "remote_write": True, "owner": "SimulationGateway"},
        {"name": "simulate_single", "mode": "SIMULATION_WRITE", "remote_write": True, "owner": "SimulationGateway"},
        {"name": "simulate_single_batch", "mode": "SIMULATION_WRITE", "remote_write": True, "owner": "SimulationGateway"},
        {"name": "simulate_multi_batch", "mode": "SIMULATION_WRITE", "remote_write": True, "owner": "SimulationGateway"},
        {"name": "get_simulation_modes", "mode": "READ_ONLY", "owner": "SimulationGateway"},
        {"name": "resume_execution", "mode": "READ_ONLY", "remote_write": False, "owner": "ExecutionGuard"},
        {"name": "reconcile_execution", "mode": "READ_ONLY", "remote_write": False, "owner": "ExecutionGuard"},
        {"name": "get_alpha_evidence", "mode": "READ_ONLY", "owner": "BRAIN"},
        {"name": "compare_alphas", "mode": "READ_ONLY", "owner": "BRAIN"},
        {"name": "refresh_remote_alphas", "mode": "READ_ONLY", "owner": "RemoteAlphaRepository"},
        {"name": "list_remote_alphas", "mode": "READ_ONLY", "owner": "RemoteAlphaRepository"},
        {"name": "find_duplicate_alphas", "mode": "READ_ONLY", "owner": "RemoteAlphaRepository"},
        {"name": "find_similar_alphas", "mode": "READ_ONLY", "owner": "RemoteAlphaRepository"},
        {"name": "group_alphas", "mode": "PURE", "owner": "RemoteAlphaRepository"},
        {"name": "preview_alpha_colors", "mode": "PURE", "owner": "RemoteAlphaRepository"},
        {"name": "sync_alpha_colors", "mode": "REMOTE_METADATA_WRITE", "owner": "BRAIN"},
        {"name": "alpha_submission", "mode": "MANUAL_ONLY", "owner": "user"},
    ]


__all__ = [
    "SimulationSpec", "list_datasets", "list_datafields", "list_all_datafields",
    "discover_fields",
    "generate_probes",
    "get_capabilities", "get_operators", "get_operator_reference", "get_operator_syntax_reference",
    "list_templates", "inspect_template",
    "create_template", "update_template", "delete_template", "validate_template",
    "classify_fields", "get_simulation_config", "validate_simulation_settings",
    "build_simulation_spec", "suggest_next_specs",
    "validate_simulation_spec", "execution_fingerprint",
    "simulate", "simulate_single", "simulate_batch", "simulate_single_batch",
    "simulate_multi_batch", "get_simulation_modes",
    "get_pending_executions", "resume_execution",
    "reconcile_execution",
    "get_alpha", "get_alpha_evidence", "get_alpha_metrics",
    "get_alpha_aggregates", "get_alpha_pnl", "get_alpha_self_correlation",
    "compare_alphas", "refresh_remote_alphas", "list_remote_alphas",
    "get_remote_alpha", "get_remote_alpha_evidence", "remote_cache_status",
    "purge_remote_cache", "simulation_quota", "group_alphas",
    "find_alpha_duplicates", "find_duplicate_alphas", "find_similar_alphas",
    "preview_alpha_colors", "sync_alpha_colors",
    "research_tool_manifest",
]
