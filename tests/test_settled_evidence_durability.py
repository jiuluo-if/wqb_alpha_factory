"""Settled Evidence Durability contracts (Phase III).

A DONE Simulation is appended to the trajectory as an *early* snapshot; the
FINAL research settlement (validation report, incremental evidence, final
outcome, research classification, evidence bundle) happens strictly later.
These tests prove the later settlement revision survives a restart through the
single ``Trajectory`` owner, without a second evidence store, without changing
execution identity and without touching Simulation/checkpoint semantics.
"""

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from wqb_agent.agent import Agent
from wqb_agent.state import RESEARCH_SETTLED_REVISION, Experiment, Trajectory

PACKAGE_ROOT = Path(__file__).resolve().parent.parent / "wqb_agent"


def done_experiment(round_no=1, expression="rank(field_a)"):
    """One canonical completed Experiment with only its early DONE snapshot."""
    experiment = Experiment(round_no, "h-1", expression, {"decay": 4}, ["field_a"], ["pv1"])
    experiment.status = "DONE"
    experiment.metrics = {
        "sharpe": 1.1,
        "fitness": 1.0,
        "turnover": 0.2,
        "checks": [{"name": "LOW_TURNOVER", "pass": True}],
    }
    experiment.provisional_outcome = {
        "reward": 1.0, "reward_quality": "PROVISIONAL_EVIDENCE", "outcome_kind": "PROVISIONAL",
    }
    experiment.search_outcome = experiment.provisional_outcome
    return experiment


def settle_evidence(experiment):
    """Apply the later, aggregated research settlement to one Experiment."""
    experiment.validation_plan = {"robustness": "aggregate"}
    experiment.validation_report = {"status": "PASS", "dimensions": {}}
    experiment.validation_status = "STABLE"
    experiment.incremental_evidence = {
        "decision": "UNAVAILABLE", "availability": "UNAVAILABLE",
    }
    experiment.final_outcome = {
        "reward": 1.2, "reward_quality": "FINAL_EVIDENCE", "settled_at": 1234.0,
    }
    experiment.research_classification = {"label": "PROMISING"}
    experiment.research_evidence_bundle = {"label": "STABLE"}
    experiment.self_correlation = {"status": "PASS"}
    experiment.yearly_evidence = {"status": "PASS"}


