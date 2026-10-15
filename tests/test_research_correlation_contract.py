import unittest

from wqb_agent import research_api
from wqb_agent.remote_evidence import RemoteAlphaEvidenceProvider


class ResearchCorrelationContractTests(unittest.TestCase):
    def test_agent_surface_does_not_expose_local_pairwise_correlation(self):
        names = {
            item["name"]
            for profile in ("core", "full")
            for item in research_api.research_tool_manifest(profile=profile)
        }

        self.assertNotIn("compare_alphas", names)
        self.assertFalse(hasattr(research_api, "compare_alphas"))
        self.assertFalse(hasattr(RemoteAlphaEvidenceProvider, "_compare_pairwise_pnl"))


if __name__ == "__main__":
    unittest.main()
