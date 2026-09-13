"""Agent 运行时所需的已解析配置投影。"""

from __future__ import annotations

from dataclasses import dataclass

from .config import AppConfig
from .incremental_policy import IncrementalValuePolicy
from .proposal_contract import FACTORY_BATCH_SIZE


@dataclass(frozen=True)
class AgentRuntimePolicy:
    """集中保存 Agent 真正消费的运行时值，不替代完整 AppConfig。"""

    state_dir: str
    simulation_settings: dict
    factory_config: dict
    max_rounds: int
    candidates_per_round: int
    max_proposals_per_round: int
    factory_batch_size: int
    research_allocation: dict
    research_integrity: bool
    fields_per_discovery: int
    pagination_limit: int
    max_pagination_pages: int
    poll_timeout_sec: float
    context_experiments: int
    correlation_refresh_window: int
    quality_policy: dict
    statistical_policy: dict
    robustness_policy: dict
    incremental_policy: IncrementalValuePolicy
    max_field_alpha_count: int | None
    require_platform_alpha_count: bool
    min_factory_datasets: int
    min_cross_dataset_pairs: int
    dataset_pool: tuple[str, ...]
    alpha_feed_refresh_interval_sec: float = 3 * 3600
    heartbeat_interval_sec: float = 20.0


def build_agent_runtime_policy(config: AppConfig) -> AgentRuntimePolicy:
    """从已规范化的 AppConfig 一次性解析 Agent 的运行时投影。"""
    if not isinstance(config, AppConfig):
        raise TypeError("build_agent_runtime_policy 需要已 normalize 的 AppConfig")

    runtime = config.runtime
    field_selection = runtime.field_selection
    return AgentRuntimePolicy(
        state_dir=runtime.state_dir,
        simulation_settings=config.simulation_config.settings,
        factory_config={
            **runtime.factory,
            "max_simulations": config.factory.max_simulations,
            "max_runtime_sec": config.factory.max_runtime_sec,
            "daily_simulation_cap": config.factory.daily_simulation_cap,
            "weekly_simulation_cap": config.factory.weekly_simulation_cap,
            "include_partial_operator_branches": config.factory.include_partial_operator_branches,
        },
        max_rounds=runtime.max_rounds,
        candidates_per_round=runtime.candidates_per_round,
        max_proposals_per_round=runtime.max_proposals_per_round,
        factory_batch_size=FACTORY_BATCH_SIZE,
        research_allocation=dict(runtime.research_allocation),
        research_integrity=runtime.research_integrity,
        fields_per_discovery=runtime.fields_per_discovery,
        pagination_limit=runtime.pagination_limit,
        max_pagination_pages=runtime.max_pagination_pages,
        poll_timeout_sec=runtime.poll_timeout_sec,
        context_experiments=runtime.context_experiments,
        correlation_refresh_window=runtime.correlation_refresh_window,
        quality_policy=dict(runtime.quality),
        statistical_policy=dict(runtime.statistical_policy),
        robustness_policy=dict(runtime.robustness_policy),
        incremental_policy=IncrementalValuePolicy(
            config.incremental_value.mode,
            config.incremental_value.max_abs_correlation,
            config.incremental_value.min_overlap,
        ),
        max_field_alpha_count=runtime.max_field_alpha_count,
        require_platform_alpha_count=bool(
            field_selection.get("require_platform_alpha_count", False)
        ),
        min_factory_datasets=int(field_selection.get("min_datasets", 1)),
        min_cross_dataset_pairs=int(
            field_selection.get("min_cross_dataset_pairs", 0)
        ),
        dataset_pool=tuple(field_selection.get("dataset_pool") or ()),
        alpha_feed_refresh_interval_sec=runtime.alpha_feed_refresh_interval_sec,
        heartbeat_interval_sec=runtime.heartbeat_interval_sec,
    )
