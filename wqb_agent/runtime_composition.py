"""Agent 的 workflow 组合边界。"""

from __future__ import annotations

from dataclasses import dataclass

from .alpha_feed_workflow import AlphaFeedHooks, AlphaFeedWorkflow
from .optimizer_workflow import OptimizerHooks, OptimizerWorkflow
from .proposal_execution import (
    ProposalExecutionContext,
    ProposalExecutionHooks,
    ProposalExecutionWorkflow,
)
from .runtime_components import RuntimeComponents
from .runtime_policy import AgentRuntimePolicy
from .suggestion_workflow import SuggestionHooks, SuggestionWorkflow


@dataclass(frozen=True)
class AgentWorkflowHooks:
    """四个 workflow 所需的显式 Agent 操作回调。"""

    suggestion: SuggestionHooks
    proposal_execution: ProposalExecutionHooks
    alpha_feed: AlphaFeedHooks
    optimizer: OptimizerHooks


@dataclass(frozen=True)
class AgentWorkflows:
    """由同一套基础组件和缓存组合出的四个 workflow。"""

    suggestion: SuggestionWorkflow
    proposal_execution: ProposalExecutionWorkflow
    alpha_feed: AlphaFeedWorkflow
    optimizer: OptimizerWorkflow


def build_agent_workflows(
    *,
    components: RuntimeComponents,
    policy: AgentRuntimePolicy,
    operator_reference: dict,
    hooks: AgentWorkflowHooks,
    daily_cache,
    weekly_cache,
    heartbeat=None,
) -> AgentWorkflows:
    """使用既有组件、缓存和显式 hooks 构造 workflow。"""
    suggestion = SuggestionWorkflow(
        discovery=components.discovery,
        memory=components.memory,
        trajectory=components.trajectory,
        alpha_factory=components.alpha_factory,
        state_dir=policy.state_dir,
        fields_per_discovery=policy.fields_per_discovery,
        context_experiments=policy.context_experiments,
        simulation_settings=policy.simulation_settings,
        operator_reference=operator_reference,
        hooks=hooks.suggestion,
    )
    proposal_execution = ProposalExecutionWorkflow(
        ProposalExecutionContext(
            state_dir=policy.state_dir,
            simulator=components.simulator,
            trajectory=components.trajectory,
            trial_ledger=components.trial_ledger,
            checkpoints=components.checkpoints,
            memory=components.memory,
            search_policy=components.search_policy,
            reflector=components.reflector,
            hooks=hooks.proposal_execution,
            operator_reference=operator_reference,
            factory_batch_size=policy.factory_batch_size,
            min_factory_datasets=policy.min_factory_datasets,
            min_cross_dataset_pairs=policy.min_cross_dataset_pairs,
            candidates_per_round=policy.candidates_per_round,
            max_proposals_per_round=policy.max_proposals_per_round,
            research_allocation=policy.research_allocation,
            research_integrity=policy.research_integrity,
            max_field_alpha_count=policy.max_field_alpha_count,
            require_platform_alpha_count=policy.require_platform_alpha_count,
        )
    )
    alpha_feed = AlphaFeedWorkflow(
        alpha_reader=hooks.alpha_feed.get_all_user_alphas,
        daily_cache=daily_cache,
        weekly_cache=weekly_cache,
        heartbeat=heartbeat,
    )
    optimizer = OptimizerWorkflow(
        trajectory=components.trajectory,
        alpha_feed_cache=weekly_cache,
        alpha_factory=components.alpha_factory,
        quality_policy=policy.quality_policy,
        operator_reference=operator_reference,
        hooks=hooks.optimizer,
    )
    return AgentWorkflows(
        suggestion=suggestion,
        proposal_execution=proposal_execution,
        alpha_feed=alpha_feed,
        optimizer=optimizer,
    )
