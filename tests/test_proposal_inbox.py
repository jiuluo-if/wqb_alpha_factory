import unittest

from wqb_agent.proposal_inbox import parse_proposal_payload


class ProposalInboxTests(unittest.TestCase):
    def test_projection_is_transient_and_preserves_schema_rows(self):
        request, errors = parse_proposal_payload(
            {"round_no": "3", "proposals": [{"expression": "rank(x)"}, "bad"]},
            default_round_no=1,
        )
        self.assertEqual(errors, [])
        self.assertEqual(request.round_no, 3)
        self.assertEqual(len(request.proposals), 2)

    def test_invalid_round_and_hypothesis_fail_closed(self):
        request, errors = parse_proposal_payload(
            {"round_no": 0, "proposals": []}, default_round_no=1
        )
        self.assertIsNone(request)
        self.assertEqual(errors, ["round_no 必须是正整数"])
        request, errors = parse_proposal_payload(
            {"hypothesis": "bad", "proposals": []}, default_round_no=1
        )
        self.assertIsNone(request)
        self.assertEqual(errors, ["hypothesis 必须是对象"])
