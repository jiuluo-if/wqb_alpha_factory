"""WQB research runtime.

Public agent-facing API lives in :mod:`wqb_agent.research_api`.
Other modules are implementation details unless a task explicitly requires
them.  The package root keeps only the agent-facing facade and research API.
"""

__all__ = [
    "Agent",
    "WQBClient",
    "SimulationSpec",
    "validate_simulation_spec",
    "execution_fingerprint",
    "simulate",
    "simulate_batch",
    "get_pending_executions",
    "resume_execution",
    "reconcile_execution",
    "get_capabilities",
    "get_alpha",
    "get_alpha_evidence",
    "compare_alphas",
    "get_remote_alpha",
    "get_remote_alpha_evidence",
    "find_similar_alphas",
    "simulation_quota",
    "refresh_remote_alphas",
    "list_remote_alphas",
    "remote_cache_status",
    "purge_remote_cache",
    "group_alphas",
    "find_alpha_duplicates",
    "inspect_state",
    "discover_fields",
    "generate_probes",
    "get_capabilities",
    "list_templates",
    "inspect_template",
    "get_operator_reference",
    "get_operator_syntax_reference",
    "run_experiment",
]


def __getattr__(name):
    """保持包级 API，同时避免导入纯工具时加载生产编排链。

    ``main.py`` 仍可使用 ``from wqb_agent import Agent, WQBClient``；只有
    访问这两个生产入口时才懒加载 Agent/HTTP client，模板工厂和审计工具
    因此不会因包初始化产生网络依赖。
    """
    if name == "Agent":
        from .agent import Agent

        globals()[name] = Agent
        return Agent
    if name == "WQBClient":
        from .client import WQBClient

        globals()[name] = WQBClient
        return WQBClient
    if name in {
        "SimulationSpec", "validate_simulation_spec", "execution_fingerprint",
        "simulate", "simulate_batch", "get_pending_executions", "resume_execution",
        "reconcile_execution", "get_alpha",
        "get_alpha_evidence", "compare_alphas", "get_remote_alpha",
        "get_remote_alpha_evidence", "find_similar_alphas", "inspect_state", "discover_fields",
        "generate_probes", "get_capabilities", "list_templates", "inspect_template",
        "simulation_quota", "refresh_remote_alphas", "list_remote_alphas",
        "remote_cache_status", "purge_remote_cache", "group_alphas",
        "find_alpha_duplicates",
        "get_operator_reference", "get_operator_syntax_reference",
        "run_experiment",
    }:
        from . import research_api

        value = getattr(research_api, name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
