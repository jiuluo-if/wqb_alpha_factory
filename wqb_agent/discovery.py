"""ROLE: CORE
AGENT_RELEVANCE: HIGH
PURPOSE: Discover and profile fields using current BRAIN data or bounded cache.
READ WHEN: changing field/dataset discovery or provenance.
DO NOT USE FOR: inventing field semantics or choosing economic hypotheses.
"""

import hashlib
import json
import math
import os
import time
from copy import deepcopy
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from .artifacts import atomic_write_json_if_changed
from .discovery_selection import (
    categorize_hypothesis,
    keywords_from_hypothesis,
    rank_fields,
    select_active_dataset_ids,
)
from .field_catalog import (
    catalog_scope,
    manifest_is_complete,
    normalize_dataset_ids,
    valid_platform_count,
)
from .field_metadata import (
    dataset_description_frequency,
    frequency_evidence,
    normalize_coverage,
    normalize_frequency,
    profile_field,
    profile_frequency_evidence,
)

__all__ = [
    "FieldDiscovery", "dataset_description_frequency", "frequency_evidence",
    "normalize_coverage", "normalize_frequency", "profile_frequency_evidence",
]

DATASET_CATEGORIES = {
    "analyst": ["analyst4"],
    "fundamental": ["fundamental2", "fundamental6"],
    "model": ["model16", "model51"],
    "news": ["news12", "news18"],
    "option": ["option8", "option9"],
    "price_volume": ["pv1", "pv13", "univ1"],
    "social": ["socialmedia12", "socialmedia8"],
}

CATEGORY_VALUE = {
    "model": 7,
    "option": 6,
    "analyst": 5,
    "fundamental": 3,
    "news": 3,
    "price_volume": 2,
    "social": 2,
}

CATEGORY_KEYWORDS = {
    "analyst": [
        "analyst", "recommendation", "target", "rating", "estimate", "eps",
        "consensus", "revision", "forecast", "broker",
    ],
    "fundamental": [
        "fundamental", "revenue", "earning", "margin", "ratio", "balance",
        "cashflow", "cash", "debt", "asset", "equity", "profit", "growth",
    ],
    "model": [
        "model", "score", "sentiment", "forecast", "prediction", "probability",
        "factor", "composite", "risk",
    ],
    "news": [
        "news", "article", "headline", "mention", "buzz", "press", "release",
    ],
    "option": [
        "option", "volatility", "implied", "iv", "put", "call", "gamma",
        "delta", "skew", "greeks", "open_interest", "oi",
    ],
    "price_volume": [
        "price", "volume", "return", "close", "open", "high", "low", "adv",
        "liquidity", "turnover", "volatility",
    ],
    "social": [
        "social", "tweet", "post", "mention", "reddit", "discussion",
        "crowdsource", "score",
    ],
}


