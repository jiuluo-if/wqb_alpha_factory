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
    "simulate_single",
    "simulate_batch",
    "simulate_multi_batch",
    "get_simulation_modes",
    "get_live_preflight",
    "get_pending_executions",
    "resume_execution",
    "reconcile_execution",
    "get_operators",
    "get_alpha",
    "get_alpha_summary",
    "get_alpha_evidence",
    "get_alpha_metrics",
    "get_alpha_aggregates",
    "get_alpha_pnl",
    "get_alpha_self_correlation",
    "get_alpha_prod_correlation",
    "get_activity_diversity",
    "get_alpha_recordsets",
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
    "find_duplicate_alphas",
    "preview_alpha_colors",
    "sync_alpha_colors",
    "list_datasets",
    "list_datafields",
    "list_all_datafields",
    "generate_probes",
    "get_capabilities",
    "list_templates",
    "inspect_template",
    "create_template",
    "update_template",
    "delete_template",
    "validate_template",
    "classify_fields",
    "get_simulation_config",
    "validate_simulation_settings",
    "build_simulation_spec",
    "build_simulation_variant",
    "get_operator_reference",
    "get_operator_syntax_reference",
    "research_status",
    "research_tool_manifest",
]


def __getattr__(name):
    """Load the Remote-First API lazily without legacy runtime imports."""
    if name == "WQBClient":
        from .client import WQBClient

        globals()[name] = WQBClient
        return WQBClient
    if name in {
        "SimulationSpec", "validate_simulation_spec", "execution_fingerprint",
        "simulate", "simulate_single", "simulate_batch",
        "simulate_multi_batch", "get_simulation_modes", "get_live_preflight", "get_pending_executions", "resume_execution",
        "reconcile_execution", "get_alpha", "get_alpha_summary",
        "get_alpha_evidence", "get_alpha_metrics", "get_alpha_aggregates",
        "get_alpha_pnl", "get_alpha_self_correlation", "get_alpha_prod_correlation",
        "get_activity_diversity",
        "get_alpha_recordsets", "compare_alphas", "get_remote_alpha",
        "get_remote_alpha_evidence", "find_similar_alphas",
         "generate_probes", "list_datasets", "list_datafields",
         "list_all_datafields",
         "get_capabilities", "get_operators", "list_templates", "inspect_template",
        "create_template", "update_template", "delete_template", "validate_template",
        "classify_fields", "get_simulation_config", "validate_simulation_settings",
        "build_simulation_spec", "build_simulation_variant",
        "simulation_quota", "refresh_remote_alphas", "list_remote_alphas",
        "remote_cache_status", "purge_remote_cache", "group_alphas",
        "find_duplicate_alphas", "preview_alpha_colors",
        "sync_alpha_colors",
        "get_operator_reference", "get_operator_syntax_reference",
        "research_status",
        "research_tool_manifest",
    }:
        from . import research_api

        value = getattr(research_api, name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
