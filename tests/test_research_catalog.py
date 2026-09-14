import unittest

from wqb_agent.agent import EXPLORATION_HYPOTHESES, SEED_HYPOTHESES
from wqb_agent.research_catalog import (
    exploration_hypothesis,
    fallback_research_hypotheses,
    seed_hypothesis,
)


class ResearchCatalogTests(unittest.TestCase):
    def test_fallback_catalog_isolated_from_callers(self):
        catalog = fallback_research_hypotheses()
        self.assertEqual(
            len(catalog), len(EXPLORATION_HYPOTHESES) + len(SEED_HYPOTHESES)
        )
        catalog[0]["tags"].append("caller-mutation")
        self.assertNotIn("caller-mutation", EXPLORATION_HYPOTHESES[0]["tags"])

    def test_round_projections_preserve_existing_catalog_order(self):
        self.assertEqual(seed_hypothesis(0), SEED_HYPOTHESES[0])
        self.assertEqual(exploration_hypothesis(1), EXPLORATION_HYPOTHESES[0])


if __name__ == "__main__":
    unittest.main()
