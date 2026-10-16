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
import math
import os
import re
import tomllib
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from datetime import date, datetime
from typing import Any

from .alpha_factory import AlphaFactory
from .alpha_grouping import (
    find_remote_duplicates,
    find_remote_similar,
    group_remote_evidence,
)
from .alpha_quality import build_calibration as _build_aqaf_calibration
from .alpha_quality import evaluate_quality
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
from .local_templates import (
    MAX_LOCAL_COUNT,
    MAX_LOCAL_FIELDS,
    MAX_LOCAL_TEMPLATES,
    normalize_local_request,
)
from .operator_reference import (
    load_operator_syntax_reference,
    load_packaged_operator_syntax_reference,
)
from .osmosis_allocation import (
    compare_osmosis_allocation_plans as _compare_osmosis_allocation_plans,
)
from .osmosis_allocation import (
    preview_osmosis_allocation as _preview_osmosis_allocation,
)
from .protocol import endpoint_truth
from .remote_alpha_repository import RemoteAlphaRepository
from .remote_colors import preview_remote_colors, sync_remote_colors
from .remote_evidence import RemoteAlphaEvidenceProvider
from .remote_quota import SimulationQuota
from .simulation_gateway import (
    MULTI_DEFAULT_CHILD_BATCH_SIZE,
    MULTI_DEFAULT_CONCURRENCY,
    MULTI_MAX_CANDIDATES_PER_CALL,
    MULTI_MAX_CHILDREN,
    MULTI_MAX_CONCURRENCY,
    MULTI_MIN_CHILDREN,
    SUPPORTED_WRITE_SIMULATION_TYPES,
    ExecutionGuard,
    SimulationGateway,
    SimulationSpec,
    classify_write_readiness,
)

# Local template reading/construction owns these bounds; the facade keeps the
# historical names so existing callers and tests keep working.
MAX_PROBE_FIELDS = MAX_LOCAL_FIELDS
MAX_PROBE_TEMPLATES = MAX_LOCAL_TEMPLATES
MAX_PROBE_COUNT = MAX_LOCAL_COUNT
# The research contract version is the compatibility handshake between the
# single research Skill and this runtime.  A Skill that declares a different
# ``metadata.wqb_alpha_factory_research_contract`` is stale and must be re-read, not reused.
RESEARCH_CONTRACT_VERSION = "2026-09-29"
RESEARCH_STATUS_TIMEOUT_SEC = 15.0

# Region-Agnostic simulations are written through the same gateway as REGULAR ones;
# whether the writer accepts them is a property of the gateway's supported set, not
# of this module, so it is derived rather than hard-coded.
_REGION_AGNOSTIC_WRITE_SUPPORTED = (
    "REGION_AGNOSTIC" in SUPPORTED_WRITE_SIMULATION_TYPES
)


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


_DATA_DISCOVERY_REGIONS = ("USA", "EUR", "ASI", "GLB")
# Explicit REGULAR discovery may target a country pool; ALL remains the four
# Region-Agnostic child catalogs above.
SUPPORTED_DATA_REGIONS = frozenset((*_DATA_DISCOVERY_REGIONS, "GBR"))


def _project_regional_metadata(regional_rows, *, identity_keys):
    first_region = next(iter(regional_rows))
    result = dict(regional_rows[first_region])
    result["available_regions"] = list(regional_rows)
    keys = set().union(*(row.keys() for row in regional_rows.values()))
    conflicts = {}
    for key in keys - set(identity_keys) - {"available_regions", "region_metadata_conflicts"}:
        values = [row.get(key) for row in regional_rows.values()]
        if (any(key not in row for row in regional_rows.values())
                or any(value != values[0] for value in values[1:])):
            result[key] = None
            conflicts[key] = {
                region: {"present": key in row, "value": row.get(key)}
                for region, row in regional_rows.items()
            }
    if conflicts:
        result["region_metadata_conflicts"] = conflicts
    return result


def _client_scope(client, region=None):
    scope = {
        "instrumentType": getattr(client, "instrument_type", "EQUITY"),
        "region": str(region or getattr(client, "region", "USA")).upper(),
        "universe": getattr(client, "universe", "TOP3000"),
        "delay": getattr(client, "delay", 1),
    }
    return scope


def list_datasets(*, client=None, config=None, region="ALL"):
    """List live datasets; ALL unions RA children, or one supported regular region."""
    normalized_region = str(region or "ALL").strip().upper()
    if normalized_region != "ALL" and normalized_region not in SUPPORTED_DATA_REGIONS:
        raise ValueError("region must be ALL or a supported data region")
    client = _remote_client(client=client)
    reader = getattr(client, "get_datasets", None)
    if not callable(reader):
        raise RuntimeError("LIVE_DATASET_CAPABILITY_REQUIRED")
    regions = _DATA_DISCOVERY_REGIONS if normalized_region == "ALL" else (normalized_region,)
    regional_rows_by_id: dict[str, dict[str, dict[str, Any]]] = {}
    for selected_region in regions:
        rows = reader(scope=_client_scope(client, selected_region))
        if not isinstance(rows, (list, tuple)):
            raise RuntimeError("LIVE_DATASET_RESPONSE_INVALID")
        seen_ids = set()
        for row in rows:
            if not isinstance(row, Mapping):
                raise RuntimeError("LIVE_DATASET_RESPONSE_INVALID")
            raw_id = row.get("id")
            dataset_id = row.get("dataset_id")
            if raw_id is not None and dataset_id is not None and str(raw_id).casefold() != str(dataset_id).casefold():
                raise RuntimeError("LIVE_DATASET_RESPONSE_INVALID: conflicting dataset IDs")
            identity = str(raw_id or dataset_id or "").strip()
            if not identity:
                raise RuntimeError("LIVE_DATASET_RESPONSE_INVALID: dataset ID missing")
            if identity.casefold() in seen_ids:
                raise RuntimeError("LIVE_DATASET_RESPONSE_INVALID: duplicate dataset ID")
            seen_ids.add(identity.casefold())
            regional_rows_by_id.setdefault(identity, {})[selected_region] = dict(row)
    return {
        "source": "LIVE",
        "status": "AVAILABLE",
        "evidence_status": "AVAILABLE",
        "scope": _client_scope(client, normalized_region),
        "regions_searched": list(regions),
        "datasets": [
            _project_regional_metadata(
                regional_rows, identity_keys={"id", "dataset_id"},
            )
            for regional_rows in regional_rows_by_id.values()
        ],
    }


