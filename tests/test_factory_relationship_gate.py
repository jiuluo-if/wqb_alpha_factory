"""Relationship gate and multi-field template assembly contracts."""

import datetime as dt
import inspect
import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from tests.helpers import operator_reference, semantic_field
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
from wqb_agent.proposal_contract import factory_batch_stats, validate_factory_batch
from wqb_agent.research_guard import parameter_only_change_reason
from wqb_agent.state import Experiment
from wqb_agent.weekly_quota import QuotaExceeded, WeeklySimulationQuota


class TestFactoryRelationshipGate(unittest.TestCase):
    def test_relationship_evaluator_has_no_template_identity_dispatch(self):
        source = inspect.getsource(AlphaFactory._relationship_gate)
        self.assertNotIn("template.family", source)
        self.assertNotIn("template.template_id", source)
        self.assertNotIn("startswith(\"toy_\")", source)

    def test_relationship_admission_is_invariant_to_template_identity(self):
        factory = AlphaFactory()
        left = semantic_field("left", "daily close price")
        right = semantic_field("right", "daily trading volume")
        first = SimpleNamespace(
            template_id="synthetic-a", family="family-a",
            required_slots=("p", "s"), relationship_contract="CO_MOVEMENT",
        )
        renamed = SimpleNamespace(
            template_id="synthetic-b", family="family-b",
            required_slots=("p", "s"), relationship_contract="CO_MOVEMENT",
        )
        self.assertEqual(
            factory._relationship_gate([left, right], first),
            factory._relationship_gate([left, right], renamed),
        )

    def test_relationship_contract_changes_admission_without_family_dispatch(self):
        factory = AlphaFactory()
        earnings = semantic_field("earnings", "daily earnings")
        assets = semantic_field("assets", "daily total assets")
        ratio = SimpleNamespace(
            template_id="synthetic-a", family="same-family",
            required_slots=("p", "s"), relationship_contract="DIRECTIONAL_RATIO",
        )
        spread = SimpleNamespace(
            template_id="synthetic-b", family="same-family",
            required_slots=("p", "s"), relationship_contract="COMPARABLE_SPREAD",
        )
        self.assertEqual(
            factory._relationship_gate([earnings, assets], ratio)["admission"],
            "ALLOW",
        )
        self.assertNotEqual(
            factory._relationship_gate([earnings, assets], spread)["admission"],
            "ALLOW",
        )
    def test_pair_relationship_gate_rejects_social_count_and_total_assets(self):
        fields = [
            {
                "id": "social_count",
                "name": "Social mention count",
                "description": "daily social media mention count",
                "dataset": "news18",
                "type": "MATRIX",
                "frequency": "daily",
                "category": "social",
                "coverage": 0.8,
                "semantic_status": "KNOWN",
            },
            {
                "id": "total_assets",
                "name": "Total assets",
                "description": "quarterly total assets on the balance sheet",
                "dataset": "fundamental6",
                "type": "MATRIX",
                "frequency": "quarterly",
                "category": "fundamental",
                "coverage": 0.98,
                "semantic_status": "KNOWN",
            },
        ]
        proposals = AlphaFactory().assemble_proposals(
            {"id": "invalid-pair", "template_ids": [
                "toy_scale_surprise", "toy_pair_spread",
            ]},
            fields, operator_reference(), max_candidates=2,
        )
        self.assertEqual(proposals, [])

    def test_relationship_decision_reports_symmetric_slots_and_frequency(self):
        factory = AlphaFactory()
        put_iv = semantic_field(
            "put_iv", "put option implied volatility", category="options"
        )
        call_iv = semantic_field(
            "call_iv", "call option implied volatility", category="options"
        )
        decision = factory._relationship_gate(
            [put_iv, call_iv], factory.registry.get("toy_pair_spread")
        )
        self.assertEqual(decision["admission"], "ALLOW")
        self.assertEqual(decision["relationship_type"], "option_pair")
        self.assertTrue(decision["symmetric"])
        self.assertEqual(
            decision["preferred_slot_assignment"], {"p": "EITHER", "s": "EITHER"}
        )
        self.assertEqual(
            decision["frequency_compatibility"]["status"], "COMPATIBLE"
        )
        self.assertTrue(any("frequency" in reason for reason in decision["reasons"]))

    def test_ratio_requires_directional_earnings_over_assets_assignment(self):
        factory = AlphaFactory()
        earnings = semantic_field(
            "earnings", "quarterly earnings per share",
            dataset="fundamental6", frequency="quarterly", category="fundamental",
        )
        assets = semantic_field(
            "assets", "quarterly total assets balance sheet",
            dataset="fundamental6", frequency="quarterly", category="fundamental",
        )
        template = factory.registry.get("toy_scale_surprise")
        forward = factory._relationship_gate([earnings, assets], template)
        reverse = factory._relationship_gate([assets, earnings], template)
        self.assertEqual(forward["admission"], "ALLOW")
        self.assertEqual(
            forward["preferred_slot_assignment"],
            {"p": "numerator", "s": "denominator"},
        )
        self.assertFalse(forward["symmetric"])
        self.assertNotEqual(reverse["admission"], "ALLOW")

    def test_private_fundamental_family_reuses_ratio_contract(self):
        factory = AlphaFactory()
        earnings = semantic_field(
            "ebitda", "daily earnings before interest and taxes",
            dataset="fundamental6", frequency="daily", category="fundamental",
        )
        assets = semantic_field(
            "assets", "daily total assets balance sheet",
            dataset="fundamental6", frequency="daily", category="fundamental",
        )
        template = SimpleNamespace(
            template_id="synthetic-ratio",
            family="renamed-family",
            required_slots=("p", "s"),
            relationship_contract="DIRECTIONAL_RATIO",
        )
        decision = factory._relationship_gate([earnings, assets], template)
        self.assertEqual(decision["admission"], "ALLOW")
        self.assertEqual(decision["relationship_type"], "numerator_denominator")

    def test_option_pair_does_not_admit_unrelated_open_interest_and_greek(self):
        factory = AlphaFactory()
        open_interest = semantic_field(
            "open_interest", "option open interest", category="options"
        )
        greek = semantic_field(
            "iv_delta", "option implied volatility delta greek", category="options"
        )
        for template_id in (
            "toy_pair_spread", "toy_scale_surprise",
            "toy_sync_corr",
        ):
            decision = factory._relationship_gate(
                [open_interest, greek], factory.registry.get(template_id)
            )
            self.assertNotEqual(decision["admission"], "ALLOW", template_id)

    def test_frequency_mismatch_is_incompatible_for_direct_correlation(self):
        factory = AlphaFactory()
        daily = semantic_field("daily_close", "daily close price")
        annual = semantic_field(
            "annual_close", "annual close price", frequency="annual"
        )
        decision = factory._relationship_gate(
            [daily, annual], factory.registry.get("toy_sync_corr")
        )
        self.assertEqual(
            decision["frequency_compatibility"]["status"], "INCOMPATIBLE"
        )
        self.assertEqual(decision["admission"], "REJECT")

    def test_frequency_review_pair_is_not_auto_generated(self):
        fields = [
            semantic_field("daily_close", "daily close price", dataset="pv1"),
            semantic_field(
                "weekly_close", "weekly close price", dataset="pv13",
                frequency="weekly",
            ),
        ]
        factory = AlphaFactory()
        decision = factory._relationship_gate(
            fields, factory.registry.get("toy_pair_spread")
        )
        self.assertEqual(decision["frequency_compatibility"]["status"], "REVIEW")
        self.assertEqual(decision["admission"], "REVIEW")
        self.assertEqual(
            factory.generate(
                {"template_ids": ["toy_pair_spread"]}, fields, count=1
            ),
            [],
        )
        self.assertEqual(
            factory.assemble_proposals(
                {"template_ids": ["toy_pair_spread"]},
                fields, operator_reference(), max_candidates=1,
            ),
            [],
        )
        self.assertEqual(
            factory.generate(
                {"template_family": "relationship_spread"}, fields, count=1
            ),
            [],
        )
        batch = factory.generate_factory_batch(
            {"id": "frequency-review"}, fields, operator_reference(),
            target=8, seed="frequency-review",
        )
        self.assertFalse(any(len(item.get("fields", [])) > 1 for item in batch))

    def test_analyst_triple_requires_one_confirmation_mechanism(self):
        fields = [
            semantic_field(
                "revision", "analyst EPS estimate revision", category="analyst"
            ),
            semantic_field(
                "dispersion", "analyst EPS estimate dispersion", category="analyst"
            ),
            semantic_field(
                "recommendation", "analyst recommendation change", category="analyst"
            ),
        ]
        decision = AlphaFactory()._relationship_gate(
            fields, AlphaFactory().registry.get("toy_triple_confirmation")
        )
        self.assertEqual(decision["admission"], "ALLOW")
        self.assertEqual(
            decision["confirmation_mechanism"], "analyst_expectation_update"
        )

    def test_triple_with_two_unified_pair_edges_is_not_confirmation(self):
        fields = [
            semantic_field("price", "daily close price"),
            semantic_field(
                "iv", "daily option implied volatility", category="options"
            ),
            semantic_field(
                "open_interest", "daily option open interest", category="options"
            ),
        ]
        decision = AlphaFactory()._relationship_gate(
            fields, AlphaFactory().registry.get("toy_triple_confirmation")
        )
        self.assertNotEqual(decision["admission"], "ALLOW")

    def test_same_concept_with_level_change_mismatch_is_not_spread(self):
        factory = AlphaFactory()
        level = semantic_field("price_level", "daily close price")
        change = semantic_field("price_return", "daily close price return")
        decision = factory._relationship_gate(
            [level, change], factory.registry.get("toy_pair_spread")
        )
        self.assertNotEqual(decision["admission"], "ALLOW")

    def test_multi_field_proposal_contains_relationship_audit_metadata(self):
        fields = [
            semantic_field(
                "put_iv", "put option implied volatility", dataset="option8",
                category="options",
            ),
            semantic_field(
                "call_iv", "call option implied volatility", dataset="option8",
                category="options",
            ),
        ]
        proposals = AlphaFactory().assemble_proposals(
            {"template_ids": ["toy_pair_spread"]},
            fields, operator_reference(), max_candidates=1,
        )
        self.assertEqual(len(proposals), 1)
        audit = proposals[0]["relationship_audit"]
        self.assertEqual(audit["relationship_type"], "option_pair")
        self.assertEqual(audit["relationship_admission"], "ALLOW")
        self.assertEqual(audit["slot_assignment"], {
            "p": "put_iv", "s": "call_iv",
        })
        self.assertEqual(audit["frequency_compatibility"]["status"], "COMPATIBLE")

    def test_factory_has_no_optimization_prefix_api(self):
        with self.assertRaises(TypeError):
            AlphaFactory().generate_factory_batch(
                {"id": "optimized-review"}, [], {}, target=1,
                optimized=[],
            )

    def test_generic_data_field_template_supports_multiple_slots_and_field_refs(self):
        registry = AlphaTemplateRegistry([
            AlphaTemplate(
                "toy_triple_confirmation",
                "generic_multi_field_confirmation",
                "rank(add(ts_zscore({data_field}, 5), add(ts_zscore({s}, 5), ts_zscore({t}, 5))))",
                required_slots=("data_field", "s", "t"),
                version="2", role="PROBE_ALPHA", economic=True,
                field_roles=("toy_primary", "toy_confirmation", "toy_context"),
                allowed_field_families=("TOY_ONLY",),
                field_relationship="three complementary toy signals",
                relationship_contract="MULTI_FIELD_CONFIRMATION",
                economic_mechanism="TOY three-stream confirmation fixture.",
                direction_reason="Aligned toy streams define the fixture direction.",
                expected_horizon="one declared lattice profile",
                falsification="The toy confirmation does not persist.",
                self_correlation_impact="UNKNOWN",
                allowed_horizon_profiles=((5, 22),),
                allowed_settings_arms=("BASE",),
                mechanism_tags=("TOY", "CONFIRMATION"),
                novelty_family="TOY_RELATION",
            ),
        ])
        fields = [
            {"id": "revision", "dataset": "analyst4", "type": "MATRIX",
             "description": "analyst EPS estimate revision", "frequency": "daily",
             "category": "analyst", "semantic_status": "KNOWN"},
            {"id": "dispersion", "dataset": "analyst4", "type": "MATRIX",
             "description": "analyst EPS estimate dispersion", "frequency": "daily",
             "category": "analyst", "semantic_status": "KNOWN"},
            {"id": "recommendation", "dataset": "analyst4", "type": "MATRIX",
             "description": "analyst recommendation change", "frequency": "daily",
             "category": "analyst", "semantic_status": "KNOWN"},
        ]
        candidates = AlphaFactory(registry=registry).generate(
            {"template_ids": ["toy_triple_confirmation"]}, fields, count=1
        )
        self.assertEqual(len(candidates), 1)
        candidate = candidates[0]
        self.assertIn("revision", candidate["expression"])
        self.assertIn("dispersion", candidate["expression"])
        self.assertIn("recommendation", candidate["expression"])
        self.assertEqual(candidate["template_slots"]["data_field"], "revision")
        self.assertEqual(candidate["template_slots"]["s"], "dispersion")
        self.assertEqual(candidate["template_slots"]["t"], "recommendation")
        self.assertEqual(
            [(item["dataset"], item["id"]) for item in candidate["field_refs"]],
            [("analyst4", "revision"), ("analyst4", "dispersion"),
             ("analyst4", "recommendation")],
        )

    def test_assemble_connects_dual_field_template_across_datasets(self):
        root = os.path.dirname(os.path.dirname(__file__))
        from wqb_agent.proposal_contract import _operator_reference

        reference = _operator_reference(
            os.path.join(root, "docs", "reference", "OPERATORS_CHEATSHEET.md")
        )
        fields = [
            {
                "id": "put_iv", "dataset": "pv1", "type": "MATRIX",
                "description": "put option implied volatility", "frequency": "daily",
                "category": "options", "semantic_status": "KNOWN",
            },
            {
                "id": "call_iv", "dataset": "option8", "type": "MATRIX",
                "description": "call option implied volatility", "frequency": "daily",
                "category": "options", "semantic_status": "KNOWN",
            },
        ]
        proposals = AlphaFactory().assemble_proposals(
            {
                "id": "pair", "datasets": ["pv1", "option8"],
                "template_ids": ["toy_pair_spread"],
            },
            fields,
            reference,
            max_candidates=1,
        )
        self.assertEqual(len(proposals), 1)
        proposal = proposals[0]
        self.assertEqual(proposal["fields"], ["put_iv", "call_iv"])
        self.assertEqual(proposal["datasets"], ["pv1", "option8"])
        self.assertEqual(
            [(item["dataset"], item["id"]) for item in proposal["field_refs"]],
            [("pv1", "put_iv"), ("option8", "call_iv")],
        )
        self.assertEqual(proposal["template_slots"]["p"], "put_iv")
        self.assertEqual(proposal["template_slots"]["s"], "call_iv")

    def test_assemble_connects_generic_triple_template_and_records_all_slots(self):
        root = os.path.dirname(os.path.dirname(__file__))
        from wqb_agent.proposal_contract import _operator_reference

        reference = _operator_reference(
            os.path.join(root, "docs", "reference", "OPERATORS_CHEATSHEET.md")
        )
        fields = [
            {"id": "revision", "dataset": "pv1", "type": "MATRIX",
             "description": "analyst EPS estimate revision", "frequency": "daily",
             "category": "analyst", "semantic_status": "KNOWN"},
            {"id": "dispersion", "dataset": "option8", "type": "MATRIX",
             "description": "analyst EPS estimate dispersion", "frequency": "daily",
             "category": "analyst", "semantic_status": "KNOWN"},
            {"id": "recommendation", "dataset": "analyst4", "type": "MATRIX",
             "description": "analyst recommendation change", "frequency": "daily",
             "category": "analyst", "semantic_status": "KNOWN"},
        ]
        proposals = AlphaFactory().assemble_proposals(
            {
                "id": "triple", "datasets": ["pv1", "option8", "analyst4"],
                "template_ids": ["toy_triple_confirmation"],
            },
            fields,
            reference,
            max_candidates=1,
        )
        self.assertEqual(len(proposals), 1)
        proposal = proposals[0]
        self.assertEqual(
            proposal["fields"], ["revision", "dispersion", "recommendation"]
        )
        self.assertEqual(
            {item["dataset"] for item in proposal["field_refs"]},
            {"pv1", "option8", "analyst4"},
        )
        self.assertEqual(proposal["template_slots"]["data_field"], "revision")
        self.assertEqual(proposal["template_slots"]["t"], "recommendation")
