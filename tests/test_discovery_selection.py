import unittest

from wqb_agent.discovery_selection import (
    keywords_from_hypothesis,
    rank_fields,
    select_active_dataset_ids,
)


class TestDiscoverySelection(unittest.TestCase):
    def test_keywords_are_deterministic_and_bounded(self):
        self.assertEqual(
            keywords_from_hypothesis({"statement": "price reversal"}, limit=1),
            ["price"],
        )

    def test_dataset_selection_returns_provenance(self):
        selected, provenance = select_active_dataset_ids(
            ["price", "volume"], [], [], 1, "seed", 1, {}
        )
        self.assertEqual(selected, ["price"])
        self.assertEqual(provenance["selection_strategy"], "bounded_semantic_dataset_pool")

    def test_rank_fields_reports_usage_exclusions(self):
        ranked, details = rank_fields(
            [{"id": "close", "alphaCount": 10}], "prices", ["close"],
            selection_mode="semantic", random_seed="seed", random_fraction=0.0,
            candidate_pool_size=5, max_alpha_count=1,
        )
        self.assertEqual(ranked, [])
        self.assertEqual(details["excluded_high_usage"][0]["id"], "close")


if __name__ == "__main__":
    unittest.main()