def list_datafields(
    dataset_id, *, client=None, config=None, limit=None, offset=0,
    field_type=None, region="ALL",
):
    """Read one bounded live datafield page without an unbounded retry loop.

    ALL divides the requested page across USA/EUR/ASI/GLB. Its offset must be
    a page boundary; count reports region-field occurrences, not unique IDs.
    """
    normalized_id = str(dataset_id or "").strip()
    if not normalized_id:
        raise ValueError("dataset_id must be non-empty")
    typed = _normalized_config(config)
    default_limit = typed.runtime.pagination_limit
    if (isinstance(offset, bool) or not isinstance(offset, int)
            or (limit is not None
                and (isinstance(limit, bool) or not isinstance(limit, int)))):
        raise ValueError("limit and offset must be integers")
    page_limit = default_limit if limit is None else limit
    page_offset = offset
    if page_limit < 1 or page_limit > 50:
        raise ValueError("limit must be between 1 and 50")
    if page_offset < 0:
        raise ValueError("offset must be non-negative")
    normalized_region = str(region or "ALL").strip().upper()
    if normalized_region != "ALL" and normalized_region not in SUPPORTED_DATA_REGIONS:
        raise ValueError("region must be ALL or a supported data region")
    regions = _DATA_DISCOVERY_REGIONS if normalized_region == "ALL" else (normalized_region,)
    if normalized_region == "ALL" and page_limit < len(regions):
        raise ValueError("ALL region search limit must be at least 4")
    if normalized_region == "ALL" and page_offset % page_limit:
        raise ValueError("ALL region search offset must be a multiple of limit")
    client = _remote_client(client=client)
    reader = getattr(client, "get_datafields", None)
    if not callable(reader):
        raise RuntimeError("LIVE_DATAFIELD_CAPABILITY_REQUIRED")
    regional_rows_by_id: dict[str, dict[str, dict[str, Any]]] = {}
    region_count = 0
    regional_fields: dict[str, list[str]] = {}
    region_limit = (
        page_limit if normalized_region != "ALL"
        else page_limit // len(regions)
    )
    region_offset = (
        page_offset if normalized_region != "ALL"
        else (page_offset // page_limit) * region_limit
    )
    for selected_region in regions:
        fields, count = reader(
            normalized_id,
            limit=region_limit,
            offset=region_offset,
            field_type=field_type,
            scope=_client_scope(client, selected_region),
        )
        if (not isinstance(fields, (list, tuple))
                or isinstance(count, bool) or not isinstance(count, int) or count < 0):
            raise RuntimeError("LIVE_DATAFIELD_RESPONSE_INVALID")
        region_count += count
        regional_fields[selected_region] = []
        seen_region_ids = set()
        for field in fields or ():
            if not isinstance(field, Mapping):
                raise RuntimeError("LIVE_DATAFIELD_RESPONSE_INVALID")
            raw_id = field.get("id")
            field_id = field.get("field_id")
            if raw_id is not None and field_id is not None and str(raw_id).casefold() != str(field_id).casefold():
                raise RuntimeError("LIVE_DATAFIELD_RESPONSE_INVALID: conflicting field IDs")
            identity = str(raw_id or field_id or "").strip()
            if not identity:
                raise RuntimeError("LIVE_DATAFIELD_RESPONSE_INVALID: field ID missing")
            folded_identity = identity.casefold()
            if folded_identity in seen_region_ids:
                raise RuntimeError("LIVE_DATAFIELD_RESPONSE_INVALID: duplicate field ID in page")
            seen_region_ids.add(folded_identity)
            regional_fields[selected_region].append(identity)
            regional_rows_by_id.setdefault(identity, {})[selected_region] = dict(field)
    if normalized_region == "ALL":
        ordered_ids = []
        seen_ids = set()
        max_region_rows = max((len(values) for values in regional_fields.values()), default=0)
        for index in range(max_region_rows):
            for selected_region in regions:
                ids = regional_fields[selected_region]
                if index < len(ids) and ids[index] not in seen_ids:
                    seen_ids.add(ids[index])
                    ordered_ids.append(ids[index])
        fields = [
            _project_regional_metadata(
                regional_rows_by_id[identity],
                identity_keys={"id", "field_id"},
            )
            for identity in ordered_ids[:page_limit]
        ]
    else:
        fields = [dict(field) for field in fields]
    return {
        "source": "LIVE",
        "scope": _client_scope(client, normalized_region),
        "regions_searched": list(regions),
        "dataset_id": normalized_id,
        "field_type": field_type,
        "limit": page_limit,
        "offset": page_offset,
        "count": region_count,
        "count_exact": normalized_region != "ALL",
        "count_semantics": (
            "region_field_occurrences" if normalized_region == "ALL"
            else "field_rows"
        ),
        "fields": fields,
    }


def list_all_datafields(
    dataset_id, *, client=None, config=None, page_limit=None,
    field_type=None, max_pages=None, region="ALL",
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

    normalized_region = str(region or "ALL").strip().upper()
    if normalized_region != "ALL" and normalized_region not in SUPPORTED_DATA_REGIONS:
        raise ValueError("region must be ALL or a supported data region")
    client = _remote_client(client=client)
    if normalized_region == "ALL":
        regional = [
            list_all_datafields(
                dataset_id, client=client, config=typed,
                page_limit=page_limit, field_type=field_type,
                max_pages=page_cap, region=child_region,
            )
            for child_region in _DATA_DISCOVERY_REGIONS
        ]
        regional_rows_by_id: dict[str, dict[str, dict[str, Any]]] = {}
        for child_region, result in zip(_DATA_DISCOVERY_REGIONS, regional):
            for field in result["fields"]:
                identity = str(field.get("id") or field.get("field_id") or "")
                if not identity:
                    raise RuntimeError("LIVE_DATAFIELD_RESPONSE_INVALID: field ID missing")
                regional_rows_by_id.setdefault(identity, {})[child_region] = dict(field)
        fields = [
            _project_regional_metadata(
                regional_rows, identity_keys={"id", "field_id"},
            )
            for regional_rows in regional_rows_by_id.values()
        ]
        first = regional[0] if regional else {}
        complete = all(bool(result.get("complete")) for result in regional)
        return {
            "source": "LIVE", "scope": _client_scope(client, "ALL"),
            "regions_searched": list(_DATA_DISCOVERY_REGIONS),
            "dataset_id": str(dataset_id).strip(), "field_type": field_type,
            "limit": first.get("limit", page_limit), "offset": 0,
            "count": len(fields), "count_exact": complete,
            "pages": sum(int(result.get("pages", 0)) for result in regional),
            "complete": complete,
            "fields": fields,
        }
    first = list_datafields(
        dataset_id,
        client=client,
        config=typed,
        limit=page_limit,
        offset=0,
        field_type=field_type,
        region=normalized_region,
    )
    fields = list(first["fields"])
    seen_ids = {
        str(field.get("id") or field.get("field_id") or "").casefold()
        for field in fields
    }
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
            region=normalized_region,
        )
        chunk = list(page["fields"])
        if not chunk or page["count"] != total:
            raise RuntimeError("LIVE_DATAFIELD_PAGINATION_INCOMPLETE")
        chunk_ids = [
            str(field.get("id") or field.get("field_id") or "").casefold()
            for field in chunk
        ]
        if (any(not identity for identity in chunk_ids)
                or len(chunk_ids) != len(set(chunk_ids))
                or seen_ids.intersection(chunk_ids)):
            raise RuntimeError(
                "LIVE_DATAFIELD_PAGINATION_INCOMPLETE: duplicate field ID across pages"
            )
        seen_ids.update(chunk_ids)
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
    """Render only Agent-selected BRAIN fields and templates into specs.

    Request validation and field normalization are owned by the standalone
    local template API (:mod:`wqb_agent.local_templates`); live operator
    capability stays here and the Simulation write chain is untouched.
    """
    template_ids, target, selected_fields = normalize_local_request(
        template_ids=template_ids, count=count, fields=fields,
        dataset_id=dataset_id,
    )
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


def list_templates(*, catalog_path=None, require_private=False, config=None):
    """List validated template metadata without constructing research state.

    Private inventory reads use the same configured catalog as probe generation
    and fail closed when that catalog is missing.
    """
    if require_private and catalog_path is None:
        catalog_path = _normalized_config(config).runtime.alpha_template_catalog
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


def validate_simulation_settings(
    settings, *, client=None, config=None, capability=None,
    simulation_type="REGULAR",
):
    """Compatibility facade for the canonical Gateway-owned validator."""
    return SimulationGateway.validate_simulation_settings(
        settings, client=client, capability=capability,
        simulation_type=simulation_type,
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
    result = validate_simulation_settings(
        settings or {}, client=client, config=config,
        simulation_type=effective_simulation_type,
    )
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
        "simulation_type": template.simulation_type,
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


_TOML_BARE_KEY_RE = re.compile(r"[A-Za-z0-9_-]+")


def _toml_key(key: object) -> str:
    key = str(key)
    if _TOML_BARE_KEY_RE.fullmatch(key):
        return key
    return json.dumps(key, ensure_ascii=False)


def _toml_inline(value) -> str:
    """Render nested structures with TOML inline-table syntax, never JSON."""
    if isinstance(value, Mapping):
        inner = ", ".join(
            f"{_toml_key(key)} = {_toml_inline(item)}" for key, item in value.items()
        )
        return "{" + inner + "}"
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_inline(item) for item in value) + "]"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return "true" if value else "false"
    return json.dumps(value)


def _toml_value(value):
    if isinstance(value, Mapping) or (
        isinstance(value, (list, tuple))
        and any(isinstance(item, Mapping) for item in value)
    ):
        return _toml_inline(value)
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


def _simulation_gateway(*, client=None, config=None, state_dir=None,
                       poll_timeout_sec=None):
    """Build the Remote-First gateway without constructing the research Agent."""
    if client is None:
        from .client import WQBClient
        client = WQBClient()
    typed = _normalized_config(config)
    directory = _state_directory(typed, state_dir)
    runtime = typed.runtime
    max_concurrent = runtime.max_concurrent_sims
    poll_timeout = runtime.poll_timeout_sec
    if poll_timeout_sec is not None:
        if (isinstance(poll_timeout_sec, bool)
                or not isinstance(poll_timeout_sec, (int, float))):
            raise ValueError("poll_timeout_sec must be a finite number >= 1")
        try:
            timeout_override = float(poll_timeout_sec)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(
                "poll_timeout_sec must be a finite number >= 1"
            ) from exc
        if not math.isfinite(timeout_override) or timeout_override < 1:
            raise ValueError("poll_timeout_sec must be a finite number >= 1")
        poll_timeout = min(poll_timeout, timeout_override)
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


def simulate_batch(
    specs, *, client=None, config=None, state_dir=None,
    poll_timeout_sec=None,
):
    """Execute Single Simulations through the ten-worker window by default."""
    gateway_options = {
        "client": client, "config": config, "state_dir": state_dir,
    }
    if poll_timeout_sec is not None:
        gateway_options["poll_timeout_sec"] = poll_timeout_sec
    gateway = _simulation_gateway(**gateway_options)
    return gateway.simulate_batch(specs)


def simulate_multi_batch(
    specs, *, client=None, config=None, state_dir=None,
    child_batch_size=MULTI_DEFAULT_CHILD_BATCH_SIZE,
    max_concurrent_multi=MULTI_DEFAULT_CONCURRENCY,
    poll_timeout_sec=None,
):
    """Execute meaningful candidates through bounded Multi-Simulation parents.

    Gateway groups compatible children into transport-sized parents, enforces
    the currently available concurrency, and routes one-child remainders via
    the Single contract. It never pads a research batch to a policy threshold.
    """
    normalized_specs = list(specs or ())
    if not normalized_specs:
        raise ValueError("Multi batch requires at least one candidate")
    if len(normalized_specs) > MULTI_MAX_CANDIDATES_PER_CALL:
        raise ValueError(
            f"Multi batch accepts at most {MULTI_MAX_CANDIDATES_PER_CALL} candidates per call"
        )
    gateway_options = {
        "client": client, "config": config, "state_dir": state_dir,
    }
    if poll_timeout_sec is not None:
        gateway_options["poll_timeout_sec"] = poll_timeout_sec
    gateway = _simulation_gateway(**gateway_options)
    return gateway.simulate_multi_batch(
        normalized_specs,
        child_batch_size=child_batch_size,
        max_concurrent_multi=max_concurrent_multi,
    )


def research_batch_status(
    specs, *, client=None, config=None, state_dir=None,
    child_batch_size=MULTI_DEFAULT_CHILD_BATCH_SIZE,
    max_concurrent_multi=MULTI_DEFAULT_CONCURRENCY,
):
    """Read candidate-scoped admission without relaxing Gateway checks.

    REGULAR batches check exact fingerprints, live write capabilities, and
    unresolved Multi parent capacity. REGION_AGNOSTIC batches use the Single
    path and do not reserve Multi slots. This read never mutates guards or
    submits writes.
    """
    return _simulation_gateway(
        client=client, config=config, state_dir=state_dir
    ).research_batch_status(
        specs, child_batch_size=child_batch_size,
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


def _research_status_budget(client):
    budget_factory = getattr(client, "read_only_budget", None)
    if (
        client is not None
        and getattr(client, "supports_read_only_budget", False) is True
        and callable(budget_factory)
    ):
        return budget_factory(RESEARCH_STATUS_TIMEOUT_SEC)
    return nullcontext()


def _operator_capability_summary(capability):
    if not isinstance(capability, Mapping):
        return {
            "source": "UNAVAILABLE", "status": "UNKNOWN",
            "evidence_status": "INCONCLUSIVE", "operator_count": None,
        }
    operators = capability.get("operators")
    count = len(operators) if isinstance(operators, (list, tuple)) else None
    return {
        "source": str(capability.get("source") or "UNKNOWN"),
        "status": str(capability.get("status") or "UNKNOWN").upper(),
        "evidence_status": str(
            capability.get("evidence_status") or "INCONCLUSIVE"
        ).upper(),
        "operator_count": count,
    }


def _simulation_capability_summary(capability):
    if not isinstance(capability, Mapping):
        return {
            "source": "UNAVAILABLE", "status": "UNKNOWN",
            "capability_status": "UNKNOWN", "simulation_type_choices": [],
            "evidence_status": "INCONCLUSIVE",
        }
    choices = capability.get("simulation_type_choices")
    choices = (
        sorted({str(item).upper() for item in choices if isinstance(item, str)})
        if isinstance(choices, (list, tuple, set)) else []
    )
    return {
        "source": str(capability.get("source") or "UNKNOWN"),
        "status": str(capability.get("status") or "UNKNOWN").upper(),
        "capability_status": str(
            capability.get("capability_status")
            or capability.get("status")
            or "UNKNOWN"
        ).upper(),
        "simulation_type_choices": choices,
        "evidence_status": str(
            capability.get("evidence_status") or "INCONCLUSIVE"
        ).upper(),
    }


def _authentication_summary(authentication):
    if not isinstance(authentication, Mapping):
        return {
            "source": "UNAVAILABLE", "status": "UNKNOWN",
            "authenticated": None, "permissions": [],
            "evidence_status": "INCONCLUSIVE",
        }
    authenticated = authentication.get("authenticated")
    permissions = authentication.get("permissions")
    permissions = (
        sorted({str(item).upper() for item in permissions if isinstance(item, str)})[:16]
        if isinstance(permissions, (list, tuple, set)) else []
    )
    return {
        "source": "BRAIN_LIVE",
        "status": (
            "AVAILABLE" if authenticated is True
            else "UNAVAILABLE" if authenticated is False else "UNKNOWN"
        ),
        "authenticated": authenticated if isinstance(authenticated, bool) else None,
        "permissions": permissions,
        "evidence_status": "AVAILABLE" if authenticated is True else "INCONCLUSIVE",
    }


def _pending_execution_summary(entries):
    if not isinstance(entries, list):
        return None
    write_rows = []
    child_count = 0
    for row in entries:
        if not isinstance(row, Mapping):
            return None
        if str(row.get("kind") or "").upper() == "MULTI_CHILD":
            child_count += 1
        else:
            write_rows.append(row)
    simulation_count = 0
    for row in write_rows:
        count = row.get("simulation_count")
        if isinstance(count, int) and not isinstance(count, bool) and count > 0:
            simulation_count += count
        else:
            simulation_count += 1
    return {
        "pending_execution_count": len(write_rows),
        "pending_simulation_count": simulation_count,
        "pending_multi_child_count": child_count,
    }


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
        "writer_supported": _REGION_AGNOSTIC_WRITE_SUPPORTED,
        "reason": (None if _REGION_AGNOSTIC_WRITE_SUPPORTED
                   else "WRITER_UNSUPPORTED"),
    }
    ra_reason = _mode_unavailable_reason(
        authentication,
        options_available=options_available,
        regular_available="REGION_AGNOSTIC" in choices,
    )
    if ra_reason is None and not _REGION_AGNOSTIC_WRITE_SUPPORTED:
        ra_reason = "WRITER_UNSUPPORTED"
    if ra_reason is None:
        region_agnostic.update({
            "available": True, "status": "AVAILABLE", "reason": None,
        })
    else:
        region_agnostic["reason"] = ra_reason
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
    """Return bounded write readiness and a summary for Research cold-starts.

    Pending row detail remains owned by :func:`get_pending_executions` and is
    fetched only when diagnosis needs it. This function uses a bounded request
    budget for the production WQBClient and never refreshes the remote Alpha
    history cache as a side effect of startup.
    """
    typed = _normalized_config(config)
    directory = _state_directory(typed, state_dir)
    pending_state_known = True
    try:
        pending = get_pending_executions(state_dir=directory)["entries"]
        pending_summary = _pending_execution_summary(pending)
        pending_state_known = pending_summary is not None
    except Exception:
        pending = None
        pending_summary = None
        pending_state_known = False
    try:
        cache = remote_cache_status(config=typed, state_dir=directory)
    except Exception:
        cache = {"freshness": "UNKNOWN"}
    try:
        quota = simulation_quota(
            client=client, config=typed, state_dir=directory,
            refresh_if_stale=False,
        )
    except Exception as exc:
        quota = {
            "status": "UNKNOWN", "source": "UNAVAILABLE",
            "reason_code": "CAPABILITY_UNAVAILABLE", "error": type(exc).__name__,
            "official": {
                "status": "UNKNOWN", "source": "BRAIN_SIMULATION_HEADERS",
                "limit": None, "remaining": None, "reset": None,
            },
        }
    authentication = None
    simulation_capability = None
    operator_capability = None
    if client is None:
        modes = {
            key: {
                **value, "source": "UNAVAILABLE",
                "evidence_status": "UNAVAILABLE",
            }
            for key, value in _simulation_modes_from_capabilities(None, None).items()
        }
    else:
        with _research_status_budget(client):
            try:
                authentication = client.get_authentication_status()
            except Exception:
                authentication = None
            try:
                simulation_capability = client.get_simulation_capability()
            except Exception:
                simulation_capability = None
            try:
                capability = get_capabilities(client=client, config=typed)
                operator_capability = capability.get("operator_capability")
            except Exception:
                capability = None
        modes = _simulation_modes_from_capabilities(
            authentication, simulation_capability
        )
    live = client is not None
    readiness = classify_write_readiness(
        pending_entries=pending,
        official_quota=quota.get("official") if isinstance(quota, Mapping) else None,
        simulation_capability=simulation_capability,
        operator_capability=operator_capability,
        authentication=authentication,
        execution_state_known=pending_state_known,
        client_available=live,
    )
    return {
        "source": "LIVE" if live else "LOCAL_ONLY",
        "status": "AVAILABLE" if live else "PARTIAL",
        "evidence_status": "AVAILABLE" if live else "INCONCLUSIVE",
        "research_contract_version": RESEARCH_CONTRACT_VERSION,
        "write_readiness": readiness["write_readiness"],
        "write_blockers": readiness["write_blockers"],
        "authentication": _authentication_summary(authentication),
        "capability": _operator_capability_summary(operator_capability),
        "simulation_capability": _simulation_capability_summary(simulation_capability),
        "simulation_modes": modes,
        "quota": quota,
        **(pending_summary or {
            "pending_execution_count": None,
            "pending_simulation_count": None,
            "pending_multi_child_count": None,
        }),
        "cache": cache,
    }


def get_pending_executions(*, state_dir=None, config=None):
    """Read unresolved local guard entries from the configured state directory."""
    typed = _normalized_config(config)
    directory = _state_directory(typed, state_dir)
    return {"entries": ExecutionGuard(directory, reconcile=False).entries()}


def resume_execution(fingerprint, *, client=None, config=None,
                     state_dir=None, poll_timeout_sec=None):
    return _simulation_gateway(
        client=client, config=config, state_dir=state_dir,
        poll_timeout_sec=poll_timeout_sec,
    ).resume_execution(fingerprint)


def reconcile_execution(fingerprint, *, client=None, config=None,
                        state_dir=None, poll_timeout_sec=None):
    """Read-only recovery of one guarded execution; never submits again."""
    return resume_execution(
        fingerprint, client=client, config=config,
        state_dir=state_dir, poll_timeout_sec=poll_timeout_sec,
    )


def _remote_client(*, client=None):
    if client is not None:
        return client
    from .client import WQBClient
    return WQBClient()


MAX_AQAF_ALPHA_IDS = 100


def _unit_declaration(value):
    if not isinstance(value, Mapping):
        return None, None
    unit = str(value.get("unit") or "").strip()
    source = str(value.get("source") or "").strip()
    if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._%/-]{0,31}", unit)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_]{0,47}", source)):
        return None, None
    return unit, source.upper()


