import unittest

from wqb_agent.execution_plan import (
    BLOCK,
    COMPLETE,
    FORCE_NEW_AUTHORIZED,
    NEW,
    RESUME,
    plan_checkpoint_disposition,
)


def _record(round_no, complete):
    return {
        "round_no": round_no,
        "checkpoint": {"complete": complete},
        "malformed": False,
        "path": f"round_{round_no}.checkpoint.json",
    }


class ExecutionPlanTests(unittest.TestCase):
    def test_plans_new_resume_complete_and_foreign_block(self):
        self.assertEqual(plan_checkpoint_disposition([], 1).disposition, NEW)
        self.assertEqual(
            plan_checkpoint_disposition([_record(1, False)], 1).disposition,
            RESUME,
        )
        self.assertEqual(
            plan_checkpoint_disposition([_record(1, True)], 1).disposition,
            COMPLETE,
        )
        self.assertEqual(
            plan_checkpoint_disposition([_record(2, False)], 1).disposition,
            BLOCK,
        )
        self.assertEqual(
            plan_checkpoint_disposition(
                [_record(2, False)], 1, allow_force=True
            ).disposition,
            FORCE_NEW_AUTHORIZED,
        )