def _rows(path):
    return [
        json.loads(line)
        for line in Path(path).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


class TestSettledEvidenceDurability(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wqb_settled_")
        self.path = os.path.join(self.tmp, "trajectory.jsonl")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- Test A
    def test_case_a_late_final_settlement_survives_restart(self):
        writer = Trajectory(max_len=25, path=self.path)
        experiment = done_experiment()
        writer.add(experiment)
        settle_evidence(experiment)
        self.assertTrue(writer.settle(experiment))

        reader = Trajectory(max_len=25, path=self.path).load()
        restored = reader.find_completed_expression(experiment.expression)
        self.assertIsNotNone(restored)
        self.assertEqual(restored.final_outcome, experiment.final_outcome)
        self.assertEqual(restored.validation_report, experiment.validation_report)
        self.assertEqual(restored.incremental_evidence, experiment.incremental_evidence)
        self.assertEqual(
            restored.research_classification, experiment.research_classification
        )
        self.assertEqual(
            restored.research_evidence_bundle, experiment.research_evidence_bundle
        )
        self.assertEqual(restored.self_correlation, experiment.self_correlation)
        self.assertEqual(restored.yearly_evidence, experiment.yearly_evidence)
        merged = {item.id: item for item in reader.experiments}
        self.assertEqual(merged[experiment.id].final_outcome, experiment.final_outcome)
        self.assertEqual(
            merged[experiment.id].provisional_outcome, experiment.provisional_outcome
        )

    # ---- Test B
    def test_case_b_revision_cannot_alter_execution_identity(self):
        writer = Trajectory(max_len=25, path=self.path)
        experiment = done_experiment()
        writer.add(experiment)
        tampered = done_experiment(expression="rank(other_field)")
        tampered.id = experiment.id
        settle_evidence(tampered)

        with self.assertRaises(ValueError):
            writer.settle(tampered)

        reader = Trajectory(max_len=25, path=self.path).load()
        self.assertIsNotNone(reader.find_completed_expression("rank(field_a)"))
        self.assertIsNone(reader.find_completed_expression("rank(other_field)"))

    def test_case_b3_settlement_cannot_change_optimization_decision_identity(self):
        writer = Trajectory(max_len=25, path=self.path)
        experiment = done_experiment()
        experiment.optimization_decision_id = "decision-a"
        writer.add(experiment)
        tampered = done_experiment()
        tampered.id = experiment.id
        tampered.optimization_decision_id = "decision-b"
        settle_evidence(tampered)

        with self.assertRaises(ValueError):
            writer.settle(tampered)

    def test_case_b2_revision_requires_a_persisted_canonical_row(self):
        writer = Trajectory(max_len=25, path=self.path)
        orphan = done_experiment()
        settle_evidence(orphan)
        with self.assertRaises(ValueError):
            writer.settle(orphan)

    # ---- Test C
    def test_case_c_corrupt_latest_revision_keeps_last_valid_canonical_evidence(self):
        writer = Trajectory(max_len=25, path=self.path)
        experiment = done_experiment()
        writer.add(experiment)
        settle_evidence(experiment)
        writer.settle(experiment)
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write('{"id": "' + experiment.id + '", "status": "DONE", broken\n')

        reader = Trajectory(max_len=25, path=self.path).load()
        restored = reader.find_completed_expression(experiment.expression)
        self.assertIsNotNone(restored)
        self.assertEqual(restored.final_outcome, experiment.final_outcome)

    # ---- Test D
    def test_case_d_duplicate_execution_add_stays_exactly_once(self):
        writer = Trajectory(max_len=25, path=self.path)
        experiment = done_experiment()
        writer.add(experiment)
        settle_evidence(experiment)
        writer.settle(experiment)

        resumed = Trajectory(max_len=25, path=self.path).load()
        resumed.add(experiment)

        rows = [row for row in _rows(self.path) if row.get("id") == experiment.id]
        canonical = [
            row for row in rows
            if row.get("trajectory_revision") != RESEARCH_SETTLED_REVISION
        ]
        settled = [
            row for row in rows
            if row.get("trajectory_revision") == RESEARCH_SETTLED_REVISION
        ]
        self.assertEqual(len(canonical), 1)
        self.assertEqual(len(settled), 1)

    # ---- Test E
    def test_case_e_settlement_revision_has_no_execution_side_effects(self):
        writer = Trajectory(max_len=25, path=self.path)
        experiment = done_experiment()
        writer.add(experiment)
        settle_evidence(experiment)
        writer.settle(experiment)
        self.assertEqual(os.listdir(self.tmp), ["trajectory.jsonl"])

        source = (PACKAGE_ROOT / "state.py").read_text(encoding="utf-8")
        for forbidden in ("submit_simulation(", "WQBClient", "import requests"):
            self.assertNotIn(forbidden, source)
        self.assertNotIn("record_outcome_settled", source)


class TestProductionSettlementPersistence(unittest.TestCase):
    """The production Agent settlement path must persist the revision."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wqb_settled_agent_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _agent(self):
        return Agent(object(), {"simulation": {}, "agent": {"state_dir": self.tmp}})

    @staticmethod
    def _report():
        return {
            "status": "PASS",
            "dimensions": {
                "platform_quality": {"status": "PASS"},
                "robustness": {"status": "PASS"},
            },
            "statistical_evidence": {},
        }

    def test_agent_settlement_appends_one_durable_revision(self):
        agent = self._agent()
        experiment = done_experiment()
        agent.trajectory.add(experiment)
        final = agent._settle_research_outcome(experiment, self._report())
        self.assertIsInstance(final, dict)
        settled = [
            row for row in _rows(os.path.join(self.tmp, "trajectory.jsonl"))
            if row.get("id") == experiment.id
            and row.get("trajectory_revision") == RESEARCH_SETTLED_REVISION
        ]
        self.assertEqual(len(settled), 1)
        self.assertEqual(settled[0]["final_outcome"], experiment.final_outcome)
        self.assertEqual(
            settled[0]["research_classification"], experiment.research_classification
        )

    def test_finalize_round_closes_existing_checkpoint_from_durable_trajectory(self):
        agent = self._agent()
        experiment = done_experiment(round_no=333)
        experiment.id = "finalize-e1"
        agent.trajectory.add(experiment)
        agent._write_proposal_checkpoint(
            333, {"id": "h-1"}, [experiment], complete=False
        )

        agent.finalize_recorded_round(333)

        checkpoint = agent._load_proposal_checkpoint(333)
        self.assertTrue(checkpoint["complete"])
        self.assertEqual(
            {row["id"] for row in checkpoint["experiments"]},
            {"finalize-e1"},
        )
        before = Path(agent.trajectory.path).read_text(encoding="utf-8")
        agent.finalize_recorded_round(333)
        self.assertEqual(Path(agent.trajectory.path).read_text(encoding="utf-8"), before)

    def test_finalize_round_rejects_unresolved_durable_execution(self):
        agent = self._agent()
        experiment = done_experiment(round_no=334)
        experiment.id = "finalize-unresolved"
        experiment.status = "UNKNOWN"
        experiment.metrics = None
        agent.trajectory.add(experiment)
        agent._write_proposal_checkpoint(
            334, {"id": "h-1"}, [experiment], complete=False
        )

        with self.assertRaisesRegex(ValueError, "FINALIZE_UNRESOLVED_EXECUTION"):
            agent.finalize_recorded_round(334)
        self.assertFalse(agent._load_proposal_checkpoint(334)["complete"])

    def test_finalize_without_checkpoint_does_not_create_one(self):
        agent = self._agent()
        experiment = done_experiment(round_no=335)
        experiment.id = "finalize-no-checkpoint"
        agent.trajectory.add(experiment)

        agent.finalize_recorded_round(335)

        self.assertFalse(os.path.exists(agent._proposal_checkpoint_path(335)))
        log_rows = _rows(os.path.join(self.tmp, "round_finalization_log.jsonl"))
        self.assertEqual(log_rows[-1]["checkpoint_status"], "NO_CHECKPOINT_TO_CLOSE")

    def test_finalize_rejects_checkpoint_execution_set_mismatch_without_rewrite(self):
        agent = self._agent()
        durable = done_experiment(round_no=336)
        durable.id = "durable-336"
        checkpoint_row = done_experiment(round_no=336)
        checkpoint_row.id = "checkpoint-336"
        agent.trajectory.add(durable)
        agent._write_proposal_checkpoint(
            336, {"id": "h-1"}, [checkpoint_row], complete=False
        )
        path = agent._proposal_checkpoint_path(336)
        before = Path(path).read_bytes()

        with self.assertRaisesRegex(ValueError, "FINALIZE_EXECUTION_SET_MISMATCH"):
            agent.finalize_recorded_round(336)
        self.assertEqual(Path(path).read_bytes(), before)

    def test_restarted_agent_sees_final_settled_evidence(self):
        agent = self._agent()
        experiment = done_experiment()
        agent.trajectory.add(experiment)
        agent._settle_research_outcome(experiment, self._report())

        restarted = self._agent()
        restarted._ensure_loaded()
        view = {item.id: item for item in restarted.trajectory.experiments}
        self.assertIn(experiment.id, view)
        self.assertEqual(view[experiment.id].final_outcome, experiment.final_outcome)
        self.assertEqual(
            view[experiment.id].research_classification,
            experiment.research_classification,
        )
