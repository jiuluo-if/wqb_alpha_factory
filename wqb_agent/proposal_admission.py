"""Pure bounded admission summaries for proposal execution."""

from __future__ import annotations

from dataclasses import dataclass


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
