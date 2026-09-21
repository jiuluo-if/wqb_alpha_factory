import os
import tempfile
import time
import unittest

from wqb_agent import research_api
from wqb_agent.remote_alpha_repository import RemoteAlphaRepository


class FakeAlphaReader:
    def __call__(self, **kwargs):
        if kwargs.get("status") == "SUBMITTED":
            return [{"id": "submitted-1", "dateSubmitted": "2026-09-16T12:00:00Z"}]
        return [{"id": "simulated-1", "dateCreated": "2026-09-15T12:00:00Z"}]

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


class TestRemoteAlphaRepository(unittest.TestCase):
    def test_refresh_and_list_use_configured_rolling_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            repository = RemoteAlphaRepository(
                FakeAlphaReader(), cache_path=f"{tmp}/remote.json",
                retention_days=3, clock=lambda: 1789560000,
            )

            refreshed = repository.refresh_remote_alphas()
            listed = repository.list_remote_alphas()

            self.assertEqual(refreshed["retention_days"], 3)
            self.assertEqual({row["alpha_id"] for row in listed},
                             {"submitted-1", "simulated-1"})
            self.assertEqual(repository.cache_status()["retention_days"], 3)

    def test_list_can_narrow_the_configured_window_without_remote_io(self):
        with tempfile.TemporaryDirectory() as tmp:
            repository = RemoteAlphaRepository(
                FakeAlphaReader(), cache_path=f"{tmp}/remote.json",
                retention_days=3, clock=lambda: 1789560000,
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
            result = research_api.refresh_remote_alphas(
                client=FakeAlphaReader(), state_dir=tmp
            )
            self.assertEqual(result["retention_days"], 7)
            listed = research_api.list_remote_alphas(state_dir=tmp)
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
