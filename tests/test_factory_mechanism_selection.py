"""Diversity audit, route decisions and budget selection contracts."""

import datetime as dt
import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from tests.helpers import diversity_proposal
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
from wqb_agent.factory_probe import selection_probe
from wqb_agent.factory_runner import AIFactoryRunner
from wqb_agent.proposal_contract import factory_batch_stats, validate_factory_batch
from wqb_agent.research_guard import parameter_only_change_reason
from wqb_agent.state import Experiment
from wqb_agent.weekly_quota import QuotaExceeded, WeeklySimulationQuota


class TestFactoryMechanismSelection(unittest.TestCase):
    def test_semantic_key_ignores_field_id_and_uses_derived_traits(self):
        first = diversity_proposal("rank(field_a)")
        second = diversity_proposal("rank(field_b)")
        self.assertEqual(semantic_mechanism_key(first), semantic_mechanism_key(second))
        self.assertNotIn("field_a", semantic_mechanism_key(first))

    def test_diversity_audit_separates_expression_structure_and_semantics(self):
        proposals = [
            diversity_proposal(f"rank(field_{i})", dataset=f"d{i}")
            for i in range(10)
        ]
        stats = diversity_audit(proposals)
        self.assertEqual(stats["expression"]["unique_count"], 10)
        self.assertEqual(stats["structure"]["unique_family_count"], 1)
        self.assertEqual(stats["semantic"]["known_mechanism_count"], 1)
        self.assertEqual(stats["datasets"]["unique_count"], 10)

    def test_same_structure_different_concepts_increase_semantic_only(self):
        proposals = [
            diversity_proposal("persistent_level(a)", concept="analyst_revision",
                                     measurement="change", behavior="event_driven"),
            diversity_proposal("persistent_level(b)", concept="liquidity",
                                     measurement="level", behavior="signed"),
        ]
        stats = diversity_audit(proposals)
        self.assertEqual(stats["structure"]["unique_family_count"], 1)
        self.assertEqual(stats["semantic"]["known_mechanism_count"], 2)

    def test_different_structures_same_mechanism_increase_structure_only(self):
        proposals = [
            diversity_proposal("rank(a)", template_family="rank_level"),
            diversity_proposal("zscore(b)", template_family="zscore_level"),
        ]
        stats = diversity_audit(proposals)
        self.assertEqual(stats["structure"]["unique_family_count"], 2)
        self.assertEqual(stats["semantic"]["known_mechanism_count"], 1)

    def test_unknown_semantics_and_child_count_do_not_create_diversity(self):
        proposals = [
            diversity_proposal(f"rank(unknown_{i})", concept="unknown",
                                     dataset=f"unknown_d{i}", lineage_id="parent-a")
            for i in range(20)
        ]
        stats = diversity_audit(proposals)
        self.assertEqual(stats["semantic"]["known_mechanism_count"], 0)
        self.assertEqual(stats["semantic"]["unknown_mechanism_count"], 20)
        self.assertEqual(stats["lineages"]["unique_independent_count"], 1)
        self.assertEqual(stats["lineages"]["unknown_count"], 0)

    def test_batch_stats_contains_probe_diversity_audit(self):
        proposals = [
            diversity_proposal("rank(a)", layer="exploration", lineage_id="p"),
            diversity_proposal("rank(b)", layer="exploration", lineage_id="e"),
        ]
        for item in proposals:
            item.update({
                "proposal_origin": "factory",
                "research_layer": "exploration",
                "research_role": "EXPLORE",
                "experiment_stage": "BASELINE",
                "exploration_objective": "signal_discovery",
            })
        stats = factory_batch_stats(proposals)
        self.assertEqual(stats["diversity_layers"]["exploration"]["mechanism_count"], 1)
        self.assertNotIn("layer_counts", stats)
        self.assertNotIn("optimization_source_counts", stats)

    def test_route_ignores_expression_only_change_but_accepts_semantic_or_relationship_change(self):
        base = {
            "candidate_expression_fingerprints": ["a"],
            "relationship_fingerprints": ["r"],
            "semantic_mechanism_fingerprints": ["fundamental:level:persistent_level"],
            "structural_family_fingerprints": ["persistent_level"],
            "dataset_route": ["d1"],
        }
        expression_only = dict(base, candidate_expression_fingerprints=["b"])
        decision = AIFactoryRunner.route_decision(
            base, expression_only, route_attempt=0, no_gain_attempts=1,
            max_no_gain_attempts=2,
        )
        self.assertFalse(decision["information_gain"])
        self.assertEqual(decision["change_type"], "candidate_change_only")
        semantic_change = dict(expression_only,
                                semantic_mechanism_fingerprints=["liquidity:level:rank_level"])
        decision = AIFactoryRunner.route_decision(
            base, semantic_change, route_attempt=0, no_gain_attempts=1,
            max_no_gain_attempts=2,
        )
        self.assertTrue(decision["information_gain"])
        self.assertIn("semantic_change", decision["information_changes"])

    def test_route_treats_partial_operator_realization_as_no_information_gain(self):
        base = {
            "candidate_expression_fingerprints": ["rank(ts_corr(a,b,20))"],
            "semantic_mechanism_fingerprints": ["synthetic-mechanism"],
            "structural_family_fingerprints": ["synthetic-family"],
            "field_concept_fingerprints": ["synthetic-concept"],
            "relationship_fingerprints": ["synthetic-relationship"],
            "dataset_route": ["synthetic-ds"],
            "research_question_fingerprints": ["synthetic-contrast"],
        }
        changed = dict(
            base,
            candidate_expression_fingerprints=["rank(ts_covariance(a,b,20))"],
        )
        decision = AIFactoryRunner.route_decision(
            base, changed, route_attempt=0, no_gain_attempts=1,
        )
        self.assertFalse(decision["information_gain"])
        self.assertEqual(decision["change_type"], "candidate_change_only")

    def test_selection_probe_uses_abstract_partial_question_key(self):
        probe = selection_probe([
            {"template_mode": "PARTIAL_OPERATOR",
             "operator_contrast_question_key": "branch::ROLE",
             "experiment_question": "uses op_a?"},
        ], {}, {})
        self.assertEqual(probe["research_question_fingerprints"], ["branch::role"])

    def test_route_accepts_new_relationship_family_as_information_gain(self):
        previous = {
            "semantic_mechanism_fingerprints": ["fundamental:level:slow_moving"],
            "relationship_fingerprints": ["pair:co_movement"],
            "dataset_route": ["d1", "d2"],
        }
        current = dict(previous, relationship_fingerprints=["pair:relative_spread"])
        decision = AIFactoryRunner.route_decision(
            previous, current, route_attempt=1, no_gain_attempts=1,
        )
        self.assertTrue(decision["information_gain"])
        self.assertEqual(decision["change_type"], "research_information_change")

    def test_route_ignores_frequency_source_relabel_but_detects_value_change(self):
        previous = {
            "frequency_evidence": "daily:2",
            "frequency_evidence_source_counts": {
                "DESCRIPTION_INFERRED": 2,
            },
        }
        relabeled = dict(
            previous,
            frequency_evidence_source_counts={"EXPLICIT_PLATFORM": 2},
        )
        decision = AIFactoryRunner.route_decision(
            previous, relabeled, route_attempt=0, no_gain_attempts=1,
        )
        self.assertFalse(decision["information_gain"])
        changed = dict(relabeled, frequency_evidence="weekly:2")
        decision = AIFactoryRunner.route_decision(
            previous, changed, route_attempt=0, no_gain_attempts=1,
        )
        self.assertTrue(decision["information_gain"])

    def test_diversity_audit_is_deterministic_for_fixed_input(self):
        proposals = [
            diversity_proposal("rank(b)", dataset="d2", lineage_id="l2"),
            diversity_proposal("rank(a)", dataset="d1", lineage_id="l1"),
        ]
        self.assertEqual(diversity_audit(proposals), diversity_audit(list(proposals)))

    def test_budget_selection_interleaves_mechanisms_after_hard_gates(self):
        candidates = [
            diversity_proposal(f"rank(a{i})", dataset="d1")
            for i in range(8)
        ] + [
            diversity_proposal("rank(b)", concept="liquidity",
                                     behavior="signed", dataset="d2"),
            diversity_proposal("rank(c)", concept="analyst_revision",
                                     measurement="change", behavior="event_driven",
                                     dataset="d3"),
        ]
        for item in candidates:
            item.update({
                "proposal_origin": "factory",
                "research_layer": "exploration",
                "research_role": "EXPLORE",
                "experiment_stage": "BASELINE",
            })
        selected, audit = select_budget_candidates(candidates, target=10)
        keys = [semantic_mechanism_key(item) for item in selected]
        self.assertGreaterEqual(len(set(keys[:3])), 3)
        self.assertEqual(audit["selected_count"], 10)
        self.assertEqual(audit["priority_counts"]["normal"], 10)

    def test_optimization_children_are_excluded_from_probe_selection(self):
        optimization = [
            diversity_proposal(f"rank(child_a{i})", lineage_id="parent-a",
                                     layer="optimization")
            for i in range(4)
        ] + [
            diversity_proposal("rank(child_b)", lineage_id="parent-b",
                                     layer="optimization"),
            diversity_proposal("rank(child_c)", lineage_id="parent-c",
                                     layer="optimization"),
        ]
        selected, audit = select_budget_candidates(optimization, target=4)
        self.assertEqual(selected, [])
        self.assertEqual(audit["selected_count"], 0)

    def test_question_and_outcome_context_drive_ordinal_priority(self):
        context = {
            "unresolved_questions": ["does normalization preserve the effect?"],
            "next_discriminating_questions": [],
        }
        high = diversity_proposal("rank(high)")
        high["experiment_question"] = "Does normalization preserve the effect?"
        inconclusive = diversity_proposal("rank(inconclusive)")
        inconclusive["hypothesis_outcome"] = "INCONCLUSIVE"
        supported = diversity_proposal("rank(supported)")
        supported.update({"hypothesis_outcome": "SUPPORTED",
                          "confirmation_status": "INDEPENDENT_CONFIRMED"})
        self.assertEqual(derive_budget_priority(high, context=context)["bucket"], "HIGH")
        self.assertEqual(derive_budget_priority(inconclusive, context=context)["bucket"], "HIGH")
        self.assertEqual(derive_budget_priority(supported, context=context)["bucket"], "LOW")

    def test_unknown_semantics_cannot_receive_novelty_priority(self):
        unknown = diversity_proposal("rank(unknown)", concept="unknown")
        unknown["semantic_status"] = "UNKNOWN"
        unknown["semantic_novelty"] = True
        priority = derive_budget_priority(unknown, context={})
        self.assertEqual(priority["bucket"], "LOW")
        self.assertIn("UNKNOWN", priority["priority_reason"])

    def test_explicit_unknown_candidates_are_not_selected_to_fill_budget(self):
        unknown = diversity_proposal("rank(unknown)", concept="fundamental")
        unknown["semantic_status"] = "UNKNOWN"
        unknown.update({
            "proposal_origin": "factory", "research_layer": "exploration",
            "research_role": "EXPLORE", "experiment_stage": "BASELINE",
        })
        selected, audit = select_budget_candidates([unknown], target=1)
        self.assertEqual(selected, [])
        self.assertEqual(audit["shortage_reason"], "SEMANTIC_GATE_SCARCITY")
        self.assertEqual(audit["unknown_rejected"], 1)

    def test_historical_exhaustion_is_mechanism_family_exhausted(self):
        factory = AlphaFactory()
        fields = [
            {"id": "put_iv", "dataset": "pv1", "type": "MATRIX",
             "description": "put option implied volatility", "frequency": "daily",
             "category": "options", "semantic_status": "KNOWN"},
            {"id": "call_iv", "dataset": "option8", "type": "MATRIX",
             "description": "call option implied volatility", "frequency": "daily",
             "category": "options", "semantic_status": "KNOWN"},
        ]
        baseline = factory.assess_feasibility(
            {"id": "probe", "datasets": ["pv1", "option8"]}, fields, {},
            excluded_expressions=[],
        )
        probe = factory.assess_feasibility(
            {"id": "probe", "datasets": ["pv1", "option8"]}, fields, {},
            excluded_expressions=baseline["candidate_expression_fingerprints"],
        )
        self.assertEqual(probe["failure_taxonomy"], "MECHANISM_FAMILY_EXHAUSTED")

    def test_route_decision_stops_after_bounded_no_gain(self):
        decision = AIFactoryRunner.route_decision(
            {"failure_taxonomy": "CROSS_DATASET_FEASIBILITY_ZERO",
             "candidate_expression_fingerprints": ["a"],
             "relationship_fingerprints": ["r"],
             "mechanism_family": "relationship",
             "dataset_route": ["d1", "d2"]},
            {"failure_taxonomy": "CROSS_DATASET_FEASIBILITY_ZERO",
             "candidate_expression_fingerprints": ["a"],
             "relationship_fingerprints": ["r"],
             "mechanism_family": "relationship",
             "dataset_route": ["d1", "d2"]},
            route_attempt=2, no_gain_attempts=1, max_route_attempts=3,
            max_no_gain_attempts=1,
        )
        self.assertFalse(decision["information_gain"])
        self.assertEqual(decision["action"], "STOP")
        self.assertEqual(decision["reason"], "NO_INFORMATION_GAIN")