def _aqaf_scope(alpha):
    if not isinstance(alpha, Mapping):
        return None
    settings = alpha.get("settings")
    if not isinstance(settings, Mapping):
        return None
    region = settings.get("region")
    universe = settings.get("universe")
    delay = settings.get("delay")
    if not isinstance(region, str) or not region.strip():
        return None
    if not isinstance(universe, str) or not universe.strip():
        return None
    if isinstance(delay, bool) or not isinstance(delay, (str, int)) or not str(delay).strip():
        return None
    delay_value = delay
    if isinstance(delay, str) and delay.strip().isdigit():
        delay_value = int(delay.strip())
    return {"region": region.strip().upper(), "universe": universe.strip(), "delay": delay_value}


def _submission_fact(alpha):
    if not isinstance(alpha, Mapping):
        return "UNKNOWN", None
    remote_status = str(alpha.get("status") or "").strip().upper()
    submitted_at = alpha.get("dateSubmitted", alpha.get("date_submitted"))
    if isinstance(submitted_at, str) and submitted_at.strip():
        value = submitted_at.strip()
        try:
            if len(value) == 10:
                date.fromisoformat(value)
            else:
                datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return "UNKNOWN", None
        if remote_status in {"UNSUBMITTED", "SIMULATED", "NOT_SUBMITTED"}:
            return "UNKNOWN", None
        return "SUBMITTED", value[:64]
    if remote_status == "SUBMITTED":
        return "SUBMITTED", None
    return "UNKNOWN", None


