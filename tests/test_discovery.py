"""Field discovery catalog acquisition and completeness tests."""

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
    make_agent,
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


class TestDiscoveryCatalog(TmpStateMixin, unittest.TestCase):

    def setUp(self):
        super().setUp()
        self.client = FakeClient()
        self.discovery = FieldDiscovery(self.client, pagination_limit=2, max_pages=20)

    def test_selects_relevant_dataset_and_fields(self):
        hypothesis = {
            "statement": "Stocks with high trading volume predict returns.",
            "tags": ["volume", "return"],
            "direction": "long",
        }
        fields = self.discovery.discover(hypothesis, target_count=4)
        ids = [f["id"] for f in fields]
        self.assertTrue(any("volume" in f for f in ids))
        self.assertTrue(any("return" in f for f in ids))
        self.assertLessEqual(len(fields), 4)
        calls = self.client.datafield_calls
        self.assertTrue(all(c[1] == 2 for c in calls))

    def test_malformed_field_page_is_ignored_without_crashing(self):
        class MalformedClient:
            def get_datafields(self, *args, **kwargs):
                return {"not": "a field list"}, None

        discovery = FieldDiscovery(MalformedClient(), pagination_limit=2, max_pages=2)
        self.assertEqual(discovery._fields_for("pv1"), [])

    def test_pagination_completeness_is_separate_for_large_matrix_and_vector(self):
        class LargeClient:
            def __init__(self):
                self.fields = {
                    "MATRIX": [
                        {"id": f"m_{index}", "type": "MATRIX"}
                        for index in range(1001)
                    ],
                    "VECTOR": [
                        {"id": f"v_{index}", "type": "VECTOR"}
                        for index in range(1002)
                    ],
                }

            def get_datafields(self, dataset_id, limit=50, offset=0, field_type=None):
                rows = self.fields[field_type]
                return rows[offset:offset + limit], len(rows)

        discovery = FieldDiscovery(LargeClient(), pagination_limit=100, max_pages=20)
        fields = discovery._fields_for("large")
        completeness = discovery.source_provenance()["field_completeness"]["large"]

        self.assertEqual(len(fields), 2003)
        self.assertEqual(completeness["MATRIX"], {
            "expected_count": 1001,
            "loaded_count": 1001,
            "complete": True,
            "truncation_reason": None,
        })
        self.assertEqual(completeness["VECTOR"], {
            "expected_count": 1002,
            "loaded_count": 1002,
            "complete": True,
            "truncation_reason": None,
        })

    def test_max_pages_marks_type_truncated_instead_of_complete(self):
        class LargeClient:
            def get_datafields(self, dataset_id, limit=50, offset=0, field_type=None):
                rows = [
                    {"id": f"{field_type.lower()}_{index}", "type": field_type}
                    for index in range(1001)
                ]
                return rows[offset:offset + limit], len(rows)

        discovery = FieldDiscovery(LargeClient(), pagination_limit=50, max_pages=20)
        discovery._fields_for("large")
        provenance = discovery.source_provenance()

        self.assertEqual(provenance["field_completeness"]["large"]["MATRIX"]["loaded_count"], 1000)
        self.assertFalse(provenance["field_completeness"]["large"]["MATRIX"]["complete"])
        self.assertEqual(
            provenance["field_completeness"]["large"]["MATRIX"]["truncation_reason"],
            "MAX_PAGES",
        )
        self.assertEqual(provenance["catalog_status"], "INCOMPLETE")

    def test_incomplete_catalog_reload_preserves_truncation_provenance(self):
        class LargeClient:
            def __init__(self):
                self.calls = []

            def get_datafields(self, dataset_id, limit=50, offset=0, field_type=None):
                self.calls.append((dataset_id, field_type, offset))
                rows = [
                    {"id": f"{field_type.lower()}_{index}", "type": field_type}
                    for index in range(1001)
                ]
                return rows[offset:offset + limit], len(rows)

        client = LargeClient()
        discovery = FieldDiscovery(
            client, pagination_limit=50, max_pages=20,
            catalog_root=self._tmp,
            cache_path=os.path.join(self._tmp, "fields_cache.json"),
            persist_catalog=True,
        )
        discovery.discover({"datasets": ["large"], "statement": "matrix"}, 1)
        reloaded_client = LargeClient()
        reloaded = FieldDiscovery(
            reloaded_client,
            catalog_root=self._tmp,
            cache_path=os.path.join(self._tmp, "reloaded_cache.json"),
        )

        provenance = reloaded.source_provenance()
        self.assertEqual(provenance["catalog_status"], "INCOMPLETE")
        self.assertFalse(provenance["field_completeness"]["large"]["MATRIX"]["complete"])
        self.assertEqual(
            provenance["field_completeness"]["large"]["MATRIX"]["truncation_reason"],
            "MAX_PAGES",
        )
        first = reloaded.discover({"datasets": ["large"], "statement": "matrix"}, 2)
        second = reloaded.discover({"datasets": ["large"], "statement": "matrix"}, 2)
        self.assertEqual([field["id"] for field in first], [field["id"] for field in second])
        self.assertEqual(reloaded.source_provenance()["catalog_status"], "INCOMPLETE")
        self.assertEqual(reloaded_client.calls, [])

    def test_platform_count_above_loaded_is_count_underrun(self):
        class UnderrunClient:
            def get_datafields(self, dataset_id, limit=50, offset=0, field_type=None):
                if offset == 0:
                    return [{"id": f"{field_type.lower()}_0", "type": field_type}], 3
                return [], 3

        discovery = FieldDiscovery(UnderrunClient(), pagination_limit=50, max_pages=2)
        discovery._fields_for("underrun")
        completeness = discovery.source_provenance()["field_completeness"]["underrun"]

        self.assertEqual(completeness["MATRIX"]["expected_count"], 3)
        self.assertEqual(completeness["MATRIX"]["loaded_count"], 1)
        self.assertFalse(completeness["MATRIX"]["complete"])
        self.assertEqual(completeness["MATRIX"]["truncation_reason"], "COUNT_UNDERRUN")

    def test_any_incomplete_dataset_keeps_catalog_incomplete(self):
        class MixedClient:
            def get_datafields(self, dataset_id, limit=50, offset=0, field_type=None):
                if dataset_id == "truncated" and offset == 0:
                    return [{"id": f"{field_type.lower()}_0", "type": field_type}], 2
                if dataset_id == "truncated":
                    return [], 2
                return [{"id": f"complete_{field_type.lower()}", "type": field_type}], 1

        discovery = FieldDiscovery(MixedClient(), pagination_limit=50, max_pages=2)
        discovery._fields_for("truncated")
        discovery._fields_for("complete")

        self.assertEqual(discovery.source_provenance()["catalog_status"], "INCOMPLETE")

    def test_malformed_pagination_is_fail_closed_with_provenance(self):
        class MalformedClient:
            def get_datafields(self, dataset_id, limit=50, offset=0, field_type=None):
                return {"results": []}, 1

        discovery = FieldDiscovery(MalformedClient(), pagination_limit=50, max_pages=2)
        self.assertEqual(discovery._fields_for("malformed"), [])
        completeness = discovery.source_provenance()["field_completeness"]["malformed"]

        self.assertFalse(completeness["MATRIX"]["complete"])
        self.assertEqual(completeness["MATRIX"]["truncation_reason"], "MALFORMED_PAGE")
        self.assertEqual(completeness["VECTOR"]["truncation_reason"], "MALFORMED_PAGE")

    def test_malformed_hypothesis_and_field_metadata_degrade_safely(self):
        class MalformedClient:
            def get_datafields(self, *args, **kwargs):
                return [
                    {"id": ["bad"], "name": {"bad": True}},
                    {"id": 7, "name": 8, "description": "volume"},
                ], 2

        discovery = FieldDiscovery(MalformedClient(), pagination_limit=10, max_pages=2)
        self.assertEqual(
            discovery.categorize_hypothesis({"statement": "trading volume", "tags": "bad"}),
            ["price_volume"],
        )
        fields = discovery.discover(
            {"statement": "volume", "tags": [123], "datasets": [{"id": "pv1"}]}, 2
        )
        self.assertEqual([field["id"] for field in fields], ["7"])

    def test_stops_when_enough_fields(self):
        hypothesis = {
            "statement": "reversal on price.",
            "tags": ["reversal", "price"],
            "direction": "reversal",
        }
        fields = self.discovery.discover(hypothesis, target_count=2)
        self.assertLessEqual(len(fields), 2)

    def test_complete_local_catalog_is_preferred_and_has_provenance(self):
        catalog = os.path.join(self._tmp, "platform_field_catalog_20260822")
        os.makedirs(catalog)
        with open(os.path.join(catalog, "manifest.json"), "w", encoding="utf-8") as f:
            json.dump({"fetched_at": "2026-08-22T00:00:00+0800", "datasets": {
                "pv1": {"file": "pv1.json"}
            }}, f)
        with open(os.path.join(catalog, "pv1.json"), "w", encoding="utf-8") as f:
            json.dump({"fields": [{"id": "local_volume", "description": "local volume", "type": "MATRIX"}]}, f)
        discovery = FieldDiscovery(
            self.client, cache_path=os.path.join(self._tmp, "fields_cache.json")
        )
        fields = discovery.discover(
            {"statement": "volume effect", "tags": ["volume"], "datasets": ["pv1"]}, 1
        )
        self.assertEqual(fields[0]["id"], "local_volume")
        self.assertEqual(fields[0]["field_source"]["kind"], "local_catalog")
        self.assertEqual(self.client.datafield_calls, [])

    def test_malformed_catalog_manifest_fails_closed_to_discovery(self):
        catalog = os.path.join(self._tmp, "platform_field_catalog_20260906")
        os.makedirs(catalog)
        with open(os.path.join(catalog, "manifest.json"), "w", encoding="utf-8") as f:
            json.dump({"datasets": {"pv1": "not-an-object"}}, f)
        discovery = FieldDiscovery(self.client, cache_path=os.path.join(self._tmp, "fields_cache.json"))
        fields = discovery._fields_for("pv1")
        self.assertEqual(discovery.source_provenance()["kind"], "brain_api")
        self.assertTrue(fields)
        self.assertTrue(self.client.datafield_calls)

    def test_newer_corrupt_catalog_does_not_hide_older_complete_catalog(self):
        older = os.path.join(self._tmp, "platform_field_catalog_20260901")
        newer = os.path.join(self._tmp, "platform_field_catalog_20260907")
        for catalog in (older, newer):
            os.makedirs(catalog)
            with open(os.path.join(catalog, "manifest.json"), "w", encoding="utf-8") as handle:
                json.dump({"fetched_at": catalog[-8:], "datasets": {
                    "pv1": {"file": "pv1.json"}
                }}, handle)
        with open(os.path.join(older, "pv1.json"), "w", encoding="utf-8") as handle:
            json.dump({"fields": [{"id": "older_valid", "description": "valid", "type": "MATRIX"}]}, handle)
        with open(os.path.join(newer, "pv1.json"), "w", encoding="utf-8") as handle:
            json.dump({"fields": "corrupt"}, handle)
        discovery = FieldDiscovery(self.client, cache_path=os.path.join(self._tmp, "fields_cache.json"))
        fields = discovery._fields_for("pv1")
        self.assertEqual([field["id"] for field in fields], ["older_valid"])
        self.assertEqual(discovery.source_provenance()["path"], os.path.abspath(older))
        self.assertEqual(self.client.datafield_calls, [])

    def test_malformed_catalog_and_disk_cache_shapes_fail_closed(self):
        catalog = os.path.join(self._tmp, "platform_field_catalog_20260907")
        os.makedirs(catalog)
        with open(os.path.join(catalog, "manifest.json"), "w", encoding="utf-8") as f:
            json.dump([], f)
        cache_path = os.path.join(self._tmp, "fields_cache.json")
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump([], f)
        discovery = FieldDiscovery(self.client, cache_path=cache_path)
        self.assertEqual(discovery.source_provenance()["kind"], "brain_api")
        self.assertEqual(discovery._disk_cache, {})

    def test_complete_local_catalog_does_not_create_legacy_disk_cache(self):
        catalog = os.path.join(self._tmp, "platform_field_catalog_20260906")
        os.makedirs(catalog)
        with open(os.path.join(catalog, "manifest.json"), "w", encoding="utf-8") as f:
            json.dump({"fetched_at": "2026-09-06T00:00:00+0800", "datasets": {
                "pv1": {"file": "pv1.json"}
            }}, f)
        with open(os.path.join(catalog, "pv1.json"), "w", encoding="utf-8") as f:
            json.dump({"fields": [{"id": "local_price", "description": "local price",
                                    "type": "MATRIX"}]}, f)
        cache_path = os.path.join(self._tmp, "fields_cache.json")
        discovery = FieldDiscovery(self.client, cache_path=cache_path)
        discovery._fields_for("pv1")
        discovery._save_disk_cache()
        self.assertFalse(os.path.exists(cache_path))

    def test_agent_preflight_reuses_complete_catalog_without_legacy_cache(self):
        catalog = os.path.join(self._tmp, "platform_field_catalog_20260906")
        os.makedirs(catalog)
        with open(os.path.join(catalog, "manifest.json"), "w", encoding="utf-8") as f:
            json.dump({"fetched_at": "2026-09-06T00:00:00+0800", "datasets": {
                "pv1": {"file": "pv1.json"}
            }}, f)
        with open(os.path.join(catalog, "pv1.json"), "w", encoding="utf-8") as f:
            json.dump({"fields": [{"id": "catalog_vector", "description": "catalog vector",
                                    "type": "VECTOR"}]}, f)
        agent, _ = make_agent(self._tmp)
        types, profiles = agent._read_field_cache()
        self.assertEqual(types["catalog_vector"], "VECTOR")
        self.assertEqual(profiles["catalog_vector"]["dataset"], "pv1")
        self.assertFalse(os.path.exists(os.path.join(self._tmp, "fields_cache.json")))

    def test_catalog_scope_mismatch_is_not_used_as_current_platform_evidence(self):
        catalog = os.path.join(self._tmp, "platform_field_catalog_20260908")
        os.makedirs(catalog)
        with open(os.path.join(catalog, "pv1.json"), "w", encoding="utf-8") as handle:
            json.dump({"fields": [{"id": "stale", "description": "stale"}]}, handle)
        with open(os.path.join(catalog, "manifest.json"), "w", encoding="utf-8") as handle:
            json.dump({
                "schema": 1,
                "fetched_at": "2026-09-08T00:00:00Z",
                "scope": {
                    "instrument_type": "EQUITY", "region": "EUR",
                    "delay": 1, "universe": "TOP3000",
                },
                "datasets": {"pv1": {"file": "pv1.json"}},
            }, handle)
        discovery = FieldDiscovery(
            self.client,
            catalog_root=self._tmp,
            cache_path=os.path.join(self._tmp, "fields_cache.json"),
        )
        self.assertIsNone(discovery._catalog_provenance)
        discovery._fields_for("pv1")
        self.assertTrue(self.client.datafield_calls)
