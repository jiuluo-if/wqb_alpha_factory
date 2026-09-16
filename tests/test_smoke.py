import unittest

from wqb_agent.config import normalize_config
from wqb_agent.smoke import run_readonly_smoke


class ReadOnlyClient:
    def __init__(self):
        self.calls = []

    def get_datasets(self):
        self.calls.append("datasets")
        return [{"id": "fallback"}]

    def get_datafields(self, dataset_id, limit=50, offset=0, field_type=None):
        self.calls.append(("fields", dataset_id))
        return [{"id": "close"}], 1


class TestReadOnlySmoke(unittest.TestCase):
    def test_smoke_uses_only_read_methods(self):
        client = ReadOnlyClient()
        result = run_readonly_smoke(
            client,
            normalize_config({"simulation": {}, "runtime": {"smoke_dataset": "pv1"}}),
        )
        self.assertEqual(result["datasets"]["status"], "PASS")
        self.assertEqual(result["fields"]["status"], "PASS")
        self.assertEqual(client.calls, ["datasets", ("fields", "pv1")])

    def test_smoke_degrades_without_read_capability(self):
        result = run_readonly_smoke(
            object(), normalize_config({"simulation": {}, "runtime": {}})
        )
        self.assertEqual(result["datasets"]["status"], "UNAVAILABLE")
        self.assertEqual(result["fields"]["status"], "UNAVAILABLE")

    def test_smoke_rejects_raw_config_input(self):
        with self.assertRaises(TypeError):
            run_readonly_smoke(ReadOnlyClient(), {"runtime": {}})