def _failed_alpha_checks(metrics):
    if not isinstance(metrics, Mapping) or "checks" not in metrics:
        return ["BRAIN_CHECK_STATUS_UNKNOWN"]
    checks = metrics.get("checks")
    if not isinstance(checks, list):
        return ["BRAIN_CHECK_STATUS_UNKNOWN"]
    if not checks:
        return ["BRAIN_CHECK_STATUS_UNKNOWN"]
    failed = []
    for item in checks:
        if not isinstance(item, Mapping):
            failed.append("BRAIN_CHECK_STATUS_UNKNOWN")
            continue
        states = {
            str(item.get(key) or "").strip().upper()
            for key in ("status", "result") if item.get(key) is not None
        }
        if item.get("pass") is False or states & {"FAIL", "FAILED", "ERROR"}:
            failed.append("BRAIN_CHECK_FAILED")
        elif (item.get("pass") is not True
              and not states & {"PASS", "PASSED", "SUCCESS", "SUCCEEDED"}):
            failed.append("BRAIN_CHECK_STATUS_UNKNOWN")
    return sorted(set(failed))


def _unknown_aqaf_result(alpha_id, reason, *, source="UNKNOWN", fetched_at=None):
    return {
        "alpha_id": str(alpha_id), "source": source, "status": "UNKNOWN",
        "submission_status": "SUBMISSION_UNKNOWN", "submitted_at": None,
        "scope": None, "reason_code": reason,
        "quality": {
            "status": "UNKNOWN", "grade": "U", "q_mid": 50.0,
            "q_low": 0.0, "q_high": 100.0, "coverage": 0.0,
            "config_version": None,
        },
        "metrics": {}, "risk_alerts": [reason], "fetched_at": fetched_at,
        "evidence_status": "UNKNOWN",
    }