class FieldDiscovery:
    CACHE_SCHEMA = 2

    # 拉取 MATRIX 与 VECTOR 两类字段，供提案按平台真实类型选择合法算子。
    # 用户 2026-08-20 校正：vec_avg/vec_sum 的输入必须是 VECTOR，输出为
    # 可继续参与矩阵运算的 MATRIX 信号；两类仍都要发现，但用途不同。
    FIELD_TYPES = ("MATRIX", "VECTOR")
    MAX_ACTIVE_DATASETS = 12

    def __init__(self, client, pagination_limit=50, max_pages=20,
                 cache_path=None, cache_ttl_sec=7 * 24 * 3600,
                 catalog_root=None, max_alpha_count=None,
                 selection_mode="semantic", random_fraction=0.35,
                 random_seed="newwqb", platform_usage_refresh=False,
                 require_platform_alpha_count=False,
                 dataset_sampling="stratified", min_datasets=2,
                 dataset_pool=None, persist_catalog=False,
                 candidate_pool_size=100):
        """Field discovery with a two-level cache: in-memory (per run) and
        on-disk (cross-run, keyed by dataset id, TTL-bounded). A large
        pagination walk is only re-done when the cache is missing or stale,
        which keeps repeated rounds cheap without freezing the catalog.
        """
        self.client = client
        self.pagination_limit = pagination_limit
        self.max_pages = max_pages
        self._cache = {}
        self.cache_path = cache_path
        self.cache_ttl_sec = cache_ttl_sec
        self.max_alpha_count = max_alpha_count
        self.platform_usage_refresh = bool(platform_usage_refresh)
        self.require_platform_alpha_count = bool(require_platform_alpha_count)
        self.selection_mode = str(selection_mode or "semantic_random").lower()
        self.dataset_sampling = str(dataset_sampling or "stratified").lower()
        try:
            self.min_datasets = max(1, int(min_datasets))
        except (TypeError, ValueError):
            self.min_datasets = 2
        self.dataset_pool = normalize_dataset_ids(dataset_pool)
        self.persist_catalog = bool(persist_catalog)
        self.catalog_root = catalog_root or (
            os.path.dirname(cache_path) if cache_path else None
        )
        try:
            self.random_fraction = max(0.0, min(1.0, float(random_fraction)))
        except (TypeError, ValueError):
            self.random_fraction = 0.35
        self.random_seed = str(random_seed or "newwqb")
        try:
            self.candidate_pool_size = max(1, int(candidate_pool_size))
        except (TypeError, ValueError):
            self.candidate_pool_size = 100
        self.last_excluded_high_usage = []
        self.last_excluded_unknown_usage = []
        self._platform_usage_provenance = None
        self._platform_usage_status = {}
        self.last_dataset_selection = {}
        self._field_completeness = {}
        self._dataset_snapshot = None
        self._catalog_status = "UNKNOWN"
        self._catalog_has_legacy_unverified = False
        self._dataset_universe_provenance = {
            "kind": "seed_fallback",
            "source": "DATASET_CATEGORIES",
        }
        self._candidate_counts = {}
        self._catalog_provenance, catalog = self._load_latest_catalog(
            self.catalog_root
        )
        if self._catalog_provenance:
            self._field_completeness = deepcopy(
                self._catalog_provenance.get("field_completeness") or {}
            )
            self._catalog_status = str(
                self._catalog_provenance.get("catalog_status") or "UNKNOWN"
            )
            self._catalog_has_legacy_unverified = (
                self._catalog_status == "LEGACY_UNVERIFIED"
            )
            self._dataset_universe_provenance = deepcopy(
                self._catalog_provenance.get("dataset_universe")
                or self._dataset_universe_provenance
            )
        # 完整的本地目录是不可变字段证据源；旧 fields_cache 只作为
        # 没有目录时的跨运行回退，避免生产运行再次复制目录内容。
        self._using_catalog = self._catalog_provenance is not None
        self._disk_cache = catalog or self._load_disk_cache()

    def _load_latest_catalog(self, catalog_root=None):
        """Load the newest structurally valid metadata catalog, if present.

        Completeness is data carried by the manifest, never inferred from the
        presence of dataset files.  Legacy manifests remain readable for
        compatibility but are explicitly marked ``LEGACY_UNVERIFIED``.
        """
        root = catalog_root or (os.path.dirname(self.cache_path) if self.cache_path else None)
        if not root:
            return None, {}
        latest = None
        try:
            entries = os.scandir(root)
        except OSError:
            return None, {}
        with entries:
            for entry in entries:
                if not entry.name.startswith("platform_field_catalog_"):
                    continue
                try:
                    if not entry.is_dir():
                        continue
                except OSError:
                    continue
                directory = entry.path
                manifest_path = os.path.join(directory, "manifest.json")
                try:
                    with open(manifest_path, encoding="utf-8") as f:
                        manifest = json.load(f)
                    if not isinstance(manifest, dict):
                        continue
                    expected_scope = {
                        "instrument_type": getattr(self.client, "instrument_type", "EQUITY"),
                        "region": getattr(self.client, "region", "USA"),
                        "delay": getattr(self.client, "delay", 1),
                        "universe": getattr(self.client, "universe", "TOP3000"),
                    }
                    manifest_scope = manifest.get("scope")
                    if isinstance(manifest_scope, dict) and any(
                        manifest_scope.get(key) != value
                        for key, value in expected_scope.items()
                        if key in manifest_scope
                    ):
                        # A field id is only meaningful within the same BRAIN
                        # query scope.  Do not reuse a complete catalog from a
                        # different region/universe/instrument configuration.
                        continue
                    datasets = manifest.get("datasets") or {}
                    if (
                        not isinstance(datasets, dict)
                        or not datasets
                        or any(not isinstance(row, dict) for row in datasets.values())
                        or any(
                            not isinstance(row.get("file"), str)
                            or not os.path.isfile(os.path.join(directory, row["file"]))
                            for row in datasets.values()
                        )
                    ):
                        continue
                    catalog = {}
                    for dataset_id, row in datasets.items():
                        with open(
                            os.path.join(directory, row["file"]),
                            encoding="utf-8",
                        ) as dataset_handle:
                            dataset_payload = json.load(dataset_handle)
                        if (not isinstance(dataset_payload, dict)
                                or not isinstance(dataset_payload.get("fields"), list)):
                            raise ValueError("catalog dataset payload must contain a fields list")
                        catalog[dataset_id] = dataset_payload["fields"]
                    candidate = (entry.name, directory, manifest, catalog)
                    if latest is None or candidate[0] > latest[0]:
                        latest = candidate
                except (OSError, ValueError, json.JSONDecodeError):
                    continue
        if latest is None:
            return None, {}
        _name, directory, manifest, datasets = latest
        field_completeness = manifest.get("field_completeness")
        if not isinstance(field_completeness, dict):
            field_completeness = {}
        catalog_status = manifest.get("catalog_status", "LEGACY_UNVERIFIED")
        if catalog_status == "COMPLETE" and not manifest_is_complete(
            field_completeness, datasets, self.FIELD_TYPES
        ):
            catalog_status = "INCOMPLETE"
        return {
            "kind": "local_catalog",
            "path": os.path.abspath(directory),
            "snapshot_date": manifest.get("fetched_at"),
            "catalog_status": catalog_status,
            "field_completeness": field_completeness,
            "dataset_universe": manifest.get("dataset_universe") or {
                "kind": "legacy_catalog",
                "source": "manifest",
            },
        }, datasets

    def source_provenance(self):
        base = {
            "kind": "brain_api",
            "path": None,
            "snapshot_date": None,
        }
        if self._platform_usage_provenance:
            base.update(self._platform_usage_provenance)
        elif self._catalog_provenance:
            base.update(self._catalog_provenance)
        base["field_completeness"] = deepcopy(self._field_completeness)
        base["catalog_status"] = self._catalog_status
        base["dataset_universe"] = deepcopy(self._dataset_universe_provenance)
        return base

    @staticmethod
    def _alpha_count(field):
        """Return a usable platform field-usage count from either API spelling."""
        if not isinstance(field, dict):
            return None
        value = field.get("alphaCount")
        if value is None:
            value = field.get("alpha_count")
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(number) or number < 0:
            return None
        return value

    def platform_dedupe_view(self):
        """Expose only live field-usage status, never Simulation/Alpha results."""
        return {
            "source": self.source_provenance(),
            "status_by_dataset": dict(self._platform_usage_status),
            "require_alpha_count": self.require_platform_alpha_count,
            "max_alpha_count": self.max_alpha_count,
        }

    def platform_usage_by_field(self, dataset_ids):
        """Return current platform counts keyed by dataset then field id."""
        result = {}
        normalized = []
        for value in dataset_ids or []:
            if isinstance(value, dict):
                value = value.get("id") or value.get("name")
            if isinstance(value, (str, int)) and str(value).strip():
                value = str(value)
                if value not in normalized:
                    normalized.append(value)
        for dataset_id in normalized:
            dataset_status = self._platform_usage_status.get(dataset_id)
            dataset_result = result.setdefault(dataset_id, {})
            for field in self._cache.get(dataset_id, self._disk_cache.get(dataset_id, [])):
                if not isinstance(field, dict) or not field.get("id"):
                    continue
                alpha_count = self._alpha_count(field) if dataset_status == "KNOWN" else None
                dataset_result[str(field["id"])] = {
                    "alpha_count": alpha_count,
                    "status": (
                        "KNOWN" if alpha_count is not None else (dataset_status or "UNKNOWN")
                    ),
                    "source": self.source_provenance().get("kind"),
                }
        return result

    def cached_datasets(self):
        """Return the active field evidence without creating another cache.

        Consumers that need manually reviewed profiles should use the same
        catalog/cache selected by discovery, otherwise a complete immutable
            catalog remains an input for discovery only.
        """
        # Only the immutable catalog is safe to reuse as an already parsed
        # source; the rebuildable cache remains isolated to discovery.
        return self._disk_cache if self._using_catalog else None

    def _load_disk_cache(self):
        if not self.cache_path or not os.path.exists(self.cache_path):
            return {}
        try:
            with open(self.cache_path, encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return {}
            if data.get("schema") != self.CACHE_SCHEMA:
                return {}
            saved_at = data.get("saved_at", 0)
            if time.time() - float(saved_at) > self.cache_ttl_sec:
                return {}
            datasets = data.get("datasets", {})
            return datasets if isinstance(datasets, dict) else {}
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return {}

    def _save_disk_cache(self):
        if not self.cache_path or self._using_catalog:
            return
        data = {
            "schema": self.CACHE_SCHEMA,
            "saved_at": time.time(),
            "datasets": self._disk_cache,
        }
        try:
            # Refresh the timestamp only when the dataset payload itself
            # changed; metadata-only writes are not new discovery artifacts.
            atomic_write_json_if_changed(
                self.cache_path, data, ignored_keys=("saved_at",),
                indent=None,
            )
        except OSError:
            pass

    def _persist_field_catalog(self, dataset_ids):
        """Write a metadata-only, New-York-day field snapshot.

        The catalog deliberately contains no Simulation, Alpha, metric, or
        submission payload.  Dataset files are written before the manifest, so
        an interrupted write cannot be mistaken for a valid snapshot by the
        loader; completeness is carried explicitly in the manifest.
        """
        if not self.persist_catalog or not self.catalog_root:
            return None
        normalized = normalize_dataset_ids(dataset_ids)
        if not normalized:
            return None
        local_day = datetime.now(UTC).astimezone(
            ZoneInfo("America/New_York")
        ).strftime("%Y%m%d")
        directory = os.path.join(
            self.catalog_root, f"platform_field_catalog_{local_day}"
        )
        try:
            os.makedirs(directory, exist_ok=True)
        except OSError:
            return None
        fetched_at = datetime.now(UTC).isoformat()
        manifest_datasets = {}
        for dataset_id in normalized:
            fields = self._cache.get(dataset_id, self._disk_cache.get(dataset_id, []))
            if not isinstance(fields, list):
                fields = []
            payload = {
                "schema": 1,
                "dataset_id": dataset_id,
                "fetched_at": fetched_at,
                "scope": catalog_scope(self.client),
                "fields": fields,
            }
            encoded = json.dumps(
                fields, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
            digest = hashlib.sha256(encoded).hexdigest()
            filename = f"dataset_{hashlib.sha256(dataset_id.encode('utf-8')).hexdigest()[:16]}.json"
            try:
                atomic_write_json_if_changed(
                    os.path.join(directory, filename), payload, indent=None
                )
            except OSError:
                return None
            manifest_datasets[dataset_id] = {
                "file": filename,
                "field_count": len(fields),
                "field_sha256": digest,
                "status": self._platform_usage_status.get(dataset_id, "UNKNOWN"),
                "completeness": deepcopy(
                    self._field_completeness.get(dataset_id, {})
                ),
            }
        completeness = {
            dataset_id: deepcopy(self._field_completeness.get(dataset_id, {}))
            for dataset_id in normalized
        }
        if not completeness or any(
            not rows or any(not row.get("complete") for row in rows.values())
            for rows in completeness.values()
        ):
            catalog_status = "INCOMPLETE"
        else:
            catalog_status = "COMPLETE"
        manifest = {
            "schema": 2,
            "kind": "platform_field_catalog",
            "fetched_at": fetched_at,
            "local_date": local_day,
            "scope": catalog_scope(self.client),
            "catalog_status": catalog_status,
            "field_completeness": completeness,
            "dataset_universe": deepcopy(self._dataset_universe_provenance),
            "datasets": manifest_datasets,
        }
        try:
            atomic_write_json_if_changed(
                os.path.join(directory, "manifest.json"), manifest, indent=None
            )
        except OSError:
            return None
        return directory

    def categorize_hypothesis(self, hypothesis):
        return categorize_hypothesis(hypothesis, CATEGORY_KEYWORDS, CATEGORY_VALUE)

    def _set_field_completeness(self, dataset_id, field_type, contract):
        dataset_contract = self._field_completeness.setdefault(dataset_id, {})
        dataset_contract[field_type] = dict(contract)
        if self._catalog_has_legacy_unverified:
            self._catalog_status = "LEGACY_UNVERIFIED"
            return
        expected_types = set(self.FIELD_TYPES)
        if not self._field_completeness:
            self._catalog_status = "UNKNOWN"
        elif any(
            set(rows) != expected_types or any(
                not rows[field_type].get("complete") for field_type in expected_types
            )
            for rows in self._field_completeness.values()
        ):
            self._catalog_status = "INCOMPLETE"
        elif all(
            set(rows) == expected_types
            and all(rows[field_type].get("complete") for field_type in expected_types)
            for rows in self._field_completeness.values()
        ):
            self._catalog_status = "COMPLETE"
        else:
            self._catalog_status = "INCOMPLETE"

    def _fetch_field_type(self, dataset_id, field_type):
        contract = {
            "expected_count": None,
            "loaded_count": 0,
            "complete": False,
            "truncation_reason": None,
        }
        collected = []
        try:
            page_budget = max(1, int(self.max_pages))
        except (TypeError, ValueError):
            page_budget = 1
        offset = 0
        expected_count = None
        for _page_number in range(page_budget):
            try:
                results, count = self.client.get_datafields(
                    dataset_id,
                    limit=self.pagination_limit,
                    offset=offset,
                    field_type=field_type,
                )
            except Exception:
                contract["truncation_reason"] = "API_ERROR"
                break
            if not isinstance(results, list):
                contract["truncation_reason"] = "MALFORMED_PAGE"
                break
            if not valid_platform_count(count):
                contract["truncation_reason"] = (
                    "MISSING_COUNT" if count is None else "INVALID_COUNT"
                )
                break
            if expected_count is None:
                expected_count = count
                contract["expected_count"] = count
            elif count != expected_count:
                contract["truncation_reason"] = "COUNT_CHANGED"
                break

            previous_offset = offset
            collected.extend(results)
            contract["loaded_count"] += len(results)
            if contract["loaded_count"] > expected_count:
                contract["truncation_reason"] = "COUNT_OVERFLOW"
                break
            if contract["loaded_count"] == expected_count:
                contract["complete"] = True
                break
            if not results:
                contract["truncation_reason"] = "COUNT_UNDERRUN"
                break
            offset += len(results)
            if offset <= previous_offset:
                contract["truncation_reason"] = "NON_PROGRESS"
                break
        else:
            contract["truncation_reason"] = "MAX_PAGES"
        if not contract["complete"] and contract["truncation_reason"] is None:
            contract["truncation_reason"] = "COUNT_UNDERRUN"
        self._set_field_completeness(dataset_id, field_type, contract)
        return collected

    def _fields_for(self, dataset_id, persist=True, force_refresh=False):
        if not force_refresh and dataset_id in self._cache:
            return self._cache[dataset_id]
        if not force_refresh and dataset_id in self._disk_cache:
            self._cache[dataset_id] = self._disk_cache[dataset_id]
            return self._cache[dataset_id]
        collected = []
        for ftype in self.FIELD_TYPES:
            collected.extend(self._fetch_field_type(dataset_id, ftype))
        # 同一 dataset 的不同类型可能返回重复 id（极端情况），去重保序。
        seen_ids = set()
        deduped = []
        for field in collected:
            if not isinstance(field, dict):
                continue
            fid = field.get("id")
            if not isinstance(fid, (str, int)):
                continue
            fid = str(fid)
            if fid in seen_ids:
                continue
            seen_ids.add(fid)
            field = dict(field)
            field["id"] = fid
            deduped.append(field)
        self._cache[dataset_id] = deduped
        self._disk_cache[dataset_id] = deduped
        if persist:
            self._save_disk_cache()
        return deduped

    def refresh_platform_usage(self, dataset_ids):
        """Refresh field metadata so alphaCount comes from BRAIN, not old cache.

        This is intentionally a read-only `/data-fields` refresh.  It updates
        the discovery cache only; Simulation results and submitted Alpha
        payloads are never reconstructed or persisted here.
        """
        normalized = []
        for value in dataset_ids or []:
            if isinstance(value, dict):
                value = value.get("id") or value.get("name")
            if isinstance(value, (str, int)) and str(value).strip():
                value = str(value)
                if value not in normalized:
                    normalized.append(value)
        if not self.platform_usage_refresh or not normalized:
            return
        self._platform_usage_status = {}
        for dataset_id in normalized:
            try:
                fields = self._fields_for(
                    dataset_id, persist=False, force_refresh=True
                )
            except Exception:
                self._platform_usage_status[dataset_id] = "UNAVAILABLE"
                continue
            known = sum(1 for field in fields if self._alpha_count(field) is not None)
            self._platform_usage_status[dataset_id] = "KNOWN" if known else "UNKNOWN"
        self._platform_usage_provenance = {
            "kind": "brain_api",
            "path": None,
            "snapshot_date": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        self._save_disk_cache()

    @staticmethod
    def _dataset_id(field):
        dataset = field.get("dataset")
        if isinstance(dataset, dict):
            dataset = dataset.get("id") or dataset.get("name")
        return str(dataset) if isinstance(dataset, (str, int)) else None

    @staticmethod
    def _keywords_from_hypothesis(hypothesis, limit=12):
        return keywords_from_hypothesis(hypothesis, limit)

    def _live_dataset_snapshot(self):
        """Return one current-round dataset listing snapshot."""
        if self._dataset_snapshot is not None:
            return self._dataset_snapshot
        getter = getattr(self.client, "get_datasets", None)
        payload = None
        status = "UNAVAILABLE"
        if callable(getter):
            try:
                candidate = getter()
            except Exception:
                candidate = None
            if isinstance(candidate, list):
                payload = [item for item in candidate if isinstance(item, dict)]
                status = "LIVE"
        normalized = [
            {
                key: item[key]
                for key in ("id", "name", "description", "category", "type")
                if key in item and item[key] is not None
            }
            for item in (payload or [])
            if item.get("id") is not None
        ]
        fingerprint = hashlib.sha256(
            json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest() if status == "LIVE" else None
        observed_at = datetime.now(UTC).isoformat() if status == "LIVE" else None
        self._dataset_snapshot = {
            "payload": payload or [],
            "ids": normalize_dataset_ids(normalized),
            "description_map": {
                str(item["id"]): str(item.get("description") or "")
                for item in normalized
            },
            "observed_at": observed_at,
            "fingerprint": fingerprint,
            "status": status,
        }
        return self._dataset_snapshot

    def _dynamic_dataset_ids(self):
        getter = getattr(self.client, "get_datasets", None)
        if not callable(getter):
            self._dataset_universe_provenance = {
                "kind": "seed_fallback",
                "source": "DATASET_CATEGORIES",
                "reason": "CLIENT_CAPABILITY_UNAVAILABLE",
            }
            return []
        snapshot = self._live_dataset_snapshot()
        if snapshot["status"] != "LIVE":
            self._dataset_universe_provenance = {
                "kind": "seed_fallback",
                "source": "DATASET_CATEGORIES",
                "reason": "DYNAMIC_LISTING_UNAVAILABLE",
            }
            return []
        dataset_ids = snapshot["ids"]
        if not dataset_ids:
            self._dataset_universe_provenance = {
                "kind": "seed_fallback",
                "source": "DATASET_CATEGORIES",
                "reason": "EMPTY_DYNAMIC_LISTING",
            }
            return []
        self._dataset_universe_provenance = {
            "kind": "brain_api",
            "source": "get_datasets",
            "status": "KNOWN",
            "count": len(dataset_ids),
        }
        return dataset_ids

    def _dataset_description_map(self):
        """Cached dataset-id -> description map from the live listing.

        Read-only; fails closed to an empty map when the client or the
        listing is unavailable so the frequency fallback simply stays UNKNOWN.
        """
        return self._live_dataset_snapshot()["description_map"]

    def _dataset_description(self, dataset_id):
        return self._dataset_description_map().get(str(dataset_id))

    def _select_active_dataset_ids(self, dataset_ids, categories, keywords, round_no):
        selected, provenance = select_active_dataset_ids(
            normalize_dataset_ids(dataset_ids), categories, keywords, round_no,
            self.random_seed, self.MAX_ACTIVE_DATASETS, DATASET_CATEGORIES,
            self._dataset_universe_provenance,
        )
        self._dataset_universe_provenance = provenance
        return selected

    def discover(self, hypothesis, target_count=6):
        hypothesis = hypothesis if isinstance(hypothesis, dict) else {}
        try:
            target_count = max(0, int(target_count))
        except (TypeError, ValueError):
            target_count = 0
        self.last_excluded_high_usage = []
        self.last_excluded_unknown_usage = []
        self._candidate_counts = {}
        self._dataset_snapshot = None
        keywords = self._keywords_from_hypothesis(hypothesis)
        chosen = []
        seen = set()

        # Preferred datasets first (carried over from memory: hypothesis
        # "datasets" / "dataset_hints" or next-idea datasets). Only fields
        # matching the hypothesis keywords are kept, so the same dataset
        # can be reused across rounds without repeating formulas.
        preferred_ids = normalize_dataset_ids(
            hypothesis.get("datasets") or hypothesis.get("dataset_hints") or []
        )
        categories = self.categorize_hypothesis(hypothesis)
        dataset_ids = list(preferred_ids)
        dynamic_ids = []
        if preferred_ids:
            self._dataset_universe_provenance = {
                "kind": "explicit_scope",
                "source": "hypothesis.datasets",
                "count": len(preferred_ids),
            }
        if not dataset_ids:
            if self.dataset_pool:
                dataset_ids.extend(self.dataset_pool)
                self._dataset_universe_provenance = {
                    "kind": "configured_pool",
                    "source": "dataset_pool",
                    "count": len(self.dataset_pool),
                }
            else:
                dynamic_ids = self._dynamic_dataset_ids()
                dataset_ids.extend(dynamic_ids)
            if not dynamic_ids:
                for category in categories:
                    for dataset_id in DATASET_CATEGORIES[category]:
                        if dataset_id not in dataset_ids:
                            dataset_ids.append(dataset_id)
            if not dynamic_ids and self.selection_mode in {
                "random", "semantic_random", "broad"
            }:
                for dataset_group in DATASET_CATEGORIES.values():
                    for dataset_id in dataset_group:
                        if dataset_id not in dataset_ids:
                            dataset_ids.append(dataset_id)
        if not dataset_ids:
            dataset_ids = [
                dataset_id
                for dataset_group in DATASET_CATEGORIES.values()
                for dataset_id in dataset_group
            ]
        if dynamic_ids:
            dataset_ids = self._select_active_dataset_ids(
                dataset_ids, categories, keywords, hypothesis.get("_round", 0)
            )
        self.refresh_platform_usage(dataset_ids)

        ranked_by_dataset = {}
        for dataset_id in dataset_ids:
            category = next(
                (name for name, values in DATASET_CATEGORIES.items()
                 if dataset_id in values),
                None,
            )
            ranked_by_dataset[dataset_id] = self._rank_fields_for_dataset(
                dataset_id, keywords, category, hypothesis
            )
        round_no = hypothesis.get("_round", 0)
        ordered_datasets = sorted(
            dataset_ids,
            key=lambda dataset_id: hashlib.sha256(
                f"{self.random_seed}|{round_no}|{dataset_id}".encode()
            ).hexdigest(),
        )
        available = [dataset_id for dataset_id in ordered_datasets
                     if ranked_by_dataset.get(dataset_id)]
        # An explicitly named single dataset remains an intentional scope.  A
        # multi-dataset scope, category scope, or configured pool is sampled
        # round-robin so semantic ranking cannot consume the whole batch from
        # the first dataset.
        stratified = (
            self.dataset_sampling in {"stratified", "random", "semantic_random", "broad"}
            and len(available) > 1
            and len(preferred_ids) != 1
        )
        offsets = {dataset_id: 0 for dataset_id in available}
        while len(chosen) < target_count and available:
            progressed = False
            for dataset_id in available:
                if len(chosen) >= target_count:
                    break
                ranked = ranked_by_dataset[dataset_id]
                index = offsets[dataset_id]
                if index >= len(ranked):
                    continue
                _rank, score, field, actual_dataset, ranking_provenance = ranked[index]
                offsets[dataset_id] = index + 1
                key = (actual_dataset, str(field.get("id")))
                if key in seen:
                    continue
                chosen.append(self._profile_from_field(
                    actual_dataset, field, score, category=(
                        next((name for name, values in DATASET_CATEGORIES.items()
                              if actual_dataset in values), "preferred")
                    ), ranking_provenance=ranking_provenance,
                ))
                seen.add(key)
                progressed = True
            if not stratified and chosen:
                # Sequential mode is retained only for an explicitly selected
                # single dataset or a caller that asks for it.
                if len(chosen) >= target_count:
                    break
                if len(preferred_ids) != 1:
                    # In non-stratified mode, consume the first available pool.
                    available = available[:1]
            if not progressed:
                break
        selected_counts = {}
        for field in chosen:
            dataset_id = str(field.get("dataset"))
            selected_counts[dataset_id] = selected_counts.get(dataset_id, 0) + 1
        self.last_dataset_selection = {
            "strategy": "stratified_round_robin" if stratified else "scoped_ranked",
            "seed": self.random_seed,
            "round": round_no,
            "pool": list(dataset_ids),
            "ordered_pool": ordered_datasets,
            "available": available,
            "min_datasets": self.min_datasets,
            "selected_counts": selected_counts,
            "available_counts": {
                dataset_id: len(ranked_by_dataset.get(dataset_id, []))
                for dataset_id in ordered_datasets
            },
            "candidate_counts": dict(self._candidate_counts),
            "dataset_universe": deepcopy(self._dataset_universe_provenance),
            "rejections": {
                "high_usage": list(self.last_excluded_high_usage),
                "unknown_usage": list(self.last_excluded_unknown_usage),
            },
        }
        self._persist_field_catalog(dataset_ids)
        # A discovery pass may touch several datasets. Persist the merged
        # cache once, after all fields for this pass have been collected.
        self._save_disk_cache()
        return chosen

    def _rank_fields_for_dataset(self, dataset_id, keywords, category, hypothesis):
        try:
            fields = self._fields_for(dataset_id, persist=False)
        except Exception:
            return []
        ranked, diagnostics = rank_fields(
            fields, dataset_id, keywords,
            selection_mode=self.selection_mode,
            random_seed=self.random_seed,
            random_fraction=self.random_fraction,
            candidate_pool_size=self.candidate_pool_size,
            max_alpha_count=self.max_alpha_count,
            require_platform_alpha_count=self.require_platform_alpha_count,
            hypothesis=hypothesis,
        )
        self.last_excluded_high_usage.extend(diagnostics["excluded_high_usage"])
        self.last_excluded_unknown_usage.extend(diagnostics["excluded_unknown_usage"])
        self._candidate_counts[dataset_id] = diagnostics["candidate_count"]
        return ranked
    def _profile_from_field(
        self, dataset_id, field, score, category, ranking_provenance=None
    ):
        snapshot = self._live_dataset_snapshot()
        return profile_field(
            field, dataset_id, score, category, ranking_provenance,
            alpha_count=self._alpha_count(field),
            dataset_description=self._dataset_description(dataset_id),
            dataset_snapshot=snapshot,
            source_provenance=self.source_provenance(),
        )

    def _collect_from_dataset(
        self, dataset_id, keywords, chosen, seen, target_count, category,
        hypothesis=None,
    ):
        ranked = self._rank_fields_for_dataset(dataset_id, keywords, category, hypothesis)
        for _rank, score, field, actual_dataset, ranking_provenance in ranked:
            if len(chosen) >= target_count:
                break
            key = (actual_dataset, str(field.get("id")))
            if key in seen:
                continue
            chosen.append(self._profile_from_field(
                actual_dataset, field, score, category, ranking_provenance
            ))
            seen.add(key)

