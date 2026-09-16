"""WQB research runtime.

Public agent-facing API lives in :mod:`wqb_agent.research_api`.
Other modules are implementation details unless a task explicitly requires
them. The package root exposes only the Remote-First research API.
"""

__all__ = [
    "WQBClient",
    "SimulationSpec",
    "validate_simulation_spec",
    "execution_fingerprint",
    "simulate",
    "simulate_batch",
    "get_pending_executions",
    "resume_execution",
    "reconcile_execution",
    "get_operators",
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
    "find_duplicate_alphas",
    "preview_alpha_colors",
    "sync_alpha_colors",
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
    """Load the Remote-First API lazily without legacy runtime imports."""
    if name == "WQBClient":
        from .client import WQBClient

        globals()[name] = WQBClient
        return WQBClient
    if name in {
        "SimulationSpec", "validate_simulation_spec", "execution_fingerprint",
        "simulate", "simulate_batch", "get_pending_executions", "resume_execution",
        "reconcile_execution", "get_alpha",
        "get_alpha_evidence", "compare_alphas", "get_remote_alpha",
        "get_remote_alpha_evidence", "find_similar_alphas", "discover_fields",
        "generate_probes", "get_capabilities", "get_operators", "list_templates", "inspect_template",
        "simulation_quota", "refresh_remote_alphas", "list_remote_alphas",
        "remote_cache_status", "purge_remote_cache", "group_alphas",
        "find_alpha_duplicates", "find_duplicate_alphas", "preview_alpha_colors",
        "sync_alpha_colors",
        "get_operator_reference", "get_operator_syntax_reference",
        "run_experiment",
    }:
        from . import research_api

        value = getattr(research_api, name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
