import json
import multiprocessing
import os
import tempfile
import time
import unittest

from wqb_agent.checkpoints import CheckpointStore
from wqb_agent.locking import OwnerBusyError
from wqb_agent.optimization_decision import OptimizationDecision
from wqb_agent.state import Experiment
from wqb_agent.trial_ledger import SIMULATION_LIFECYCLE_PHASES, TrialLedger


def _record_selection_in_process(path, result):
    decision = OptimizationDecision(parent_id="p-cross-process", decision="STOP")
    for _ in range(50):
        try:
            written = TrialLedger(path, persist=True).record_optimization_selection(
                decision, timestamp=time.time()
            )
            result.put(bool(written))
            return
        except OwnerBusyError:
            time.sleep(0.01)
    result.put("busy")


class TestOptimizationSelectionAccounting(unittest.TestCase):
    def test_candidate_only_uses_generated_candidates_as_selection_trials(self):
        ledger = TrialLedger(None, persist=False)
        for index in range(6):
            ledger.record({"candidate_id": f"c{index}", "expression": f"rank(x{index})"}, "generated")
        summary = ledger.summarize()
        self.assertEqual(summary["candidate_count"], 6)
        self.assertEqual(summary["optimization_selection_count"], 0)
        self.assertEqual(summary["selection_trial_count"], 6)

    def test_candidates_plus_non_emitted_selections_form_union(self):
        ledger = TrialLedger(None, persist=False)
        for index in range(6):
            ledger.record({"candidate_id": f"c{index}", "expression": f"rank(x{index})"}, "generated")
        ledger.record_optimization_selection(
            OptimizationDecision(parent_id="p-stop", decision="STOP"), outcome="STOP"
        )
        ledger.record_optimization_selection(
            OptimizationDecision(parent_id="p-reroute", decision="REROUTE"), outcome="REROUTE"
        )
        self.assertEqual(ledger.summarize()["selection_trial_count"], 8)
    def test_final_decisions_count_once_and_are_idempotent(self):
        ledger = TrialLedger(None, persist=False)
        stop = OptimizationDecision(parent_id="p-stop", decision="STOP", reason="falsified")
        reroute = OptimizationDecision(parent_id="p-reroute", decision="REROUTE", reason="reroute")

        self.assertTrue(ledger.record_optimization_selection(stop))
        self.assertTrue(ledger.record_optimization_selection(reroute))
        self.assertFalse(ledger.record_optimization_selection(stop))
        summary = ledger.summarize()

        self.assertEqual(summary["selection_trial_count"], 2)
        self.assertEqual(summary["optimization_selection_count"], 2)
        self.assertEqual(summary["non_emitted_optimization_selection_count"], 2)
        self.assertEqual(summary["candidate_count"], 0)
        self.assertEqual(summary["trial_count"], 0)

    def test_rejected_and_pruned_decisions_count_without_changing_lifecycle(self):
        ledger = TrialLedger(None, persist=False)
        for decision, outcome in (("CHILD", "REJECTED"), ("VALIDATE", "PRUNED")):
            item = OptimizationDecision(parent_id=f"p-{decision}", decision=decision)
            self.assertTrue(ledger.record_optimization_selection(item, outcome=outcome))

        ledger.record({"candidate_id": "c1", "expression": "rank(x)"}, "generated")
        summary = ledger.summarize()
        self.assertEqual(summary["selection_trial_count"], 3)
        self.assertEqual(summary["candidate_count"], 1)
        self.assertEqual(SIMULATION_LIFECYCLE_PHASES, (
            "simulation_committed", "simulation_submitted",
            "simulation_settled", "research_outcome_settled",
        ))

    def test_persisted_selection_is_deduplicated_by_semantics_not_timestamp(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = TrialLedger(f"{tmp}/ledger.jsonl")
            decision = OptimizationDecision(parent_id="p1", decision="STOP", reason="no gain")
            self.assertTrue(ledger.record_optimization_selection(decision, timestamp=1))
            self.assertFalse(ledger.record_optimization_selection(decision, timestamp=2))
            self.assertEqual(ledger.summarize()["selection_trial_count"], 1)

    def test_selection_survives_restart_and_generic_replay_ignores_timestamp(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "trial_ledger.jsonl")
            first = TrialLedger(path, persist=True)
            decision = OptimizationDecision(parent_id="p-cross", decision="STOP")
            self.assertTrue(first.record_optimization_selection(decision, timestamp=1))
            self.assertTrue(first.record({"candidate_id": "c1", "expression": "rank(x)"},
                                         "candidate_generated", timestamp=2))

            second = TrialLedger(path, persist=True)
            self.assertFalse(second.record_optimization_selection(decision, timestamp=99))
            self.assertFalse(second.record({"candidate_id": "c1", "expression": "rank(x)"},
                                          "candidate_generated", timestamp=100))
            summary = second.summarize()
            self.assertEqual(summary["optimization_selection_count"], 1)
            self.assertEqual(summary["selection_trial_count"], 2)

    def test_cross_process_selection_has_one_physical_canonical_row(self):
        context = multiprocessing.get_context("spawn")
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "trial_ledger.jsonl")
            result = context.Queue()
            first = context.Process(target=_record_selection_in_process, args=(path, result))
            second = context.Process(target=_record_selection_in_process, args=(path, result))
            first.start()
            second.start()
            first.join(10)
            second.join(10)

            self.assertFalse(first.is_alive())
            self.assertFalse(second.is_alive())
            with open(path, encoding="utf-8") as handle:
                rows = [json.loads(line) for line in handle]
            selections = [row for row in rows if row.get("phase") == "optimization_selection"]
            self.assertEqual(len(selections), 1)
            self.assertEqual(TrialLedger(path).summarize()["selection_trial_count"], 1)

    def test_legacy_trajectory_marks_incomplete_history_without_inventing_selections(self):
        with tempfile.TemporaryDirectory() as tmp:
            trajectory_path = os.path.join(tmp, "trajectory.jsonl")
            with open(trajectory_path, "w", encoding="utf-8") as handle:
                json.dump(Experiment(1, "h", "rank(x)", {}, ["x"]).to_dict(), handle)
                handle.write("\n")
            ledger = TrialLedger(os.path.join(tmp, "trial_ledger.jsonl"), persist=True)
            self.assertEqual(
                ledger.initialize_history_completeness(trajectory_path),
                "INCOMPLETE_LEGACY",
            )
            summary = ledger.summarize()
            self.assertEqual(summary["history_completeness"], "INCOMPLETE_LEGACY")
            self.assertEqual(summary["selection_trial_count"], 0)

    def test_empty_workspace_marks_history_complete_from_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = TrialLedger(os.path.join(tmp, "trial_ledger.jsonl"), persist=True)
            self.assertEqual(
                ledger.initialize_history_completeness(os.path.join(tmp, "trajectory.jsonl")),
                "COMPLETE_FROM_START",
            )
            self.assertEqual(ledger.summarize()["history_completeness"], "COMPLETE_FROM_START")

    def test_decision_id_is_carried_by_experiment_and_lifecycle_row(self):
        decision_id = "decision-123"
        experiment = Experiment(1, "h", "rank(x)", {}, ["x"])
        experiment.optimization_decision_id = decision_id
        restored = Experiment.from_dict(experiment.to_dict())
        self.assertEqual(restored.optimization_decision_id, decision_id)
        with tempfile.TemporaryDirectory() as tmp:
            ledger = TrialLedger(os.path.join(tmp, "ledger.jsonl"), persist=True)
            ledger.record(restored, "simulation_committed")
            with open(os.path.join(tmp, "ledger.jsonl"), encoding="utf-8") as handle:
                row = json.loads(handle.readline())
            self.assertEqual(row["optimization_decision_id"], decision_id)

    def test_decision_id_survives_checkpoint_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            experiment = Experiment(1, "h", "rank(x)", {}, ["x"])
            experiment.optimization_decision_id = "decision-checkpoint"
            experiment.status = "PENDING"
            CheckpointStore(tmp).write(1, {"id": "h"}, [experiment], complete=False)
            restored = CheckpointStore(tmp).load(1)["experiments"][0]
            self.assertEqual(restored["optimization_decision_id"], "decision-checkpoint")
