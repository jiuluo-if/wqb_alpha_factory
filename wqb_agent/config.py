"""Single, typed, fail-closed application configuration parse."""

from __future__ import annotations

import copy
import math
from dataclasses import dataclass, field, replace

from .incremental_policy import IncrementalValuePolicy

_MEMORY_DEFAULTS = {
    "max_lessons": 20,
    "max_avoid": 30,
    "max_next": 15,
    "max_hypotheses": 12,
    "max_short_term": 30,
    "short_term_window": 5,
    "promote_hits": 2,
    "max_garbage": 200,
    "garbage_max_age_rounds": 60,
    "next_max_age_rounds": 20,
    "max_lineages": 256,
    "max_seen_expressions": 4096,
    "max_used_hypotheses": 256,
}
_FIELD_SELECTION_DEFAULTS = {
    "mode": "semantic_random",
    "random_fraction": 0.35,
    "random_seed": "newwqb",
    # Field usage is platform truth when local Simulation/Alpha results are
    # intentionally ephemeral.  Keep the switch explicit for offline tests.
    "platform_usage_refresh": False,
    "require_platform_alpha_count": False,
    "dataset_sampling": "stratified",
    "dataset_pool": [],
    "min_datasets": 1,
    "min_cross_dataset_pairs": 0,
    "persist_catalog": False,
}
_QUALITY_DEFAULTS = {
    "max_self_correlation": 0.5,
    "success_sharpe": 1.25,
    "success_fitness": 1.0,
    "promising_sharpe": 0.9,
    "promising_fitness": 0.6,
    "min_turnover": 0.01,
    "max_turnover": 0.7,
    "max_drawdown": 0.5,
}


@dataclass(frozen=True)
class IncrementalValueConfig:
    mode: str = "required_when_available"
    max_abs_correlation: float = 0.7
    min_overlap: int = 60


@dataclass(frozen=True)
class ValidationConfig:
    yearly_min_years: int = 2


@dataclass(frozen=True)
class StatisticalConfig:
    mode: str = "required_when_available"


@dataclass(frozen=True)
class RobustnessConfig:
    min_sharpe_retention: float = 0.7
    min_fitness_retention: float = 0.6


@dataclass(frozen=True)
class SimulationConfig:
    settings: dict = field(default_factory=lambda: {"neutralization": "SUBINDUSTRY"})


@dataclass(frozen=True)
class RemoteCacheConfig:
    retention_days: int = 7


@dataclass(frozen=True)
class AgentRuntimeConfig:
    """Typed values consumed while constructing the existing Agent runtime.

    Policy mappings remain mappings because their schemas are intentionally
    extensible; scalar defaults and path/limit values are resolved once here.
    This is a configuration boundary, not a second runtime or state model.
    """

    state_dir: str = ".wqb_state"
    alpha_template_catalog: str | None = None
    smoke_dataset: str | None = None
    max_rounds: int = 5
    candidates_per_round: int = 6
    max_proposals_per_round: int = 18
    max_concurrent_sims: int = 3
    research_integrity: bool = False
    correlation_refresh_window: int = 256
    fields_per_discovery: int = 6
    pagination_limit: int = 50
    max_pagination_pages: int = 20
    poll_timeout_sec: float = 1500
    replace_attempts: int = 3
    replace_backoff_sec: float = 60
    trajectory_window: int = 100
    context_experiments: int = 10
    fields_cache_ttl_sec: float = 7 * 24 * 3600
    alpha_feed_refresh_interval_sec: float = 3 * 3600
    heartbeat_interval_sec: float = 20.0
    max_field_alpha_count: int | None = None
    factory: dict = field(default_factory=dict)
    research_allocation: dict = field(default_factory=dict)
    field_selection: dict = field(default_factory=lambda: dict(_FIELD_SELECTION_DEFAULTS))
    submission_pool_filename: str = "submission_pool.json"
    memory: dict = field(default_factory=lambda: dict(_MEMORY_DEFAULTS))
    quality: dict = field(default_factory=lambda: dict(_QUALITY_DEFAULTS))
    statistical_policy: dict = field(default_factory=dict)
    robustness_policy: dict = field(default_factory=dict)
    yearly_policy: dict = field(default_factory=lambda: {"min_years": 2})

