import unittest

from wqb_agent.factory_route import route_decision


class FactoryRouteTests(unittest.TestCase):
    def test_route_stops_after_repeated_no_information_gain(self):
        result = route_decision(
            {"candidate_expression_set_digest": "a", "candidate_expression_count": 1},
            {"candidate_expression_set_digest": "a", "candidate_expression_count": 1, "failure_taxonomy": "BLOCKED"},
            route_attempt=1,
            no_gain_attempts=1,
            max_no_gain_attempts=2,
        )
        self.assertEqual(result["action"], "STOP")
        self.assertEqual(result["reason"], "NO_INFORMATION_GAIN")


if __name__ == "__main__":
    unittest.main()
