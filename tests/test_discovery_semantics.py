"""Field semantics normalization and hypothesis keyword tests."""

import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.helpers import (
    FakeClient,
    TmpStateMixin,
)
from wqb_agent.agent import (
    SEED_HYPOTHESES,
    Agent,
)
from wqb_agent.artifacts import atomic_write_json_if_changed
from wqb_agent.discovery import CATEGORY_KEYWORDS, CATEGORY_VALUE, FieldDiscovery
from wqb_agent.discovery_selection import (
    categorize_hypothesis,
    keywords_from_hypothesis,
    score_components,
    score_field,
)
from wqb_agent.field_metadata import (
    dataset_description_frequency,
    frequency_evidence,
    normalize_coverage,
    normalize_frequency,
    profile_frequency_evidence,
)
from wqb_agent.memory import ExperienceMemory
from wqb_agent.proposal_contract import validate_proposal, validate_vector_inputs
from wqb_agent.reflection import Reflector
from wqb_agent.simulator import Simulator
from wqb_agent.state import Experiment, Trajectory
from wqb_agent.submission import SubmissionPool, self_correlation_evidence


class TestDiscoveryFieldSemantics(TmpStateMixin, unittest.TestCase):

    def setUp(self):
        super().setUp()
        self.client = FakeClient()
        self.discovery = FieldDiscovery(self.client, pagination_limit=2, max_pages=20)

    def test_coverage_normalization_is_shared_and_conservative(self):
        self.assertEqual(normalize_coverage({"coverage": 0.95}), 0.95)
        self.assertEqual(normalize_coverage({"coveragePercentage": 95}), 0.95)
        self.assertEqual(normalize_coverage({"coverage_percent": 1}), 1.0)
        self.assertEqual(normalize_coverage({"coverage": 0}), 0.0)
        for value in (-1, 101, float("nan"), "not-a-number"):
            self.assertIsNone(normalize_coverage({"coverage": value}))

    def test_selection_kernel_is_pure_and_explainable(self):
        field = {"id": "daily_close", "description": "daily price", "coverage": 50}
        before = dict(field)
        components = score_components(field, ["price"], alpha_count=9)
        self.assertEqual(field, before)
        self.assertEqual(components["keyword_contribution"], 1.0)
        self.assertGreater(score_field(field, ["price"], alpha_count=9), 0.0)

    def test_canonical_keyword_projection_preserves_field_discovery_order(self):
        hypothesis = {"statement": "成交量 momentum", "tags": ["price", "volume"]}
        self.assertEqual(
            FieldDiscovery._keywords_from_hypothesis(hypothesis),
            keywords_from_hypothesis(hypothesis),
        )

    def test_canonical_category_projection_preserves_order(self):
        hypothesis = {"statement": "analyst forecast and price volume"}
        self.assertEqual(
            self.discovery.categorize_hypothesis(hypothesis),
            categorize_hypothesis(hypothesis, CATEGORY_KEYWORDS, CATEGORY_VALUE),
        )

    def test_selection_kernel_reuses_request_local_coverage(self):
        field = {"id": "daily_close", "description": "daily price", "coverage": 50}
        with mock.patch(
            "wqb_agent.discovery_selection.normalize_coverage",
            wraps=normalize_coverage,
        ) as normalize:
            components = score_components(
                field, ["price"], alpha_count=1, coverage_value=0.5
            )
        normalize.assert_not_called()
        self.assertEqual(components["coverage_contribution"], 1.0)

        with mock.patch(
            "wqb_agent.discovery_selection.normalize_coverage",
            wraps=normalize_coverage,
        ) as normalize:
            score_components(field, ["price"], alpha_count=1)
        normalize.assert_called_once_with(field)

    def test_frequency_normalization_uses_explicit_description_evidence(self):
        self.assertEqual(
            normalize_frequency({"description": "daily close price"}),
            "daily",
        )
        self.assertEqual(
            normalize_frequency({"description": "quarterly earnings estimate"}),
            "quarterly",
        )
        self.assertIsNone(normalize_frequency({"description": "model score"}))

    def test_frequency_evidence_preserves_explicit_inferred_and_unknown_sources(self):
        explicit = frequency_evidence({
            "frequency": "daily", "description": "daily estimate",
        })
        inferred = frequency_evidence({"description": "quarterly earnings estimate"})
        unknown = frequency_evidence({"description": "model score"})

        self.assertEqual(explicit["source"], "EXPLICIT_PLATFORM")
        self.assertEqual(explicit["frequency"], "daily")
        self.assertEqual(inferred["source"], "DESCRIPTION_INFERRED")
        self.assertEqual(inferred["frequency"], "quarterly")
        self.assertEqual(unknown["source"], "UNKNOWN")
        self.assertIsNone(unknown["frequency"])

    def test_profile_frequency_evidence_preserves_nested_provenance(self):
        profile = {
            "id": "field",
            "frequency": "daily",
            "frequency_evidence": {
                "frequency": "daily",
                "source": "DATASET_DESCRIPTION_INFERRED",
                "status": "INFERRED",
                "confidence": "MEDIUM",
            },
        }

        evidence = profile_frequency_evidence(profile)

        self.assertEqual(evidence["source"], "DATASET_DESCRIPTION_INFERRED")
        self.assertEqual(evidence["status"], "INFERRED")
        self.assertEqual(evidence["frequency"], "daily")

    def test_profile_frequency_evidence_legacy_fallback_is_not_explicit(self):
        evidence = profile_frequency_evidence({"frequency": "daily"})

        self.assertEqual(evidence["source"], "LEGACY_REDERIVED")
        self.assertNotEqual(evidence["source"], "EXPLICIT_PLATFORM")
        self.assertEqual(evidence["frequency"], "daily")

    def test_frequency_evidence_conflict_is_fail_closed(self):
        field = {"frequency": "daily", "description": "quarterly earnings estimate"}
        evidence = frequency_evidence(field)

        self.assertEqual(evidence["status"], "CONFLICT")
        self.assertIsNone(evidence["frequency"])
        self.assertIsNone(normalize_frequency(field))
        profile = self.discovery._profile_from_field(
            "pv1", {"id": "close", "description": "daily close price"}, 1.0, "price"
        )
        self.assertEqual(profile["frequency"], "daily")

    def test_chinese_hypothesis_produces_stable_useful_tokens(self):
        tokens = FieldDiscovery._keywords_from_hypothesis({
            "statement": "高成交量预测未来收益",
            "tags": ["价格动量"],
        })

        self.assertIn("成交量", tokens)
        self.assertIn("价格", tokens)
        self.assertIn("动量", tokens)

    def test_hypothesis_keywords_ignore_english_function_words(self):
        tokens = FieldDiscovery._keywords_from_hypothesis({
            "statement": (
                "Which liquidity and trading activity identify deterioration "
                "or participation changes?"
            ),
            "tags": ["liquidity", "volume"],
        })

        self.assertIn("liquidity", tokens)
        self.assertIn("volume", tokens)
        self.assertNotIn("and", tokens)
        self.assertNotIn("or", tokens)

    def test_semantic_discovery_does_not_admit_coverage_only_fields(self):
        class CoverageOnlyClient(FakeClient):
            def get_datafields(self, dataset_id, limit=50, offset=0, field_type=None):
                return [{
                    "id": "sector",
                    "description": "sector classification",
                    "coverage": 0.95,
                    "type": field_type,
                }], 1

        discovery = FieldDiscovery(
            CoverageOnlyClient(), selection_mode="semantic", max_pages=1
        )

        fields = discovery.discover(
            {"datasets": ["pv13"], "statement": "liquidity", "tags": ["liquidity"]},
            target_count=1,
        )

        self.assertEqual(fields, [])

    def test_ranking_provenance_explains_each_selected_field(self):
        discovery = FieldDiscovery(
            self.client,
            selection_mode="semantic_random",
            random_fraction=0.25,
            random_seed="ranking-provenance",
        )
        fields = discovery.discover(
            {"statement": "trading volume", "tags": ["volume"]},
            target_count=2,
        )

        self.assertTrue(fields)
        for field in fields:
            self.assertEqual(
                set(field["ranking_provenance"]),
                {
                    "keyword_contribution",
                    "coverage_contribution",
                    "alpha_count_penalty",
                    "random_exploration_contribution",
                },
            )

    def test_dataset_description_frequency_is_fail_closed(self):
        self.assertEqual(
            dataset_description_frequency("comprehensive daily volatility metrics"),
            {"frequency": "daily", "source": "DATASET_DESCRIPTION_INFERRED",
             "status": "INFERRED",
             "matched_evidence": ["dataset_description:daily"],
             "confidence": "MEDIUM"},
        )
        # "daily" + "end-of-day" collapse to one bucket -> daily.
        self.assertEqual(
            dataset_description_frequency("updated daily using end-of-day data"),
            {"frequency": "daily", "source": "DATASET_DESCRIPTION_INFERRED",
             "status": "INFERRED",
             "matched_evidence": ["dataset_description:daily"],
             "confidence": "MEDIUM"},
        )
        # Two buckets -> ambiguous -> None.
        self.assertIsNone(
            dataset_description_frequency("daily snapshots and annual restatements"))
        # Empty / no marker -> None.
        self.assertIsNone(dataset_description_frequency(""))
        self.assertIsNone(dataset_description_frequency(None))
        self.assertIsNone(dataset_description_frequency("no cadence stated"))

    def test_profile_falls_back_to_dataset_cadence_only_when_field_unknown(self):
        self.discovery._dataset_snapshot = {
            "payload": [{"id": "opt8", "description": "comprehensive daily volatility metrics"}],
            "ids": ["opt8"],
            "description_map": {"opt8": "comprehensive daily volatility metrics"},
            "observed_at": "2026-09-14T00:00:00Z",
            "fingerprint": "snapshot",
            "status": "LIVE",
        }
        # Field has no cadence evidence -> dataset daily applies.
        profile = self.discovery._profile_from_field(
            "opt8", {"id": "iv", "description": "model score"}, 1.0, "option")
        self.assertEqual(profile["frequency"], "daily")
        self.assertEqual(profile["frequency_evidence"]["source"],
                         "DATASET_DESCRIPTION_INFERRED")
        self.assertEqual(profile["frequency_evidence"]["scope"], "DATASET")
        self.assertEqual(profile["frequency_evidence"]["fingerprint"], "snapshot")
        self.assertEqual(profile["frequency_evidence"]["observed_at"],
                         "2026-09-14T00:00:00Z")
        # Field-level inferred cadence wins over the dataset fallback.
        profile2 = self.discovery._profile_from_field(
            "opt8",
            {"id": "qv", "description": "quarterly estimate"}, 1.0, "option")
        self.assertEqual(profile2["frequency"], "quarterly")
        self.assertEqual(profile2["frequency_evidence"]["source"],
                         "DESCRIPTION_INFERRED")

    def test_profile_dataset_fallback_never_overrides_field_conflict(self):
        self.discovery._dataset_snapshot = {
            "payload": [{"id": "opt8", "description": "comprehensive daily volatility metrics"}],
            "ids": ["opt8"],
            "description_map": {"opt8": "comprehensive daily volatility metrics"},
            "observed_at": "2026-09-14T00:00:00Z",
            "fingerprint": "snapshot",
            "status": "LIVE",
        }
        # Field-level CONFLICT (daily key vs quarterly description) stays
        # unresolved; the dataset fallback must not paper over it.
        profile = self.discovery._profile_from_field(
            "opt8",
            {"id": "x", "frequency": "daily",
             "description": "quarterly estimate"}, 1.0, "option")
        self.assertIsNone(profile["frequency"])
        self.assertEqual(profile["frequency_evidence"]["status"], "CONFLICT")

    def test_dataset_snapshot_is_shared_by_ids_and_descriptions(self):
        calls = []

        def fake_get_datasets():
            calls.append(1)
            return [{"id": "opt8", "description": "daily option data"}]

        self.client.get_datasets = fake_get_datasets
        self.assertEqual(self.discovery._dynamic_dataset_ids(), ["opt8"])
        mapping = self.discovery._dataset_description_map()
        self.assertEqual(mapping, {"opt8": "daily option data"})
        # Both derived views share one live listing snapshot.
        self.discovery._dynamic_dataset_ids()
        self.discovery._dataset_description_map()
        self.assertEqual(len(calls), 1)

    def test_dataset_snapshot_refreshes_for_each_discovery_round(self):
        class RoundClient(FakeClient):
            def __init__(self):
                super().__init__()
                self.round = 0
                self.calls = 0

            def get_datasets(self):
                self.calls += 1
                cadence = "daily" if self.round == 0 else "weekly"
                return [{"id": "opt8", "description": f"{cadence} option data"}]

            def get_datafields(self, dataset_id, limit=50, offset=0, field_type=None):
                return [{"id": "model_score", "description": "model score", "type": field_type}], 1

        client = RoundClient()
        discovery = FieldDiscovery(client, selection_mode="random", max_pages=1)
        first = discovery.discover({"datasets": ["opt8"]}, target_count=1)
        client.round = 1
        second = discovery.discover({"datasets": ["opt8"]}, target_count=1)

        self.assertEqual(client.calls, 2)
        self.assertEqual(first[0]["frequency"], "daily")
        self.assertEqual(second[0]["frequency"], "weekly")

    def test_dataset_snapshot_failure_does_not_reuse_previous_round(self):
        class FailingRoundClient(FakeClient):
            def __init__(self):
                super().__init__()
                self.fail = False

            def get_datasets(self):
                if self.fail:
                    raise RuntimeError("listing unavailable")
                return [{"id": "opt8", "description": "daily option data"}]

            def get_datafields(self, dataset_id, limit=50, offset=0, field_type=None):
                return [{"id": "model_score", "description": "model score", "type": field_type}], 1

        client = FailingRoundClient()
        discovery = FieldDiscovery(client, selection_mode="random", max_pages=1)
        first = discovery.discover({"datasets": ["opt8"]}, target_count=1)
        client.fail = True
        second = discovery.discover({"datasets": ["opt8"]}, target_count=1)

        self.assertEqual(first[0]["frequency"], "daily")
        self.assertIsNone(second[0]["frequency"])
        self.assertEqual(second[0]["frequency_evidence"]["source"], "UNKNOWN")