def build_aqaf_calibration(
    reference_rows, *, candidate_alpha_id, scope, minimum_reference_count,
    scale_floors, units, frozen_at, reference_id, source, version, pnl_unit,
    turnover_budget_unit, turnover_budgets,
):
    """Freeze one offline, leave-one-candidate-out AQAF reference artifact."""
    return _build_aqaf_calibration(
        reference_rows, candidate_alpha_id=candidate_alpha_id, scope=scope,
        minimum_reference_count=minimum_reference_count,
        scale_floors=scale_floors, units=units, frozen_at=frozen_at,
        reference_id=reference_id, source=source, version=version,
        pnl_unit=pnl_unit, turnover_budget_unit=turnover_budget_unit,
        turnover_budgets=turnover_budgets,
    )


def _evaluate_submitted_alpha_snapshot(
    alpha_id, *, client, calibration, metric_units, expected_trading_dates,
    expected_trading_dates_source, pnl_unit, pnl_unit_source, as_of,
    type_label, type_source,
):
    provider = _evidence_provider(client=client)
    summary = provider.get_alpha_evidence(alpha_id, live=True, depth="summary")
    fetched_at = summary.get("fetched_at") if isinstance(summary, Mapping) else None
    alpha = summary.get("alpha") if isinstance(summary, Mapping) else None
    submission_status, submitted_at = _submission_fact(alpha)
    if submission_status != "SUBMITTED":
        return {
            **_unknown_aqaf_result(
                alpha_id, "SUBMISSION_UNKNOWN", source="BRAIN_LIVE",
                fetched_at=fetched_at,
            ),
            "evidence_status": "UNAVAILABLE",
        }, None
    evidence = provider.get_alpha_evidence(
        alpha_id, live=True, depth="full", alpha_detail=alpha,
    )
    scope = _aqaf_scope(alpha)
    detail = alpha.get("is") if isinstance(alpha, Mapping) else None
    detail = detail if isinstance(detail, Mapping) else {}
    unit_values = {}
    unit_sources = {}
    metric_units = metric_units if isinstance(metric_units, Mapping) else {}
    for name, declaration in metric_units.items():
        unit, source = _unit_declaration(declaration)
        if unit:
            unit_values[str(name)] = unit
            unit_sources[str(name)] = source
    pnl_unit_value, pnl_unit_source_value = _unit_declaration(
        {"unit": pnl_unit, "source": pnl_unit_source},
    )
    expected_dates = None
    if (isinstance(expected_trading_dates, Sequence)
            and not isinstance(expected_trading_dates, (str, bytes))
            and _unit_declaration({
                "unit": "calendar", "source": expected_trading_dates_source,
            })[1]):
        expected_dates = [str(item) for item in expected_trading_dates]
    if scope is None:
        score = _unknown_aqaf_result(
            alpha_id, "SCOPE_UNKNOWN", source="BRAIN_LIVE", fetched_at=fetched_at,
        )
        score["submission_status"] = submission_status
        score["submitted_at"] = submitted_at
        score["evidence_status"] = "AVAILABLE"
        return score, None
    aggregates = evidence.get("aggregates") if isinstance(evidence, Mapping) else None
    yearly_data = aggregates
    pnl = evidence.get("pnl") if isinstance(evidence, Mapping) else None
    self_correlation = evidence.get("self_correlation") if isinstance(evidence, Mapping) else None
    calculated = evaluate_quality(
        alpha_id=alpha_id, alpha_scope=scope, metrics=detail,
        yearly_data=yearly_data, pnl_rows=pnl,
        pnl_unit=(pnl_unit_value if pnl_unit_source_value and expected_dates is not None
                  else None),
        expected_trading_dates=expected_dates,
        self_correlation=self_correlation, calibration=calibration,
        as_of=as_of or date.today(), metric_units=unit_values,
        type_label=type_label, type_source=type_source,
    )
    statuses = evidence.get("status") if isinstance(evidence, Mapping) else {}
    if not isinstance(statuses, Mapping):
        statuses = {}
    failed_checks = _failed_alpha_checks(detail)
    calculated_alerts = sorted(set(calculated["risk_alerts"]) | set(failed_checks))
    quality = dict(calculated["quality"])
    if failed_checks:
        quality["grade"] = "U"
        quality["status"] = "UNKNOWN"
        quality["risk_grade_cap"] = "U"
    record = {
        "alpha_id": str(alpha_id), "source": "BRAIN_LIVE",
        "status": "AVAILABLE" if quality["status"] == "AVAILABLE" else "UNKNOWN",
        "submission_status": submission_status, "submitted_at": submitted_at,
        "scope": scope,
        "reason_code": (
            "QUALITY_EVIDENCE_INCOMPLETE"
            if quality.get("status") != "AVAILABLE" or quality.get("grade") == "U"
            else None
        ),
        "quality": quality, "type": calculated["type"],
        "metrics": calculated["metrics"], "risk_alerts": calculated_alerts,
        "fitness_diagnostic": calculated["fitness_diagnostic"],
        "config_version": calculated["quality"]["config_version"],
        "fetched_at": fetched_at,
        "evidence_status": "AVAILABLE",
        "provenance": {
            "alpha_detail": "BRAIN_LIVE", "aggregates": statuses.get("aggregates", "UNKNOWN"),
            "pnl": statuses.get("pnl", "UNKNOWN"),
            "self_correlation": statuses.get("self_correlation", "UNKNOWN"),
            "pnl_unit_source": pnl_unit_source_value,
            "trading_calendar_source": (
                _unit_declaration({"unit": "calendar", "source": expected_trading_dates_source})[1]
                if expected_dates is not None else None
            ),
            "metric_unit_sources": unit_sources,
        },
        "scope_compatibility": "VERIFIED_FROM_ALPHA_SETTINGS",
    }
    internal = {
        "alpha_id": str(alpha_id), "submission_status": submission_status,
        "scope": scope, "quality": quality,
        "disqualifying_risks": failed_checks,
        "pnl": {
            "rows": pnl,
            "unit": pnl_unit_value if pnl_unit_source_value else None,
            "expected_dates": expected_dates,
        },
    }
    return record, internal


