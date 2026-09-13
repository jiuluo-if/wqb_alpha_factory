"""Local, read-only takeover preflight for an Agent session."""

from __future__ import annotations

import json
import os

from .audit import audit_state
from .config import normalize_config
from .doctor import run_doctor
from .workspace_snapshot import read_workspace_snapshot

PROJECT_NAME = "wqb_alpha_factory"
SAFETY_INVARIANTS = (
    "BRAIN live response 高于 cache/fixture/report",
    "Simulation 只能沿唯一安全执行路径",
    "SUBMIT_UNKNOWN 不自动重发",
    "checkpoint 是 exactly-once / recovery boundary",
    "trajectory append-only，UNKNOWN/UNAVAILABLE 不升级为 PASS",
    "Alpha submission 由用户手动完成",
)
VALIDATION_COMMANDS = (
    "python scripts/run_targeted_tests.py --files <changed-files>",
    "python -m py_compile <changed-python-files>",
    "python -m ruff check <changed-python-files>",
)
TASK_ROUTES = {
    "general": {
        "files": [
            "wqb_agent/research_api.py",
            "wqb_agent/agent.py",
            "wqb_agent/proposal_contract.py",
        ],
        "tests": ["tests/test_research_api.py", "tests/test_proposal_safety.py"],
        "docs": ["AGENTS.md"],
    },
    "state-recovery": {
        "files": [
            "wqb_agent/preflight.py",
            "wqb_agent/audit.py",
            "wqb_agent/state.py",
        ],
        "tests": ["tests/test_research_constraints.py", "tests/test_runtime_safety.py"],
        "docs": ["AGENTS.md"],
    },
    "execution": {
        "files": ["wqb_agent/agent.py", "wqb_agent/simulator.py", "wqb_agent/client.py"],
        "tests": ["tests/test_simulator.py", "tests/test_recovery.py"],
        "docs": ["docs/RESEARCH_POLICY.md"],
    },
    "config": {
        "files": ["wqb_agent/config.py", "wqb_agent/agent.py"],
        "tests": ["tests/test_runtime_safety.py", "tests/test_agent_flow.py"],
        "docs": ["AGENTS.md"],
    },
    "expression": {
        "files": ["wqb_agent/expression.py", "wqb_agent/proposal_contract.py"],
        "tests": ["tests/test_research_api.py", "tests/test_proposal_safety.py"],
        "docs": ["docs/RESEARCH_POLICY.md"],
    },
    "runtime-composition": {
        "files": [
            "wqb_agent/runtime_components.py",
            "wqb_agent/config.py",
            "wqb_agent/agent.py",
        ],
        "tests": ["tests/test_runtime_safety.py", "tests/test_agent_flow.py"],
        "docs": ["AGENTS.md"],
    },
    "workspace-diagnostics": {
        "files": [
            "wqb_agent/workspace_snapshot.py",
            "wqb_agent/preflight.py",
            "wqb_agent/doctor.py",
        ],
        "tests": ["tests/test_agent_context.py", "tests/test_runtime_safety.py"],
        "docs": ["AGENTS.md"],
    },
    "checkpoint-recovery": {
        "files": [
            "wqb_agent/checkpoints.py",
            "wqb_agent/agent.py",
            "wqb_agent/workspace_snapshot.py",
        ],
        "tests": ["tests/test_recovery.py", "tests/test_agent_context.py"],
        "docs": ["AGENTS.md"],
    },
    "lifecycle-audit": {
        "files": [
            "wqb_agent/audit.py",
            "wqb_agent/workspace_snapshot.py",
            "wqb_agent/trial_ledger.py",
        ],
        "tests": ["tests/test_runtime_safety.py", "tests/test_workspace_snapshot.py"],
        "docs": ["docs/ARCHITECTURE_AGENT.md"],
    },
    "protocol-safety": {
        "files": [
            "wqb_agent/client.py",
            "wqb_agent/simulator.py",
            "wqb_agent/protocol.py",
        ],
        "tests": ["tests/test_simulator.py", "tests/test_protocol_truth.py"],
        "docs": ["AGENTS.md"],
    },
    "proposal-validation": {
        "files": [
            "wqb_agent/proposal_contract.py",
            "wqb_agent/expression.py",
            "wqb_agent/research_api.py",
        ],
        "tests": ["tests/test_proposal_safety.py", "tests/test_research_api.py"],
        "docs": ["docs/RESEARCH_POLICY.md"],
    },
}


def _task_route(task):
    name = str(task or "general").strip().lower()
    route = TASK_ROUTES.get(name, TASK_ROUTES["general"])
    return {
        "name": name if name in TASK_ROUTES else "general",
        "files": list(route["files"]),
        "tests": list(route["tests"]),
        "docs": list(route["docs"]),
    }


