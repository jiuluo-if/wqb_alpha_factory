import unittest
from types import SimpleNamespace

from wqb_agent.execution_recovery import merge_checkpoint_with_trajectory
from wqb_agent.state import Experiment
from wqb_agent.terminal_evidence import (
    failure_category,
    has_full_terminal_evidence,
    validate_terminal_snapshot,
)


class ExecutionProjectionTests(unittest.TestCase):
    def test_sparse_terminal_without_progress_url_is_unrecoverable(self):
        class SparseExperiment(SimpleNamespace):
            def to_dict(self):
                return {"id": self.id, "status": self.status, "metrics": self.metrics}

        experiment = SparseExperiment(id="e1", status="DONE", metrics={}, progress_url=None)
        with self.assertRaises(ValueError):
            merge_checkpoint_with_trajectory(
                [experiment], {}, 3, terminal_statuses={"DONE", "FAILED"}
            )

    def test_terminal_evidence_requires_metrics_or_failure_reason(self):
        self.assertFalse(has_full_terminal_evidence(SimpleNamespace(status="DONE", metrics={})))
        self.assertTrue(has_full_terminal_evidence(SimpleNamespace(status="DONE", metrics={"fitness": 1})))

    def test_terminal_snapshot_is_pure_and_fail_closed(self):
        experiment = Experiment(1, "h", "rank(returns)", {}, ["returns"], ["ds"])
        experiment.id = "exp-1"
        experiment.status = "DONE"
        experiment.metrics = {"fitness": 1}
        validate_terminal_snapshot(
            [experiment],
            {"exp-1": experiment.to_dict()},
            {"exp-1"},
            1,
        )
        self.assertEqual(failure_category(SimpleNamespace(status="FAILED", error="HTTP timeout")), "INFRA")
        self.assertEqual(failure_category(SimpleNamespace(status="FAILED", error="syntax error")), "RESEARCH")


if __name__ == "__main__":
    unittest.main()
