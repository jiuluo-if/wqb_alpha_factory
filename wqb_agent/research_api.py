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

from .artifacts import atomic_write_json_if_changed
from .config import normalize_config
from .expression import analyze_expression
from .optimization_decision import OptimizationDecision, optimization_decision_identity
from .proposal_contract import (
    TARGETED_BATCH_TTL_SEC,
    TARGETED_BATCH_TYPE,
    _operator_reference,
    validate_targeted_batch,
)
from .state import Trajectory


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
    trajectory.load()
    recent = [row.to_dict() for row in trajectory.recent(max(1, int(limit)))]
    return {
        "state_dir": os.path.abspath(state_dir),
        "experiment_count": sum(1 for _ in trajectory.iter_rows()),
        "recent_experiments": recent,
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


def get_operator_reference(path=None) -> dict[str, Any]:
    """Return the checked-in operator snapshot used by proposal validation."""
    if path is None:
        path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "docs", "reference", "OPERATORS_CHEATSHEET.md")
    return _operator_reference(path)


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
    """Run one agent-authored experiment through the existing safe proposal path.

    The facade creates a temporary input envelope only; ``Agent.run_proposals``
    remains the sole owner of deduplication, checkpoints, retries, and remote
    Simulation writes.  The temporary file is removed after the call.
    """
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
        merged.setdefault("field_analysis", {
            str(row["id"]): {
                "semantic": row.get("description", ""),
                "coverage": row.get("coverage"),
                "frequency": row.get("frequency"),
                "data_type": row.get("type"),
            }
            for row in used_profiles
        })
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
                               ttl_sec=TARGETED_BATCH_TTL_SEC):
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
    fields = _targeted_field_profiles(runtime, proposals)
    envelope = {
        "batch_type": TARGETED_BATCH_TYPE,
        "source": "agent_optimizer",
        "round_no": runtime.next_round_no(),
        "created_at": now,
        "expires_at": now + max(0.0, float(ttl_sec)),
        "optimization_decision_ids": decision_ids,
        "decision_fingerprint": fingerprint,
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


__all__ = [
    "ExperimentSpec", "inspect_state", "discover_fields",
    "get_operator_reference", "run_experiment", "get_experiment",
    "compare_experiments", "search_history", "reconcile",
    "inspect_optimizer_parents", "inspect_optimizer_context",
    "propose_optimization", "materialize_targeted_batch",
]
