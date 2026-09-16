"""
ROLE: CORE
AGENT_RELEVANCE: HIGH

PURPOSE:
Provide the small, stable agent-facing interface over the existing research
runtime.  This module composes discovery, execution, state, and evaluation;
it does not implement a second state machine or bypass safety checks.

READ WHEN:
- starting a research task
- choosing which runtime capability to call
- writing facade-level tests

DO NOT USE FOR:
- deciding economic hypotheses
- bypassing proposal validation or reconciliation
- treating cache or derived output as platform truth
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Mapping, Sequence
from typing import Any

from .alpha_grouping import (
    find_remote_duplicates,
    find_remote_similar,
    group_remote_evidence,
)
from .alpha_templates import AlphaTemplateRegistry
from .artifacts import atomic_write_json_if_changed
from .checkpoints import CheckpointStore
from .config import AppConfig, normalize_config
from .factory_runner import AIFactoryRunner
from .locking import OwnerBusyError, single_instance_scope
from .optimization_decision import OptimizationDecision, optimization_decision_identity
from .optimization_interfaces import RemoteAlphaEvidenceProvider
from .proposal_contract import (
    TARGETED_BATCH_TTL_SEC,
    TARGETED_BATCH_TYPE,
    load_operator_syntax_reference,
    load_packaged_operator_syntax_reference,
    validate_targeted_batch,
)
from .remote_alpha_repository import RemoteAlphaRepository
from .remote_colors import preview_remote_colors, sync_remote_colors
from .remote_quota import RemoteSimulationQuota
from .research_context import build_research_context, project_cycle
from .research_cursor import build_research_cursor
from .research_cursor import research_cycle_id as _research_cycle_id
from .research_quality import assess_experiment as _assess_experiment
from .research_quality import assess_records
from .simulation_gateway import ExecutionGuard, SimulationGateway, SimulationSpec
from .state import Trajectory
from .trial_ledger import TrialLedger


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


def _agent(*, agent=None, client=None, config=None, state_dir=None):
    if agent is not None:
        return agent
    if client is None:
        from .client import WQBClient

        client = WQBClient()
    raw = _load_config(config)
    if state_dir is not None:
        raw.setdefault("agent", {})["state_dir"] = state_dir
    from .agent import Agent

    return Agent(client, normalize_config(raw))


def _state_dir(agent=None, state_dir=None) -> str:
    return state_dir or getattr(agent, "state_dir", ".wqb_state")


def inspect_state(*, state_dir=".wqb_state", limit=10) -> dict[str, Any]:
    """Return a compact view of immutable experiment evidence and workspace files."""
    trajectory = Trajectory(max_len=max(1, int(limit)), path=os.path.join(state_dir, "trajectory.jsonl"))
    summary = trajectory.load_summary(max(1, int(limit)))
    return {
        "state_dir": os.path.abspath(state_dir),
        "experiment_count": summary["experiment_count"],
        "recent_experiments": [row.to_dict() for row in summary["recent_experiments"]],
        "derived_files": sorted(
            name for name in os.listdir(state_dir) if name.endswith((".json", ".md"))
        ) if os.path.isdir(state_dir) else [],
    }


def discover_fields(query, *, agent=None, client=None, config=None, state_dir=None, limit=None):
    """Discover fields through the existing BRAIN-backed discovery component."""
    runtime = _agent(agent=agent, client=client, config=config, state_dir=state_dir)
    if isinstance(query, str):
        hypothesis = {"id": "agent-query", "statement": query, "tags": query.split(), "datasets": []}
    elif isinstance(query, Mapping):
        hypothesis = dict(query)
    else:
        raise TypeError("query must be a string or object")
    fields = runtime.discovery.discover(
        hypothesis,
        target_count=limit or runtime.fields_per_discovery,
    )
    return {
        "fields": fields,
        "field_source": runtime.discovery.source_provenance(),
        "query": hypothesis,
    }


def generate_probes(query=None, *, template_ids=None, count=100, seed=None,
                    agent=None, client=None, config=None, state_dir=None):
    """Generate reviewable ``SimulationSpec`` probes without an inbox write."""
    runtime = _agent(agent=agent, client=client, config=config, state_dir=state_dir)
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
    fields = runtime.discovery.discover(hypothesis, target_count=target)
    reference = runtime.operator_reference
    if callable(reference):
        reference = reference()
    return runtime.alpha_factory.generate_probe_specs(
        hypothesis, fields, reference, target=target, seed=seed,
    )


def get_operator_syntax_reference(path=None) -> dict[str, Any]:
    """Return evergreen syntax hints, never current capability truth."""
    if path is None:
        return load_packaged_operator_syntax_reference()
    return load_operator_syntax_reference(path)


def get_operator_reference(*, agent=None, client=None, config=None) -> dict[str, Any]:
    """Return a bounded current operator view from the existing BRAIN client."""
    if agent is None and client is None:
        raise RuntimeError("LIVE_OPERATOR_CAPABILITY_REQUIRED")
    runtime = _agent(agent=agent, client=client, config=config)
    capability = getattr(runtime, "operator_capability", None)
    if callable(capability):
        capability = capability()
    if capability is None:
        capability = getattr(runtime.client, "get_operator_capability", None)
        if not callable(capability):
            raise RuntimeError("LIVE_OPERATOR_CAPABILITY_REQUIRED")
        capability = capability()
    return {
        key: capability[key]
        for key in (
            "key", "status", "availability", "source", "valid", "errors",
            "operators", "capability_fingerprint", "endpoint",
        )
        if key in capability
    }


def get_capabilities(*, agent=None, client=None, config=None) -> dict[str, Any]:
    """Return a bounded live platform capability view for the AI tools."""
    reference = get_operator_reference(agent=agent, client=client, config=config)
    return {
        "source": "BRAIN_LIVE_ONLY",
        "operators": list(reference.get("operators") or []),
        "operator_capability": reference,
    }


def get_operators(*, agent=None, client=None, config=None) -> list[str]:
    """Return only the live verified operator names."""
    return list(get_operator_reference(
        agent=agent, client=client, config=config
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


def run_experiment(spec, *, agent=None, client=None, config=None, state_dir=None, context=None):
    """Execute only the stable Remote-First ``SimulationSpec`` contract."""
    if not isinstance(spec, SimulationSpec):
        raise TypeError("run_experiment requires SimulationSpec")
    return simulate(spec, agent=agent, client=client, config=config,
                    state_dir=state_dir)


def _simulation_gateway(*, agent=None, client=None, config=None, state_dir=None):
    """Build the Remote-First gateway without constructing the research Agent."""
    if client is None and agent is not None:
        client = agent.client
    if client is None:
        from .client import WQBClient
        client = WQBClient()
    directory = state_dir or getattr(agent, "state_dir", None) or ".wqb_state"
    runtime = getattr(agent, "runtime_policy", None)
    max_concurrent = getattr(runtime, "max_concurrent_sims", 3)
    poll_timeout = getattr(runtime, "poll_timeout_sec", 1500)
    return SimulationGateway(
        client, state_dir=directory, max_concurrent=max_concurrent,
        poll_timeout_sec=poll_timeout,
    )


def validate_simulation_spec(spec, *, agent=None, client=None, config=None,
                             state_dir=None):
    """Validate only executable request shape and return bounded facts."""
    gateway = _simulation_gateway(
        agent=agent, client=client, config=config, state_dir=state_dir
    )
    return gateway.validate_simulation_spec(spec)


def execution_fingerprint(spec, *, agent=None, client=None, config=None,
                          state_dir=None):
    return _simulation_gateway(
        agent=agent, client=client, config=config, state_dir=state_dir
    ).execution_fingerprint(spec)


def simulate(spec, *, agent=None, client=None, config=None, state_dir=None):
    """Start one real Simulation through the only public write gateway."""
    return _simulation_gateway(
        agent=agent, client=client, config=config, state_dir=state_dir
    ).simulate(spec)


def simulate_batch(specs, *, agent=None, client=None, config=None, state_dir=None):
    """Execute a bounded batch while applying exact dedupe per item."""
    gateway = _simulation_gateway(
        agent=agent, client=client, config=config, state_dir=state_dir
    )
    return [gateway.simulate(item) for item in (specs or ())]


def get_pending_executions(*, state_dir=".wqb_state"):
    return {"entries": ExecutionGuard(state_dir).entries()}


def resume_execution(fingerprint, *, agent=None, client=None, config=None,
                     state_dir=None):
    return _simulation_gateway(
        agent=agent, client=client, config=config, state_dir=state_dir
    ).resume_execution(fingerprint)


def reconcile_execution(fingerprint, *, agent=None, client=None, config=None,
                        state_dir=None):
    """Read-only recovery of one guarded execution; never submits again."""
    return resume_execution(
        fingerprint, agent=agent, client=client, config=config,
        state_dir=state_dir,
    )


def _remote_client(*, agent=None, client=None):
    if client is not None:
        return client
    if agent is not None:
        return agent.client
    from .client import WQBClient
    return WQBClient()


def get_alpha(alpha_id, *, agent=None, client=None, config=None):
    return _remote_client(agent=agent, client=client).get_alpha(str(alpha_id).strip())


def get_alpha_evidence(alpha_id, *, agent=None, client=None, config=None,
                       live=True):
    if not live:
        raise ValueError("LIVE_EVIDENCE_REQUIRED")
    import time as _time
    snapshot = RemoteAlphaEvidenceProvider(
        _remote_client(agent=agent, client=client)
    ).collect(str(alpha_id).strip())
    return {
        "alpha_id": snapshot.alpha_id, "source": "LIVE",
        "fetched_at": _time.time(), "age_sec": 0.0,
        "alpha": dict(snapshot.alpha_detail),
        "aggregates": snapshot.aggregates, "pnl": snapshot.pnl,
        "self_correlation": snapshot.self_correlation,
        "status": dict(snapshot.status), "availability": dict(snapshot.availability),
    }


def get_alpha_metrics(alpha_id, *, agent=None, client=None, config=None):
    return get_alpha_evidence(alpha_id, agent=agent, client=client,
                              config=config)["alpha"].get("is", {})


def get_alpha_aggregates(alpha_id, *, agent=None, client=None, config=None):
    return get_alpha_evidence(alpha_id, agent=agent, client=client,
                              config=config)["aggregates"]


def get_alpha_pnl(alpha_id, *, agent=None, client=None, config=None):
    return get_alpha_evidence(alpha_id, agent=agent, client=client,
                              config=config)["pnl"]


def get_alpha_self_correlation(alpha_id, *, agent=None, client=None, config=None):
    return get_alpha_evidence(alpha_id, agent=agent, client=client,
                              config=config)["self_correlation"]


def compare_alphas(alpha_ids, *, agent=None, client=None, config=None):
    return {"source": "LIVE", "alphas": [
        get_alpha_evidence(item, agent=agent, client=client, config=config)
        for item in (alpha_ids or ())
    ]}


def _remote_repository(*, agent=None, client=None, config=None, state_dir=None,
                       require_client=True):
    if require_client or client is not None or agent is not None:
        client = _remote_client(agent=agent, client=client)
    if isinstance(config, AppConfig):
        retention = config.remote_cache.retention_days
    elif config is not None:
        retention = normalize_config(_load_config(config)).remote_cache.retention_days
    else:
        retention = 7
    directory = state_dir or getattr(agent, "state_dir", None) or ".wqb_state"
    cache_path = os.path.join(directory, ".alpha_feed_cache", "remote.json")
    return RemoteAlphaRepository(
        client.get_all_user_alphas if client is not None else None,
        cache_path=cache_path, retention_days=retention, evidence_client=client,
    )


def refresh_remote_alphas(*, agent=None, client=None, config=None, state_dir=None,
    limit=100, days=None):
    if days is not None:
        if isinstance(config, AppConfig):
            if int(days) != config.remote_cache.retention_days:
                raise ValueError("days must equal the configured retention window")
        elif int(days) < 1 or int(days) > 90:
            raise ValueError("days must be within 1-90")
    return _remote_repository(
        agent=agent, client=client, config=config, state_dir=state_dir,
        require_client=True,
    ).refresh_remote_alphas(limit=limit)


def list_remote_alphas(*, agent=None, client=None, config=None, state_dir=None,
                       days=None, status=None):
    return _remote_repository(
        agent=agent, client=client, config=config, state_dir=state_dir,
        require_client=False,
    ).list_remote_alphas(days=days, status=status)


def remote_cache_status(*, agent=None, client=None, config=None, state_dir=None):
    return _remote_repository(
        agent=agent, client=client, config=config, state_dir=state_dir,
        require_client=False,
    ).cache_status()


def purge_remote_cache(*, agent=None, client=None, config=None, state_dir=None):
    return {"removed": _remote_repository(
        agent=agent, client=client, config=config, state_dir=state_dir,
        require_client=False,
    ).purge_remote_cache()}


def simulation_quota(*, agent=None, client=None, config=None, state_dir=None):
    """Return a read-only quota projection from remote usage and active guards."""
    repository = _remote_repository(
        agent=agent, client=client, config=config, state_dir=state_dir,
        require_client=False,
    )
    if isinstance(config, AppConfig):
        factory = config.factory
    elif config is not None:
        factory = normalize_config(_load_config(config)).factory
    else:
        factory = None
    return RemoteSimulationQuota(
        repository, ExecutionGuard(state_dir or ".wqb_state"),
        daily_cap=factory.daily_simulation_cap if factory else 1600,
        rolling_cap=factory.weekly_simulation_cap if factory else 11200,
    ).snapshot()


def get_remote_alpha(alpha_id, *, live=False, agent=None, client=None,
                     config=None, state_dir=None):
    """Read one Alpha from the rebuildable cache or from BRAIN explicitly."""
    return _remote_repository(
        agent=agent, client=client, config=config, state_dir=state_dir,
        require_client=live,
    ).get_remote_alpha(alpha_id, live=live)


def get_remote_alpha_evidence(alpha_id, *, live=True, agent=None, client=None,
                              config=None, state_dir=None):
    """Return remote evidence; live reads are the default and source-labeled."""
    return _remote_repository(
        agent=agent, client=client, config=config, state_dir=state_dir,
        require_client=True,
    ).get_remote_alpha_evidence(alpha_id, live=live)


def group_alphas(alpha_ids=None, *, rows=None, agent=None, client=None,
                 config=None, state_dir=None, days=None):
    if rows is None:
        repository = _remote_repository(
            agent=agent, client=client, config=config, state_dir=state_dir
        )
        ids = alpha_ids or [
            item["alpha_id"] for item in repository.list_remote_alphas(days=days)
        ]
        rows = [repository.get_remote_alpha_evidence(item) for item in ids]
    return group_remote_evidence(rows)


def find_alpha_duplicates(alpha_id, *, rows=None, agent=None, client=None,
                          config=None, state_dir=None):
    if rows is None:
        rows = []
        repository = _remote_repository(
            agent=agent, client=client, config=config, state_dir=state_dir
        )
        for item in repository.list_remote_alphas():
            rows.append(repository.get_remote_alpha_evidence(item["alpha_id"]))
    return find_remote_duplicates(rows, alpha_id)


def find_duplicate_alphas(alpha_id, *, rows=None, agent=None, client=None,
                          config=None, state_dir=None):
    """Public name for exact execution duplicate lookup."""
    return find_alpha_duplicates(
        alpha_id, rows=rows, agent=agent, client=client,
        config=config, state_dir=state_dir,
    )


def find_similar_alphas(expression_or_alpha_id, *, rows=None, agent=None,
                        client=None, config=None, state_dir=None, days=None):
    """Return advisory remote exact/structural matches; never blocks a POST."""
    if rows is None:
        repository = _remote_repository(
            agent=agent, client=client, config=config, state_dir=state_dir
        )
        rows = [repository.get_remote_alpha_evidence(item["alpha_id"])
                for item in repository.list_remote_alphas(days=days)]
    return find_remote_similar(rows, expression_or_alpha_id)


def preview_alpha_colors(alpha_ids=None, *, rows=None, agent=None, client=None,
                         config=None, state_dir=None, days=None):
    if rows is None:
        repository = _remote_repository(
            agent=agent, client=client, config=config, state_dir=state_dir
        )
        ids = alpha_ids or [
            item["alpha_id"] for item in repository.list_remote_alphas(days=days)
        ]
        rows = [repository.get_remote_alpha_evidence(item) for item in ids]
    return preview_remote_colors(rows)


def sync_alpha_colors(alpha_ids=None, *, rows=None, agent=None, client=None,
                      config=None, state_dir=None, days=None, overwrite=False,
                      dry_run=False):
    repository = _remote_repository(
        agent=agent, client=client, config=config, state_dir=state_dir
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


def get_experiment(experiment_id, *, state_dir=".wqb_state"):
    """Return the latest canonical Agent-facing record for one experiment.

    ``Trajectory.find_row()`` is the owner-side revision merge primitive, so an
    early DONE snapshot is never surfaced over later ``RESEARCH_SETTLED``
    evidence and this surface does not re-implement revision merging.
    """
    trajectory = Trajectory(path=os.path.join(state_dir, "trajectory.jsonl"))
    return trajectory.find_row(experiment_id)


def compare_experiments(ids: Sequence[str], *, state_dir=".wqb_state") -> dict[str, Any]:
    """Compare stored experiments with one canonical streaming pass.

    ``Trajectory.find_rows()`` is the owner-side batch merge primitive, so the
    comparison does not reload and re-scan the append-only file once per id.
    """
    trajectory = Trajectory(path=os.path.join(state_dir, "trajectory.jsonl"))
    found = trajectory.find_rows(ids)
    records = [found.get(str(item)) for item in ids]
    records = [record for record in records if record is not None]
    return {"experiments": records, "missing": [item for item in ids if not any(str(record.get("id")) == str(item) or str(record.get("proposal_id")) == str(item) for record in records)]}


def search_history(query, *, state_dir=".wqb_state", limit=20):
    needle = str(query or "").casefold()
    matches = []
    trajectory = Trajectory(path=os.path.join(state_dir, "trajectory.jsonl"))
    for row in trajectory.iter_canonical_rows():
        haystack = " ".join(str(row.get(key, "")) for key in ("id", "proposal_id", "hypothesis_id", "expression", "rationale", "status")).casefold()
        if not needle or needle in haystack:
            matches.append(row)
            if len(matches) >= max(1, int(limit)):
                break
    return matches


def reconcile(progress_url, *, client=None, timeout=60):
    """Poll one known remote job; never submit a replacement write."""
    if not isinstance(progress_url, str) or not progress_url.strip():
        raise ValueError("progress_url must be a non-empty known remote URL")
    if client is None:
        from .client import WQBClient

        client = WQBClient()
    return client.get_progress_snapshot(progress_url, timeout=timeout)


def inspect_optimizer_parents(*, agent=None, client=None, config=None,
                              state_dir=None, limit=8):
    """Return a bounded, read-only summary of evidence-eligible parents.

    Only the limited summary is exposed so an Agent prompt never receives the
    whole trajectory or the full cloud feed.  Mechanism-state hints derived
    elsewhere stay UNKNOWN until the Agent supplies them.
    """
    runtime = _agent(agent=agent, client=client, config=config, state_dir=state_dir)
    return runtime.inspect_optimizer_parents(limit=limit)


def inspect_optimizer_context(*, agent=None, client=None, config=None,
                              state_dir=None, limit=8):
    """Return the bounded, read-only optimizer context for the Inner Agent.

    Thin facade only: the optimizer gate, ranking, generation bound and
    pre-correlation policy stay owned by ``OptimizerWorkflow``.  The limit is
    capped at 8 so an Agent prompt never receives an unbounded view, nothing is
    written and no Simulation runs here.
    """
    runtime = _agent(agent=agent, client=client, config=config, state_dir=state_dir)
    return runtime.optimizer_context(limit=min(int(limit), 8))


def propose_optimization(decision, *, agent=None, client=None, config=None,
                         state_dir=None, max_candidates=4):
    """Validate one Agent-authored ``OptimizationDecision`` and emit proposals.

    This facade never bypasses ``OptimizerWorkflow``: the workflow checks the
    decision against canonical evidence and the deterministic gates, then
    reuses the single CHILD generation path.  Only proposals are produced; no
    Simulation, checkpoint write or remote call happens here.  A VALIDATE,
    STOP or REROUTE decision returns no child proposal.
    """
    runtime = _agent(agent=agent, client=client, config=config, state_dir=state_dir)
    if isinstance(decision, Mapping):
        decision = OptimizationDecision.from_mapping(decision)
    return runtime.propose_optimization([decision], max_candidates=max_candidates)


def _targeted_field_profiles(runtime, proposals):
    """用 Agent 已验证的 discovery cache 为 targeted batch 附上真实字段画像。

    优化提案的字段证据来自已完成的 parent，而执行路径的 preflight 需要平台语义
    画像（``description`` / ``semantic_status``）。这里只复用 Agent 已有的只读
    field cache；找不到就保持缺画像由 preflight fail-closed，不伪造字段元数据。
    """
    used = set()
    for proposal in proposals or ():
        if not isinstance(proposal, Mapping):
            continue
        for field_id in proposal.get("fields") or ():
            if str(field_id).strip():
                used.add(str(field_id))
    if not used:
        return []
    reader = getattr(runtime, "_read_field_cache", None)
    if not callable(reader):
        return []
    try:
        _types, profiles = reader()
    except Exception:
        return []
    if not isinstance(profiles, Mapping):
        return []
    out = []
    for key, profile in profiles.items():
        if not isinstance(profile, Mapping):
            continue
        field_id = str(profile.get("id") or str(key).split("::")[-1])
        if field_id not in used:
            continue
        enriched = dict(profile)
        enriched.setdefault("id", field_id)
        out.append(enriched)
    return out


def materialize_targeted_batch(decisions, *, agent=None, client=None,
                               config=None, state_dir=None, max_candidates=4,
                               ttl_sec=TARGETED_BATCH_TTL_SEC,
                               expected_research_cursor=None,
                               research_cycle_id=None):
    runtime = _agent(agent=agent, client=client, config=config, state_dir=state_dir)
    directory = state_dir or getattr(runtime, "state_dir", None) or ".wqb_state"
    try:
        with single_instance_scope(directory, operation="materialize-targeted-batch"):
            return _materialize_targeted_batch_locked(
                decisions, agent=runtime, client=client, config=config,
                state_dir=directory, max_candidates=max_candidates, ttl_sec=ttl_sec,
                expected_research_cursor=expected_research_cursor,
                research_cycle_id=research_cycle_id,
            )
    except OwnerBusyError:
        return {
            "written": False,
            "status": "LOCAL_OWNER_BUSY",
            "path": os.path.join(directory, "proposals.json"),
        }


def _materialize_targeted_batch_locked(decisions, *, agent=None, client=None,
                                       config=None, state_dir=None,
                                       max_candidates=4,
                                       ttl_sec=TARGETED_BATCH_TTL_SEC,
                                       expected_research_cursor=None,
                                       research_cycle_id=None):
    """Write Agent-authored CHILD/VALIDATE decisions into the one inbox.

    The optimizer already validated the decisions; this only freezes the
    resulting bounded batch into the canonical ``proposals.json`` so it is not
    silently replaced by a factory exploration batch.  The Agent also records
    local canonical accounting and a lossy memory projection; this is distinct
    from remote Simulation writes.  It reuses
    ``OptimizerWorkflow`` (the single CHILD path), the canonical round counter
    and the existing atomic writer: no second inbox, no second Simulation path,
    and no Simulation/checkpoint write happens here.
    """
    authored = [
        OptimizationDecision.from_mapping(item) if isinstance(item, Mapping) else item
        for item in (decisions or ())
    ]
    runtime = _agent(agent=agent, client=client, config=config, state_dir=state_dir)
    directory = state_dir or getattr(runtime, "state_dir", None) or ".wqb_state"
    path = os.path.join(directory, "proposals.json")
    decision_ids = [optimization_decision_identity(item) for item in authored]
    fingerprint = _targeted_batch_fingerprint(decision_ids)
    existing = _read_targeted_envelope(path)
    current_runtime = inspect_runtime_context(state_dir=directory)
    current_cursor = current_runtime["research_cursor"]
    if (expected_research_cursor is not None
            and str(expected_research_cursor) != str(current_cursor)):
        existing_fingerprint = existing.get("decision_fingerprint") if isinstance(existing, Mapping) else None
        if (not (_active_targeted_batch(existing, time.time())
                 and existing_fingerprint == fingerprint)):
            return {
                "written": False, "status": "RESEARCH_CONTEXT_STALE",
                "path": path, "research_cursor": current_cursor,
            }
    barrier = _targeted_recovery_barrier(runtime)
    if barrier is not None:
        return {
            "written": False,
            "status": "TARGETED_BATCH_RECOVERY_BLOCKED",
            "path": path,
            "reason": barrier,
        }
    if _active_targeted_batch(existing, time.time()):
        existing_fingerprint = existing.get("decision_fingerprint")
        if not existing_fingerprint:
            existing_ids = existing.get("optimization_decision_ids")
            existing_fingerprint = (
                _targeted_batch_fingerprint(existing_ids)
                if isinstance(existing_ids, list) else None
            )
        if existing_fingerprint == fingerprint:
            return {
                "written": False,
                "status": "TARGETED_BATCH_UNCHANGED",
                "path": path,
                "round_no": existing.get("round_no"),
                "expires_at": existing.get("expires_at"),
                "proposal_count": len(existing.get("proposals") or []),
            }
        return {
            "written": False,
            "status": "TARGETED_BATCH_CONFLICT",
            "path": path,
            "round_no": existing.get("round_no"),
        }
    report = runtime.propose_optimization(authored, max_candidates=max_candidates)
    proposals = list(report.get("proposals") or [])
    if not proposals:
        return {**report, "written": False, "status": "NO_TARGETED_PROPOSAL"}
    ok, errors = validate_targeted_batch(proposals)
    if not ok:
        return {
            **report, "written": False,
            "status": "TARGETED_BATCH_REJECTED", "errors": list(errors),
        }
    now = time.time()
    cycle = research_cycle_id or _research_cycle_id(
        current_cursor, decision_ids, kind="optimization"
    )
    for proposal in proposals:
        proposal["source_research_cursor"] = current_cursor
        proposal["research_cycle_id"] = cycle
    fields = _targeted_field_profiles(runtime, proposals)
    envelope = {
        "batch_type": TARGETED_BATCH_TYPE,
        "source": "agent_optimizer",
        "round_no": runtime.next_round_no(),
        "created_at": now,
        "expires_at": now + max(0.0, float(ttl_sec)),
        "optimization_decision_ids": decision_ids,
        "decision_fingerprint": fingerprint,
        "source_research_cursor": current_cursor,
        "research_cycle_id": cycle,
        "hypothesis": {
            "id": f"h-targeted-r{int(now)}",
            "statement": "Agent authored optimization decisions",
            "tags": ["agent_optimizer", "targeted_optimization"],
            "datasets": [],
        },
        "fields": fields,
        "proposals": proposals,
    }
    os.makedirs(directory, exist_ok=True)
    atomic_write_json_if_changed(path, envelope)
    return {
        **report,
        "written": True,
        "status": "TARGETED_BATCH_WRITTEN",
        "path": path,
        "round_no": envelope["round_no"],
        "expires_at": envelope["expires_at"],
        "proposal_count": len(proposals),
    }


def _targeted_recovery_barrier(runtime):
    checkpoints = getattr(runtime, "checkpoints", None)
    scan = getattr(checkpoints, "scan", None)
    if not callable(scan):
        return None
    for record in scan() or ():
        if record.get("malformed"):
            return "MALFORMED_CHECKPOINT"
        checkpoint = record.get("checkpoint")
        if not isinstance(checkpoint, Mapping) or checkpoint.get("complete") is not True:
            return "UNFINISHED_CHECKPOINT"
        for experiment in checkpoint.get("experiments") or ():
            if not isinstance(experiment, Mapping):
                return "MALFORMED_CHECKPOINT"
            if str(experiment.get("status") or "").upper() in {"SUBMIT_UNKNOWN", "UNKNOWN"}:
                return "UNKNOWN_REQUIRES_RECONCILIATION"
    return None


def _targeted_batch_fingerprint(decision_ids):
    return hashlib.sha256(json.dumps(sorted(str(item) for item in decision_ids),
                                         separators=(",", ":")).encode("utf-8")).hexdigest()


def _read_targeted_envelope(path):
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
        return payload if isinstance(payload, dict) else None
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


def _active_targeted_batch(payload, now):
    if not isinstance(payload, Mapping):
        return False
    if payload.get("batch_type") != TARGETED_BATCH_TYPE or payload.get("source") != "agent_optimizer":
        return False
    try:
        return float(payload.get("expires_at")) > float(now)
    except (TypeError, ValueError):
        return False


def _projection_inputs(state_dir):
    directory = os.fspath(state_dir or ".wqb_state")
    trajectory = Trajectory(path=os.path.join(directory, "trajectory.jsonl"))
    rows = list(trajectory.iter_canonical_rows() or ())
    ledger = TrialLedger(
        os.path.join(directory, "trial_ledger.jsonl"),
        trajectory_path=os.path.join(directory, "trajectory.jsonl"),
    )
    ledger_summary = ledger.summarize()
    checkpoints = CheckpointStore(directory).scan()
    proposals = _read_targeted_envelope(os.path.join(directory, "proposals.json"))
    active = proposals if _active_targeted_batch(proposals, time.time()) else None
    return directory, rows, ledger_summary, checkpoints, active


def inspect_runtime_context(*, state_dir=".wqb_state"):
    """Return bounded runtime facts and safe next actions for the Outer Agent."""
    directory, rows, ledger_summary, checkpoints, active = _projection_inputs(state_dir)
    unfinished = [record for record in checkpoints
                  if record.get("malformed") or not (
                      isinstance(record.get("checkpoint"), Mapping)
                      and record["checkpoint"].get("complete") is True
                  )]
    unknown = any(
        str(item.get("status") or "").upper() in {"SUBMIT_UNKNOWN", "UNKNOWN"}
        for record in unfinished
        for item in ((record.get("checkpoint") or {}).get("experiments") or [])
        if isinstance(item, Mapping)
    )
    unknown = unknown or any(
        str(item.get("status") or "").upper() in {"SUBMIT_UNKNOWN", "UNKNOWN"}
        for item in rows
        if isinstance(item, Mapping)
    )
    runtime_state = "BLOCKED" if unfinished or unknown else "READY"
    cursor = build_research_cursor(
        rows, ledger_summary=ledger_summary, checkpoints=checkpoints,
        runtime_state=runtime_state, active_batch=active,
    )
    factory_status = AIFactoryRunner.status_view(directory)
    return {
        **cursor,
        "capabilities": {
            "simulation": "BLOCKED" if runtime_state == "BLOCKED" else "AVAILABLE",
            "self_correlation": "UNKNOWN",
            "targeted_batch": "BLOCKED" if runtime_state == "BLOCKED" else "AVAILABLE",
        },
        "unfinished_checkpoint": [
            os.path.basename(record.get("path", "")) for record in unfinished
        ],
        "submit_unknown_present": unknown,
        "active_targeted_batch": (
            {key: active[key] for key in ("round_no", "decision_fingerprint", "expires_at")
             if key in active} if active else None
        ),
        "factory_status": factory_status,
        "quota_summary": (factory_status or {}).get("quota"),
        "allowed_actions": ["READ_ONLY"] if runtime_state == "BLOCKED" else [
            "READ_ONLY", "AUTHOR_DECISION", "MATERIALIZE_TARGETED_BATCH",
            "EXECUTE_PENDING_ROUND",
        ],
    }


def assess_experiment_quality(experiment_id, *, state_dir=".wqb_state"):
    """Assess one canonical experiment without reading raw state into output."""
    directory, rows, ledger_summary, _checkpoints, _active = _projection_inputs(state_dir)
    row = next((item for item in rows if str(item.get("id")) == str(experiment_id)
                or str(item.get("proposal_id")) == str(experiment_id)), None)
    if row is None:
        return {"status": "NOT_FOUND", "experiment_id": str(experiment_id)}
    return _assess_experiment(row, trial_summary=ledger_summary).as_dict()


def assess_execution_round(round_no, *, state_dir=".wqb_state"):
    """Return a bounded quality/accounting projection for one execution round."""
    _directory, rows, ledger_summary, _checkpoints, _active = _projection_inputs(state_dir)
    result = assess_records(rows, trial_summary=ledger_summary, round_no=int(round_no))
    result["validation_completeness"] = dict(result["validation_completeness"])
    result["classification_counts"] = dict(result["classification_counts"])
    result["status_counts"] = dict(result["status_counts"])
    result.pop("assessments", None)
    return result


def assess_research_cycle(cycle_id, *, state_dir=".wqb_state"):
    """Return cycle mapping and quality counts from existing canonical rows."""
    _directory, rows, ledger_summary, _checkpoints, _active = _projection_inputs(state_dir)
    selected = [row for row in rows if str(row.get("research_cycle_id") or "") == str(cycle_id)]
    result = project_cycle(rows, cycle_id)
    summary = assess_records(selected, trial_summary=ledger_summary)
    result["quality_status"] = dict(summary["classification_counts"])
    result["missing_evidence"] = summary["missing_evidence"]
    return result


def inspect_research_context(*, state_dir=".wqb_state", agent=None, client=None,
                             config=None, limit=8):
    """Build the bounded Inner-Agent handoff from current canonical projections."""
    runtime = inspect_runtime_context(state_dir=state_dir)
    _directory, rows, ledger_summary, _checkpoints, _active = _projection_inputs(state_dir)
    summaries = [_assess_experiment(row, trial_summary=ledger_summary)
                 for row in rows[-max(1, min(int(limit), 8)):]]
    optimizer = {}
    if agent is not None or client is not None:
        optimizer = inspect_optimizer_context(
            agent=agent, client=client, config=config, state_dir=state_dir,
            limit=limit,
        )
    gaps = sorted({gap for item in summaries for gap in item.missing_evidence})
    return build_research_context(
        runtime_context=runtime, cursor=runtime, quality_summaries=summaries,
        optimizer_context=optimizer, capabilities=runtime["capabilities"],
        unresolved_gaps=gaps, limit=limit,
    )


def inspect_execution_round(round_no, *, state_dir=".wqb_state"):
    """Expose execution lifecycle and quality for one round."""
    result = assess_execution_round(round_no, state_dir=state_dir)
    runtime = inspect_runtime_context(state_dir=state_dir)
    result["runtime_state"] = runtime["runtime_state"]
    result["recovery_status"] = "BLOCKED" if runtime["unfinished_checkpoint"] else "CLEAR"
    return result


def inspect_research_cycle(cycle_id, *, state_dir=".wqb_state"):
    return assess_research_cycle(cycle_id, state_dir=state_dir)


def inspect_pending_work(*, state_dir=".wqb_state"):
    runtime = inspect_runtime_context(state_dir=state_dir)
    return {
        "targeted_inbox_present": bool(runtime.get("active_targeted_batch")),
        "unfinished_checkpoint": runtime["unfinished_checkpoint"],
        "submit_unknown": runtime["submit_unknown_present"],
        "factory_blocker": (runtime.get("factory_status") or {}).get("blocker"),
        "next_safe_action": "READ_ONLY_RECONCILE" if runtime["runtime_state"] == "BLOCKED" else "INSPECT",
    }


def inspect_trial_accounting(*, state_dir=".wqb_state"):
    summary = _projection_inputs(state_dir)[2]
    return {key: summary.get(key) for key in (
        "trial_count", "submitted_count", "completed_count", "effective_trial_count",
        "history_completeness", "settled_observation_count",
    )}


def inspect_factory_status(*, state_dir=".wqb_state"):
    return AIFactoryRunner.status_view(os.fspath(state_dir))


def assess_experiment(experiment_id, *, state_dir=".wqb_state"):
    return assess_experiment_quality(experiment_id, state_dir=state_dir)


def execute_pending_round(*, state_dir=None, agent=None, client=None, config=None):
    """Execute the existing canonical proposals through Agent.run_proposals only."""
    runtime = _agent(agent=agent, client=client, config=config, state_dir=state_dir)
    path = os.path.join(_state_dir(runtime, state_dir), "proposals.json")
    if not os.path.exists(path):
        return {"status": "NO_PENDING_PROPOSALS", "path": path}
    return runtime.run_proposals(path)


def resume_pending_round(**kwargs):
    """Resume the same canonical pending batch via the existing recovery path."""
    return execute_pending_round(**kwargs)


def request_factory_stop(*, state_dir=".wqb_state"):
    return AIFactoryRunner.request_stop(os.fspath(state_dir))


def research_tool_manifest():
    return [
        {"name": "get_capabilities", "mode": "READ_ONLY", "owner": "BRAIN"},
        {"name": "discover_fields", "mode": "READ_ONLY", "owner": "BRAIN"},
        {"name": "get_operators", "mode": "READ_ONLY", "owner": "BRAIN"},
        {"name": "list_templates", "mode": "READ_ONLY", "owner": "AlphaFactory"},
        {"name": "inspect_template", "mode": "READ_ONLY", "owner": "AlphaFactory"},
        {"name": "generate_probes", "mode": "PURE", "owner": "AlphaFactory"},
        {"name": "validate_simulation_spec", "mode": "READ_ONLY", "owner": "SimulationGateway"},
        {"name": "simulate", "mode": "SIMULATION_WRITE", "remote_write": True, "owner": "SimulationGateway"},
        {"name": "simulate_batch", "mode": "SIMULATION_WRITE", "remote_write": True, "owner": "SimulationGateway"},
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
    "SimulationSpec", "discover_fields",
    "generate_probes",
    "get_capabilities", "get_operators", "get_operator_reference", "get_operator_syntax_reference",
    "list_templates", "inspect_template",
    "run_experiment", "validate_simulation_spec", "execution_fingerprint",
    "simulate", "simulate_batch", "get_pending_executions", "resume_execution",
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
