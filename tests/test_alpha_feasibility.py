import unittest

from wqb_agent.alpha_feasibility import assess_feasibility


class AlphaFeasibilityTests(unittest.TestCase):
    def test_assessment_is_bounded_and_does_not_require_factory_state(self):
        class Template:
            economic_field_count = 2
            family = "ratio"
            field_slots = ("p", "s")

            def render(self, values):
                return f"divide({values['p']},{values['s']})"

        fields = [
            {
                "id": "a",
                "dataset": "d1",
                "description": "analyst revision",
                "semantic_status": "KNOWN",
                "frequency": "daily",
                "frequency_evidence": {
                    "frequency": "daily",
                    "source": "EXPLICIT_PLATFORM",
                },
            },
            {
                "id": "b",
                "dataset": "d2",
                "description": "analyst revision",
                "semantic_status": "KNOWN",
                "frequency": "daily",
                "frequency_evidence": {
                    "frequency": "daily",
                    "source": "EXPLICIT_PLATFORM",
                },
            },
        ]

        result = assess_feasibility(
            {"id": "probe"},
            fields,
            [Template()],
            "SUBINDUSTRY",
            lambda profiles, template: {
                "admission": "ALLOW",
                "frequency_compatibility": {"status": "COMPATIBLE"},
            },
            max_combinations=1,
        )

        self.assertEqual(result["probe_id"], "probe")
        self.assertEqual(result["pair_examined"], 1)
        self.assertEqual(result["novel_cross_dataset_relationship_count"], 1)
        self.assertTrue(result["batch_gate"]["feasible"])


if __name__ == "__main__":
    unittest.main()