def evaluate_submitted_alpha(
    alpha_id, *, calibration=None, metric_units=None,
    expected_trading_dates=None, expected_trading_dates_source=None,
    pnl_unit=None, pnl_unit_source=None, type_label=None, type_source=None,
    as_of=None, client=None, config=None,
):
    """Evaluate one explicit Alpha using fresh live evidence only."""
    normalized_id = str(alpha_id or "").strip()
    if not normalized_id or len(normalized_id) > 128:
        raise ValueError("alpha_id must be non-empty and at most 128 characters")
    if as_of is not None and not isinstance(as_of, date):
        raise ValueError("as_of must be a date")
    if metric_units is not None and not isinstance(metric_units, Mapping):
        raise ValueError("metric_units must be a mapping")
    if expected_trading_dates is not None and (
        not isinstance(expected_trading_dates, Sequence)
        or isinstance(expected_trading_dates, (str, bytes))
    ):
        raise ValueError("expected_trading_dates must be a sequence of dates")
    result, _ = _evaluate_submitted_alpha_snapshot(
        normalized_id, client=client, calibration=calibration,
        metric_units=metric_units, expected_trading_dates=expected_trading_dates,
        expected_trading_dates_source=expected_trading_dates_source,
        pnl_unit=pnl_unit, pnl_unit_source=pnl_unit_source, as_of=as_of,
        type_label=type_label, type_source=type_source,
    )
    return result


