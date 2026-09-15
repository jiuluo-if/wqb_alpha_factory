"""Canonical auditable Experiment model and execution identity projections.

This module is deliberately persistence-free.  ``Trajectory`` remains the
sole durable JSONL owner; this boundary only defines the model it stores and
the immutable identity projections shared by readers.
"""

import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from dataclasses import fields as dataclass_fields

from .expression import submission_fingerprint
from .research_settlement import settlement_id as canonical_settlement_id
from .schema import CREATED_BY_VERSION, TRAJECTORY_VERSION

ACTIVE_EXECUTION_STATUSES = frozenset({"PENDING", "RUNNING", "SUBMITTING"})
UNKNOWN_STATUSES = frozenset({"SUBMIT_UNKNOWN", "UNKNOWN"})
UNRESOLVED_STATUSES = ACTIVE_EXECUTION_STATUSES | UNKNOWN_STATUSES
RECOVERABLE_STATUSES = ACTIVE_EXECUTION_STATUSES | frozenset({"UNKNOWN"})
TERMINAL_STATUSES = frozenset({"DONE", "FAILED", "SKIPPED", "SKIPPED_STALE", "SKIPPED_UNKNOWN"})
TRAJECTORY_REVISION_KEY = "trajectory_revision"
RESEARCH_SETTLED_REVISION = "RESEARCH_SETTLED"

IDENTITY_FIELDS = (
    "id", "round", "hypothesis_id", "expression", "settings", "fields_used",
    "datasets", "candidate_id", "proposal_id", "submission_fingerprint",
    "submission_started_at", "parent_expression", "lineage_id", "created_at",
    "optimization_decision_id", "parent_id",
)

OPERATOR_PROVENANCE_FIELDS = (
    "template_version", "template_mode", "template_branch_of",
    "template_fingerprint", "template_structural_fingerprint",
    "template_mechanism_fingerprint", "operator_role",
    "operator_role_mapping", "operator_realization_fingerprint",
    "operator_capability_fingerprint",
)


def execution_identity_projection(row):
    """Return only the immutable execution identity fields for a row."""
    def identity_value(row, key):
        value = row.get(key)
        if key == "submission_fingerprint" and not value:
            expression = row.get("expression")
            settings = row.get("settings")
            if isinstance(expression, str) and isinstance(settings, dict):
                return submission_fingerprint(expression, settings)
        return value

    if not isinstance(row, Mapping):
        row = row.to_dict()
    return {key: identity_value(row, key) for key in IDENTITY_FIELDS}


def same_execution_identity(left, right):
    """True when two rows describe the same executed Experiment."""
    return {
        **execution_identity_projection(left)
    } == {
        **execution_identity_projection(right)
    }


def research_settlement_identity(experiment):
    """Return the stable identity shared by derived settlement projections."""
    final = getattr(experiment, "final_outcome", None)
    if not isinstance(final, dict) or not final:
        experiment_id = getattr(experiment, "id", None) or "unknown-experiment"
        return f"experiment:{experiment_id}:outcome:unsettled"
    settlement_id = final.get("settlement_id")
    if not settlement_id and isinstance(final.get("settlement"), dict):
        settlement_id = final["settlement"].get("settlement_id")
    if settlement_id:
        return f"settlement:{settlement_id}"
    return f"settlement:{canonical_settlement_id(final)}"


def dataset_ref(value):
    """Normalize a proposal dataset entry to its durable string reference."""
    if isinstance(value, dict):
        value = value.get("id") or value.get("name")
    return str(value) if value is not None else None


@dataclass
class Experiment:
    round: int
    hypothesis_id: str
    expression: str
    settings: dict
    fields_used: list
    datasets: list | None = None
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    candidate_id: object = None
    proposal_id: object = None
    submission_fingerprint: object = None
    submission_started_at: object = None
    field_source: object = None
    field_understanding: object = None
    field_analysis: object = None
    field_hypothesis_basis: object = None
    operator_evidence: object = None
    template_id: object = None
    template_family: object = None
    template_version: object = None
    template_mode: object = None
    template_branch_of: object = None
    template_fingerprint: object = None
    template_structural_fingerprint: object = None
    template_mechanism_fingerprint: object = None
    operator_role: object = None
    operator_role_mapping: object = None
    operator_realization_fingerprint: object = None
    operator_capability_fingerprint: object = None
    template_stage_path: object = None
    template_ref: object = None
    template_slots: object = None
    search_evidence: object = None
    search_outcome: object = None
    provisional_outcome: object = None
    final_outcome: object = None
    robustness_evidence: object = None
    incremental_evidence: object = None
    pnl_evidence: object = None
    research_classification: object = None
    research_evidence_bundle: object = None
    submission_eligibility: object = None
    novelty_score: object = None
    allocation_arm: object = None
    allocation_key: object = None
    factory_session_id: object = None
    proposal_origin: object = None
    research_layer: object = None
    self_correlation: object = None
    status: str = "PENDING"
    metrics: object = None
    error: object = None
    alpha_id: object = None
    progress_url: object = None
    skip_record: object = None
    mutation: object = None
    lineage_id: object = None
    experiment_stage: object = None
    research_role: object = None
    change_type: object = None
    parent_expression: object = None
    parent_id: object = None
    child_economic_hypothesis: object = None
    changed_variable: object = None
    expected_failure_modes: list = field(default_factory=list)
    tuning_risk: object = None
    rationale: object = None
    direction: object = None
    economic_mechanism: object = None
    direction_transform: object = None
    self_correlation_impact: object = None
    expected_horizon: object = None
    falsification: object = None
    health: object = None
    yearly_evidence: object = None
    validation_plan: object = None
    validation_report: object = None
    validation_status: object = None
    elapsed_sec: object = None
    created_at: float = field(default_factory=time.time)
    optimization_decision_id: object = None

    def __post_init__(self):
        self.settings = dict(self.settings)
        self.fields_used = list(self.fields_used)
        self.datasets = [
            ref for ref in (dataset_ref(item) for item in (self.datasets or []))
            if ref
        ]
        self.expected_failure_modes = list(self.expected_failure_modes or [])

    def to_dict(self):
        data = {"schema_version": TRAJECTORY_VERSION, "created_by_version": CREATED_BY_VERSION}
        data.update({item.name: getattr(self, item.name) for item in dataclass_fields(self)})
        return data

    @classmethod
    def from_dict(cls, data):
        exp = cls(
            data["round"], data["hypothesis_id"], data["expression"],
            data["settings"], data["fields_used"], data.get("datasets"),
        )
        field_names = {item.name for item in dataclass_fields(cls)}
        for name in field_names:
            if name in {"round", "hypothesis_id", "expression", "settings", "fields_used", "datasets"}:
                continue
            if name in data:
                setattr(exp, name, data[name])
        exp.id = data["id"]
        exp.status = data["status"]
        exp.expected_failure_modes = data.get("expected_failure_modes") or []
        exp.created_at = data.get("created_at", 0)
        return exp
