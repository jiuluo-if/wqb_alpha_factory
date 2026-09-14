import unittest

from wqb_agent.alpha_relationships import (
    frequency_compatibility,
    relationship_labels,
    relationship_type,
)


class AlphaRelationshipsTests(unittest.TestCase):
    def test_relationship_labels_capture_explicit_price_liquidity_relation(self):
        labels = relationship_labels(
            {"concept": "market_price", "tags": ["price"]},
            {"concept": "liquidity", "tags": ["volume"]},
        )
        self.assertEqual(labels, {"price_volume"})
        self.assertEqual(relationship_type(labels), "price_volume")

    def test_frequency_compatibility_rejects_wide_co_movement_gap(self):
        result = frequency_compatibility(
            [{"frequency": "daily"}, {"frequency": "annual"}],
            "CO_MOVEMENT",
        )
        self.assertEqual(result["status"], "INCOMPATIBLE")


if __name__ == "__main__":
    unittest.main()
