"""Read-only local health diagnostics; no client construction or network."""

from __future__ import annotations

import os

from .config import normalize_config
from .diagnostics import DiagnosticEvent
from .workspace_snapshot import is_doctor_artifact, read_workspace_snapshot


def run_doctor(raw_config, *, offline=True, snapshot=None):
    parsed = normalize_config(raw_config)
    state_dir = parsed.runtime.state_dir
    snapshot = snapshot or read_workspace_snapshot(state_dir)
    ledger_info = snapshot.inventory.info("trial_ledger.jsonl")
    ledger_path = os.path.join(state_dir, "trial_ledger.jsonl")
    result = {
        "config_valid": True,
        "offline": bool(offline),
        "state_dir": state_dir,
        "state_dir_writable": os.path.isdir(state_dir) and os.access(state_dir, os.W_OK),
        "ledger_readable": ledger_info.readable,
        "ledger_status": (
            "MISSING" if not ledger_info.exists
            else "READABLE" if ledger_info.readable
            else "UNREADABLE"
        ),
        "checkpoint_consistency": "PASS",
        "schema_versions": {},
        "operator_reference": os.path.exists(os.path.join(os.path.dirname(__file__), "..", "docs", "reference", "OPERATORS_CHEATSHEET.md")),
        "field_cache_status": (
            "PRESENT" if snapshot.inventory.info("fields_cache.json").exists
            else "MISSING"
        ),
        "unresolved_simulation_count": 0,
        "submit_unknown_count": 0,
        "pending_validation_count": 0,
        "pnl_capability": "UNAVAILABLE",
        "incremental_capability": "UNAVAILABLE",
    }
    unresolved = 0
    submit_unknown = 0
    for record in snapshot.checkpoint_records:
        checkpoint = record["checkpoint"]
        if record["malformed"] or checkpoint.get("complete") is not True:
            result["checkpoint_consistency"] = "FAIL"
            unresolved += 1
        for row in checkpoint.get("experiments") or []:
            if isinstance(row, dict) and row.get("status") == "SUBMIT_UNKNOWN":
                submit_unknown += 1
    submit_unknown += snapshot.trajectory.submit_unknown_count
    pending_validation = snapshot.trajectory.pending_validation_count
    result["unresolved_simulation_count"] = unresolved
    result["submit_unknown_count"] = submit_unknown
    result["pending_validation_count"] = pending_validation
    if unresolved:
        result["checkpoint_consistency"] = "UNRESOLVED"
    diagnostics = []
    if not result["state_dir_writable"]:
        diagnostics.append(DiagnosticEvent(
            "STATE_DIR_NOT_WRITABLE", "ERROR", "doctor", message=state_dir
        ).as_dict())
    if result["ledger_status"] == "MISSING":
        diagnostics.append(DiagnosticEvent(
            "LEDGER_MISSING", "WARN", "trial_ledger",
            message="append-only TrialLedger 尚未建立，无法完成 checkpoint 对账"
        ).as_dict())
    elif result["ledger_status"] == "UNREADABLE":
        diagnostics.append(DiagnosticEvent(
            "LEDGER_UNREADABLE", "ERROR", "trial_ledger",
            message=ledger_path
        ).as_dict())
    if result["pnl_capability"] != "LIVE_VERIFIED":
        diagnostics.append(DiagnosticEvent(
            "PNL_CAPABILITY_UNAVAILABLE", "WARN", "incremental_value",
            message="仅允许记录 UNAVAILABLE，不生成行为相关性"
        ).as_dict())
    for name, code_name, invalid_rows in (
        ("trajectory", "TRAJECTORY_EVIDENCE_DEGRADED", snapshot.trajectory.invalid_rows),
        ("trial_ledger", "LEDGER_EVIDENCE_DEGRADED", snapshot.ledger.invalid_rows),
        ("validation_reports", "VALIDATION_EVIDENCE_DEGRADED", snapshot.validation.invalid_rows),
    ):
        if invalid_rows:
            diagnostics.append(DiagnosticEvent(
                code_name, "WARN", name,
                message=f"发现 {invalid_rows} 个无效 JSONL 行；已跳过并保留后续有效行"
            ).as_dict())
    result["diagnostics"] = diagnostics
    for info in snapshot.inventory.entries:
        name = info.name
        if not is_doctor_artifact(name) or not info.exists:
            continue
        result["schema_versions"][name] = info.schema_version or "LEGACY"
    return result
