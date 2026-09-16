import tempfile
import unittest

from wqb_agent import research_api
from wqb_agent.remote_alpha_repository import RemoteAlphaRepository


class FakeAlphaReader:
    def __call__(self, **kwargs):
        if kwargs.get("status") == "SUBMITTED":
            return [{"id": "submitted-1", "dateSubmitted": "2026-09-16T12:00:00Z"}]
        return [{"id": "simulated-1", "dateCreated": "2026-09-15T12:00:00Z"}]

    get_all_user_alphas = __call__


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


if __name__ == "__main__":
    unittest.main()
