"""Factory batch contract: exact-100 schema, dedup, generation and preflight."""

import datetime as dt
import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from tests.helpers import proposal
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
from wqb_agent.simulation_gateway import SimulationSpec
from wqb_agent.state import Experiment
from wqb_agent.weekly_quota import QuotaExceeded, WeeklySimulationQuota


class TestFactoryBatchContract(unittest.TestCase):
    def test_factory_can_project_candidates_to_simulation_specs(self):
        factory = AlphaFactory()
        factory.generate_factory_batch = Mock(return_value=[{
            "expression": "rank(close)",
            "settings": {"delay": 1},
            "fields": ["close"],
            "template_id": "toy_rank",
        }])

        specs = factory.generate_probe_specs(
            {"id": "h1"}, [], {}, target=1
        )

        self.assertEqual(specs, [SimulationSpec(
            "rank(close)", {"delay": 1}, ("close",), template_id="toy_rank"
        )])
        factory.generate_factory_batch.assert_called_once_with(
            {"id": "h1"}, [], {}, target=1, excluded_expressions=None,
            seed=None, research_context=None, max_pending_per_arm=1,
        )

    def test_factory_batch_requires_exactly_one_hundred_unique_proposals(self):
        ok, errors = validate_factory_batch(
            [proposal(index) for index in range(100)]
        )
        self.assertTrue(ok, errors)

        ok, errors = validate_factory_batch(
            [proposal(index) for index in range(99)]
        )
        self.assertFalse(ok)
        self.assertIn("100", " ".join(errors))

    def test_factory_batch_rejects_duplicate_and_unowned_candidates(self):
        proposals = [proposal(index) for index in range(99)]
        proposals.append(proposal(0))
        ok, errors = validate_factory_batch(proposals)
        self.assertFalse(ok)
        self.assertTrue(any("重复" in error for error in errors))

        proposals = [proposal(index) for index in range(99)]
        proposals.append(proposal(99, origin="legacy_agent"))
        ok, errors = validate_factory_batch(proposals)
        self.assertFalse(ok)
        self.assertTrue(any("来源" in error for error in errors))

    def test_factory_batch_rejects_non_probe_layer_role_or_stage(self):
        mixed = [proposal(index) for index in range(99)]
        mixed.append({
            **proposal(99),
            "proposal_origin": "agent_optimizer",
            "research_layer": "optimization",
            "research_role": "EXPLOIT",
            "experiment_stage": "CHILD",
        })
        ok, errors = validate_factory_batch(mixed)
        self.assertFalse(ok)
        joined = " ".join(errors)
        self.assertIn("exploration", joined)
        self.assertIn("EXPLORE", joined)
        self.assertIn("BASELINE", joined)

    def test_factory_generator_does_not_accept_an_optimization_pool(self):
        with self.assertRaises(TypeError):
            AlphaFactory().generate_factory_batch(
                {"id": "probe"}, [], {}, target=1, optimized=[]
            )

    def test_factory_does_not_realize_from_static_operator_syntax_only(self):
        from wqb_agent.proposal_contract import load_operator_syntax_reference

        root = os.path.dirname(os.path.dirname(__file__))
        static = load_operator_syntax_reference(
            os.path.join(root, "docs", "reference", "OPERATORS_CHEATSHEET.md")
        )
        fields = [{
            "id": "field", "description": "synthetic verified field",
            "type": "MATRIX", "semantic_status": "KNOWN", "frequency": "daily",
            "category": "market", "dataset": "pv1",
        }]
        factory = AlphaFactory()
        self.assertEqual(
            factory.generate_factory_batch(
                {"id": "static-only"}, fields, static, target=1
            ),
            [],
        )
        self.assertEqual(factory.last_budget_audit["status"], "OPERATOR_CAPABILITY_UNKNOWN")

    def test_factory_batch_can_require_real_multi_dataset_coverage(self):
        single_dataset = [
            {
                **proposal(index),
                "datasets": ["pv1"],
            }
            for index in range(100)
        ]
        ok, errors = validate_factory_batch(single_dataset, min_datasets=2)
        self.assertFalse(ok)
        self.assertTrue(any("dataset" in error for error in errors))

        multi_dataset = []
        for index in range(100):
            dataset = "pv1" if index % 2 else "option8"
            multi_dataset.append({
                **proposal(index),
                "datasets": [dataset],
                "field_refs": [{"dataset": dataset, "id": f"field_{index}"}],
            })
        ok, errors = validate_factory_batch(
            multi_dataset,
            min_datasets=2,
            require_cross_dataset_pairs=True,
        )
        self.assertFalse(ok)
        self.assertTrue(any("跨 dataset" in error for error in errors))
        stats = factory_batch_stats(multi_dataset)
        self.assertEqual(stats["dataset_counts"], {"pv1": 50, "option8": 50})
        self.assertEqual(stats["cross_dataset_pair_count"], 0)

    def test_factory_generates_a_full_batch_from_mechanism_templates(self):
        root = os.path.dirname(os.path.dirname(__file__))
        from tests.helpers import operator_reference

        reference = operator_reference()
        fields = [
            {"id": f"field_{index}", "description": "daily close price", "type": "MATRIX",
             "semantic_status": "KNOWN", "frequency": "daily", "category": "market",
             "dataset": "pv1"}
            for index in range(30)
        ]
        proposals = AlphaFactory().generate_factory_batch(
            {"id": "factory", "datasets": ["fundamental6"]},
            fields, reference, target=100,
        )
        self.assertEqual(len(proposals), 100)
        self.assertTrue(all(isinstance(item, dict) for item in proposals))
        self.assertTrue(all(item["proposal_origin"] == "factory" for item in proposals))
        self.assertTrue(all(item["expression"] for item in proposals))
        ok, errors = validate_factory_batch(proposals)
        self.assertTrue(ok, errors)

    def test_normal_factory_mode_reaches_partial_alternative_without_explicit_ids(self):
        from tests.helpers import operator_reference

        fields = [
            {"id": f"field_{index}", "description": "daily close price",
             "type": "MATRIX", "semantic_status": "KNOWN", "frequency": "daily",
             "category": "market", "dataset": "pv1"}
            for index in range(30)
        ]
        proposals = AlphaFactory().generate_factory_batch(
            {"id": "normal-factory", "include_partial_operator_branches": True},
            fields, operator_reference(), target=100, seed="partial-normal",
            max_pending_per_arm=1,
        )
        self.assertTrue(any(item.get("template_mode") == "PARTIAL_OPERATOR" for item in proposals))
        self.assertTrue(any(item.get("template_mode") == "CONCRETE" for item in proposals))

    def test_factory_partial_kill_switch_excludes_partial_templates(self):
        from tests.helpers import operator_reference

        fields = [{
            "id": f"field_{index}", "description": "daily close price", "type": "MATRIX",
            "semantic_status": "KNOWN", "frequency": "daily", "category": "market",
            "dataset": "pv1",
        } for index in range(10)]
        proposals = AlphaFactory().generate_factory_batch(
            {"id": "disabled-factory", "include_partial_operator_branches": False},
            fields, operator_reference(), target=1, seed="partial-disabled",
        )
        self.assertTrue(proposals)
        self.assertTrue(all(item.get("template_mode") == "CONCRETE" for item in proposals))

    def test_factory_exploration_is_seeded_and_marked_as_signal_discovery(self):
        root = os.path.dirname(os.path.dirname(__file__))
        from tests.helpers import operator_reference

        reference = operator_reference()
        fields = [
            {
                "id": f"field_{index}", "description": f"daily close price {index}",
                "type": "MATRIX", "semantic_status": "KNOWN", "frequency": "daily",
                "category": "market", "dataset": "pv1",
            }
            for index in range(30)
        ]
        hypothesis = {"id": "factory-seeded", "datasets": ["fundamental6"]}
        first = AlphaFactory().generate_factory_batch(
            hypothesis, fields, reference, target=8, seed="round-a"
        )
        repeat = AlphaFactory().generate_factory_batch(
            hypothesis, fields, reference, target=8, seed="round-a"
        )
        other = AlphaFactory().generate_factory_batch(
            hypothesis, fields, reference, target=8, seed="round-b"
        )
        self.assertEqual(
            [item["expression"] for item in first],
            [item["expression"] for item in repeat],
        )
        self.assertNotEqual(
            [item["expression"] for item in first[:8]],
            [item["expression"] for item in other[:8]],
        )
        self.assertTrue(all(item["research_role"] == "EXPLORE" for item in first))
        self.assertTrue(all(item["experiment_stage"] == "BASELINE" for item in first))
        self.assertTrue(
            all(item["research_layer"] == "exploration" for item in first)
        )
        self.assertTrue(
            all(item["exploration_objective"] == "signal_discovery" for item in first)
        )

    def test_numeric_window_change_is_not_a_new_mechanism(self):
        self.assertIsNotNone(parameter_only_change_reason(
            "rank(ts_zscore(field, 20))",
            "rank(ts_zscore(field, 60))",
        ))
        self.assertIsNone(parameter_only_change_reason(
            "rank(field)", "group_neutralize(rank(field), SUBINDUSTRY)"
        ))

    def test_factory_execution_blocks_partial_batch_before_simulation(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = Agent(object(), {"simulation": {}, "agent": {"state_dir": tmp}})
            path = os.path.join(tmp, "proposals.json")
            with open(path, "w", encoding="utf-8") as handle:
                import json
                json.dump({
                    "round_no": 1,
                    "batch_type": "factory_100",
                    "proposals": [proposal(index) for index in range(99)],
                }, handle)
            self.assertIsNone(agent.run_proposals(path))
            self.assertFalse(os.path.exists(os.path.join(tmp, "round_1.checkpoint.json")))

    def test_factory_batch_passes_normal_preflight_as_one_hundred_atomic_jobs(self):
        from tests.helpers import operator_reference

        reference = operator_reference()
        fields = [
            {"id": f"field_{index}", "description": "daily close price", "type": "MATRIX",
             "semantic_status": "KNOWN", "frequency": "daily", "category": "market",
             "dataset": "pv1"}
            for index in range(30)
        ]
        proposals = AlphaFactory().generate_factory_batch(
            {"id": "factory", "datasets": ["fundamental6"]},
            fields, reference, target=100,
        )
        with tempfile.TemporaryDirectory() as tmp:
            client = SimpleNamespace(get_operator_capability=lambda: reference)
            agent = Agent(client, {"simulation": {}, "agent": {"state_dir": tmp}})
            agent.simulator.run = Mock()
            path = os.path.join(tmp, "proposals.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({
                    "round_no": 1,
                    "batch_type": "factory_100",
                    "hypothesis": {
                        "id": "factory", "statement": "检验已核验字段的经济机制",
                        "datasets": ["fundamental6"], "direction": "long",
                    },
                    "fields": fields,
                    "proposals": proposals,
                }, handle)
            agent.run_proposals(path)
            self.assertEqual(agent.last_run_stats.get("accepted"), 100)
            agent.simulator.run.assert_called_once()
            self.assertEqual(len(agent.simulator.run.call_args.args[0]), 100)
            self.assertTrue(os.path.exists(os.path.join(tmp, "round_1.checkpoint.json")))
