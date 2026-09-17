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
from collections.abc import Mapping
from typing import Any

from .alpha_factory import AlphaFactory
from .alpha_grouping import (
    find_remote_duplicates,
    find_remote_similar,
    group_remote_evidence,
)
from .alpha_templates import AlphaTemplateRegistry
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


def generate_probes(query=None, *, template_ids=None, count=100, seed=None,
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
        target = int(count)
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
        hypothesis, target_count=target
    )
    if callable(reference):
        reference = reference()
    return factory.generate_probe_specs(
        hypothesis, fields, reference, target=target, seed=seed,
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
    return registry.catalog()


def inspect_template(template_id, *, catalog_path=None, require_private=False):
    registry = (
        AlphaTemplateRegistry.from_private(catalog_path)
        if require_private else AlphaTemplateRegistry(private_catalog=catalog_path)
    )
    template = registry.get(str(template_id))
    if template is None:
        raise KeyError(f"template not found: {template_id}")
    return template.catalog_entry()


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
    return gateway.validate_simulation_spec(spec)


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
            "max_concurrent": 10,
        },
        "multi": {
            "name": "Multi-Simulation",
            "available": True,
            "children_per_job": 10,
            "max_concurrent_jobs": 8,
        },
        "region_agnostic": {
            "name": "Region-Agnostic Simulation",
            "available": False,
            "reason": "NO_VERIFIED_WRITE_CONTRACT",
        },
    }


def get_pending_executions(*, state_dir=".wqb_state"):
    return {"entries": ExecutionGuard(state_dir).entries()}


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
    return {"source": "LIVE", "alphas": [
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
        repository, ExecutionGuard(_state_directory(typed, state_dir)),
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
