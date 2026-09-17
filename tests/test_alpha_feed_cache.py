import unittest

from wqb_agent.alpha_feed_cache import refresh_due


class TestAlphaFeedCache(unittest.TestCase):
    def test_refresh_due_fails_safe_on_invalid_timestamps(self):
        self.assertTrue(refresh_due("bad", 0))
        self.assertTrue(refresh_due(10, 0, interval_sec=10))
        self.assertFalse(refresh_due(9, 0, interval_sec=10))


if __name__ == "__main__":
    unittest.main()
