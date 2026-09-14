import unittest

from wqb_agent.execution_identity import ExecutionBindingIndex


class ExecutionBindingIndexTests(unittest.TestCase):
    def test_reuses_exact_projection_until_source_signature_changes(self):
        index = ExecutionBindingIndex()
        calls = []

        class Rows:
            def __iter__(self):
                calls.append("trajectory")
                return iter([{"proposal_id": "p1", "submission_fingerprint": "f1"}])

        source = Rows()

        first = index.refresh(
            ("generation", 1), source, [], {"p2": {"f2"}}
        )
        second = index.refresh(
            ("generation", 1), source, [], {"p2": {"f2"}}
        )
        self.assertEqual(first, {"p1": {"f1"}, "p2": {"f2"}})
        self.assertEqual(second, first)
        self.assertEqual(calls, ["trajectory"])

    def test_rebuilds_after_explicit_owner_generation_changes(self):
        index = ExecutionBindingIndex()
        self.assertEqual(
            index.refresh(("generation", 1), [], [], {}), {}
        )
        result = index.refresh(
            ("generation", 2), [],
            [{"checkpoint": {"experiments": [
                {"proposal_id": "p1", "submission_fingerprint": "f1"}
            ]}}],
            {},
        )
        self.assertEqual(result, {"p1": {"f1"}})


if __name__ == "__main__":
    unittest.main()