def evaluate_submitted_alphas(
    alpha_ids, *, calibration=None, metric_units=None,
    expected_trading_dates_by_alpha=None, expected_trading_dates_source=None,
    pnl_unit_by_alpha=None, type_labels=None, type_sources=None,
    as_of=None, max_concurrent=1, client=None, config=None,
):
    """Evaluate a bounded explicit ID set; no feed omission becomes not-submitted."""
    if not isinstance(alpha_ids, list) or not alpha_ids or len(alpha_ids) > MAX_AQAF_ALPHA_IDS:
        raise ValueError(f"alpha_ids must contain 1 to {MAX_AQAF_ALPHA_IDS} IDs (at most {MAX_AQAF_ALPHA_IDS})")
    ids = [str(item).strip() for item in alpha_ids]
    if any(not item or len(item) > 128 for item in ids):
        raise ValueError("alpha_ids must be non-empty and at most 128 characters")
    if len(set(ids)) != len(ids):
        raise ValueError("alpha_ids must be unique")
    if isinstance(max_concurrent, bool) or not isinstance(max_concurrent, int) or not 1 <= max_concurrent <= 4:
        raise ValueError("max_concurrent must be between 1 and 4")
    if as_of is not None and not isinstance(as_of, date):
        raise ValueError("as_of must be a date")
    if metric_units is not None and not isinstance(metric_units, Mapping):
        raise ValueError("metric_units must be a mapping")
    for name, value in (
        ("expected_trading_dates_by_alpha", expected_trading_dates_by_alpha),
        ("pnl_unit_by_alpha", pnl_unit_by_alpha),
        ("type_labels", type_labels), ("type_sources", type_sources),
    ):
        if value is not None and not isinstance(value, Mapping):
            raise ValueError(f"{name} must be a mapping")
    expected_trading_dates_by_alpha = expected_trading_dates_by_alpha or {}
    pnl_unit_by_alpha = pnl_unit_by_alpha or {}
    type_labels = type_labels or {}
    type_sources = type_sources or {}
    for name, value in (
        ("expected_trading_dates_by_alpha", expected_trading_dates_by_alpha),
        ("pnl_unit_by_alpha", pnl_unit_by_alpha),
        ("type_labels", type_labels), ("type_sources", type_sources),
    ):
        if not isinstance(value, Mapping):
            raise ValueError(f"{name} must be a mapping")
    active_client = _remote_client(client=client)
    def evaluate(item):
        alpha_id = item
        declaration = pnl_unit_by_alpha.get(alpha_id, {})
        return _evaluate_submitted_alpha_snapshot(
            alpha_id, client=active_client, calibration=calibration,
            metric_units=metric_units,
            expected_trading_dates=expected_trading_dates_by_alpha.get(alpha_id),
            expected_trading_dates_source=expected_trading_dates_source,
            pnl_unit=declaration.get("unit") if isinstance(declaration, Mapping) else None,
            pnl_unit_source=declaration.get("source") if isinstance(declaration, Mapping) else None,
            as_of=as_of, type_label=type_labels.get(alpha_id),
            type_source=type_sources.get(alpha_id),
        )[0]

    with ThreadPoolExecutor(max_workers=min(max_concurrent, len(ids))) as pool:
        records = list(pool.map(evaluate, ids))
    return {
        "source": "BRAIN_LIVE", "status": "AVAILABLE",
        "evaluation_count": len(records), "evaluations": records,
        "network_write": False,
    }


def _collect_allocation_candidates(
    alpha_ids, *, scope, calibration, metric_units,
    expected_trading_dates_by_alpha, expected_trading_dates_source,
    pnl_unit_by_alpha, family_labels, type_labels, type_sources,
    as_of, client,
):
    if not isinstance(scope, Mapping) or not scope:
        raise ValueError("scope must be explicit")
    if not isinstance(alpha_ids, list) or not 1 <= len(alpha_ids) <= MAX_AQAF_ALPHA_IDS:
        raise ValueError(f"alpha_ids must contain 1 to {MAX_AQAF_ALPHA_IDS} IDs (at most {MAX_AQAF_ALPHA_IDS})")
    for name, value in (
        ("metric_units", metric_units),
        ("expected_trading_dates_by_alpha", expected_trading_dates_by_alpha),
        ("pnl_unit_by_alpha", pnl_unit_by_alpha),
        ("family_labels", family_labels), ("type_labels", type_labels),
        ("type_sources", type_sources),
    ):
        if value is not None and not isinstance(value, Mapping):
            raise ValueError(f"{name} must be a mapping")
    if len(set(str(item).strip() for item in alpha_ids)) != len(alpha_ids):
        raise ValueError("alpha_ids must be unique")
    active_client = _remote_client(client=client)
    candidates = []
    for alpha_id in alpha_ids:
        alpha_id = str(alpha_id).strip()
        pnl_declaration = (pnl_unit_by_alpha or {}).get(alpha_id, {})
        unit = pnl_declaration.get("unit") if isinstance(pnl_declaration, Mapping) else None
        source = pnl_declaration.get("source") if isinstance(pnl_declaration, Mapping) else None
        record, private = _evaluate_submitted_alpha_snapshot(
            alpha_id, client=active_client, calibration=calibration,
            metric_units=metric_units,
            expected_trading_dates=(expected_trading_dates_by_alpha or {}).get(alpha_id),
            expected_trading_dates_source=expected_trading_dates_source,
            pnl_unit=unit, pnl_unit_source=source, as_of=as_of,
            type_label=(type_labels or {}).get(alpha_id),
            type_source=(type_sources or {}).get(alpha_id),
        )
        family = (family_labels or {}).get(alpha_id)
        candidates.append({
            "alpha_id": alpha_id,
            "submission_status": (
                "UNKNOWN" if record.get("submission_status") == "SUBMISSION_UNKNOWN"
                else record.get("submission_status", "UNKNOWN")
            ),
            "scope": record.get("scope"), "quality": record.get("quality"),
            "family": family,
            "pnl": private.get("pnl") if private is not None else None,
            "fetched_at": record.get("fetched_at"),
            "quality_status": (record.get("quality") or {}).get("status"),
            "config_version": record.get("config_version"),
            "provenance": record.get("provenance"),
        })
    return candidates


