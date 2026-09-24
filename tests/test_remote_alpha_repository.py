import os
import tempfile
import time
import unittest
from datetime import UTC, datetime, timedelta

from wqb_agent import research_api
from wqb_agent.client import WQBQueryTooBroadError
from wqb_agent.query_errors import QueryTooBroadError
from wqb_agent.remote_alpha_repository import RemoteAlphaRepository

FAKE_NOW = datetime(2026, 9, 24, 12, tzinfo=UTC)


class FakeAlphaReader:
    def __call__(self, **kwargs):
        now = FAKE_NOW.isoformat().replace("+00:00", "Z")
        if kwargs.get("status") == "SUBMITTED":
            return [{
                "id": "submitted-1",
                "status": "SUBMITTED",
                "dateCreated": "2026-09-16T12:00:00Z",
                "dateSubmitted": now,
            }]
        created = (FAKE_NOW - timedelta(days=2)).isoformat().replace("+00:00", "Z")
        return [{"id": "simulated-1", "dateCreated": created}]

    get_all_user_alphas = __call__


class FakeEvidenceClient(FakeAlphaReader):
    def get_alpha(self, alpha_id):
        return {"id": str(alpha_id), "is": {"sharpe": 1.0}}

    def get_aggregates(self, alpha_id):
        return {"alpha_id": str(alpha_id), "years": []}

    def get_pnl(self, alpha_id):
        return {"alpha_id": str(alpha_id), "records": []}

    def get_self_correlation(self, alpha_id):
        return {"alpha_id": str(alpha_id), "status": "AVAILABLE", "value": 0.2}


class ManySyntheticAlphaReader:
    def __init__(self, count):
        self.rows = [
            {
                "id": f"synthetic-alpha-{index:04d}",
                "dateCreated": "2026-09-16T12:00:00Z",
            }
            for index in range(count)
        ]

    def __call__(self, **kwargs):
        if kwargs.get("status") == "SUBMITTED":
            return []
        return list(self.rows)


class SubmittedAlphaReader:
    def __init__(self, rows):
        self.rows = list(rows)

    def __call__(self, **kwargs):
        if kwargs.get("status") == "SUBMITTED":
            return list(self.rows)
        return []


