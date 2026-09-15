import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from wqb_agent.execution_recovery import merge_checkpoint_with_trajectory
from wqb_agent.proposal_execution import ProposalExecutionWorkflow
from wqb_agent.state import Experiment
from wqb_agent.terminal_evidence import (
    DurableTerminalEvidenceView,
    build_terminal_evidence_view,
    failure_category,
    has_full_terminal_evidence,
    validate_terminal_snapshot,
)


class ExecutionProjectionTests(unittest.TestCase):
    def test_terminal_evidence_view_is_immutable_and_reuses_validated_rows(self):
        experiment = Experiment(1, "h", "rank(returns)", {}, ["returns"], ["ds"])
        experiment.id = "exp-1"
        experiment.status = "DONE"
        experiment.metrics = {"fitness": 1}
        view = build_terminal_evidence_view(
            [experiment], {"exp-1": experiment.to_dict()}, {"exp-1"}, 1
        )
        self.assertIsInstance(view, DurableTerminalEvidenceView)
        self.assertEqual(view.canonical_round_ids, frozenset({"exp-1"}))
        with self.assertRaises(TypeError):
            view.rows["exp-2"] = {}

    def test_normal_settlement_passes_one_terminal_view_to_finalize(self):
        workflow = object.__new__(ProposalExecutionWorkflow)
        terminal_view = object()
        context = Mock()
        context.hooks = Mock()
        context.hooks.print_summary = Mock()
        context.hooks.write_sims_results = Mock()
        context.trajectory.add_many = Mock()
        context.search_policy.release = Mock()
        workflow.context = context
        workflow._require_durable_terminal_evidence = Mock(return_value=terminal_view)
        workflow._finalize_round_projection = Mock(return_value={})
        experiment = Experiment(1, "h", "rank(returns)", {}, ["returns"], ["ds"])
        experiment.status = "FAILED"
        experiment.error = "syntax error"
        workflow._settle_complete_round(1, {"id": "h"}, [experiment])
        workflow._require_durable_terminal_evidence.assert_called_once_with(
            [experiment], 1
        )
        self.assertIs(
            workflow._finalize_round_projection.call_args.kwargs["terminal_evidence"],
            terminal_view,
        )

    def test_incomplete_validation_skips_final_research_projections(self):
        workflow = object.__new__(ProposalExecutionWorkflow)
        context = Mock()
        context.hooks = Mock()
        context.hooks.refresh_self_correlation_evidence = Mock()
        context.hooks.sync_submission_pool = Mock()
        context.hooks.save_state = Mock()
        context.hooks.write_context = Mock()
        context.hooks.validation_candidates = Mock(return_value=[])
        context.reflector.reflect = Mock()
        context.checkpoints.write = Mock()
        workflow.context = context
        experiment = Experiment(1, "h", "rank(returns)", {}, ["returns"], ["ds"])
        experiment.status = "DONE"
        experiment.metrics = {"fitness": 1}

        def mark_incomplete(rows):
            rows[0].validation_report = {
                "status": "INCOMPLETE", "complete": False, "terminal": False,
            }

        context.hooks.mark_robustness_stability.side_effect = mark_incomplete

        workflow._finalize_round_projection(
            1, {"id": "h"}, [experiment], close_checkpoint=True,
            terminal_evidence=object(),
        )

        context.reflector.reflect.assert_not_called()
        context.hooks.sync_submission_pool.assert_not_called()
        context.checkpoints.write.assert_not_called()

    def test_incomplete_validation_skips_settlement_side_effects(self):
        workflow = object.__new__(ProposalExecutionWorkflow)
        context = Mock()
        context.hooks = Mock()
        context.hooks.refresh_self_correlation_evidence = Mock()
        context.hooks.mark_robustness_stability = Mock()
        context.hooks.mark_robustness_stability.side_effect = (
            lambda rows: setattr(rows[0], "validation_report", {
                "status": "INCOMPLETE", "complete": False, "terminal": False,
            })
        )
        context.trajectory.add_many = Mock()
        context.search_policy.release = Mock()
        context.hooks.write_sims_results = Mock()
        context.hooks.print_summary = Mock()
        workflow.context = context
        experiment = Experiment(1, "h", "rank(returns)", {}, ["returns"], ["ds"])
        experiment.status = "DONE"
        experiment.metrics = {"fitness": 1}
        context.trajectory.find_rows = Mock(
            return_value={experiment.id: experiment.to_dict()}
        )
        context.trajectory.iter_canonical_round = Mock(
            return_value=[experiment.to_dict()]
        )

        result = workflow._settle_complete_round(1, {"id": "h"}, [experiment])

        self.assertEqual(result["status"], "INCOMPLETE")
        context.trajectory.add_many.assert_not_called()
        context.search_policy.release.assert_not_called()
        context.hooks.print_summary.assert_not_called()

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