def preview_osmosis_allocation(
    alpha_ids, *, scope, calibration, metric_units=None,
    expected_trading_dates_by_alpha=None, expected_trading_dates_source=None,
    pnl_unit_by_alpha=None, family_labels=None, type_labels=None,
    type_sources=None, as_of=None, client=None, config=None, **options,
):
    """Fetch explicit live evidence and return a per-scope internal point preview."""
    candidates = _collect_allocation_candidates(
        alpha_ids, scope=scope, calibration=calibration,
        metric_units=metric_units,
        expected_trading_dates_by_alpha=expected_trading_dates_by_alpha,
        expected_trading_dates_source=expected_trading_dates_source,
        pnl_unit_by_alpha=pnl_unit_by_alpha, family_labels=family_labels,
        type_labels=type_labels, type_sources=type_sources,
        as_of=as_of, client=client,
    )
    result = _preview_osmosis_allocation(candidates, scope=scope, **options)
    result.update({
        "source": "BRAIN_LIVE_PLUS_LOCAL_CALCULATION", "network_write": False,
        "evidence_freshness": {
            item["alpha_id"]: {
                "source": "BRAIN_LIVE", "fetched_at": item.get("fetched_at"),
                "quality_status": item.get("quality_status"),
                "config_version": item.get("config_version"),
                "provenance": item.get("provenance"),
            } for item in candidates
        },
    })
    return result


def compare_osmosis_allocation_plans(
    alpha_ids, *, scope, calibration, metric_units=None,
    expected_trading_dates_by_alpha=None, expected_trading_dates_source=None,
    pnl_unit_by_alpha=None, family_labels=None, type_labels=None,
    type_sources=None, as_of=None, client=None, config=None, **options,
):
    """Compare three historical read-only allocation baselines for one scope."""
    candidates = _collect_allocation_candidates(
        alpha_ids, scope=scope, calibration=calibration,
        metric_units=metric_units,
        expected_trading_dates_by_alpha=expected_trading_dates_by_alpha,
        expected_trading_dates_source=expected_trading_dates_source,
        pnl_unit_by_alpha=pnl_unit_by_alpha, family_labels=family_labels,
        type_labels=type_labels, type_sources=type_sources,
        as_of=as_of, client=client,
    )
    result = _compare_osmosis_allocation_plans(candidates, scope=scope, **options)
    result.update({
        "source": "BRAIN_LIVE_PLUS_LOCAL_CALCULATION", "network_write": False,
        "evidence_freshness": {
            item["alpha_id"]: {
                "source": "BRAIN_LIVE", "fetched_at": item.get("fetched_at"),
                "quality_status": item.get("quality_status"),
                "config_version": item.get("config_version"),
                "provenance": item.get("provenance"),
            } for item in candidates
        },
    })
    return result


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
    payload = RemoteAlphaEvidenceProvider(
        _remote_client(client=client)
    ).get_alpha_prod_correlation(str(alpha_id).strip())
    if not isinstance(payload, Mapping):
        return payload

    schema = payload.get("schema")
    properties = schema.get("properties") if isinstance(schema, Mapping) else None
    property_names = {
        item.get("name") for item in properties or ()
        if isinstance(item, Mapping)
    }
    records = payload.get("records")
    if (
        isinstance(schema, Mapping)
        and schema.get("name") == "prodCorrelation"
        and {"min", "max", "alphas"}.issubset(property_names)
        and isinstance(records, list)
        and all(isinstance(row, (list, tuple)) and len(row) >= 3 for row in records)
        and all(
            isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(float(value))
            for row in records for value in row[:2]
        )
        and all(
            isinstance(row[2], int) and not isinstance(row[2], bool) and row[2] >= 0
            for row in records
        )
    ):
        result = dict(payload)
        result.update({
            "source": "BRAIN_LIVE",
            "status": "AVAILABLE",
            "evidence_status": "AVAILABLE",
        })
        return result
    return payload


def get_alpha_recordsets(alpha_id, names, *, client=None, config=None):
    """Read only explicitly selected, currently discoverable Alpha recordsets."""
    return _evidence_provider(client=client).get_alpha_recordsets(
        str(alpha_id).strip(), names,
    )


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


_AGENT_CORE_TOOL_NAMES = frozenset({
    "research_status", "research_batch_status", "list_datasets", "list_datafields",
    "list_templates", "generate_probes",
    "get_operator_reference", "validate_simulation_spec",
    "simulate_batch", "simulate_multi_batch", "get_alpha_evidence",
    "get_alpha_recordsets",
    "reconcile_execution", "get_alpha_prod_correlation",
    "get_pending_executions",
})


def research_tool_manifest(profile="core"):
    """Return a deterministic default CORE surface or the opt-in full catalog."""
    rows: list[dict[str, Any]] = [
        {"name": "research_status", "mode": "READ_ONLY", "owner": "research_api"},
        {"name": "research_batch_status", "mode": "READ_ONLY", "owner": "SimulationGateway"},
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
        {"name": "resume_execution", "mode": "LOCAL_STATE_WRITE", "remote_write": False, "local_write": True, "owner": "ExecutionGuard"},
        {"name": "reconcile_execution", "mode": "LOCAL_STATE_WRITE", "remote_write": False, "local_write": True, "owner": "ExecutionGuard"},
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
        {"name": "evaluate_submitted_alpha", "mode": "READ_ONLY", "owner": "AQAF"},
        {"name": "evaluate_submitted_alphas", "mode": "READ_ONLY", "owner": "AQAF"},
        {"name": "build_aqaf_calibration", "mode": "PURE", "owner": "AQAF"},
        {"name": "preview_osmosis_allocation", "mode": "READ_ONLY", "owner": "AQAF"},
        {"name": "compare_osmosis_allocation_plans", "mode": "READ_ONLY", "owner": "AQAF"},
        {"name": "get_activity_diversity", "mode": "READ_ONLY", "owner": "BRAIN"},
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
    return [row for row in rows if row["name"] in _AGENT_CORE_TOOL_NAMES]


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
    "reconcile_execution", "research_status", "research_batch_status",
    "get_alpha", "get_alpha_summary", "get_alpha_evidence", "get_alpha_metrics",
    "get_alpha_aggregates", "get_alpha_pnl", "get_alpha_self_correlation",
    "get_alpha_prod_correlation",
    "get_alpha_recordsets",
    "evaluate_submitted_alpha", "evaluate_submitted_alphas",
    "build_aqaf_calibration",
    "preview_osmosis_allocation", "compare_osmosis_allocation_plans",
    "refresh_remote_alphas", "list_remote_alphas",
    "get_activity_diversity",
    "get_remote_alpha", "get_remote_alpha_evidence", "remote_cache_status",
    "purge_remote_cache", "simulation_quota", "group_alphas",
    "find_duplicate_alphas", "find_similar_alphas",
    "preview_alpha_colors", "sync_alpha_colors",
    "research_tool_manifest",
]
