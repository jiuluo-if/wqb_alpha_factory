"""Single, typed, fail-closed application configuration parse."""

from __future__ import annotations

import copy
import math
from dataclasses import dataclass, field, replace

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
    max_concurrent_sims: int = 3
    fields_per_discovery: int = 6
    pagination_limit: int = 50
    max_pagination_pages: int = 20
    poll_timeout_sec: float = 1500
    replace_attempts: int = 3
    replace_backoff_sec: float = 60
    fields_cache_ttl_sec: float = 7 * 24 * 3600
    max_field_alpha_count: int | None = None
    field_selection: dict = field(default_factory=lambda: dict(_FIELD_SELECTION_DEFAULTS))

@dataclass(frozen=True)
class FactoryConfig:
    max_simulations: int = 11200
    max_runtime_sec: int = 86400
    daily_simulation_cap: int = 1600
    weekly_simulation_cap: int = 11200
    include_partial_operator_branches: bool = True


@dataclass(frozen=True)
class AppConfig:
    factory: FactoryConfig = field(default_factory=FactoryConfig)
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


def _resolve_field_selection(agent):
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

    return field_selection

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
    factory_raw = dict(agent.get("factory") or {})
    legacy_factory_max = _int_in_range(
        factory_raw.get("max_simulations", 11200),
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
    field_selection = _resolve_field_selection(agent)
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
        max_concurrent_sims=_int_in_range(
            agent.get("max_concurrent_sims", 3),
            key="config.agent.max_concurrent_sims",
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
        fields_cache_ttl_sec=_finite_float(
            agent.get("fields_cache_ttl_sec", 7 * 24 * 3600),
            key="config.agent.fields_cache_ttl_sec",
            minimum=0.0,
        ),
        max_field_alpha_count=_optional_int_in_range(
            field_selection.get("max_alpha_count"),
            key="config.agent.field_selection.max_alpha_count",
            minimum=0,
        ),
        field_selection=copy.deepcopy(field_selection),
    )
    return AppConfig(
        factory=factory,
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