@dataclass(frozen=True)
class SearchConfig:
    enabled: bool = True
    max_simulations: int = 100
    validation_max_simulations: int = 4


@dataclass(frozen=True)
class ResearchAllocation:
    """Per-round role allocation, separate from the process hard cap."""

    max_simulations: int = 100
    maximum: dict = field(default_factory=dict)


@dataclass(frozen=True)
class FactoryConfig:
    max_simulations: int = 11200
    max_runtime_sec: int = 86400
    daily_simulation_cap: int = 1600
    weekly_simulation_cap: int = 11200
    include_partial_operator_branches: bool = True


@dataclass(frozen=True)
class AppConfig:
    search: SearchConfig = field(default_factory=SearchConfig)
    research_allocation: ResearchAllocation = field(default_factory=ResearchAllocation)
    factory: FactoryConfig = field(default_factory=FactoryConfig)
    incremental_value: IncrementalValueConfig = field(default_factory=IncrementalValueConfig)
    validation: ValidationConfig = field(default_factory=ValidationConfig)
    statistical: StatisticalConfig = field(default_factory=StatisticalConfig)
    robustness: RobustnessConfig = field(default_factory=RobustnessConfig)
    simulation_config: SimulationConfig = field(default_factory=SimulationConfig)
    remote_cache: RemoteCacheConfig = field(default_factory=RemoteCacheConfig)
    runtime: AgentRuntimeConfig = field(default_factory=AgentRuntimeConfig)


def _as_bool(value, default, key):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "yes", "on", "1"}:
            return True
        if normalized in {"false", "no", "off", "0"}:
            return False
    raise ValueError(f"{key} 必须是布尔值")


def _int_in_range(value, *, key, minimum=None, maximum=None):
    """Parse a user integer without accepting booleans or clamping it."""
    if isinstance(value, bool):
        raise ValueError(f"{key} 必须是整数")
    if isinstance(value, float) and (
        not math.isfinite(value) or not value.is_integer()
    ):
        raise ValueError(f"{key} 必须是整数")
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{key} 必须是整数") from exc
    if minimum is not None and parsed < minimum:
        raise ValueError(f"{key} 必须大于或等于 {minimum}")
    if maximum is not None and parsed > maximum:
        raise ValueError(f"{key} 必须小于或等于 {maximum}")
    return parsed


def _finite_float(value, *, key, minimum=None, maximum=None):
    """Parse a finite user float without accepting booleans or clamping it."""
    if isinstance(value, bool):
        raise ValueError(f"{key} 必须是有限数字")
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{key} 必须是有限数字") from exc
    if not math.isfinite(parsed):
        raise ValueError(f"{key} 必须是有限数字")
    if minimum is not None and parsed < minimum:
        raise ValueError(f"{key} 必须大于或等于 {minimum}")
    if maximum is not None and parsed > maximum:
        raise ValueError(f"{key} 必须小于或等于 {maximum}")
    return parsed


def _optional_int_in_range(value, *, key, minimum=None, maximum=None):
    if value is None:
        return None
    return _int_in_range(
        value, key=key, minimum=minimum, maximum=maximum
    )


def _resolved_ints(values, defaults, *, minimum=0):
    resolved = dict(values)
    for key, default in defaults.items():
        resolved[key] = _int_in_range(
            values.get(key, default),
            key=f"config.agent.{key}",
            minimum=minimum,
        )
    return resolved


