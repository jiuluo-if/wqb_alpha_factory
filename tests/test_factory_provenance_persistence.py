"""Provenance, runtime evidence, checkpoint and optimizer gate boundaries."""

import datetime as dt
import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from wqb_agent.agent import Agent
from wqb_agent.alpha_factory import (
    AlphaFactory,
    AlphaTemplate,
    AlphaTemplateRegistry,
)
from wqb_agent.alpha_feed_cache import WeeklyAlphaFeedCache
from wqb_agent.alpha_feed_workflow import AlphaFeedWorkflow
from wqb_agent.checkpoints import CheckpointStore
from wqb_agent.client import WQBQueryTooBroadError
from wqb_agent.daily_cache import DailyResearchCache
from wqb_agent.diversity import (
    derive_budget_priority,
    diversity_audit,
    select_budget_candidates,
    semantic_mechanism_key,
)
from wqb_agent.factory_runner import AIFactoryRunner
from wqb_agent.proposal_contract import factory_batch_stats, validate_factory_batch
from wqb_agent.research_guard import parameter_only_change_reason
from wqb_agent.state import Experiment, Trajectory
from wqb_agent.weekly_quota import QuotaExceeded, WeeklySimulationQuota


class TestFactoryProvenancePersistence(unittest.TestCase):
    def test_partial_operator_provenance_round_trips_through_checkpoint(self):
        experiment = Experiment(1, "h", "rank(ts_corr(a, b, 20))", {}, ["a", "b"])
        experiment.status = "UNKNOWN"
        experiment.template_version = "v1"
        experiment.template_mode = "PARTIAL_OPERATOR"
        experiment.template_branch_of = "toy_base"
        experiment.template_fingerprint = "template-fp"
        experiment.template_structural_fingerprint = "struct-fp"
        experiment.template_mechanism_fingerprint = "mechanism-fp"
        experiment.operator_role = "CO_MOVEMENT_ESTIMATOR"
        experiment.operator_role_mapping = {"CO_MOVEMENT_ESTIMATOR": "ts_corr"}
        experiment.operator_realization_fingerprint = "realization-fp"
        experiment.operator_capability_fingerprint = "capability-fp"
        with tempfile.TemporaryDirectory() as tmp:
            store = CheckpointStore(tmp)
            store.write(1, {"id": "h"}, [experiment], complete=False)
            restored = Experiment.from_dict(store.load(1)["experiments"][0])
        for name in (
            "template_version", "template_mode", "template_branch_of",
            "template_fingerprint", "template_structural_fingerprint",
            "template_mechanism_fingerprint", "operator_role",
            "operator_role_mapping", "operator_realization_fingerprint",
            "operator_capability_fingerprint",
        ):
            self.assertEqual(getattr(restored, name), getattr(experiment, name))

    def test_settlement_cannot_mutate_operator_provenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "trajectory.jsonl")
            trajectory = Trajectory(path=path)
            original = Experiment(1, "h", "rank(x)", {}, ["x"])
            original.template_mode = "PARTIAL_OPERATOR"
            original.operator_role = "ROLE"
            original.operator_role_mapping = {"ROLE": "op_a"}
            original.operator_realization_fingerprint = "real-a"
            trajectory.add(original)
            changed = Experiment.from_dict(original.to_dict())
            changed.operator_role_mapping = {"ROLE": "op_b"}
            with self.assertRaises(ValueError):
                trajectory.settle(changed)
    def test_code_screen_precedes_agent_economic_gate(self):
        from tests.helpers import operator_reference

        reference = operator_reference()
        complete_parent = {
            "status": "DONE",
            "expression": "rank(field)",
            "fields_used": ["field"],
            "datasets": ["fundamental6"],
            "metrics": {"sharpe": 1.1, "fitness": 0.8, "turnover": 0.2},
            "health": {"ok": True},
            "field_understanding": {"field": "已核验字段"},
            "field_analysis": {"field": {"data_type": "MATRIX"}},
            "field_source": {"kind": "brain_api", "snapshot_date": "2026-09-08"},
            "field_hypothesis_basis": {"field": {"mechanism": "质量变化"}},
        }
        weak_parent = dict(complete_parent, metrics={"sharpe": 0.1, "fitness": 0.1, "turnover": 0.2})
        screened = AlphaFactory().screen_optimization_parents(
            [weak_parent, complete_parent], min_sharpe=0.9, min_fitness=0.6
        )
        self.assertEqual([item["expression"] for item in screened], ["rank(field)"])
        self.assertEqual(
            AlphaFactory().screen_optimization_parents(
                [dict(complete_parent, metrics={
                    "sharpe": float("nan"), "fitness": 0.8, "turnover": 0.2,
                })]
            ),
            [],
        )

        with tempfile.TemporaryDirectory() as tmp:
            agent = Agent(object(), {"simulation": {}, "agent": {"state_dir": tmp}})
            self.assertEqual(
                agent.generate_optimized_proposals(screened, max_candidates=4), []
            )
        self.assertEqual(
            AlphaFactory().optimize_signal_proposals(
                screened, reference, max_candidates=4
            ), []
        )

    def test_cloud_alpha_metadata_prioritizes_matching_evidence_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = Agent(object(), {"simulation": {}, "agent": {"state_dir": tmp}})
            cloud_parent = Experiment(1, "h", "rank(cloud_field)", {}, ["cloud_field"])
            cloud_parent.datasets = ["fundamental6"]
            cloud_parent.status = "DONE"
            cloud_parent.alpha_id = "cloud-alpha"
            cloud_parent.metrics = {"sharpe": 1.0}
            cloud_parent.metrics["checks"] = [{"name": "SELF_CORRELATION", "status": "UNKNOWN"}]
            cloud_parent.field_understanding = {"cloud_field": "verified"}
            cloud_parent.field_analysis = {"cloud_field": {"data_type": "MATRIX"}}
            cloud_parent.field_source = {"kind": "brain_api"}
            cloud_parent.field_hypothesis_basis = {"cloud_field": {"mechanism": "变化"}}
            cloud_parent.economic_mechanism = "信息变化导致相对定价差异"
            cloud_parent.hypothesis_id = "h-cloud"
            current_parent = Experiment(2, "h", "rank(current_field)", {}, ["current_field"])
            current_parent.datasets = ["fundamental6"]
            current_parent.status = "DONE"
            current_parent.alpha_id = "current-alpha"
            current_parent.metrics = {"sharpe": 1.0}
            current_parent.metrics["checks"] = [{"name": "SELF_CORRELATION", "status": "UNKNOWN"}]
            current_parent.field_understanding = {"current_field": "verified"}
            current_parent.field_analysis = {"current_field": {"data_type": "MATRIX"}}
            current_parent.field_source = {"kind": "brain_api"}
            current_parent.field_hypothesis_basis = {"current_field": {"mechanism": "变化"}}
            current_parent.economic_mechanism = "信息变化导致相对定价差异"
            current_parent.hypothesis_id = "h-current"
            agent.trajectory.experiments = [current_parent, cloud_parent]
            today = agent.alpha_feed_cache.local_date
            agent.alpha_feed_cache.refresh({
                today: {
                    "simulations": [{"alpha_id": "cloud-alpha", "status": "SIMULATED"}],
                    "submitted_alphas": [],
                }
            })
            records = agent.optimizable_signal_records()

        self.assertEqual(
            [item["alpha_id"] for item in records], ["cloud-alpha", "current-alpha"]
        )
        self.assertEqual(records[0]["optimization_source"], "cloud")
        self.assertEqual(records[1]["optimization_source"], "current_run")

    def test_factory_batch_stats_reports_probe_contract_only(self):
        stats = factory_batch_stats([
            {
                "expression": "rank(new_field)",
                "proposal_origin": "factory",
                "research_layer": "exploration",
                "research_role": "EXPLORE",
                "experiment_stage": "BASELINE",
                "exploration_objective": "signal_discovery",
            },
        ])
        self.assertEqual(stats["probe_counts"], {
            "factory": 1, "exploration": 1, "EXPLORE": 1, "BASELINE": 1,
        })
        self.assertNotIn("optimization_source_counts", stats)
        self.assertEqual(stats["exploration_objective_counts"], {
            "signal_discovery": 1,
        })

    def test_factory_batch_prefers_cross_dataset_companions_and_reports_stats(self):
        from tests.helpers import operator_reference

        reference = operator_reference()
        datasets = ["pv1", "pv13", "option8"]
        fields = [
            {
                "id": f"field_{index}", "description": "daily close price",
                "type": "MATRIX", "semantic_status": "KNOWN",
                "frequency": "daily", "category": "market",
                "dataset": datasets[index % len(datasets)],
            }
            for index in range(30)
        ]
        proposals = AlphaFactory().generate_factory_batch(
            {"id": "factory", "datasets": datasets}, fields, reference, target=50
        )
        stats = factory_batch_stats(proposals)
        ok, errors = validate_factory_batch(
            proposals, target=50, min_datasets=3,
            require_cross_dataset_pairs=True,
        )
        self.assertTrue(ok, errors)
        self.assertGreater(stats["dual_or_multi_field_count"], 0)
        self.assertGreater(stats["cross_dataset_pair_count"], 0)
        self.assertTrue(stats["template_counts"])

    def test_agent_runtime_persists_trajectory_evidence_but_not_result_sidecars(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = Agent(object(), {"simulation": {}, "agent": {"state_dir": tmp}})
            experiment = Experiment(1, "h", "rank(field)", {}, ["field"])
            experiment.status = "DONE"
            experiment.metrics = {"sharpe": 1.0}
            agent.trajectory.add(experiment)
            agent._record_trial_phase(experiment, "completed", outcome="DONE")
            agent._write_sims_results(1, [experiment])
            agent.memory.save()
            # Canonical completed evidence is persisted by its sole owner
            # (Trajectory -> trajectory.jsonl) so a fresh process can rehydrate
            # a legal optimizer parent; derived result/submission/color
            # sidecars stay in the in-memory day view.
            durable = set(os.listdir(tmp))
            self.assertTrue({"trajectory.jsonl", "trial_ledger.jsonl"}.issubset(durable))
            unexpected = {
                name for name in durable
                if name not in {"trajectory.jsonl", "trial_ledger.jsonl", "run.lock", "run.lock.guard"}
            }
            self.assertEqual(unexpected, set())
            self.assertEqual(len(agent.daily_cache.simulations()), 1)

    def test_runtime_rehydrates_trajectory_evidence_without_result_sidecars(self):
        with tempfile.TemporaryDirectory() as tmp:
            trajectory_path = os.path.join(tmp, "trajectory.jsonl")
            old = Experiment(7, "old", "rank(old_field)", {}, ["old_field"])
            old.status = "DONE"
            old.metrics = {"sharpe": 99.0}
            with open(trajectory_path, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(old.to_dict()) + "\n")
            agent = Agent(object(), {"simulation": {}, "agent": {"state_dir": tmp}})
            agent._ensure_loaded()
            # Cross-process rehydration restores the canonical DONE evidence
            # from the persisted Trajectory owner...
            restored = agent.trajectory.experiments
            self.assertEqual([exp.round for exp in restored], [7])
            self.assertEqual(restored[0].metrics["sharpe"], 99.0)
            terminal = agent._terminal_expressions()
            self.assertEqual(len(terminal), 1)
            self.assertIn("old_field", next(iter(terminal)))
            # ...but never resurrects derived result sidecars.
            self.assertEqual(agent.daily_cache.simulations(), [])

    def test_nested_platform_dataset_id_is_normalized_for_alpha_count_overlay(self):
        agent = Agent(object(), {"simulation": {}, "agent": {"state_dir": tempfile.mkdtemp()}})
        agent.discovery._using_catalog = True
        agent.discovery._disk_cache = {
            "news18": [{
                "id": "news_field",
                "description": "news semantic field",
                "type": "MATRIX",
                "dataset": {"id": "news18", "name": "News"},
                "alphaCount": 7,
            }]
        }
        _types, profiles = agent._read_field_cache()
        self.assertEqual(profiles["news_field"]["dataset"], "news18")

    def test_agent_optimizer_requires_explicit_non_parameter_mechanism(self):
        root = os.path.dirname(os.path.dirname(__file__))
        from wqb_agent.proposal_contract import _operator_reference
        reference = _operator_reference(
            os.path.join(root, "docs", "reference", "OPERATORS_CHEATSHEET.md")
        )
        parent = {
            "status": "DONE",
            "expression": "rank(ts_zscore(field, 20))",
            "fields_used": ["field"],
            "datasets": ["fundamental6"],
            "metrics": {"sharpe": 1.1, "fitness": 0.8, "turnover": 0.2},
            "health": {"ok": True},
            "field_understanding": {"field": "已核验字段"},
            "field_analysis": {"field": {"data_type": "MATRIX"}},
            "field_source": {"kind": "brain_api", "snapshot_date": "2026-09-08"},
            "field_hypothesis_basis": {"field": {"mechanism": "质量变化"}},
            "hypothesis_id": "h1",
            "child_economic_hypothesis": {
                "expression": "rank(ts_zscore(field, 60))",
                "economic_mechanism": "长期标准化不能单独构成新机制",
                "change_type": "window_change",
            },
        }
        self.assertEqual(AlphaFactory().optimize_signal_proposals(
            [parent], reference, max_candidates=4
        ), [])

    def test_completed_checkpoint_is_not_a_result_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            experiment = Experiment(1, "h", "rank(field)", {}, ["field"])
            experiment.status = "DONE"
            experiment.alpha_id = "alpha-secret-result-id"
            experiment.metrics = {"sharpe": 9.9, "checks": [{"name": "SELF_CORRELATION"}]}
            CheckpointStore(tmp).write(1, {"id": "h"}, [experiment], complete=True)
            with open(os.path.join(tmp, "round_1.checkpoint.json"), encoding="utf-8") as handle:
                payload = handle.read()
            self.assertNotIn("alpha-secret-result-id", payload)
            self.assertNotIn("sharpe", payload)
            self.assertNotIn("SELF_CORRELATION", payload)

    def test_unfinished_checkpoint_keeps_recovery_identity_but_not_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            experiment = Experiment(1, "h", "rank(field)", {}, ["field"])
            experiment.status = "UNKNOWN"
            experiment.progress_url = "https://api.worldquantbrain.com/simulations/known"
            experiment.alpha_id = "alpha-secret-result-id"
            experiment.metrics = {"sharpe": 9.9}
            CheckpointStore(tmp).write(1, {"id": "h"}, [experiment], complete=False)
            with open(os.path.join(tmp, "round_1.checkpoint.json"), encoding="utf-8") as handle:
                payload = handle.read()
            self.assertIn("known", payload)
            self.assertNotIn("alpha-secret-result-id", payload)
            self.assertNotIn("sharpe", payload)

    def test_checkpoint_identity_prevents_cross_process_resubmission(self):
        with tempfile.TemporaryDirectory() as tmp:
            experiment = Experiment(1, "h", "rank(field)", {}, ["field"])
            experiment.status = "DONE"
            CheckpointStore(tmp).write(1, {"id": "h"}, [experiment], complete=True)
            agent = Agent(object(), {"simulation": {}, "agent": {"state_dir": tmp}})
            self.assertEqual(agent.next_round_no(), 2)
            terminal, _fingerprints = agent._terminal_identities(["rank(field)"])
            self.assertIn("rank(field)", terminal)
