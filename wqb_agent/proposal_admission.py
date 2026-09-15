"""Pure bounded admission summaries for proposal execution."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from .expression import canonical_expression, submission_fingerprint


@dataclass(frozen=True)
class ProposalAdmission:
    """Pure early admission result; workflow owns trial/rejection mutation."""

    status: str
    expression: str
    reason_code: str | None = None
    reason: str | None = None
    effective_settings: object = None
    execution_fingerprint: str | None = None
    research_key: str | None = None


@dataclass(frozen=True)
class AdmissionFacts:
    """Immutable request-scoped facts collected before candidate admission."""

    research_seen: frozenset[str]
    batch_execution_fingerprints: frozenset[str]
    durable_proposal_bindings: dict[str, set[str]]
    unresolved_identities: frozenset[str]
    discovered_profiles: dict[str, dict[str, Any]]
    proposal_field_profiles: tuple[dict[str, Any], ...]
    field_types: dict[str, Any]
    parent_rows: dict[str, dict[str, Any]]
    legacy_parent_candidates: dict[str, tuple[Any, ...]]

    def __post_init__(self):
        for name in (
            "durable_proposal_bindings", "discovered_profiles", "field_types",
            "parent_rows", "legacy_parent_candidates",
        ):
            value = getattr(self, name)
            object.__setattr__(self, name, MappingProxyType(dict(value)))
        object.__setattr__(self, "research_seen", frozenset(self.research_seen))
        object.__setattr__(
            self, "batch_execution_fingerprints",
            frozenset(self.batch_execution_fingerprints),
        )
        object.__setattr__(
            self, "unresolved_identities", frozenset(self.unresolved_identities)
        )
        object.__setattr__(
            self, "proposal_field_profiles", tuple(self.proposal_field_profiles)
        )


def admit_proposal(
    proposal, *, settings_resolver, batch_execution_fingerprints,
    research_seen,
):
    """Classify schema/settings/local duplicate gates without touching owners."""
    if not isinstance(proposal, dict):
        return ProposalAdmission("REJECTED", str(proposal or ""), "NOT_OBJECT", "proposal 必须是对象")
    expression = (proposal.get("expression") or "").strip()
    if not expression:
        return ProposalAdmission("REJECTED", expression, "MISSING_EXPRESSION", "expression 不能为空")
    try:
        settings = settings_resolver(proposal.get("settings"))
    except ValueError as exc:
        return ProposalAdmission("REJECTED", expression, "INVALID_SETTINGS", str(exc))
    fingerprint = submission_fingerprint(expression, settings)
    if fingerprint in batch_execution_fingerprints:
        return ProposalAdmission(
            "SKIPPED", expression, "DUPLICATE_EFFECTIVE_EXECUTION",
            "相同的完整 effective Simulation settings 已存在，禁止重复执行",
            settings, fingerprint,
        )
    research_key = (
        "settings::" + fingerprint if proposal.get("settings")
        else canonical_expression(expression)
    )
    if research_key in research_seen:
        return ProposalAdmission(
            "SKIPPED", expression, "DUPLICATE_LOCAL",
            "同一研究表达式与设置已在本批出现", settings, fingerprint, research_key,
        )
    return ProposalAdmission("ACCEPTED", expression, effective_settings=settings,
                             execution_fingerprint=fingerprint, research_key=research_key)


@dataclass(frozen=True)
class IdentityAdmission:
    """Pure decision for the proposal-to-execution identity fence."""

    status: str
    proposal_id: str
    reason_code: str | None = None
    reason: str | None = None
    collision: bool = False


def admit_execution_identity(
    proposal,
    execution_fingerprint,
    *,
    unresolved_identities,
    durable_bindings,
    batch_bindings,
    conflicting_proposal_ids,
):
    """Classify one proposal without mutating any durable or workflow owner."""
    if execution_fingerprint in unresolved_identities:
        return IdentityAdmission(
            "REJECTED",
            "",
            "UNRESOLVED_SUBMISSION_IDENTITY",
            "同一 submission_fingerprint 仍存在未决远程执行；force-new-round 不能绕过 execution identity fence",
        )
    proposal_id = str(proposal.get("proposal_id") or "p-" + execution_fingerprint[:16])
    durable_fingerprints = durable_bindings.get(proposal_id, set())
    if durable_fingerprints and execution_fingerprint not in durable_fingerprints:
        return IdentityAdmission(
            "REJECTED",
            proposal_id,
            "PROPOSAL_ID_REBIND",
            "proposal_id 已经绑定其他 durable execution identity",
        )
    previous_fingerprint = batch_bindings.get(proposal_id)
    if proposal_id in conflicting_proposal_ids or (
        previous_fingerprint is not None
        and previous_fingerprint != execution_fingerprint
    ):
        return IdentityAdmission(
            "REJECTED",
            proposal_id,
            "PROPOSAL_ID_EXECUTION_COLLISION",
            "同一 batch 的 proposal_id 绑定多个 execution identity",
            collision=True,
        )
    return IdentityAdmission("ACCEPTED", proposal_id)


def rejection_reason_counts(
    rejected, skipped, diversity_rejected, settings_rejected, budget_rejected
):
    return {
        key: count
        for key, count in {
            "PREFLIGHT_REJECTED": len(rejected),
            "DUPLICATE_LOCAL": len(skipped),
            "DIVERSITY_REJECTED": len(diversity_rejected),
            "INVALID_SETTINGS": len(settings_rejected),
            "BATCH_CAP": len(budget_rejected),
        }.items()
        if count
    }