def _resolve_runtime_policies(agent):
    memory_raw = dict(agent.get("memory") or {})
    memory = _resolved_ints(memory_raw, _MEMORY_DEFAULTS)
    for key in ("max_lineages", "max_seen_expressions", "max_used_hypotheses"):
        if memory[key] < 1:
            raise ValueError(f"config.agent.memory.{key} 必须大于 0")

    field_selection = {**_FIELD_SELECTION_DEFAULTS, **dict(agent.get("field_selection") or {})}
    field_selection["random_fraction"] = _finite_float(
        field_selection["random_fraction"],
        key="config.agent.field_selection.random_fraction",
        minimum=0.0,
        maximum=1.0,
    )
    field_selection["mode"] = str(field_selection["mode"] or "semantic_random")
    field_selection["random_seed"] = str(field_selection["random_seed"] or "newwqb")
    field_selection["dataset_sampling"] = str(
        field_selection["dataset_sampling"] or "stratified"
    ).lower()
    field_selection["min_datasets"] = _int_in_range(
        field_selection["min_datasets"],
        key="config.agent.field_selection.min_datasets",
        minimum=1,
    )
    field_selection["min_cross_dataset_pairs"] = _int_in_range(
        field_selection["min_cross_dataset_pairs"],
        key="config.agent.field_selection.min_cross_dataset_pairs",
        minimum=0,
    )
    raw_pool = field_selection.get("dataset_pool") or []
    if isinstance(raw_pool, (str, int)):
        raw_pool = [raw_pool]
    if not isinstance(raw_pool, list):
        raise ValueError("config.agent.field_selection.dataset_pool 必须是数组")
    field_selection["dataset_pool"] = [
        str(item.get("id") or item.get("name")) if isinstance(item, dict)
        else str(item)
        for item in raw_pool
        if isinstance(item, (str, int)) or (
            isinstance(item, dict) and (item.get("id") or item.get("name"))
        )
    ]
    for key in (
        "platform_usage_refresh", "require_platform_alpha_count", "persist_catalog"
    ):
        value = field_selection[key]
        if isinstance(value, bool):
            continue
        if isinstance(value, str) and value.strip().lower() in {"true", "false"}:
            field_selection[key] = value.strip().lower() == "true"
            continue
        raise ValueError(f"config.agent.field_selection.{key} 必须是布尔值")

    quality = {**_QUALITY_DEFAULTS, **dict(agent.get("quality") or {})}
    for key in _QUALITY_DEFAULTS:
        quality[key] = _finite_float(
            quality[key], key=f"config.agent.quality.{key}"
        )
    for group_name, keys in {
        "excellent": (
            "min_sharpe", "min_fitness", "min_margin",
            "min_turnover", "max_turnover",
        ),
        "spectacular": (
            "min_sharpe", "min_fitness", "min_margin",
            "min_turnover", "max_turnover",
        ),
    }.items():
        group = quality.get(group_name)
        if group is None:
            continue
        if not isinstance(group, dict):
            raise ValueError(f"config.agent.quality.{group_name} 必须是对象")
        for key in keys:
            if key in group:
                group[key] = _finite_float(
                    group[key],
                    key=f"config.agent.quality.{group_name}.{key}",
                )
    return memory, field_selection, quality

