"""Feasibility probe reporting without result payloads."""

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
from wqb_agent.state import Experiment
from wqb_agent.weekly_quota import QuotaExceeded, WeeklySimulationQuota


class TestFactoryFeasibilityCheck(unittest.TestCase):
    def test_factory_stats_retains_feasibility_check_without_result_payload(self):
        stats = factory_batch_stats([], {
            "probe_id": "p1",
            "failure_taxonomy": "RELATIONSHIP_REVIEW",
            "batch_gate": {"feasible": False},
        })

        self.assertEqual(
            stats["feasibility_check"]["failure_taxonomy"],
            "RELATIONSHIP_REVIEW",
        )
        self.assertNotIn("metrics", stats["feasibility_check"])

    def test_feasibility_check_exposes_separate_semantic_and_structural_fingerprints(self):
        fields = [
            {"id": "eps_revision", "dataset": "analyst4", "type": "MATRIX",
             "description": "analyst EPS estimate revision", "frequency": "daily",
             "category": "analyst", "semantic_status": "KNOWN",
             "frequency_evidence": {
                 "frequency": "daily", "source": "EXPLICIT_PLATFORM",
                 "status": "KNOWN", "confidence": "HIGH",
             }},
            {"id": "book_value", "dataset": "fundamental6", "type": "MATRIX",
             "description": "fundamental book value", "frequency": "quarterly",
             "category": "fundamental", "semantic_status": "KNOWN",
             "frequency_evidence": {
                 "frequency": "quarterly", "source": "EXPLICIT_PLATFORM",
                 "status": "KNOWN", "confidence": "HIGH",
             }},
        ]
        probe = AlphaFactory().assess_feasibility(
            {"id": "fingerprints"}, fields, {}, max_combinations=32,
        )
        self.assertIn("semantic_mechanism_fingerprints", probe)
        self.assertIn("structural_family_fingerprints", probe)
        self.assertIn("field_concept_fingerprints", probe)

    def test_feasibility_check_reports_bounded_cross_dataset_diagnosis(self):
        root = os.path.dirname(os.path.dirname(__file__))
        from wqb_agent.proposal_contract import _operator_reference

        reference = _operator_reference(
            os.path.join(root, "docs", "reference", "OPERATORS_CHEATSHEET.md")
        )
        fields = [
            {"id": "put_iv", "dataset": "pv1", "type": "MATRIX",
             "description": "put option implied volatility", "frequency": "daily",
             "category": "options", "semantic_status": "KNOWN",
             "frequency_evidence": {"frequency": "daily",
                 "source": "EXPLICIT_PLATFORM", "status": "KNOWN"}},
            {"id": "call_iv", "dataset": "option8", "type": "MATRIX",
             "description": "call option implied volatility", "frequency": "daily",
             "category": "options", "semantic_status": "KNOWN",
             "frequency_evidence": {"frequency": "daily",
                 "source": "EXPLICIT_PLATFORM", "status": "KNOWN"}},
        ]
        probe = AlphaFactory().assess_feasibility(
            {"id": "probe", "datasets": ["pv1", "option8"]},
            fields,
            reference,
            excluded_expressions=[],
        )

        self.assertEqual(probe["probe_id"], "probe")
        self.assertEqual(probe["explicit_frequency_count"], 2)
        self.assertEqual(probe["inferred_frequency_count"], 0)
        self.assertGreaterEqual(probe["pair_examined"], 1)
        self.assertGreaterEqual(probe["relationship_allow"], 1)
        self.assertGreaterEqual(probe["novel_cross_dataset_relationship_count"], 1)
        self.assertTrue(probe["batch_gate"]["feasible"])
        self.assertIn("failure_taxonomy", probe)

    def test_feasibility_counts_normalized_profile_evidence_without_relabeling(self):
        fields = [{
            "id": "field", "dataset": "pv1", "type": "MATRIX",
            "description": "model score", "frequency": "daily",
            "frequency_evidence": {
                "frequency": "daily", "source": "DATASET_DESCRIPTION_INFERRED",
                "status": "INFERRED", "confidence": "MEDIUM",
            },
            "category": "model", "semantic_status": "KNOWN",
        }]

        probe = AlphaFactory().assess_feasibility(
            {"id": "provenance"}, fields, {}, max_combinations=1,
        )

        self.assertEqual(probe["explicit_frequency_count"], 0)
        self.assertEqual(probe["inferred_frequency_count"], 1)
        self.assertEqual(probe["frequency_evidence_source_counts"], {
            "DATASET_DESCRIPTION_INFERRED": 1,
        })
