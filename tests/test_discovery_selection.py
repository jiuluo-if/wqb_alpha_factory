"""Field discovery selection, usage scoping and ranking input tests."""

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
    FAKE_FIELDS,
    FakeClient,
    TmpStateMixin,
)
from wqb_agent.agent import (
    SEED_HYPOTHESES,
    Agent,
)
from wqb_agent.artifacts import atomic_write_json_if_changed
from wqb_agent.discovery import (
    FieldDiscovery,
    frequency_evidence,
    normalize_coverage,
    normalize_frequency,
)
from wqb_agent.memory import ExperienceMemory
from wqb_agent.proposal_contract import validate_proposal, validate_vector_inputs
from wqb_agent.reflection import Reflector
from wqb_agent.simulator import Simulator
from wqb_agent.state import Experiment, Trajectory
from wqb_agent.submission import SubmissionPool, self_correlation_evidence


class TestDiscoverySelection(TmpStateMixin, unittest.TestCase):

    def setUp(self):
        super().setUp()
        self.client = FakeClient()
        self.discovery = FieldDiscovery(self.client, pagination_limit=2, max_pages=20)

    def test_high_alpha_count_field_is_excluded_before_selection(self):
        discovery = FieldDiscovery(self.client, max_alpha_count=10)
        discovery._cache["pv1"] = [
            {"id": "overused_volume", "description": "volume", "type": "MATRIX", "alphaCount": 99},
            {"id": "fresh_volume", "description": "volume", "type": "MATRIX", "alphaCount": 2},
        ]
        fields = discovery.discover(
            {"statement": "volume", "tags": ["volume"], "datasets": ["pv1"]}, 2
        )
        self.assertEqual([field["id"] for field in fields], ["fresh_volume"])
        self.assertEqual(discovery.last_excluded_high_usage[0]["id"], "overused_volume")

    def test_platform_alpha_count_refresh_overrides_local_field_cache(self):
        class CurrentUsageClient(FakeClient):
            def get_datafields(self, dataset_id, limit=50, offset=0, field_type=None):
                self.datafield_calls.append((dataset_id, limit, offset, field_type))
                if dataset_id != "pv1" or offset:
                    return [], 2
                return [
                    {"id": "overused_volume", "description": "volume", "type": "MATRIX",
                     "alphaCount": 99},
                    {"id": "fresh_volume", "description": "volume", "type": "MATRIX",
                     "alphaCount": 2},
                ], 2

        client = CurrentUsageClient()
        discovery = FieldDiscovery(
            client, max_alpha_count=10, platform_usage_refresh=True,
        )
        discovery._disk_cache["pv1"] = [
            {"id": "overused_volume", "description": "volume", "type": "MATRIX",
             "alphaCount": 1},
            {"id": "fresh_volume", "description": "volume", "type": "MATRIX",
             "alphaCount": 1},
        ]

        fields = discovery.discover(
            {"statement": "volume", "tags": ["volume"], "datasets": ["pv1"]}, 2
        )

        self.assertEqual([field["id"] for field in fields], ["fresh_volume"])
        self.assertEqual(discovery.last_excluded_high_usage[0]["alpha_count"], 99)
        self.assertEqual(fields[0]["platform_dedupe"]["source"], "brain_api")
        self.assertEqual(fields[0]["platform_dedupe"]["status"], "KNOWN")

    def test_required_platform_alpha_count_fails_closed_when_missing(self):
        class MissingUsageClient(FakeClient):
            def get_datafields(self, dataset_id, limit=50, offset=0, field_type=None):
                return [
                    {"id": "volume_without_usage", "description": "volume",
                     "type": "MATRIX"}
                ], 1

        discovery = FieldDiscovery(
            MissingUsageClient(), max_alpha_count=10,
            platform_usage_refresh=True, require_platform_alpha_count=True,
        )
        fields = discovery.discover(
            {"statement": "volume", "tags": ["volume"], "datasets": ["pv1"]}, 1
        )
        self.assertEqual(fields, [])
        self.assertEqual(discovery.platform_dedupe_view()["status_by_dataset"]["pv1"], "UNKNOWN")
        self.assertEqual(discovery.last_excluded_unknown_usage[0]["id"], "volume_without_usage")

    def test_platform_usage_is_scoped_by_dataset_and_field_id(self):
        discovery = FieldDiscovery(FakeClient(), platform_usage_refresh=True)
        discovery._cache = {
            "pv1": [{"id": "low", "alphaCount": 63765}],
            "univ1": [{"id": "low", "alphaCount": 85437}],
        }
        discovery._platform_usage_status = {"pv1": "KNOWN", "univ1": "KNOWN"}

        usage = discovery.platform_usage_by_field(["pv1", "univ1"])

        self.assertEqual(usage["pv1"]["low"]["alpha_count"], 63765)
        self.assertEqual(usage["univ1"]["low"]["alpha_count"], 85437)

    def test_stratified_sampling_covers_multiple_datasets_and_persists_catalog(self):
        discovery = FieldDiscovery(
            self.client,
            pagination_limit=2,
            max_pages=20,
            catalog_root=self._tmp,
            cache_path=os.path.join(self._tmp, "fields_cache.json"),
            dataset_sampling="stratified",
            min_datasets=3,
            persist_catalog=True,
            selection_mode="semantic_random",
            random_seed="coverage-seed",
        )
        fields = discovery.discover(
            {
                "statement": "price volume option",
                "tags": ["price", "volume", "option"],
                "datasets": ["pv1", "pv13", "option8"],
                "_round": 4,
            },
            target_count=6,
        )
        counts = {}
        for field in fields:
            counts[field["dataset"]] = counts.get(field["dataset"], 0) + 1
        self.assertEqual(set(counts), {"pv1", "pv13", "option8"})
        self.assertTrue(all(value >= 1 for value in counts.values()))
        self.assertEqual(discovery.last_dataset_selection["selected_counts"], counts)

        catalog_dirs = [
            name for name in os.listdir(self._tmp)
            if name.startswith("platform_field_catalog_")
        ]
        self.assertEqual(len(catalog_dirs), 1)
        with open(os.path.join(self._tmp, catalog_dirs[0], "manifest.json"),
                  encoding="utf-8") as handle:
            manifest = json.load(handle)
        self.assertEqual(
            set(manifest["datasets"]), {"pv1", "pv13", "option8"}
        )
        self.assertEqual(manifest["scope"]["region"], "USA")
        self.assertEqual(manifest["catalog_status"], "COMPLETE")
        self.assertTrue(manifest["field_completeness"]["pv1"]["MATRIX"]["complete"])
        catalog_text = json.dumps(manifest, ensure_ascii=False).lower()
        self.assertNotIn("metrics", catalog_text)
        self.assertNotIn("results", catalog_text)
        for filename in os.listdir(os.path.join(self._tmp, catalog_dirs[0])):
            with open(
                os.path.join(self._tmp, catalog_dirs[0], filename),
                encoding="utf-8",
            ) as handle:
                self.assertNotIn("metrics", handle.read().lower())
                handle.seek(0)
                self.assertNotIn("results", handle.read().lower())

        reloaded_client = FakeClient()
        reloaded = FieldDiscovery(
            reloaded_client,
            catalog_root=self._tmp,
            cache_path=os.path.join(self._tmp, "reloaded_cache.json"),
            dataset_sampling="stratified",
            min_datasets=3,
            random_seed="coverage-seed",
        )
        reloaded.discover(
            {"datasets": ["pv1", "pv13", "option8"], "_round": 4},
            target_count=3,
        )
        self.assertEqual(reloaded_client.datafield_calls, [])
        self.assertEqual(reloaded.source_provenance()["catalog_status"], "COMPLETE")

    def test_same_field_id_from_two_datasets_is_not_collapsed_by_discovery(self):
        class SharedFieldClient(FakeClient):
            def get_datafields(self, dataset_id, limit=50, offset=0, field_type=None):
                if dataset_id not in {"pv1", "pv13"}:
                    return [], 0
                fields = [{
                    "id": "low",
                    "description": f"{dataset_id} low price",
                    "type": "MATRIX",
                }]
                return fields[offset:offset + limit], len(fields)

        discovery = FieldDiscovery(
            SharedFieldClient(),
            pagination_limit=2,
            dataset_sampling="stratified",
            min_datasets=2,
            selection_mode="random",
            random_seed="shared-id",
        )
        fields = discovery.discover(
            {"datasets": ["pv1", "pv13"], "_round": 1}, target_count=2
        )
        self.assertEqual(
            {(field["dataset"], field["id"]) for field in fields},
            {("pv1", "low"), ("pv13", "low")},
        )

    def test_dynamic_dataset_listing_is_used_without_inventing_dataset_capability(self):
        class DynamicClient(FakeClient):
            def get_datasets(self):
                return [{"id": "dynamic1", "name": "Dynamic dataset"}]

            def get_datafields(self, dataset_id, limit=50, offset=0, field_type=None):
                if dataset_id != "dynamic1":
                    return [], 0
                rows = [{
                    "id": "dynamic_volume",
                    "description": "dynamic volume",
                    "type": field_type,
                }]
                return rows[offset:offset + limit], len(rows)

        discovery = FieldDiscovery(
            DynamicClient(), pagination_limit=2, max_pages=2,
            selection_mode="semantic_random",
        )
        fields = discovery.discover({"statement": "volume"}, target_count=1)

        self.assertEqual(fields[0]["dataset"], "dynamic1")
        self.assertEqual(discovery.last_dataset_selection["pool"], ["dynamic1"])
        self.assertEqual(
            discovery.source_provenance()["dataset_universe"]["kind"],
            "brain_api",
        )

    def test_dynamic_dataset_universe_is_bounded_before_field_pagination(self):
        class LargeDynamicClient(FakeClient):
            def get_datasets(self):
                return ([{"id": "revision_signal_dataset"}] + [
                    {"id": f"irrelevant_{index}"} for index in range(120)
                ])

            def get_datafields(self, dataset_id, limit=50, offset=0, field_type=None):
                self.datafield_calls.append((dataset_id, limit, offset, field_type))
                if dataset_id != "revision_signal_dataset":
                    return [], 0
                rows = [{
                    "id": "revision_field",
                    "name": "EPS revision",
                    "description": "analyst EPS estimate revision",
                    "type": field_type,
                }]
                return rows[offset:offset + limit], len(rows)

        client = LargeDynamicClient()
        discovery = FieldDiscovery(
            client,
            pagination_limit=2,
            max_pages=2,
            selection_mode="semantic",
            random_seed="bounded-datasets",
        )
        fields = discovery.discover({"statement": "analyst revision"}, target_count=1)

        self.assertEqual(fields[0]["dataset"], "revision_signal_dataset")
        queried = {call[0] for call in client.datafield_calls}
        self.assertLessEqual(
            len(queried), discovery.MAX_ACTIVE_DATASETS,
        )
        self.assertLess(len(queried), 20)
        selection = discovery.last_dataset_selection
        self.assertEqual(selection["dataset_universe"]["count"], 121)
        self.assertLessEqual(
            selection["dataset_universe"]["active_count"],
            discovery.MAX_ACTIVE_DATASETS,
        )

    def test_candidate_pool_is_bounded_before_active_target_count(self):
        class ManyFieldClient(FakeClient):
            def get_datafields(self, dataset_id, limit=50, offset=0, field_type=None):
                rows = [
                    {
                        "id": f"volume_{index}",
                        "description": "volume field" if index < 150 else "other",
                        "type": field_type,
                    }
                    for index in range(400)
                ]
                return rows[offset:offset + limit], len(rows)

        discovery = FieldDiscovery(
            ManyFieldClient(), pagination_limit=100, max_pages=10,
            candidate_pool_size=100,
        )
        fields = discovery.discover(
            {"statement": "volume", "datasets": ["pv1"]}, target_count=6
        )

        self.assertEqual(len(fields), 6)
        self.assertLessEqual(discovery.last_dataset_selection["candidate_counts"]["pv1"], 100)

    def test_no_fake_fields(self):
        hypothesis = {
            "statement": "option volatility.",
            "tags": ["option", "volatility"],
            "direction": "reversal",
        }
        fields = self.discovery.discover(hypothesis, target_count=6)
        known = {f["id"] for f in sum(FAKE_FIELDS.values(), [])}
        self.assertTrue(all(f["id"] in known for f in fields))

    def test_preferred_datasets_used_first(self):
        hypothesis = {
            "statement": "guidance target revision predicts returns.",
            "tags": ["analyst", "eps", "target", "revision"],
            "direction": "long",
            "datasets": ["analyst4"],
        }
        fields = self.discovery.discover(hypothesis, target_count=3)
        self.assertGreaterEqual(len(fields), 3)
        self.assertTrue(all(f["dataset"] == "analyst4" for f in fields))
        # analyst4 must be queried before any fallback dataset
        first_dataset = self.client.datafield_calls[0][0]
        self.assertEqual(first_dataset, "analyst4")
