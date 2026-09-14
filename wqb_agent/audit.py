"""Small, read-only invariant audit for local state."""

from __future__ import annotations

from .schema import ARTIFACT_SCHEMAS
from .state import TERMINAL_STATUSES
from .workspace_snapshot import read_workspace_snapshot


def audit_state(state_dir, *, snapshot=None, lifecycle_persistent=True):
    """Audit durable state and, when enabled, durable lifecycle projections.

    Production Agent owns durable trajectory and TrialLedger artifacts.  The
    persistent mode therefore checks their lifecycle parity with checkpoints;
    the opt-out remains only for explicit legacy/ephemeral callers.
    """
    snapshot = snapshot or read_workspace_snapshot(state_dir)
    errors = []
    warnings = []
    findings = []

    def record(code, source, *, legacy_codes=(), severity="ERROR", **details):
        target = errors if severity == "ERROR" else warnings
        for error_code in (*legacy_codes, code):
            if error_code not in target:
                target.append(error_code)
        finding = {"code": code, "source": source, "severity": severity}
        finding.update(details)
        if finding not in findings:
            findings.append(finding)

    trajectory_summary = snapshot.trajectory
    trajectory_ids = set(trajectory_summary.trajectory_ids)
    observed_committed = set(trajectory_summary.observed_committed)
    observed_submitted = set(trajectory_summary.observed_submitted)
    observed_simulation_submitted = set(trajectory_summary.observed_simulation_submitted)
    observed_simulation_settled = set(trajectory_summary.observed_simulation_settled)
    observed_research_settled = set(trajectory_summary.observed_research_settled)
    checkpoint_terminal = {}
    recoverable_status_behind = set(
        trajectory_summary.checkpoint_status_behind_canonical_terminal
    )
    ledger_summary = snapshot.ledger
    ledger_committed = set(ledger_summary.committed)
    ledger_submitted = set(ledger_summary.submitted)
    ledger_simulation_submitted = set(ledger_summary.simulation_submitted)
    ledger_simulation_settled = set(ledger_summary.simulation_settled)
    ledger_research_settled = set(ledger_summary.research_settled)
    if trajectory_summary.invalid_rows:
        record("malformed_trajectory_evidence", "trajectory",
               invalid_rows=trajectory_summary.invalid_rows)
    if trajectory_summary.unsupported_schema_rows:
        record("unsupported_schema", "trajectory",
               rows=trajectory_summary.unsupported_schema_rows)
    if trajectory_summary.unknown_phase_rows:
        record("unknown_trajectory_phase", "trajectory",
               rows=trajectory_summary.unknown_phase_rows)
    if trajectory_summary.incomplete_rows:
        record("incomplete_trajectory_evidence", "trajectory",
               rows=trajectory_summary.incomplete_rows)
    if trajectory_summary.identity_mismatch_rows:
        record(
            "trajectory_execution_identity_mismatch",
            "trajectory",
            rows=trajectory_summary.identity_mismatch_rows,
        )
    for code, count in (trajectory_summary.parent_identity_issues or {}).items():
        record(code, "trajectory", rows=count)
    if trajectory_summary.duplicate_lifecycle_phases:
        record("duplicate_trajectory_lifecycle_phase", "trajectory",
               rows=trajectory_summary.duplicate_lifecycle_phases)
    if trajectory_summary.phase_order_violations:
        record("trajectory_projection_order", "trajectory",
               violations=[list(item) for item in trajectory_summary.phase_order_violations])
    if ledger_summary.invalid_rows:
        record("malformed_ledger_evidence", "trial_ledger",
               invalid_rows=ledger_summary.invalid_rows)
    if ledger_summary.unsupported_schema_rows:
        record("unsupported_schema", "trial_ledger",
               rows=ledger_summary.unsupported_schema_rows)
    if ledger_summary.unknown_phase_rows:
        record("unknown_ledger_phase", "trial_ledger",
               rows=ledger_summary.unknown_phase_rows)
    if ledger_summary.incomplete_rows:
        record("incomplete_ledger_evidence", "trial_ledger",
               rows=ledger_summary.incomplete_rows)
    if ledger_summary.duplicate_lifecycle_phases:
        record("duplicate_ledger_lifecycle_phase", "trial_ledger",
               rows=ledger_summary.duplicate_lifecycle_phases)
    if ledger_summary.phase_order_violations:
        record("ledger_lifecycle_order", "trial_ledger",
               violations=[list(item) for item in ledger_summary.phase_order_violations])
    for code, count in (ledger_summary.lifecycle_identity_drift or {}).items():
        record(code, "trial_ledger", rows=count)
    if trajectory_summary.duplicate_remote_execution_projection:
        record(
            "DUPLICATE_REMOTE_EXECUTION_PROJECTION",
            "trajectory",
            collision_count=trajectory_summary.duplicate_remote_execution_projection,
            round_count=len(trajectory_summary.duplicate_remote_execution_projection_rounds),
        )
    if trajectory_summary.terminal_checkpoint_missing_canonical_evidence:
        record(
            "TERMINAL_CHECKPOINT_MISSING_CANONICAL_EVIDENCE",
            "checkpoint+trajectory",
            experiment_ids=list(trajectory_summary.terminal_checkpoint_missing_canonical_evidence),
        )
    if trajectory_summary.checkpoint_trajectory_identity_mismatch:
        record(
            "CHECKPOINT_TRAJECTORY_IDENTITY_MISMATCH",
            "checkpoint+trajectory",
            experiment_ids=list(trajectory_summary.checkpoint_trajectory_identity_mismatch),
        )
    if trajectory_summary.done_canonical_result_evidence_incomplete:
        record(
            "DONE_CANONICAL_RESULT_EVIDENCE_INCOMPLETE",
            "trajectory+checkpoint",
            experiment_ids=list(trajectory_summary.done_canonical_result_evidence_incomplete),
        )
    if trajectory_summary.checkpoint_status_behind_canonical_terminal:
        record(
            "CHECKPOINT_STATUS_BEHIND_CANONICAL_TERMINAL",
            "checkpoint+trajectory",
            severity="WARN",
            experiment_ids=list(trajectory_summary.checkpoint_status_behind_canonical_terminal),
        )
    if ledger_summary.proposal_id_execution_rebind:
        record(
            "proposal_id_execution_rebind", "trial_ledger",
            rows=ledger_summary.proposal_id_execution_rebind,
        )
    if snapshot.validation.invalid_rows:
        record("malformed_validation_evidence", "validation_reports",
               severity="WARN", invalid_rows=snapshot.validation.invalid_rows)
    if snapshot.validation.unsupported_schema_rows:
        record("unsupported_schema", "validation_reports", severity="WARN",
               rows=snapshot.validation.unsupported_schema_rows)
    for info in snapshot.inventory.entries:
        expected = ARTIFACT_SCHEMAS.get(info.name)
        if (
            expected is not None
            and isinstance(info.schema_version, int)
            and info.schema_version > expected
        ):
            record("unsupported_schema", info.name,
                   actual_schema=info.schema_version, expected_schema=expected)
    if (
        trajectory_summary.duplicate_observed_settlements
        or ledger_summary.duplicate_settlements
    ):
        record("duplicate_settlement", "trajectory+trial_ledger")
    if trajectory_summary.missing_alpha_id_rows:
        record("missing_trajectory_alpha_id", "trajectory",
               rows=trajectory_summary.missing_alpha_id_rows)
    if ledger_summary.missing_alpha_id_rows:
        record("missing_ledger_alpha_id", "trial_ledger",
               rows=ledger_summary.missing_alpha_id_rows)
    if snapshot.unresolved_submission_identity_collisions:
        record(
            "duplicate_unresolved_submission_identity",
            "checkpoint",
            collisions=snapshot.unresolved_submission_identity_collisions,
        )
    for checkpoint_record in snapshot.checkpoint_records:
        if checkpoint_record["malformed"]:
            validation_code = checkpoint_record.get("validation_code")
            checkpoint_codes = {
                "CHECKPOINT_COMPLETE_TYPE_INVALID":
                    "checkpoint_complete_type_invalid",
                "CHECKPOINT_COMPLETE_WITH_UNRESOLVED_EXECUTION":
                    "checkpoint_complete_with_unresolved_execution",
                "CHECKPOINT_STATUS_INVALID":
                    "checkpoint_status_invalid",
                "CHECKPOINT_REQUIRED_IDENTITY_MISSING":
                    "checkpoint_identity_invalid",
                "CHECKPOINT_REQUIRED_IDENTITY_INVALID":
                    "checkpoint_identity_invalid",
                "CHECKPOINT_ROUND_IDENTITY_MISMATCH":
                    "checkpoint_round_identity_mismatch",
                "CHECKPOINT_HYPOTHESIS_IDENTITY_INVALID":
                    "checkpoint_hypothesis_identity_invalid",
                "CHECKPOINT_HYPOTHESIS_IDENTITY_MISMATCH":
                    "checkpoint_hypothesis_identity_mismatch",
                "CHECKPOINT_DUPLICATE_EXPERIMENT_ID":
                    "checkpoint_duplicate_experiment_id",
                "CHECKPOINT_DUPLICATE_SUBMISSION_IDENTITY":
                    "checkpoint_duplicate_submission_identity",
                "CHECKPOINT_DUPLICATE_PROPOSAL_ID":
                    "checkpoint_duplicate_proposal_id",
                "CHECKPOINT_PROGRESS_IDENTITY_COLLISION":
                    "checkpoint_progress_identity_collision",
                "CHECKPOINT_SUBMISSION_IDENTITY_MISMATCH":
                    "checkpoint_submission_fingerprint_mismatch",
            }
            if validation_code in checkpoint_codes:
                record(checkpoint_codes[validation_code], "checkpoint",
                       round=checkpoint_record["round_no"])
            checkpoint_ids = {
                str(row.get("id"))
                for row in checkpoint_record["checkpoint"].get("experiments") or []
                if isinstance(row, dict) and row.get("id")
            }
            if (
                checkpoint_record["checkpoint"].get("complete") is not True
                and not checkpoint_ids
                or (
                    checkpoint_record["checkpoint"].get("complete") is not True
                    and not checkpoint_ids.issubset(recoverable_status_behind)
                )
            ):
                record("unverifiable_unfinished_execution_identity", "checkpoint",
                       round=checkpoint_record["round_no"])
            record("checkpoint_unreadable", "checkpoint",
                   round=checkpoint_record["round_no"])
        for row in checkpoint_record["checkpoint"].get("experiments") or []:
            if isinstance(row, dict):
                missing_legacy = [
                    key for key in ("candidate_id", "datasets", "created_at")
                    if row.get(key) in (None, "", [], {})
                ]
                if str(row.get("experiment_stage") or "").upper() in {"CHILD", "ROBUSTNESS"}:
                    if row.get("parent_id") in (None, ""):
                        missing_legacy.append("parent_id")
                if missing_legacy:
                    record("legacy_identity_incomplete", "checkpoint",
                           severity="WARN", round=checkpoint_record["round_no"])
            if isinstance(row, dict) and row.get("status") == "PENDING" and not row.get("proposal_id"):
                record("phantom_reservation", "checkpoint",
                       path=checkpoint_record["path"])
                break
            if not isinstance(row, dict):
                continue
            status = str(row.get("status") or "").upper()
            proposal_id = row.get("proposal_id")
            if proposal_id and status in TERMINAL_STATUSES:
                checkpoint_terminal[str(proposal_id)] = status
            if status == "SUBMIT_UNKNOWN" and row.get("budget_held") is False:
                record("unknown_not_budget_held", "checkpoint",
                       proposal_ids=[str(proposal_id)] if proposal_id else [])
            if status in {"DONE", "FAILED", "SKIPPED"} and row.get("reserved") is True:
                record("terminal_occupies_arm", "checkpoint",
                       proposal_ids=[str(proposal_id)] if proposal_id else [])
    ledger_order_broken = (
        not ledger_simulation_submitted.issubset(ledger_committed)
        or not ledger_simulation_settled.issubset(ledger_simulation_submitted)
        or not ledger_research_settled.issubset(ledger_simulation_submitted)
    )
    if ledger_order_broken and not ledger_summary.phase_order_violations:
        record("ledger_lifecycle_order", "trial_ledger", legacy_codes=("lifecycle_order",))
    elif ledger_order_broken:
        if "lifecycle_order" not in errors:
            errors.append("lifecycle_order")
    trajectory_order_broken = (
        not observed_simulation_submitted.issubset(observed_committed)
        or not observed_simulation_settled.issubset(observed_simulation_submitted)
        or not observed_research_settled.issubset(observed_simulation_submitted)
    )
    if trajectory_order_broken and not trajectory_summary.phase_order_violations:
        record("trajectory_projection_order", "trajectory",
               legacy_codes=("trajectory_lifecycle_order",))
    elif trajectory_order_broken:
        if "trajectory_lifecycle_order" not in errors:
            errors.append("trajectory_lifecycle_order")
    phase_sets = (
        ("simulation_committed", observed_committed, ledger_committed),
        ("simulation_submitted", observed_simulation_submitted, ledger_simulation_submitted),
        ("simulation_settled", observed_simulation_settled, ledger_simulation_settled),
        ("research_outcome_settled", observed_research_settled, ledger_research_settled),
    )
    for phase, observed, authoritative in phase_sets:
        missing = sorted(observed - authoritative)
        if missing:
            record("trajectory_lifecycle_missing_ledger", "trajectory→trial_ledger",
                   phase=phase, proposal_ids=missing)
        observed_for_phase = {
            "simulation_committed": observed_committed,
            "simulation_submitted": observed_simulation_submitted,
            "simulation_settled": observed_simulation_settled,
            "research_outcome_settled": observed_research_settled,
        }[phase]
        missing_projection = sorted(authoritative - observed_for_phase)
        if missing_projection:
            record("ledger_lifecycle_missing_trajectory", "trial_ledger→trajectory",
                   phase=phase, proposal_ids=missing_projection)
    if lifecycle_persistent:
        if checkpoint_terminal and not snapshot.inventory.info("trial_ledger.jsonl").exists:
            record("ledger_missing", "checkpoint+trial_ledger")
        mismatch = sorted(
            proposal_id for proposal_id in checkpoint_terminal
            if proposal_id not in ledger_simulation_settled
        )
        if mismatch:
            record("checkpoint_ledger_mismatch", "checkpoint+trial_ledger",
                   proposal_ids=mismatch)

    for parent_id in sorted(snapshot.validation.parent_ids):
        if parent_id not in trajectory_ids:
            record("orphan_validation_parent", "validation_reports",
                   proposal_ids=[parent_id])
    for plan_id, nested_plan_id in snapshot.validation.plan_pairs:
        if plan_id != nested_plan_id:
            record("validation_plan_mismatch", "validation_reports",
                   plan_id=plan_id, nested_plan_id=nested_plan_id)
    if snapshot.submission_pool.unverifiable_candidates:
        record("unverifiable_submission", "submission_pool",
               rows=snapshot.submission_pool.unverifiable_candidates)
    trajectory_identity_sources = {
        "alpha_id": set(trajectory_summary.trajectory_alpha_ids),
        "proposal_id": set(trajectory_summary.trajectory_proposal_ids),
    }
    for index, identities in enumerate(snapshot.submission_pool.candidate_identities):
        if not identities:
            continue
        sources = ()
        if index < len(snapshot.submission_pool.candidate_identity_sources):
            sources = snapshot.submission_pool.candidate_identity_sources[index]
        if sources:
            linked = any(
                value in trajectory_identity_sources.get(kind, set())
                for kind, value in sources
            )
        else:
            linked = any(identity in trajectory_ids for identity in identities)
        if not linked:
            record("orphan_submission", "submission_pool",
                   identities=sorted(identities))
            break
    unique_errors = list(dict.fromkeys(errors))
    return {"ok": not unique_errors, "errors": unique_errors,
            "warnings": list(dict.fromkeys(warnings)),
             "blocking": bool(unique_errors), "findings": findings,
             "lifecycle_persistent": bool(lifecycle_persistent),
            "committed": len(ledger_committed), "submitted": len(ledger_submitted),
            "simulation_submitted": len(ledger_simulation_submitted),
            "settled": len(ledger_research_settled),
            "trajectory_observed_committed": len(observed_committed),
            "trajectory_observed_submitted": len(observed_submitted),
            "trajectory_observed_simulation_submitted": len(observed_simulation_submitted),
            "trajectory_observed_simulation_settled": len(observed_simulation_settled),
            "trajectory_observed_research_settled": len(observed_research_settled)}
