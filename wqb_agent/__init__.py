"""WQB research runtime.

Public agent-facing API lives in :mod:`wqb_agent.research_api`.
Other modules are implementation details unless a task explicitly requires
them.  Legacy exports below remain available for compatibility and tests.
"""

from .alpha_pool import AlphaPoolSnapshot, build_pool_snapshot
from .behavior import extract_behavior_series
from .diagnostics import DiagnosticEvent
from .diversity import deduplicate, extract_fields, is_redundant
from .failures import (
    FailureKind,
    classify_error,
    classify_experiment,
    is_research_relevant,
)
from .identity import candidate_identity
from .incremental_policy import IncrementalValuePolicy, incremental_gate
from .incremental_value import (
    IncrementalValueEvidence,
    build_incremental_value,
    select_trusted_pool,
)
from .optimization_decision import optimization_decision_identity
from .optimization_interfaces import (
    ClientOptimizationEvidenceProvider,
    OptimizationEvidenceProvider,
    OptimizationEvidenceSnapshot,
    OptimizationTrial,
    diagnose_optimization,
    record_optimization_trial,
)
from .research_evidence import ResearchEvidenceBundle, classify_research
from .robustness import RobustnessEvidence, evaluate_robustness, retention
from .search_calibration import SearchPolicyReplay, build_search_calibration, reward_v2
from .search_outcome import (
    SearchOutcome,
    extract_statistical_decision,
    parent_relative_delta,
    resolve_reward,
    reward_v1,
    settle_search_outcome,
    staged_promotion,
)
from .search_policy import BudgetAllocator, SearchPolicy
from .search_snapshot import SearchSnapshot

__all__ = [
    "Agent",
    "WQBClient",
    "extract_fields",
    "is_redundant",
    "deduplicate",
    "BudgetAllocator",
    "SearchPolicy",
    "SearchSnapshot",
    "SearchOutcome",
    "parent_relative_delta",
    "reward_v1",
    "extract_statistical_decision",
    "resolve_reward",
    "settle_search_outcome",
    "staged_promotion",
    "SearchPolicyReplay",
    "build_search_calibration",
    "reward_v2",
    "RobustnessEvidence",
    "evaluate_robustness",
    "retention",
    "IncrementalValueEvidence",
    "build_incremental_value",
    "select_trusted_pool",
    "IncrementalValuePolicy",
    "incremental_gate",
    "AlphaPoolSnapshot",
    "build_pool_snapshot",
    "extract_behavior_series",
    "DiagnosticEvent",
    "ResearchEvidenceBundle",
    "classify_research",
    "ClientOptimizationEvidenceProvider",
    "OptimizationEvidenceProvider",
    "OptimizationEvidenceSnapshot",
    "OptimizationTrial",
    "optimization_decision_identity",
    "diagnose_optimization",
    "record_optimization_trial",
    "candidate_identity",
    "HighSignalValidator",
    "FailureKind",
    "classify_error",
    "classify_experiment",
    "is_research_relevant",
    "ExperimentSpec",
    "inspect_state",
    "discover_fields",
    "get_operator_reference",
    "run_experiment",
    "get_experiment",
    "compare_experiments",
    "search_history",
    "reconcile",
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
    if name == "HighSignalValidator":
        from .validation import HighSignalValidator

        globals()[name] = HighSignalValidator
        return HighSignalValidator
    if name in {
        "ExperimentSpec", "inspect_state", "discover_fields",
        "get_operator_reference", "run_experiment", "get_experiment",
        "compare_experiments", "search_history", "reconcile",
    }:
        from . import research_api

        value = getattr(research_api, name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