class TestRemoteAlphaRepository(unittest.TestCase):
    def test_refresh_keeps_all_retained_metadata_without_quota_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            repository = RemoteAlphaRepository(
                ManySyntheticAlphaReader(1601),
                cache_path=f"{tmp}/remote.json",
                retention_days=1, clock=lambda: 1789560000,
            )

            refreshed = repository.refresh_remote_alphas()

            self.assertEqual(refreshed["rolling_simulated_count"], 1601)
            self.assertEqual(refreshed["simulation_count"], 1601)
            self.assertNotIn("pruned_simulation_count", refreshed)
            self.assertEqual(len(repository.list_remote_alphas()), 1601)

    def test_remote_feed_refresh_has_a_total_shard_budget(self):
        class BroadReader:
            def __init__(self):
                self.calls = 0

            def __call__(self, **_kwargs):
                self.calls += 1
                raise WQBQueryTooBroadError("too broad")

        with tempfile.TemporaryDirectory() as tmp:
            reader = BroadReader()
            repository = RemoteAlphaRepository(
                reader, cache_path=f"{tmp}/remote.json", clock=lambda: 1789560000,
            )
            with self.assertRaisesRegex(QueryTooBroadError, "shard budget"):
                repository.refresh_remote_alphas(max_shards=3)
            self.assertEqual(reader.calls, 3)

    def test_refresh_and_list_use_configured_rolling_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            repository = RemoteAlphaRepository(
                FakeAlphaReader(), cache_path=f"{tmp}/remote.json",
                retention_days=3, clock=lambda: FAKE_NOW.timestamp(),
            )

            refreshed = repository.refresh_remote_alphas()
            listed = repository.list_remote_alphas()

            self.assertEqual(refreshed["retention_days"], 3)
            self.assertEqual({row["alpha_id"] for row in listed},
                             {"submitted-1", "simulated-1"})
            submitted = next(row for row in listed if row["alpha_id"] == "submitted-1")
            self.assertEqual(submitted["date_created"], "2026-09-16T12:00:00Z")
            self.assertEqual(submitted["simulation_local_date"], "2026-09-16")
            self.assertEqual(repository.cache_status()["retention_days"], 3)

    def test_submitted_metadata_keeps_submission_day_separate_from_creation_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            repository = RemoteAlphaRepository(
                SubmittedAlphaReader([{
                    "id": "created-yesterday",
                    "status": "SUBMITTED",
                    "dateCreated": "2026-09-22T12:00:00Z",
                    "dateSubmitted": "2026-09-23T12:00:00Z",
                }]),
                cache_path=f"{tmp}/remote.json", retention_days=7,
                clock=lambda: 1790164800,
            )

            repository.refresh_remote_alphas()
            listed = repository.list_remote_alphas(status="SUBMITTED")

            self.assertEqual(len(listed), 1)
            self.assertEqual(listed[0]["local_date"], "2026-09-23")
            self.assertEqual(listed[0]["date_created"], "2026-09-22T12:00:00Z")
            self.assertEqual(listed[0]["simulation_local_date"], "2026-09-22")

    def test_submitted_metadata_without_creation_date_remains_visible(self):
        with tempfile.TemporaryDirectory() as tmp:
            repository = RemoteAlphaRepository(
                SubmittedAlphaReader([{
                    "id": "missing-created-date",
                    "status": "SUBMITTED",
                    "dateSubmitted": "2026-09-23T12:00:00Z",
                }]),
                cache_path=f"{tmp}/remote.json", retention_days=7,
                clock=lambda: 1790164800,
            )

            repository.refresh_remote_alphas()
            listed = repository.list_remote_alphas(status="SUBMITTED")

            self.assertEqual(len(listed), 1)
            self.assertEqual(listed[0]["local_date"], "2026-09-23")
            self.assertIsNone(listed[0]["date_created"])
            self.assertIsNone(listed[0]["simulation_local_date"])

    def test_list_can_narrow_the_configured_window_without_remote_io(self):
        with tempfile.TemporaryDirectory() as tmp:
            repository = RemoteAlphaRepository(
                FakeAlphaReader(), cache_path=f"{tmp}/remote.json",
                retention_days=3, clock=lambda: FAKE_NOW.timestamp(),
            )
            repository.refresh_remote_alphas()

            listed = repository.list_remote_alphas(days=1)

            self.assertEqual({row["alpha_id"] for row in listed}, {"submitted-1"})

    def test_retention_window_is_fail_closed(self):
        with self.assertRaises(ValueError):
            RemoteAlphaRepository(FakeAlphaReader(), cache_path="x", retention_days=0)
        with self.assertRaises(ValueError):
            RemoteAlphaRepository(FakeAlphaReader(), cache_path="x", retention_days=91)

    def test_public_repository_functions_do_not_require_agent_state_machine(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = {"remote_cache": {"retention_days": 14}}
            result = research_api.refresh_remote_alphas(
                client=FakeAlphaReader(), config=config, state_dir=tmp
            )
            self.assertEqual(result["retention_days"], 14)
            listed = research_api.list_remote_alphas(config=config, state_dir=tmp)
            self.assertEqual(len(listed), 2)

    def test_public_remote_repository_reads_are_explicitly_live_or_cached(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeEvidenceClient()
            live = research_api.get_remote_alpha_evidence(
                "alpha-1", client=client, state_dir=tmp
            )
            self.assertEqual(live["source"], "LIVE")
            self.assertEqual(
                research_api.get_remote_alpha("missing", state_dir=tmp), None
            )

    def test_purge_remote_cache_is_explicit_main_cache_delete_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            repository = RemoteAlphaRepository(
                FakeAlphaReader(), cache_path=f"{tmp}/remote.json",
                clock=lambda: 1789560000,
            )
            repository.refresh_remote_alphas()
            self.assertTrue(os.path.exists(repository.cache.path))

            self.assertTrue(repository.purge_remote_cache())
            self.assertFalse(os.path.exists(repository.cache.path))

    def test_remote_cache_status_read_does_not_remove_stale_temp_resources(self):
        with tempfile.TemporaryDirectory() as tmp:
            repository = RemoteAlphaRepository(
                FakeAlphaReader(),
                cache_path=f"{tmp}/.alpha_feed_cache/remote.json",
                clock=None,
            )
            repository.refresh_remote_alphas()
            stale_temp = repository.cache.path + ".tmp.stale"
            with open(stale_temp, "w", encoding="utf-8") as handle:
                handle.write("synthetic temp")
            stale_at = time.time() - 7 * 24 * 60 * 60 - 1
            os.utime(stale_temp, (stale_at, stale_at))
            with open(repository.cache.path, "rb") as handle:
                before = handle.read()

            status = research_api.remote_cache_status(state_dir=tmp)

            self.assertIn(status["freshness"], {"FRESH", "STALE"})
            self.assertTrue(os.path.exists(stale_temp))
            with open(repository.cache.path, "rb") as handle:
                self.assertEqual(handle.read(), before)


if __name__ == "__main__":
    unittest.main()
