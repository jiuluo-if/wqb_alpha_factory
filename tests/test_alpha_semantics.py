import unittest

from wqb_agent.alpha_relationships import frequency_compatibility
from wqb_agent.alpha_semantics import derive_field_semantic_traits


class AlphaSemanticsTests(unittest.TestCase):
    def test_direct_description_allows_conservative_semantic_admission(self):
        result = derive_field_semantic_traits({
            "id": "eps_revision",
            "description": "Analyst earnings estimate revision change",
            "frequency": "daily",
        })
        self.assertEqual(result["concept"], "analyst_revision")
        self.assertEqual(result["semantic_admission"], "ALLOW")
        self.assertEqual(result["sign_semantics"], "signed_change")

    def test_missing_profile_is_unknown(self):
        result = derive_field_semantic_traits(None)
        self.assertEqual(result["semantic_admission"], "UNKNOWN")
        self.assertEqual(result["concept"], "unknown")

    def test_declared_text_supplies_frequency_when_catalog_omits_it(self):
        daily = derive_field_semantic_traits(
            {"id": "returns", "description": "Daily returns"}
        )
        annual = derive_field_semantic_traits({
            "id": "annual_total_assets_value",
            "description": "Total assets for the most recent fiscal year",
        })
        silent = derive_field_semantic_traits(
            {"id": "momentum", "description": "Composite momentum score"}
        )
        peer = {"frequency": "daily"}
        self.assertEqual(
            frequency_compatibility([daily, peer], "CO_MOVEMENT")["status"],
            "COMPATIBLE",
        )
        self.assertEqual(
            frequency_compatibility(
                [annual, {"frequency": "annual"}], "CO_MOVEMENT"
            )["status"],
            "COMPATIBLE",
        )
        # A field whose declared text states no cadence stays unknown and keeps
        # the relationship at REVIEW instead of being silently assumed.
        self.assertEqual(
            frequency_compatibility([silent, peer], "CO_MOVEMENT")["status"],
            "REVIEW",
        )


if __name__ == "__main__":
    unittest.main()
