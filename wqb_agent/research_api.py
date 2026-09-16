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
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
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
from .expression import analyze_expression
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


@dataclass(frozen=True)
class ExperimentSpec:
    """The minimal input an external research agent should author.

    The existing proposal contract remains authoritative. ``to_proposal`` is
    only a compatibility adapter; it must not invent research evidence or
    semantic claims before normal Agent preflight.
    """

    hypothesis: str
    expression: str
    fields: tuple[str, ...] = field(default_factory=tuple)
    settings: Mapping[str, Any] = field(default_factory=dict)
    rationale: str = ""
    operator_mapping: str = ""
    experiment_question: str = ""
    parent_id: str | None = None
    change: Any = None
    source_research_cursor: str | None = None
    research_cycle_id: str | None = None

    def __post_init__(self):
        if not isinstance(self.hypothesis, str) or not self.hypothesis.strip():
            raise ValueError("hypothesis must be a non-empty string")
        if not isinstance(self.expression, str) or not self.expression.strip():
            raise ValueError("expression must be a non-empty string")
        object.__setattr__(
            self,
            "fields",
            tuple(str(value) for value in (self.fields or ()) if str(value).strip()),
        )
        object.__setattr__(self, "settings", dict(self.settings or {}))

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> ExperimentSpec:
        if not isinstance(value, Mapping):
            raise TypeError("experiment spec must be an object")
        return cls(
            hypothesis=value.get("hypothesis", ""),
            expression=value.get("expression", ""),
            fields=tuple(value.get("fields") or ()),
            settings=value.get("settings") or {},
            rationale=value.get("rationale", "") or "",
            operator_mapping=value.get("operator_mapping", "") or "",
            experiment_question=value.get("experiment_question", "") or "",
            parent_id=value.get("parent_id"),
            change=value.get("change"),
            source_research_cursor=value.get("source_research_cursor"),
            research_cycle_id=value.get("research_cycle_id"),
        )

    def to_proposal(self, *, round_no: int = 1, context: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Adapt this lightweight input to the current proposal envelope.

        Missing discovery/evaluation metadata is intentionally not fabricated.
        The existing validation path will reject an incomplete proposal before
        a remote Simulation write, preserving fail-closed behavior.
        """
        context = dict(context or {})
        change_type = context.get("change_type")
        changed_variable = context.get("changed_variable")
        if isinstance(self.change, Mapping):
            change_type = change_type or self.change.get("type") or self.change.get("kind")
            changed_variable = changed_variable or self.change.get("variable")
        elif isinstance(self.change, str):
            change_type = change_type or self.change
        proposal = {
            "hypothesis": self.hypothesis,
            "hypothesis_id": context.get("hypothesis_id") or "agent-proposed",
            "expression": self.expression.strip(),
            "fields": list(self.fields),
            "settings": dict(self.settings),
            "rationale": self.rationale.strip() or self.hypothesis.strip(),
            "parent_id": self.parent_id,
            "parent_expression": context.get("parent_expression"),
            "change": self.change,
            "change_type": change_type or "baseline",
            "changed_variable": changed_variable,
            "round": int(round_no),
            "experiment_stage": context.get("experiment_stage") or ("CHILD" if self.parent_id else "BASELINE"),
            "research_role": context.get("research_role") or ("EXPLOIT" if self.parent_id else "EXPLORE"),
        }
        if self.source_research_cursor:
            proposal["source_research_cursor"] = self.source_research_cursor
        if self.research_cycle_id:
            proposal["research_cycle_id"] = self.research_cycle_id
        operator_mapping = context.get("operator_mapping") or self.operator_mapping.strip()
        experiment_question = context.get("experiment_question") or self.experiment_question.strip()
        if operator_mapping:
            proposal["operator_mapping"] = operator_mapping
        if experiment_question:
            proposal["experiment_question"] = experiment_question
        if "expected_failure_modes" in context:
            proposal["expected_failure_modes"] = list(context.get("expected_failure_modes") or [])
        if "tuning_risk" in context:
            proposal["tuning_risk"] = bool(context["tuning_risk"])
        for key in (
            "datasets", "field_source", "field_understanding", "field_analysis",
            "field_hypothesis_basis", "operator_evidence", "validation_plan",
        ):
            if key in context:
                proposal[key] = context[key]
        return proposal


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


def _suggestion_context(state_dir: str) -> dict[str, Any]:
    path = os.path.join(state_dir, "suggestions.json")
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return {
        key: payload[key]
        for key in (
            "round_no", "fields", "field_source", "operator_reference",
            "research_space",
        )
        if key in payload
    }


def run_experiment(spec, *, agent=None, client=None, config=None, state_dir=None, context=None):
    """Run an experiment, preferring the Remote-First path for ``SimulationSpec``.

    ``SimulationSpec`` is the current execution contract and never constructs
    an Agent or writes local research results.  ``ExperimentSpec`` and its
    mapping adapter remain a bounded compatibility path until their callers
    have migrated.

    The legacy adapter creates a temporary input envelope only; its existing
    checkpoint owner remains unchanged while migration is in progress.
    """
    if isinstance(spec, SimulationSpec):
        return simulate(spec, agent=agent, client=client, config=config,
                        state_dir=state_dir)
    runtime = _agent(agent=agent, client=client, config=config, state_dir=state_dir)
    spec = spec if isinstance(spec, ExperimentSpec) else ExperimentSpec.from_mapping(spec)
    directory = _state_dir(runtime, state_dir)
    suggestion = _suggestion_context(directory)
    merged = dict(suggestion)
    merged.update(dict(context or {}))
    if spec.parent_id and not merged.get("parent_expression"):
        parent = get_experiment(spec.parent_id, state_dir=directory)
        if parent:
            merged["parent_expression"] = parent.get("expression")
    profiles = {
        str(row.get("id")): row
        for row in (merged.get("fields") or [])
        if isinstance(row, Mapping) and row.get("id")
    }
    used_profiles = [profiles[field_id] for field_id in spec.fields if field_id in profiles]
    if used_profiles:
        merged.setdefault("datasets", sorted({
            str(row.get("dataset")) for row in used_profiles if row.get("dataset")
        }))
        merged.setdefault("field_understanding", {
            str(row["id"]): row.get("description", "")
            for row in used_profiles
        })
        field_analysis = {
            str(row["id"]): {
                "semantic": row.get("description", ""),
                "coverage": row.get("coverage"),
                "frequency": row.get("frequency"),
                "data_type": row.get("type"),
            }
            for row in used_profiles
        }
        for row in used_profiles:
            if "frequency_evidence" in row:
                field_analysis.setdefault(str(row["id"]), {})["frequency_evidence"] = (
                    row["frequency_evidence"]
                )
        merged.setdefault("field_analysis", field_analysis)
        merged.setdefault("field_hypothesis_basis", {
            str(row["id"]): {
                "description": row.get("description", ""),
                "mechanism": spec.rationale.strip() or spec.hypothesis.strip(),
            }
            for row in used_profiles
        })
    reference = merged.get("operator_reference") or getattr(runtime, "operator_reference", None)
    if reference:
        merged.setdefault("operator_evidence", {
            "sha256": reference.get("sha256"),
            "operators": list(analyze_expression(spec.expression).operators),
            "rationale": spec.rationale.strip() or spec.hypothesis.strip(),
        })
    round_no = int(merged.get("round_no") or runtime.next_round_no())
    proposal = spec.to_proposal(round_no=round_no, context=merged)
    envelope = {
        "round_no": round_no,
        "hypothesis": {
            "id": merged.get("hypothesis_id") or "agent-proposed",
            "statement": spec.hypothesis,
            "tags": ["agent-authored"],
            "datasets": list(merged.get("datasets") or []),
        },
        "proposals": [proposal],
    }
    os.makedirs(directory, exist_ok=True)
    fd, path = tempfile.mkstemp(prefix="experiment-", suffix=".json", dir=directory, text=True)
    os.close(fd)
    try:
        atomic_write_json_if_changed(path, envelope)
        return runtime.run_proposals(path)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


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
                          limit=100):
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
        {"name": "inspect_runtime_context", "mode": "READ_ONLY", "owner": "preflight/checkpoint"},
        {"name": "inspect_research_context", "mode": "READ_ONLY", "owner": "research_context"},
        {"name": "inspect_execution_round", "mode": "READ_ONLY", "owner": "Trajectory/TrialLedger"},
        {"name": "inspect_research_cycle", "mode": "READ_ONLY", "owner": "research_cursor"},
        {"name": "inspect_pending_work", "mode": "READ_ONLY", "owner": "preflight"},
        {"name": "inspect_trial_accounting", "mode": "READ_ONLY", "owner": "TrialLedger"},
        {"name": "inspect_factory_status", "mode": "READ_ONLY", "owner": "factory_session"},
        {"name": "assess_experiment", "mode": "READ_ONLY", "owner": "research_quality"},
        {"name": "assess_execution_round", "mode": "READ_ONLY", "owner": "research_quality"},
        {"name": "assess_research_cycle", "mode": "READ_ONLY", "owner": "research_quality"},
        {"name": "materialize_targeted_batch", "mode": "LOCAL_MUTATION", "requires_research_cursor": True, "owner": "OptimizerWorkflow"},
        {"name": "execute_pending_round", "mode": "SIMULATION_WRITE", "requires_ready_state": True, "remote_write": True, "owner": "Agent.run_proposals"},
        {"name": "resume_pending_round", "mode": "SIMULATION_WRITE", "requires_ready_state": False, "remote_write": True, "owner": "Agent.run_proposals"},
        {"name": "reconcile", "mode": "READ_ONLY", "owner": "WQBClient"},
        {"name": "alpha_submission", "mode": "MANUAL_ONLY", "owner": "user"},
    ]


__all__ = [
    "ExperimentSpec", "SimulationSpec", "inspect_state", "discover_fields",
    "generate_probes",
    "get_capabilities", "get_operator_reference", "get_operator_syntax_reference",
    "list_templates", "inspect_template",
    "run_experiment", "validate_simulation_spec", "execution_fingerprint",
    "simulate", "simulate_batch", "get_pending_executions", "resume_execution",
    "reconcile_execution",
    "get_alpha", "get_alpha_evidence", "get_alpha_metrics",
    "get_alpha_aggregates", "get_alpha_pnl", "get_alpha_self_correlation",
    "compare_alphas", "refresh_remote_alphas", "list_remote_alphas",
    "get_remote_alpha", "get_remote_alpha_evidence", "remote_cache_status",
    "purge_remote_cache", "simulation_quota", "group_alphas",
    "find_alpha_duplicates", "find_similar_alphas", "preview_alpha_colors", "sync_alpha_colors",
    "get_experiment",
    "compare_experiments", "search_history", "reconcile",
    "inspect_optimizer_parents", "inspect_optimizer_context",
    "propose_optimization", "materialize_targeted_batch",
    "inspect_runtime_context", "inspect_research_context",
    "inspect_execution_round", "inspect_research_cycle", "inspect_pending_work",
    "inspect_trial_accounting", "inspect_factory_status", "assess_experiment",
    "assess_execution_round", "assess_research_cycle", "execute_pending_round",
    "resume_pending_round", "request_factory_stop", "research_tool_manifest",
]
