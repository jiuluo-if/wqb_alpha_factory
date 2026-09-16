"""Read-only handoff for the Remote-First research tools."""

from __future__ import annotations

import json
import os

from .audit import audit_state
from .config import normalize_config
from .doctor import run_doctor

PROJECT_NAME = "wqb_alpha_factory"
SAFETY_INVARIANTS = (
    "BRAIN live response 是 Alpha/Simulation 事实源",
    "Simulation 只能沿 research_api → SimulationGateway → Simulator → WQBClient",
    "SUBMIT_UNKNOWN 不自动重发",
    "known progress_url 只轮询同一远端任务",
    "Alpha submission 由用户手动完成",
)
VALIDATION_COMMANDS = (
    "python scripts/run_targeted_tests.py --files <changed-files>",
    "python -m compileall -q wqb_agent scripts tests",
    "python -m ruff check .",
)
TASK_ROUTES = {
    "general": {
        "files": ["wqb_agent/research_api.py", "wqb_agent/simulation_gateway.py"],
        "tests": ["tests/test_research_api.py", "tests/test_simulation_gateway.py"],
        "docs": ["AGENTS.md"],
    },
    "execution": {
        "files": ["wqb_agent/simulation_gateway.py", "wqb_agent/simulator.py", "wqb_agent/client.py"],
        "tests": ["tests/test_simulation_gateway.py", "tests/test_simulator.py"],
        "docs": ["docs/RESEARCH_POLICY.md"],
    },
    "remote-alpha": {
        "files": ["wqb_agent/remote_alpha_repository.py", "wqb_agent/optimization_interfaces.py"],
        "tests": ["tests/test_remote_alpha_repository.py", "tests/test_simulation_gateway.py"],
        "docs": ["docs/STATE_LAYOUT.md"],
    },
    "factory": {
        "files": ["wqb_agent/alpha_factory.py", "wqb_agent/alpha_templates/"],
        "tests": ["tests/test_factory_batch_contract.py", "tests/test_research_api.py"],
        "docs": ["wqb_agent/AGENTS.md"],
    },
}


def _task_route(task):
    name = str(task or "general").strip().lower()
    route = TASK_ROUTES.get(name, TASK_ROUTES["general"])
    return {"name": name if name in TASK_ROUTES else "general", **route}


def _next_safe_action(evidence):
    guard = evidence.get("doctor", {}).get("execution_guard", {})
    if guard.get("submit_unknown_count"):
        return "只读对账 SUBMIT_UNKNOWN 的已知 progress URL；不得重发 POST。"
    if guard.get("active_count"):
        return "按 fingerprint 查询并恢复已有远端任务；不要创建重复 POST。"
    return "从 discover_fields / generate_probes 开始，审阅后调用 simulate。"


def run_takeover_preflight(raw_config):
    config = normalize_config(raw_config)
    state_dir = config.runtime.state_dir
    doctor = run_doctor(config, offline=True)
    state = audit_state(state_dir)
    guard = doctor["execution_guard"]
    blocking = list(state.get("errors") or [])
    if guard.get("submit_unknown_count"):
        blocking.append("SUBMIT_UNKNOWN_REQUIRES_RECONCILIATION")
    status = "READY" if not blocking else "BLOCKED"
    return {
        "status": status,
        "network_write": False,
        "state_dir": state_dir,
        "blocking": sorted(set(blocking)),
        "doctor": doctor,
        "state": state,
        "unfinished_executions": guard.get("active_count", 0),
        "cache": doctor.get("cache", {}),
    }


def build_agent_context(raw_config, *, task="general", preflight=None):
    evidence = preflight or run_takeover_preflight(raw_config)
    doctor = evidence.get("doctor") or {}
    route = _task_route(task)
    return {
        "project": {
            "name": PROJECT_NAME,
            "root": os.path.abspath(os.path.join(os.path.dirname(__file__), "..")),
        },
        "safety_invariants": list(SAFETY_INVARIANTS),
        "workspace_status": "SAFE" if evidence.get("status") == "READY" else "BLOCKED",
        "unfinished_executions": evidence.get("unfinished_executions", 0),
        "submit_unknown": doctor.get("execution_guard", {}).get("submit_unknown_count", 0),
        "next_safe_action": _next_safe_action(evidence),
        "task": route,
        "validation_commands": list(VALIDATION_COMMANDS),
        "network_write": False,
        "evidence": {"blocking": list(evidence.get("blocking") or []),
                     "cache": evidence.get("cache", {}),
                     "execution_guard": doctor.get("execution_guard", {})},
    }


def render_agent_context(context, *, compact=False, json_mode=False):
    if json_mode:
        return json.dumps(context, ensure_ascii=False, indent=2, sort_keys=True)
    project = context["project"]
    task = context["task"]
    lines = [
        "PROJECT", f"  {project['name']} ({project['root']})",
        "SAFETY INVARIANTS", *[f"  - {item}" for item in context["safety_invariants"]],
        f"WORKSPACE STATUS: {context['workspace_status']}",
        f"ACTIVE EXECUTIONS: {context['unfinished_executions']}",
        f"SUBMIT_UNKNOWN: {context['submit_unknown']}",
        f"NEXT SAFE ACTION: {context['next_safe_action']}",
        f"TASK → FILES ({task['name']})", *[f"  - {item}" for item in task["files"]],
        f"TASK → TESTS ({task['name']})", *[f"  - {item}" for item in task["tests"]],
        "VALIDATION COMMANDS", *[f"  - {item}" for item in context["validation_commands"]],
    ]
    if not compact:
        lines.append(f"CACHE: {context['evidence']['cache'].get('freshness', 'UNKNOWN')}")
    return "\n".join(lines)
