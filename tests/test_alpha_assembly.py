import unittest

from wqb_agent.alpha_assembly import field_mechanism


class AlphaAssemblyTests(unittest.TestCase):
    def test_field_mechanism_explains_allowed_trait_and_relation(self):
        result = field_mechanism(
            {"id": "eps_revision"},
            {
                "concept": "analyst_revision",
                "measurement": "change",
                "frequency": "daily",
                "sign_semantics": "signed_change",
                "behavior": "event_driven",
                "semantic_admission": "ALLOW",
            },
            {"family": "revision"},
            {"labels": ["revision_dispersion"]},
        )
        self.assertIn("analyst_revision", result)
        self.assertIn("revision_dispersion", result)


if __name__ == "__main__":
    unittest.main()
