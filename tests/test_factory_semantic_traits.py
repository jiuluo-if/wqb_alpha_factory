"""Field semantic traits: analyst/fundamental/option admission and review-only unknowns."""

import datetime as dt
import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from tests.helpers import operator_reference
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


class TestFactorySemanticTraits(unittest.TestCase):
    def test_analyst_revision_traits_prioritize_update_mechanisms(self):
        factory = AlphaFactory()
        profile = {
            "id": "eps_revision",
            "name": "Analyst EPS estimate revision",
            "description": "analyst consensus EPS estimate revision",
            "dataset": "analyst4",
            "type": "MATRIX",
            "frequency": "daily",
            "category": "analyst",
            "coverage": 0.95,
            "semantic_status": "KNOWN",
        }

        traits = factory.derive_field_semantic_traits(profile)
        self.assertEqual(traits["concept"], "analyst_revision")
        self.assertEqual(traits["measurement"], "change")
        self.assertEqual(traits["update_style"], "event_driven")
        self.assertEqual(traits["frequency"], "daily")
        self.assertEqual(traits["sign_semantics"], "signed_change")

        ranked = factory.rank_compatible_templates(profile)
        self.assertTrue(all(
            item["template"].template_id.startswith("toy_")
            for item in ranked[:4]
        ))

        proposals = factory.generate_factory_batch(
            {"id": "revision-semantics"}, [profile],
            operator_reference(), target=4, seed="revision-semantics",
        )
        families = {item["template_family"] for item in proposals}
        self.assertEqual(families, set())
        self.assertNotIn("data_quality_penalty", {
            item["template_id"] for item in proposals
        })

    def test_analyst_category_fallback_does_not_invent_revision(self):
        factory = AlphaFactory()
        estimate = {
            "id": "eps_estimate",
            "name": "Analyst EPS estimate",
            "description": "analyst consensus EPS estimate",
            "dataset": "analyst4",
            "type": "MATRIX",
            "category": "analyst",
            "semantic_status": "KNOWN",
        }
        target_price = {
            "id": "target_price",
            "name": "Analyst target price",
            "description": "consensus analyst target price",
            "dataset": "analyst4",
            "type": "MATRIX",
            "category": "analyst",
            "semantic_status": "KNOWN",
        }
        broad = {
            "id": "analyst_misc",
            "name": "Analyst data",
            "description": "analyst data point",
            "dataset": "analyst4",
            "type": "MATRIX",
            "category": "analyst",
            "semantic_status": "KNOWN",
        }

        estimate_traits = factory.derive_field_semantic_traits(estimate)
        target_traits = factory.derive_field_semantic_traits(target_price)
        broad_traits = factory.derive_field_semantic_traits(broad)
        self.assertNotEqual(estimate_traits["concept"], "analyst_revision")
        self.assertEqual(estimate_traits["measurement"], "level")
        self.assertNotEqual(target_traits["concept"], "analyst_revision")
        self.assertEqual(target_traits["measurement"], "level")
        self.assertEqual(broad_traits["concept"], "analyst")
        self.assertEqual(broad_traits["semantic_admission"], "REVIEW")
        self.assertNotEqual(broad_traits["confidence"], "HIGH")

        category_only = {
            "id": "misc_field",
            "name": "misc field",
            "description": "generic numeric field",
            "dataset": "fundamental6",
            "type": "MATRIX",
            "category": "fundamental",
            "semantic_status": "KNOWN",
        }
        category_traits = factory.derive_field_semantic_traits(category_only)
        self.assertEqual(category_traits["concept"], "fundamental")
        self.assertEqual(category_traits["semantic_admission"], "REVIEW")
        self.assertNotEqual(category_traits["confidence"], "HIGH")

    def test_slow_moving_fundamental_is_not_event_triggered_by_default(self):
        profile = {
            "id": "total_assets",
            "name": "Total assets",
            "description": "quarterly total assets on the balance sheet",
            "dataset": "fundamental6",
            "type": "MATRIX",
            "frequency": "quarterly",
            "category": "fundamental",
            "coverage": 0.98,
            "semantic_status": "KNOWN",
        }
        proposals = AlphaFactory().generate_factory_batch(
            {"id": "fundamental-semantics"}, [profile],
            operator_reference(), target=8, seed="fundamental-semantics",
        )
        traits = AlphaFactory().derive_field_semantic_traits(profile)
        self.assertEqual(traits["frequency"], "quarterly")
        self.assertEqual(traits["sign_semantics"], "nonnegative_level")
        self.assertEqual(traits["behavior"], "slow_moving")
        self.assertEqual(proposals, [])
        self.assertNotIn(
            "event_trigger",
            {item["template_family"] for item in proposals},
        )

    def test_option_volatility_prefers_risk_regime_or_relative_families(self):
        profile = {
            "id": "implied_vol",
            "name": "Option implied volatility",
            "description": "option implied volatility",
            "dataset": "option8",
            "type": "MATRIX",
            "frequency": "daily",
            "category": "options",
            "coverage": 0.91,
            "semantic_status": "KNOWN",
        }
        proposals = AlphaFactory().generate_factory_batch(
            {"id": "option-semantics"}, [profile],
            operator_reference(), target=4, seed="option-semantics",
        )
        traits = AlphaFactory().derive_field_semantic_traits(profile)
        self.assertEqual(traits["sign_semantics"], "nonnegative_level")
        self.assertEqual(proposals, [])
        self.assertNotIn("data_quality_penalty", {
            item["template_id"] for item in proposals
        })

    def test_option_activity_and_skew_are_not_collapsed_into_plain_volatility(self):
        factory = AlphaFactory()
        open_interest = {
            "id": "open_interest",
            "name": "Option open interest",
            "description": "option open interest",
            "dataset": "option8",
            "type": "MATRIX",
            "category": "options",
            "semantic_status": "KNOWN",
        }
        put_call_skew = {
            "id": "put_call_skew",
            "name": "Put-call skew",
            "description": "put-call implied volatility skew",
            "dataset": "option8",
            "type": "MATRIX",
            "category": "options",
            "semantic_status": "KNOWN",
        }
        open_traits = factory.derive_field_semantic_traits(open_interest)
        skew_traits = factory.derive_field_semantic_traits(put_call_skew)
        self.assertEqual(open_traits["concept"], "liquidity")
        self.assertNotEqual(open_traits["concept"], "volatility")
        self.assertEqual(skew_traits["concept"], "option_relative")
        self.assertEqual(skew_traits["measurement"], "dispersion")
        self.assertNotEqual(skew_traits["concept"], "volatility")

        generic_skew = {
            "id": "generic_skew",
            "name": "Generic skew",
            "description": "generic distribution skew",
            "dataset": "research1",
            "type": "MATRIX",
            "category": "fundamental",
            "semantic_status": "KNOWN",
        }
        generic_traits = factory.derive_field_semantic_traits(generic_skew)
        self.assertNotEqual(generic_traits["concept"], "option_relative")

    def test_unknown_semantics_are_review_only_and_do_not_claim_template_mechanism(self):
        profile = {
            "id": "mystery_signal",
            "name": "Mystery signal",
            "description": "verified proprietary signal",
            "dataset": "model16",
            "type": "MATRIX",
            "frequency": "daily",
            "category": "model",
            "coverage": 0.9,
            "semantic_status": "KNOWN",
        }
        proposals = AlphaFactory().assemble_proposals(
            {"id": "unknown-semantics", "template_ids": [
                "event_triggered_signal",
            ]},
            [profile], operator_reference(), max_candidates=1,
        )
        self.assertEqual(proposals, [])

    def test_derived_unknown_semantics_never_admit_a_strong_economic_mechanism(self):
        profile = {
            "id": "opaque_signal",
            "name": "Opaque signal",
            "description": "verified proprietary signal",
            "dataset": "model16",
            "type": "MATRIX",
            "frequency": "daily",
            "category": "model",
            "coverage": 0.9,
            "semantic_status": "KNOWN",
        }
        factory = AlphaFactory()
        ranked = factory.rank_compatible_templates(profile)
        self.assertTrue(ranked)
        self.assertTrue(all(item["admission"] != "ALLOW" for item in ranked))
        candidate = factory.generate(
            {"template_ids": ["toy_control_rank"]}, [profile], count=1
        )[0]
        self.assertIn("UNKNOWN", candidate["economic_mechanism"])
        self.assertNotIn("平滑后的相对高低", candidate["economic_mechanism"])

    def test_factory_coverage_forms_have_identical_sparse_semantics(self):
        factory = AlphaFactory()
        base = {
            "id": "opaque_signal",
            "name": "Opaque signal",
            "description": "verified proprietary signal",
            "dataset": "model16",
            "type": "MATRIX",
            "semantic_status": "KNOWN",
        }
        fractional = factory.derive_field_semantic_traits(
            {**base, "coverage": 0.95}
        )
        percentage = factory.derive_field_semantic_traits(
            {**base, "coveragePercentage": 95}
        )
        invalid = factory.derive_field_semantic_traits(
            {**base, "coverage": float("nan")}
        )
        self.assertEqual(fractional["behavior"], percentage["behavior"])
        self.assertEqual(fractional["semantic_admission"], "UNKNOWN")
        self.assertEqual(percentage["semantic_admission"], "UNKNOWN")
        self.assertEqual(invalid["semantic_admission"], "UNKNOWN")

    def test_semantic_matching_is_deterministic_for_a_fixed_seed(self):
        fields = [
            {
                "id": "revision_a", "name": "EPS revision",
                "description": "analyst EPS estimate revision", "dataset": "analyst4",
                "type": "MATRIX", "frequency": "daily", "category": "analyst",
                "coverage": 0.9, "semantic_status": "KNOWN",
            },
            {
                "id": "iv_a", "name": "Implied volatility",
                "description": "option implied volatility", "dataset": "option8",
                "type": "MATRIX", "frequency": "daily", "category": "options",
                "coverage": 0.9, "semantic_status": "KNOWN",
            },
        ]
        factory = AlphaFactory()
        first = factory.generate_factory_batch(
            {"id": "semantic-seed"}, fields, operator_reference(),
            target=8, seed="fixed-seed",
        )
        repeat = factory.generate_factory_batch(
            {"id": "semantic-seed"}, fields, operator_reference(),
            target=8, seed="fixed-seed",
        )
        self.assertEqual(
            [(item["expression"], item["template_id"]) for item in first],
            [(item["expression"], item["template_id"]) for item in repeat],
        )
