"""Single, typed, fail-closed application configuration parse."""

from __future__ import annotations

import copy
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from os import PathLike

_FIELD_SELECTION_DEFAULTS = {
    "mode": "semantic_random",
    "random_fraction": 0.35,
    "random_seed": "newwqb",
    # Field usage is platform truth; keep the switch explicit for offline tests.
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
class RuntimeConfig:
    """Typed values for platform access, discovery and local safety paths."""

    state_dir: str = ".wqb_state"
    alpha_template_catalog: str | None = None
    smoke_dataset: str | None = None
    max_concurrent_sims: int = 10
    fields_per_discovery: int = 6
    pagination_limit: int = 50
    max_pagination_pages: int = 20
    poll_timeout_sec: float = 1500
    repoll_attempts: int = 3
    repoll_backoff_sec: float = 60
    fields_cache_ttl_sec: float = 7 * 24 * 3600
    max_field_alpha_count: int | None = None
    field_selection: dict = field(default_factory=lambda: dict(_FIELD_SELECTION_DEFAULTS))

@dataclass(frozen=True)
class FactoryConfig:
    default_probe_count: int = 100


@dataclass(frozen=True)
class QuotaConfig:
    daily: int = 1600
    rolling_days: int = 7
    rolling_limit: int = 11200


@dataclass(frozen=True)
class AppConfig:
    simulation_config: SimulationConfig = field(default_factory=SimulationConfig)
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)
    remote_cache: RemoteCacheConfig = field(default_factory=RemoteCacheConfig)
    quota: QuotaConfig = field(default_factory=QuotaConfig)
    factory: FactoryConfig = field(default_factory=FactoryConfig)


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


def _resolve_field_selection(runtime):
    field_selection = {**_FIELD_SELECTION_DEFAULTS, **dict(runtime.get("field_selection") or {})}
    field_selection["random_fraction"] = _finite_float(
        field_selection["random_fraction"],
        key="config.runtime.field_selection.random_fraction",
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
        key="config.runtime.field_selection.min_datasets",
        minimum=1,
    )
    field_selection["min_cross_dataset_pairs"] = _int_in_range(
        field_selection["min_cross_dataset_pairs"],
        key="config.runtime.field_selection.min_cross_dataset_pairs",
        minimum=0,
    )
    raw_pool = field_selection.get("dataset_pool") or []
    if isinstance(raw_pool, (str, int)):
        raw_pool = [raw_pool]
    if not isinstance(raw_pool, list):
        raise ValueError("config.runtime.field_selection.dataset_pool 必须是数组")
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
        raise ValueError(f"config.runtime.field_selection.{key} 必须是布尔值")

    return field_selection

def parse_config(raw):
    if not isinstance(raw, dict) or not isinstance(raw.get("simulation", {}), dict):
        raise ValueError("config.simulation 必须是对象")  # noqa: TRY004
    runtime_raw = raw.get("runtime", {})
    if not isinstance(runtime_raw, dict):
        raise ValueError("config.runtime 必须是对象")  # noqa: TRY004
    remote_cache_raw = raw.get("remote_cache", {})
    if not isinstance(remote_cache_raw, dict):
        raise ValueError("config.remote_cache 必须是对象")
    remote_cache = RemoteCacheConfig(_int_in_range(
        remote_cache_raw.get("retention_days", 7),
        key="config.remote_cache.retention_days", minimum=1, maximum=90,
    ))
    quota_raw = raw.get("quota", {})
    if not isinstance(quota_raw, dict):
        raise ValueError("config.quota 必须是对象")
    rolling_days = _int_in_range(
        quota_raw.get("rolling_days", 7), key="config.quota.rolling_days",
        minimum=1, maximum=90,
    )
    rolling_limit = _int_in_range(
        quota_raw.get("rolling_limit", 11200), key="config.quota.rolling_limit",
        minimum=0,
    )
    daily_limit = _int_in_range(
        quota_raw.get("daily", 1600), key="config.quota.daily", minimum=0,
    )
    if daily_limit > rolling_limit:
        raise ValueError("config.quota.daily 不得超过 config.quota.rolling_limit")
    quota = QuotaConfig(daily_limit, rolling_days, rolling_limit)
    factory_raw = raw.get("factory", {})
    if not isinstance(factory_raw, dict):
        raise ValueError("config.factory 必须是对象")
    factory = FactoryConfig(_int_in_range(
        factory_raw.get("default_probe_count", 100),
        key="config.factory.default_probe_count", minimum=1,
    ))
    field_selection = _resolve_field_selection(runtime_raw)
    runtime = RuntimeConfig(
        state_dir=str(runtime_raw.get("state_dir", ".wqb_state")),
        alpha_template_catalog=(
            str(runtime_raw["alpha_template_catalog"])
            if runtime_raw.get("alpha_template_catalog") is not None else None
        ),
        smoke_dataset=(
            str(runtime_raw["smoke_dataset"])
            if runtime_raw.get("smoke_dataset") is not None
            else None
        ),
        max_concurrent_sims=_int_in_range(
            runtime_raw.get("max_concurrent_sims", 10),
            key="config.runtime.max_concurrent_sims",
            minimum=1,
        ),
        fields_per_discovery=_int_in_range(
            runtime_raw.get("fields_per_discovery", 6),
            key="config.runtime.fields_per_discovery",
            minimum=1,
        ),
        pagination_limit=_int_in_range(
            runtime_raw.get("pagination_limit", 50),
            key="config.runtime.pagination_limit",
            minimum=1,
        ),
        max_pagination_pages=_int_in_range(
            runtime_raw.get("max_pagination_pages", 20),
            key="config.runtime.max_pagination_pages",
            minimum=1,
        ),
        poll_timeout_sec=_finite_float(
            runtime_raw.get("poll_timeout_sec", 1500),
            key="config.runtime.poll_timeout_sec",
            minimum=0.0,
        ),
        repoll_attempts=_int_in_range(
            runtime_raw.get("repoll_attempts", 3),
            key="config.runtime.repoll_attempts",
            minimum=1,
        ),
        repoll_backoff_sec=_finite_float(
            runtime_raw.get("repoll_backoff_sec", 60),
            key="config.runtime.repoll_backoff_sec",
            minimum=0.0,
        ),
        fields_cache_ttl_sec=_finite_float(
            runtime_raw.get("fields_cache_ttl_sec", 7 * 24 * 3600),
            key="config.runtime.fields_cache_ttl_sec",
            minimum=0.0,
        ),
        max_field_alpha_count=_optional_int_in_range(
            field_selection.get("max_alpha_count"),
            key="config.runtime.field_selection.max_alpha_count",
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
        quota=quota,
        runtime=runtime,
    )


def normalize_config(config):
    """Normalize the one supported external config boundary."""
    if isinstance(config, AppConfig):
        return config
    if isinstance(config, (str, PathLike)):
        with open(config, encoding="utf-8-sig") as handle:
            config = json.load(handle)
    if isinstance(config, Mapping):
        return parse_config(dict(config))
    raise TypeError("config 必须是 AppConfig、配置对象或配置文件路径")


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
