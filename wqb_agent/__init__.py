"""WQB research runtime.

Public agent-facing API lives in :mod:`wqb_agent.research_api`.
Other modules are implementation details unless a task explicitly requires
them.  The package root keeps only the agent-facing facade and research API.
"""

__all__ = [
    "Agent",
    "WQBClient",
    "ExperimentSpec",
    "SimulationSpec",
    "simulate",
    "simulate_batch",
    "get_pending_executions",
    "resume_execution",
    "get_alpha",
    "get_alpha_evidence",
    "compare_alphas",
    "get_remote_alpha",
    "get_remote_alpha_evidence",
    "inspect_state",
    "discover_fields",
    "get_operator_reference",
    "get_operator_syntax_reference",
    "run_experiment",
    "get_experiment",
    "compare_experiments",
    "search_history",
    "reconcile",
    "inspect_optimizer_parents",
    "inspect_optimizer_context",
    "propose_optimization",
    "materialize_targeted_batch",
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
        "ExperimentSpec", "SimulationSpec", "simulate", "simulate_batch",
        "get_pending_executions", "resume_execution", "get_alpha",
        "get_alpha_evidence", "compare_alphas", "get_remote_alpha",
        "get_remote_alpha_evidence", "inspect_state", "discover_fields",
        "get_operator_reference", "get_operator_syntax_reference",
        "run_experiment", "get_experiment",
        "compare_experiments", "search_history", "reconcile",
        "inspect_optimizer_parents", "inspect_optimizer_context",
        "propose_optimization", "materialize_targeted_batch",
    }:
        from . import research_api

        value = getattr(research_api, name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
