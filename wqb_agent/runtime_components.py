"""Composition helpers for the existing Agent runtime."""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass

from .candidate import CandidateBuilder
from .checkpoints import CheckpointStore
from .discovery import FieldDiscovery
from .memory import ExperienceMemory
from .reflection import Reflector
from .search_policy import SearchPolicy
from .simulator import Simulator
from .state import Trajectory
from .submission import SubmissionPool
from .trial_ledger import TrialLedger


@dataclass
class RuntimeComponents:
    """Already-resolved runtime objects; contains no orchestration logic."""

    search_policy: SearchPolicy
    memory: ExperienceMemory
    trajectory: Trajectory
    trial_ledger: TrialLedger
    builder: CandidateBuilder
    discovery: FieldDiscovery
    simulator: Simulator
    reflector: Reflector
    checkpoints: CheckpointStore
    submission_pool: SubmissionPool


def build_runtime_components(client, config):
    """Construct the existing runtime components from resolved AppConfig."""
    runtime = config.runtime
    search_cfg = {
        **runtime.search_policy,
        "enabled": config.search.enabled,
        "max_simulations": config.search.max_simulations,
        "validation_max_simulations": config.search.validation_max_simulations,
    }
    search_policy = SearchPolicy(search_cfg)
    state_dir = runtime.state_dir
    submission_pool = SubmissionPool(state_dir, filename=runtime.submission_pool_filename)
    memory = ExperienceMemory(
        state_dir,
        max_lessons=runtime.memory["max_lessons"],
        max_avoid=runtime.memory["max_avoid"],
        max_next=runtime.memory["max_next"],
        max_hypotheses=runtime.memory["max_hypotheses"],
        max_short_term=runtime.memory["max_short_term"],
        short_term_window=runtime.memory["short_term_window"],
        promote_hits=runtime.memory["promote_hits"],
        max_garbage=runtime.memory["max_garbage"],
        garbage_max_age_rounds=runtime.memory["garbage_max_age_rounds"],
        next_max_age_rounds=runtime.memory["next_max_age_rounds"],
        max_lineages=runtime.memory["max_lineages"],
        max_seen_expressions=runtime.memory["max_seen_expressions"],
        max_used_hypotheses=runtime.memory["max_used_hypotheses"],
        persist=False,
    )
    # Canonical completed Experiment evidence (metrics/checks/field evidence)
    # must survive a restart so the next process can rehydrate a legal
    # optimizer parent.  Trajectory stays the sole evidence owner; nothing is
    # reconstructed from Alpha Feed metadata or checkpoints.
    trajectory = Trajectory(
        max_len=runtime.trajectory_window,
        path=os.path.join(state_dir, "trajectory.jsonl"),
        persist=True,
    )
    trial_ledger = TrialLedger(
        os.path.join(state_dir, "trial_ledger.jsonl"), persist=True
    )
    trial_ledger.initialize_history_completeness(trajectory.path)
    builder = CandidateBuilder(
        neutralization=config.simulation_config.settings["neutralization"],
        catalog_path=config.runtime.alpha_template_catalog,
        require_private=True,
    )
    discovery = FieldDiscovery(
        client,
        pagination_limit=runtime.pagination_limit,
        max_pages=runtime.max_pagination_pages,
        cache_path=os.path.join(state_dir, "fields_cache.json"),
        cache_ttl_sec=runtime.fields_cache_ttl_sec,
        catalog_root=state_dir,
        max_alpha_count=runtime.max_field_alpha_count,
        selection_mode=runtime.field_selection["mode"],
        random_fraction=runtime.field_selection["random_fraction"],
        random_seed=runtime.field_selection["random_seed"],
        platform_usage_refresh=runtime.field_selection["platform_usage_refresh"],
        require_platform_alpha_count=runtime.field_selection["require_platform_alpha_count"],
        dataset_sampling=runtime.field_selection["dataset_sampling"],
        min_datasets=runtime.field_selection["min_datasets"],
        dataset_pool=runtime.field_selection["dataset_pool"],
        persist_catalog=runtime.field_selection["persist_catalog"],
    )
    simulator = Simulator(
        client,
        max_concurrent=runtime.max_concurrent_sims,
        poll_timeout_sec=runtime.poll_timeout_sec,
        replace_attempts=runtime.replace_attempts,
        replace_backoff_sec=runtime.replace_backoff_sec,
        yearly_policy={
            "min_sharpe": runtime.quality["promising_sharpe"],
            "min_fitness": runtime.quality["promising_fitness"],
            "max_turnover": runtime.quality["max_turnover"],
            "min_years": runtime.yearly_policy["min_years"],
        },
    )
    reflector_keys = {
        "success_sharpe": "success_sharpe",
        "promising_sharpe": "promising_sharpe",
        "promising_fitness": "promising_fitness",
        "success_fitness": "success_fitness",
        "min_turnover": "min_turnover",
        "max_turnover": "max_turnover",
        "max_drawdown": "max_drawdown",
        "max_self_correlation": "self_correlation_limit",
    }
    reflector = Reflector(
        memory,
        **{target: runtime.quality[key] for key, target in reflector_keys.items()
           if key in runtime.quality},
    )
    # Unresolved remote work is still resumed only from checkpoint state, and
    # no derived result sidecar cache is resurrected on startup.
    reflector.evidence_cache = {}
    lock = threading.Lock()
    return RuntimeComponents(
        search_policy=search_policy,
        memory=memory,
        trajectory=trajectory,
        trial_ledger=trial_ledger,
        builder=builder,
        discovery=discovery,
        simulator=simulator,
        reflector=reflector,
        checkpoints=CheckpointStore(state_dir, lock=lock),
        submission_pool=submission_pool,
    )
