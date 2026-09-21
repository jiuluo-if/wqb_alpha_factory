import json
import os
import tempfile
import unittest
from datetime import date, timedelta

from wqb_agent.alpha_feed_cache import (
    TEMP_RESOURCE_TTL_SEC,
    RemoteAlphaCache,
    refresh_due,
)
from wqb_agent.remote_alpha_repository import RemoteAlphaRepository

FIXED_NOW = 1789560000.0


def _make_cache(path, *, retention_days=7):
    cache = RemoteAlphaCache(
        path, clock=lambda: FIXED_NOW, retention_days=retention_days
    )
    cache.refresh({
        cache.local_date: {
            "simulations": [{"alpha_id": "synthetic-simulation"}],
            "submitted_alphas": [{"alpha_id": "synthetic-submitted"}],
        }
    })
    return cache


def _bytes(path):
    with open(path, "rb") as handle:
        return handle.read()


def _rewrite_payload(path, **updates):
    with open(path, encoding="utf-8") as handle:
        payload = json.load(handle)
    payload.update(updates)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, sort_keys=True)


class TestAlphaFeedCache(unittest.TestCase):
    def test_refresh_due_fails_safe_on_invalid_timestamps(self):
        self.assertTrue(refresh_due("bad", 0))
        self.assertTrue(refresh_due(10, 0, interval_sec=10))
        self.assertFalse(refresh_due(9, 0, interval_sec=10))

    def test_matching_read_returns_payload_without_changing_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "remote.json")
            cache = _make_cache(path)
            before = _bytes(path)

            self.assertEqual(cache.load()["days"], {
                cache.local_date: {
                    "simulations": [{"alpha_id": "synthetic-simulation"}],
                    "submitted_alphas": [{"alpha_id": "synthetic-submitted"}],
                }
            })
            self.assertEqual(
                cache.freshness_snapshot(now=FIXED_NOW)["freshness"],
                "FRESH",
            )
            self.assertEqual(_bytes(path), before)

    def test_contract_mismatches_are_unknown_without_deleting_or_rewriting_cache(self):
        cases = (
            ("schema_version", 999, 7),
            ("timezone", "UTC", 7),
            ("week_start", "2099-01-01", 7),
        )
        for field, value, reader_retention in cases:
            with self.subTest(field=field), tempfile.TemporaryDirectory() as tmp:
                path = os.path.join(tmp, "remote.json")
                _make_cache(path)
                _rewrite_payload(path, **{field: value})
                before = _bytes(path)
                reader = RemoteAlphaCache(
                    path, clock=lambda: FIXED_NOW,
                    retention_days=reader_retention,
                )

                self.assertIsNone(reader.load())
                self.assertEqual(
                    reader.freshness_snapshot(now=FIXED_NOW)["freshness"],
                    "UNKNOWN",
                )
                self.assertTrue(os.path.exists(path))
                self.assertEqual(_bytes(path), before)

    def test_retention_mismatch_is_unknown_without_deleting_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "remote.json")
            _make_cache(path, retention_days=14)
            before = _bytes(path)
            reader = RemoteAlphaCache(
                path, clock=lambda: FIXED_NOW, retention_days=7
            )

            self.assertIsNone(reader.load())
            self.assertEqual(
                reader.freshness_snapshot(now=FIXED_NOW)["freshness"],
                "UNKNOWN",
            )
            self.assertTrue(os.path.exists(path))
            self.assertEqual(_bytes(path), before)

    def test_read_paths_do_not_remove_stale_temp_resources(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "remote.json")
            cache = _make_cache(path)
            stale_temp = path + ".tmp.stale"
            with open(stale_temp, "w", encoding="utf-8") as handle:
                handle.write("synthetic temp")
            stale_at = FIXED_NOW - TEMP_RESOURCE_TTL_SEC - 1
            os.utime(stale_temp, (stale_at, stale_at))
            before = _bytes(path)
            repository = RemoteAlphaRepository(
                None, cache_path=path, retention_days=7,
                clock=lambda: FIXED_NOW,
            )

            cache.load()
            cache.freshness_snapshot(now=FIXED_NOW)
            repository.list_remote_alphas()
            repository.cache_status()

            self.assertTrue(os.path.exists(stale_temp))
            self.assertEqual(_bytes(path), before)

    def test_refresh_owns_stale_temp_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "remote.json")
            cache = _make_cache(path)
            stale_temp = path + ".tmp.stale"
            with open(stale_temp, "w", encoding="utf-8") as handle:
                handle.write("synthetic temp")
            stale_at = FIXED_NOW - TEMP_RESOURCE_TTL_SEC - 1
            os.utime(stale_temp, (stale_at, stale_at))

            result = cache.refresh({
                cache.local_date: {"simulations": [], "submitted_alphas": []}
            })

            self.assertFalse(os.path.exists(stale_temp))
            self.assertEqual(result["expired_resource_count"], 1)

    def test_refresh_keeps_retained_rows_and_dedupes_without_quota_pruning(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "remote.json")
            cache = RemoteAlphaCache(
                path, clock=lambda: FIXED_NOW, retention_days=3,
            )
            local = cache.local_date
            local_day = date.fromisoformat(local)
            days = {
                local: {
                    "simulations": [
                        {"alpha_id": "synthetic-dup"},
                        {"alpha_id": "synthetic-dup"},
                        {"alpha_id": "synthetic-today"},
                    ],
                    "submitted_alphas": [
                        {"alpha_id": "synthetic-submitted"},
                    ],
                },
                (local_day - timedelta(days=1)).isoformat(): {
                    "simulations": [{"alpha_id": "synthetic-yesterday"}],
                    "submitted_alphas": [],
                },
                (local_day - timedelta(days=2)).isoformat(): {
                    "simulations": [{"alpha_id": "synthetic-two-days"}],
                    "submitted_alphas": [],
                },
                (local_day - timedelta(days=3)).isoformat(): {
                    "simulations": [{"alpha_id": "synthetic-expired"}],
                    "submitted_alphas": [],
                },
            }

            result = cache.refresh(days)
            payload = cache.load()

            self.assertEqual(result["simulation_count"], 4)
            self.assertEqual(result["submitted_count"], 1)
            self.assertNotIn("pruned_simulation_count", result)
            self.assertNotIn(
                "synthetic-expired",
                {
                    row["alpha_id"]
                    for bucket in payload["days"].values()
                    for row in bucket["simulations"]
                },
            )
            self.assertEqual(
                {
                    row["alpha_id"]
                    for bucket in payload["days"].values()
                    for row in bucket["simulations"]
                },
                {
                    "synthetic-dup",
                    "synthetic-today",
                    "synthetic-yesterday",
                    "synthetic-two-days",
                },
            )
            self.assertEqual(
                payload["days"][local]["submitted_alphas"],
                [{"alpha_id": "synthetic-submitted"}],
            )


if __name__ == "__main__":
    unittest.main()
