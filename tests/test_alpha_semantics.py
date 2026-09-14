import unittest

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


if __name__ == "__main__":
    unittest.main()
