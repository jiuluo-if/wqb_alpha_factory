import unittest

from wqb_agent.research_planning import (
    form_research_space,
    iterate_best_hypothesis,
    select_exploration_seed,
)


class ResearchPlanningTests(unittest.TestCase):
    def test_form_research_space_merges_configured_dataset_pool_once(self):
        result = form_research_space(
            4,
            {"id": "h", "datasets": ["pv1"], "tags": ["return"]},
            ["pv1", "fundamental6"],
        )
        self.assertEqual(result["id"], "h")
        self.assertEqual(result["datasets"], ["pv1", "fundamental6"])
        self.assertEqual(result["statement"], "Which low-usage, semantically documented fields can test a new mechanism?")

    def test_iterate_best_hypothesis_uses_idea_without_mutating_inputs(self):
        best = {"id": "e1", "expression": "rank(x)", "fields_used": ["x"], "datasets": ["pv1"]}
        idea = {"idea": "cash flow quality", "datasets": ["fundamental6"]}
        result = iterate_best_hypothesis(7, best, idea)
        self.assertEqual(result["id"], "h-iter-r7")
        self.assertIn("cash flow quality", result["statement"])
        self.assertEqual(result["datasets"], ["fundamental6", "pv1"])
        self.assertEqual(best["datasets"], ["pv1"])

    def test_select_exploration_seed_rotates_deterministically(self):
        hypotheses = [{"id": "a"}, {"id": "b"}]
        self.assertEqual(select_exploration_seed(3, hypotheses), {"id": "b"})


if __name__ == "__main__":
    unittest.main()