def _next_safe_action(preflight):
    if preflight.get("status") == "BLOCKED":
        blocking = ", ".join(preflight.get("blocking") or [])
        return f"先只读对账并恢复阻塞项：{blocking or '状态边界未知'}；不要启动 Simulation。"
    if (preflight.get("doctor") or {}).get("submit_unknown_count", 0):
        return "先对账 SUBMIT_UNKNOWN 的已知 progress URL，再决定是否继续；不要重发 POST。"
    return "先阅读 TASK → FILES 与 TASK → TESTS，再从既有 research_api/安全入口开始。"


def build_agent_context(raw_config, *, task="general", preflight=None):
    """Build a bounded, read-only context bundle from takeover preflight evidence."""
    evidence = preflight or run_takeover_preflight(raw_config)
    doctor = evidence.get("doctor") or {}
    trajectory = evidence.get("trajectory") or {}
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    status = evidence.get("status")
    if status == "READY":
        status = "SAFE"
    elif status not in {"SAFE", "REVIEW", "BLOCKED"}:
        status = "REVIEW"
    return {
        "project": {"name": PROJECT_NAME, "root": root},
        "safety_invariants": list(SAFETY_INVARIANTS),
        "workspace_status": status,
        "unfinished_checkpoints": list(evidence.get("unfinished_checkpoints") or []),
        "submit_unknown": int(doctor.get("submit_unknown_count") or 0),
        "latest_round": trajectory.get("latest_round"),
        "next_safe_action": _next_safe_action(evidence),
        "task": _task_route(task),
        "validation_commands": list(VALIDATION_COMMANDS),
        "network_write": False,
        "evidence": {
            "blocking": list(evidence.get("blocking") or []),
            "trajectory_records": trajectory.get("records", 0),
            "current_best": evidence.get("current_best"),
            "evidence_cache_entries": evidence.get("evidence_cache_entries", 0),
            "proposal_round": evidence.get("proposal_round"),
            "proposal_count": evidence.get("proposal_count", 0),
        },
    }


def render_agent_context(context, *, compact=False, json_mode=False):
    """Render context for a human Agent or a machine without adding state."""
    if json_mode:
        return json.dumps(context, ensure_ascii=False, indent=2, sort_keys=True)
    project = context["project"]
    task = context["task"]
    lines = [
        "PROJECT",
        f"  {project['name']} ({project['root']})",
        "SAFETY INVARIANTS",
        *[f"  - {item}" for item in context["safety_invariants"]],
        f"WORKSPACE STATUS: {context['workspace_status']}",
        "UNFINISHED CHECKPOINTS",
        *[f"  - {item}" for item in context["unfinished_checkpoints"] or ["none"]],
        f"SUBMIT_UNKNOWN: {context['submit_unknown']}",
        f"LATEST ROUND: {context['latest_round']}",
        f"NEXT SAFE ACTION: {context['next_safe_action']}",
        f"TASK → FILES ({task['name']})",
        *[f"  - {item}" for item in task["files"]],
        f"TASK → TESTS ({task['name']})",
        *[f"  - {item}" for item in task["tests"]],
        "VALIDATION COMMANDS",
        *[f"  - {item}" for item in context["validation_commands"]],
    ]
    if not compact:
        evidence = context["evidence"]
        lines.extend([
            f"TRAJECTORY RECORDS: {evidence['trajectory_records']}",
            f"EVIDENCE CACHE ENTRIES: {evidence['evidence_cache_entries']}",
            f"PROPOSAL ROUND: {evidence['proposal_round']}",
            f"PROPOSAL COUNT: {evidence['proposal_count']}",
        ])
    return "\n".join(lines)


def run_takeover_preflight(raw_config):
    """Return one bounded evidence bundle before suggestion or execution.

    The result is deliberately derived at call time and is never persisted.
    It tells a newly attached Agent what to read first without creating a
    second state machine or treating cache/report files as platform truth.
    """
    config = normalize_config(raw_config)
    state_dir = config.runtime.state_dir
    snapshot = read_workspace_snapshot(state_dir)
    doctor = run_doctor(config, offline=True, snapshot=snapshot)
    # RuntimeComponents deliberately constructs trajectory/TrialLedger with
    # persist=False.  Checkpoint is therefore the sole durable recovery
    # boundary; do not reject a clean terminal checkpoint because its
    # in-memory lifecycle projection is absent after takeover.
    state = audit_state(state_dir, snapshot=snapshot, lifecycle_persistent=False)
    unfinished = list(snapshot.unfinished_checkpoint_paths)
    blocking = list(unfinished)
    if not state.get("ok"):
        blocking.extend(state.get("errors") or [])
    status = "READY" if not blocking else "BLOCKED"
    return {
        "status": status,
        "network_write": False,
        "state_dir": state_dir,
        "blocking": sorted(set(blocking)),
        "doctor": doctor,
        "state": state,
        "unfinished_checkpoints": unfinished,
        "trajectory": {
            "records": snapshot.trajectory.records,
            "latest_round": snapshot.trajectory.latest_round,
        },
        "current_best": snapshot.current_best,
        "evidence_cache_entries": snapshot.evidence_cache_entries,
        "proposal_round": snapshot.proposal_round,
        "proposal_count": snapshot.proposal_count,
    }
