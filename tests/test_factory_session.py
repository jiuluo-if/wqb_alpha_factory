import tempfile
import unittest

from wqb_agent.factory_session import read_session


class FactorySessionTests(unittest.TestCase):
    def test_missing_or_invalid_session_is_not_treated_as_runtime_state(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertIsNone(read_session(directory))


if __name__ == "__main__":
    unittest.main()