def parse_config(raw):
    if not isinstance(raw, dict) or not isinstance(raw.get("simulation", {}), dict):
        raise ValueError("config.simulation 必须是对象")  # noqa: TRY004
    agent = raw.get("agent")
    if not isinstance(agent, dict):
        raise ValueError("config.agent 必须是对象")  # noqa: TRY004
    remote_cache_raw = raw.get("remote_cache", {})
    if not isinstance(remote_cache_raw, dict):
        raise ValueError("config.remote_cache 必须是对象")
    remote_cache = RemoteCacheConfig(_int_in_range(
        remote_cache_raw.get("retention_days", 7),
        key="config.remote_cache.retention_days", minimum=1, maximum=90,
    ))
    incremental = dict(agent.get("incremental_value") or {})
    incremental_max_correlation = _finite_float(
        incremental.get("max_abs_correlation", 0.7),
        key="config.agent.incremental_value.max_abs_correlation",
        minimum=0.0,
        maximum=1.0,
    )
    incremental_min_overlap = _int_in_range(
        incremental.get("min_overlap", 60),
        key="config.agent.incremental_value.min_overlap",
        minimum=1,
    )
    policy = IncrementalValuePolicy(
        mode=incremental.get("mode", "required_when_available"),
        max_abs_correlation=incremental_max_correlation,
        min_overlap=incremental_min_overlap,
    )
    search_raw = dict(agent.get("search_policy") or {})
    research_raw = dict(agent.get("research_allocation") or {})
    search_max = _int_in_range(
        search_raw.get(
            "max_simulations", research_raw.get("max_simulations", 100)
        ),
        key="config.agent.search_policy.max_simulations",
        minimum=0,
    )
    research_max = _int_in_range(
        research_raw.get("max_simulations", search_max),
        key="config.agent.research_allocation.max_simulations",
        minimum=0,
    )
    factory_raw = dict(agent.get("factory") or {})
    legacy_factory_max = _int_in_range(
        factory_raw.get("max_simulations", search_max),
        key="config.agent.factory.max_simulations",
        minimum=0,
    )
    factory_max = _int_in_range(
        factory_raw.get("weekly_simulation_cap", legacy_factory_max),
        key="config.agent.factory.weekly_simulation_cap",
        minimum=0,
    )
    daily_factory_max = _int_in_range(
        factory_raw.get("daily_simulation_cap", factory_max),
        key="config.agent.factory.daily_simulation_cap",
        minimum=0,
    )
    if daily_factory_max > factory_max:
        raise ValueError("daily_simulation_cap 不得超过 weekly_simulation_cap")
    search = SearchConfig(
        enabled=_as_bool(
            search_raw.get("enabled"), bool(research_raw),
            "config.agent.search_policy.enabled",
        ),
        max_simulations=search_max,
        validation_max_simulations=_int_in_range(
            search_raw.get("validation_max_simulations", 0),
            key="config.agent.search_policy.validation_max_simulations",
            minimum=0,
        ),
    )
    maximum_raw = research_raw.get("maximum") or {}
    if not isinstance(maximum_raw, dict):
        raise ValueError("config.agent.research_allocation.maximum 必须是对象")
    maximum = {
        role: _int_in_range(
            value,
            key=f"config.agent.research_allocation.maximum.{role}",
            minimum=0,
        )
        for role, value in maximum_raw.items()
    }
    allocation = ResearchAllocation(
        max_simulations=research_max,
        maximum=maximum,
    )
    factory = FactoryConfig(
        max_simulations=factory_max,
        max_runtime_sec=_int_in_range(
            factory_raw.get("max_runtime_sec", 86400),
            key="config.agent.factory.max_runtime_sec",
            minimum=0,
        ),
        daily_simulation_cap=daily_factory_max,
        weekly_simulation_cap=factory_max,
        include_partial_operator_branches=_as_bool(
            factory_raw.get("include_partial_operator_branches"), True,
            "config.agent.factory.include_partial_operator_branches",
        ),
    )
    if search.max_simulations + search.validation_max_simulations > factory.max_simulations:
        raise ValueError("discovery + validation 预算不得超过 factory.max_simulations")
    memory, field_selection, quality = _resolve_runtime_policies(agent)
    # Keep the validated typed factory model; the raw mapping remains
    # available only through ``runtime.factory`` for extensible legacy keys.
    factory_settings = dict(agent.get("factory") or {})
    # The compatibility runner still treats this legacy key as its per-run
    # cap; the typed FactoryConfig separately owns the weekly cap.
    factory_settings["max_simulations"] = legacy_factory_max
    factory_settings["max_runtime_sec"] = factory.max_runtime_sec
    factory_settings["daily_simulation_cap"] = daily_factory_max
    factory_settings["weekly_simulation_cap"] = factory_max
    factory_settings["include_partial_operator_branches"] = factory.include_partial_operator_branches
    if "max_rounds" in factory_settings:
        factory_settings["max_rounds"] = _int_in_range(
            factory_settings["max_rounds"],
            key="config.agent.factory.max_rounds",
            minimum=0,
        )
    else:
        factory_settings["max_rounds"] = 0
    if "idle_sleep_sec" in factory_settings:
        factory_settings["idle_sleep_sec"] = _finite_float(
            factory_settings["idle_sleep_sec"],
            key="config.agent.factory.idle_sleep_sec",
            minimum=1.0,
            maximum=60.0,
        )
    else:
        factory_settings["idle_sleep_sec"] = 30.0
    factory_settings["max_route_attempts"] = _int_in_range(
        factory_settings.get("max_route_attempts", 3),
        key="config.agent.factory.max_route_attempts", minimum=0,
    )
    factory_settings["max_no_gain_attempts"] = _int_in_range(
        factory_settings.get("max_no_gain_attempts", 2),
        key="config.agent.factory.max_no_gain_attempts", minimum=1,
    )
    factory_settings["blocker_recheck_sec"] = _finite_float(
        factory_settings.get("blocker_recheck_sec", 3600.0),
        key="config.agent.factory.blocker_recheck_sec", minimum=0.0,
    )
    research_allocation_raw = dict(agent.get("research_allocation") or {})
    statistical_policy = dict(agent.get("statistical_policy") or {})
    robustness_policy = dict(agent.get("robustness_policy") or {})
    yearly_policy = dict(agent.get("yearly_policy") or {})
    yearly_policy["min_years"] = _int_in_range(
        yearly_policy.get("min_years", 2),
        key="config.agent.yearly_policy.min_years",
        minimum=1,
    )
    statistical_policy.setdefault("mode", "required_when_available")
    for key in ("min_psr", "min_dsr", "max_pbo_proxy"):
        if key in statistical_policy and statistical_policy[key] is not None:
            statistical_policy[key] = _finite_float(
                statistical_policy[key],
                key=f"config.agent.statistical_policy.{key}",
                minimum=0.0,
                maximum=1.0,
            )
    robustness_policy = {
        "min_sharpe_retention": 0.7,
        "min_fitness_retention": 0.6,
        "max_turnover_multiple": 1.5,
        "max_drawdown_multiple": 1.5,
        "require_checks_passed": True,
        **robustness_policy,
    }
    for key in (
        "min_sharpe_retention", "min_fitness_retention",
        "max_turnover_multiple", "max_drawdown_multiple",
    ):
        robustness_policy[key] = _finite_float(
            robustness_policy[key],
            key=f"config.agent.robustness_policy.{key}",
            minimum=0.0,
        )
    runtime = AgentRuntimeConfig(
        state_dir=str(agent.get("state_dir", ".wqb_state")),
        alpha_template_catalog=(
            str(agent["alpha_template_catalog"])
            if agent.get("alpha_template_catalog") is not None else None
        ),
        smoke_dataset=(
            str(agent["smoke_dataset"])
            if agent.get("smoke_dataset") is not None
            else None
        ),
        max_rounds=_int_in_range(
            agent.get("max_rounds", 5),
            key="config.agent.max_rounds",
            minimum=0,
        ),
        candidates_per_round=_int_in_range(
            agent.get("candidates_per_round", 6),
            key="config.agent.candidates_per_round",
            minimum=0,
        ),
        max_proposals_per_round=_int_in_range(
            agent.get("max_proposals_per_round", 18),
            key="config.agent.max_proposals_per_round",
            minimum=0,
            maximum=100,
        ),
        max_concurrent_sims=_int_in_range(
            agent.get("max_concurrent_sims", 3),
            key="config.agent.max_concurrent_sims",
            minimum=1,
        ),
        research_integrity=_as_bool(
            agent.get("research_integrity"), False,
            "config.agent.research_integrity",
        ),
        correlation_refresh_window=_int_in_range(
            agent.get("correlation_refresh_window", 256),
            key="config.agent.correlation_refresh_window",
            minimum=1,
        ),
        fields_per_discovery=_int_in_range(
            agent.get("fields_per_discovery", 6),
            key="config.agent.fields_per_discovery",
            minimum=1,
        ),
        pagination_limit=_int_in_range(
            agent.get("pagination_limit", 50),
            key="config.agent.pagination_limit",
            minimum=1,
        ),
        max_pagination_pages=_int_in_range(
            agent.get("max_pagination_pages", 20),
            key="config.agent.max_pagination_pages",
            minimum=1,
        ),
        poll_timeout_sec=_finite_float(
            agent.get("poll_timeout_sec", 1500),
            key="config.agent.poll_timeout_sec",
            minimum=0.0,
        ),
        replace_attempts=_int_in_range(
            agent.get("replace_attempts", 3),
            key="config.agent.replace_attempts",
            minimum=1,
        ),
        replace_backoff_sec=_finite_float(
            agent.get("replace_backoff_sec", 60),
            key="config.agent.replace_backoff_sec",
            minimum=0.0,
        ),
        trajectory_window=_int_in_range(
            agent.get("trajectory_window", 100),
            key="config.agent.trajectory_window",
            minimum=1,
        ),
        context_experiments=_int_in_range(
            agent.get("context_experiments", 10),
            key="config.agent.context_experiments",
            minimum=0,
        ),
        fields_cache_ttl_sec=_finite_float(
            agent.get("fields_cache_ttl_sec", 7 * 24 * 3600),
            key="config.agent.fields_cache_ttl_sec",
            minimum=0.0,
        ),
        alpha_feed_refresh_interval_sec=_finite_float(
            agent.get("alpha_feed_refresh_interval_sec", 3 * 3600),
            key="config.agent.alpha_feed_refresh_interval_sec",
            minimum=1.0, maximum=7 * 24 * 3600,
        ),
        heartbeat_interval_sec=_finite_float(
            agent.get("heartbeat_interval_sec", 20.0),
            key="config.agent.heartbeat_interval_sec",
            minimum=1.0, maximum=300.0,
        ),
        max_field_alpha_count=_optional_int_in_range(
            field_selection.get("max_alpha_count"),
            key="config.agent.field_selection.max_alpha_count",
            minimum=0,
        ),
        factory=copy.deepcopy(factory_settings),
        research_allocation=copy.deepcopy(research_allocation_raw),
        field_selection=copy.deepcopy(field_selection),
        submission_pool_filename=str(
            (agent.get("submission_pool") or {}).get("filename", "submission_pool.json")
        ),
        memory=copy.deepcopy(memory),
        quality=copy.deepcopy(quality),
        statistical_policy=copy.deepcopy(statistical_policy),
        robustness_policy=copy.deepcopy(robustness_policy),
        yearly_policy=copy.deepcopy(yearly_policy),
    )
    return AppConfig(
        search=search,
        research_allocation=allocation,
        factory=factory,
        incremental_value=IncrementalValueConfig(
            policy.mode, policy.max_abs_correlation, policy.min_overlap
        ),
        validation=ValidationConfig(yearly_policy["min_years"]),
        statistical=StatisticalConfig(str((agent.get("statistical_policy") or {}).get("mode", "required_when_available"))),
        robustness=RobustnessConfig(
            robustness_policy["min_sharpe_retention"],
            robustness_policy["min_fitness_retention"],
        ),
        simulation_config=SimulationConfig({
            "neutralization": "SUBINDUSTRY",
            **copy.deepcopy(raw.get("simulation", {})),
        }),
        remote_cache=remote_cache,
        runtime=runtime,
    )


def normalize_config(config):
    """Normalize the one supported external config boundary."""
    if isinstance(config, AppConfig):
        return config
    return parse_config(config)


def apply_cli_overrides(
    config: AppConfig,
    *,
    state_dir: str | None = None,
) -> AppConfig:
    """Apply only explicit CLI overrides to an already normalized config."""
    if not isinstance(config, AppConfig):
        raise TypeError("apply_cli_overrides 需要已 normalize 的 AppConfig")
    runtime = config.runtime
    if state_dir is not None:
        if not isinstance(state_dir, str):
            raise TypeError("state_dir CLI override 必须是字符串")
        runtime = replace(runtime, state_dir=state_dir)
    return replace(config, runtime=runtime)
