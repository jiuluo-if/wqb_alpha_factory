import inspect
import json
import multiprocessing
import os
import tempfile
import threading
import time
import unittest
from datetime import UTC, datetime

from wqb_agent import research_api
from wqb_agent.client import (
    WQBRateLimitError,
    WQBRemoteSimulationError,
    WQBSimulationError,
    WQBSubmitUnknownError,
    WQBTimeoutError,
)
from wqb_agent.locking import OwnerBusyError
from wqb_agent.remote_evidence import RemoteAlphaEvidenceProvider
from wqb_agent.remote_quota import SimulationQuota
from wqb_agent.simulation_gateway import (
    MULTI_DEFAULT_CHILD_BATCH_SIZE,
    MULTI_DEFAULT_CONCURRENCY,
    MULTI_MAX_CHILDREN,
    MULTI_MAX_CONCURRENCY,
    MULTI_MIN_CHILDREN,
    ExecutionGuard,
    SimulationGateway,
    SimulationSpec,
    classify_write_readiness,
)


class FakeGatewayClient:
    def __init__(self, *, unknown=False, quota_unknown=False, quota_exhausted=False):
        self.unknown = unknown
        self.quota_unknown = quota_unknown
        self.quota_exhausted = quota_exhausted
        self.submissions = []

    def get_simulation_quota_observation(self):
        if self.quota_unknown:
            return {
                "status": "UNKNOWN", "source": "BRAIN_SIMULATION_HEADERS",
                "limit": None, "remaining": None, "reset": None,
            }
        if self.quota_exhausted:
            return {
                "status": "AVAILABLE", "source": "BRAIN_SIMULATION_HEADERS",
                "limit": 100, "remaining": 0, "reset": 1,
            }
        return {
            "status": "AVAILABLE", "source": "BRAIN_SIMULATION_HEADERS",
            "limit": 100, "remaining": 50, "reset": 1,
        }

    def get_operator_capability(self):
        return {
            "valid": True, "status": "AVAILABLE", "source": "BRAIN_LIVE_ONLY",
            "operators": ["rank"],
        }

    def submit_simulation(self, expression, settings, **kwargs):
        self.submissions.append((expression, settings, kwargs))
        if self.unknown:
            raise WQBSubmitUnknownError("response ambiguous")
        return "progress-1"

    def poll_progress(self, progress_url, **kwargs):
        self.assert_progress_url = progress_url
        return "alpha-1"

    def get_alpha(self, alpha_id):
        return {"id": alpha_id, "regular": "rank(close)", "is": {"sharpe": 1.0}}

    def get_aggregates(self, alpha_id):
        return {"alpha_id": alpha_id, "years": []}

    def get_pnl(self, alpha_id):
        return {"alpha_id": alpha_id, "records": []}

    def get_all_user_alphas(self, **_kwargs):
        return []

    def get_self_correlation(self, alpha_id):
        return {"alpha_id": alpha_id, "status": "AVAILABLE", "value": 0.1}

    def get_authentication_status(self):
        return {"authenticated": True, "permissions": ["MULTI_SIMULATION"]}

    def get_simulation_capability(self):
        return {"status": "AVAILABLE", "capability_status": "AVAILABLE",
                "simulation_type_choices": ["REGULAR", "SUPER"],
                "settings": {}, "required_fields": [], "required_settings": []}


class UnauthenticatedGatewayClient(FakeGatewayClient):
    def get_authentication_status(self):
        return {"authenticated": False, "permissions": []}


class TestWriteReadiness(unittest.TestCase):
    def test_simulation_spec_rejects_non_string_expression_without_stringifying(self):
        for value in (None, 123, {"code": "rank(close)"}):
            with self.subTest(value_type=type(value).__name__):
                with self.assertRaisesRegex(TypeError, "expression must be a string"):
                    SimulationSpec(value)

    def _facts(self, **overrides):
        facts = {
            "pending_entries": [],
            "official_quota": {
                "status": "AVAILABLE", "source": "BRAIN_SIMULATION_HEADERS",
                "limit": 100, "remaining": 25, "reset": 1,
            },
            "simulation_capability": {
                "status": "AVAILABLE", "capability_status": "AVAILABLE",
                "source": "BRAIN_LIVE", "simulation_type_choices": ["REGULAR"],
            },
            "operator_capability": {
                "valid": True, "status": "AVAILABLE", "source": "BRAIN_LIVE_ONLY",
                "operators": ["rank"],
            },
            "authentication": {"authenticated": True},
            "execution_state_known": True,
            "client_available": True,
        }
        facts.update(overrides)
        return facts

    def test_write_readiness_is_ready_only_with_official_quota_and_live_capability(self):
        result = classify_write_readiness(**self._facts())
        self.assertEqual(result, {"write_readiness": "READY", "write_blockers": []})

    def test_write_readiness_keeps_unobserved_official_quota_non_blocking(self):
        # BRAIN only exposes the official quota on a successful POST response,
        # so a cold start has no official observation yet. Treating that as a
        # blocker would make the first write impossible forever.
        result = classify_write_readiness(**self._facts(
            pending_entries=[{"status": "SUBMIT_UNKNOWN", "kind": "SINGLE"}],
            official_quota={"status": "UNKNOWN", "source": "BRAIN_SIMULATION_HEADERS"},
        ))
        self.assertEqual(result["write_readiness"], "BLOCKED_BY_REMOTE_STATE")
        self.assertEqual(result["write_blockers"], [
            "UNRESOLVED_EXECUTIONS", "SUBMIT_UNKNOWN_PRESENT",
        ])

    def test_write_readiness_blocks_observed_official_quota_exhaustion(self):
        result = classify_write_readiness(**self._facts(
            official_quota={
                "status": "AVAILABLE", "source": "BRAIN_SIMULATION_HEADERS",
                "limit": 100, "remaining": 0, "reset": 1,
            },
        ))
        self.assertEqual(result["write_readiness"], "BLOCKED_BY_REMOTE_STATE")
        self.assertEqual(result["write_blockers"], ["OFFICIAL_QUOTA_EXHAUSTED"])

    def test_write_readiness_ignores_approximate_quota_estimate(self):
        result = classify_write_readiness(**self._facts(
            official_quota={
                "status": "AVAILABLE",
                "source": "ESTIMATE_REMOTE_ALPHA_REPOSITORY+EXECUTION_GUARD",
                "limit": 100, "remaining": 0, "reset": 1,
            },
        ))
        self.assertEqual(result, {"write_readiness": "READY", "write_blockers": []})

    def test_write_readiness_preserves_waiting_when_no_live_client_exists(self):
        result = classify_write_readiness(**self._facts(
            client_available=False,
            authentication=None,
            official_quota={"status": "UNKNOWN", "source": "BRAIN_SIMULATION_HEADERS"},
            simulation_capability=None,
            operator_capability=None,
        ))
        self.assertEqual(result["write_readiness"], "WAITING_FOR_CAPABILITY")
        self.assertIn("AUTHENTICATION_UNKNOWN", result["write_blockers"])
        self.assertNotIn("OFFICIAL_QUOTA_UNKNOWN", result["write_blockers"])

    def test_write_readiness_counts_multi_child_as_unresolved_state(self):
        result = classify_write_readiness(**self._facts(
            pending_entries=[{"status": "RUNNING", "kind": "MULTI_CHILD"}],
        ))
        self.assertEqual(result["write_readiness"], "BLOCKED_BY_REMOTE_STATE")
        self.assertIn("UNRESOLVED_EXECUTIONS", result["write_blockers"])

    def test_batch_admission_allows_disjoint_child_and_reserves_unknown_parent_slot(self):
        from wqb_agent.simulation_gateway import classify_batch_write_admission

        pending = [
            {
                "execution_fingerprint": "unknown-parent",
                "kind": ExecutionGuard.MULTI_PARENT,
                "status": "SUBMIT_UNKNOWN",
                "simulation_count": 10,
            },
            {
                "execution_fingerprint": "unknown-child",
                "kind": ExecutionGuard.MULTI_CHILD,
                "status": "SUBMIT_UNKNOWN",
                "parent_fingerprint": "unknown-parent",
            },
        ]
        result = classify_batch_write_admission(
            **self._facts(pending_entries=pending),
            candidate_fingerprints=["unknown-child", "new-child"],
            requested_max_concurrent_multi=8,
        )

        self.assertEqual(result["write_readiness"], "READY")
        self.assertEqual(result["write_blockers"], [])
        self.assertEqual(result["eligible_count"], 1)
        self.assertEqual(result["blocked_count"], 1)
        self.assertEqual(result["active_multi_parent_count"], 1)
        self.assertEqual(result["effective_max_concurrent_multi"], 7)
        self.assertEqual(result["available_multi_parent_slots"], 7)
        self.assertEqual(result["children_per_multi_parent"], 10)
        self.assertEqual(result["upper_bound_children_in_active_window"], 70)

    def test_batch_admission_blocks_when_all_candidates_match_unknown_guards(self):
        from wqb_agent.simulation_gateway import classify_batch_write_admission

        result = classify_batch_write_admission(
            **self._facts(pending_entries=[
                {
                    "execution_fingerprint": "unknown-child",
                    "kind": ExecutionGuard.MULTI_CHILD,
                    "status": "SUBMIT_UNKNOWN",
                    "parent_fingerprint": "unknown-parent",
                },
            ]),
            candidate_fingerprints=["unknown-child"],
            requested_max_concurrent_multi=8,
        )

        self.assertEqual(result["write_readiness"], "BLOCKED_BY_REMOTE_STATE")
        self.assertEqual(result["eligible_count"], 0)
        self.assertEqual(result["blocked_count"], 1)
        self.assertIn("NO_ELIGIBLE_CANDIDATES", result["write_blockers"])

    def test_batch_admission_blocks_when_unknown_parents_reserve_all_multi_slots(self):
        from wqb_agent.simulation_gateway import classify_batch_write_admission

        pending = [
            {
                "execution_fingerprint": f"parent-{index}",
                "kind": ExecutionGuard.MULTI_PARENT,
                "status": "SUBMIT_UNKNOWN",
                "simulation_count": 10,
            }
            for index in range(MULTI_MAX_CONCURRENCY)
        ]
        result = classify_batch_write_admission(
            **self._facts(pending_entries=pending),
            candidate_fingerprints=["new-child"],
            requested_max_concurrent_multi=MULTI_MAX_CONCURRENCY,
        )

        self.assertEqual(result["write_readiness"], "BLOCKED_BY_REMOTE_STATE")
        self.assertEqual(result["effective_max_concurrent_multi"], 0)
        self.assertIn("MULTI_PARENT_CAPACITY_RESERVED", result["write_blockers"])

    def test_batch_admission_keeps_capacity_unknown_when_execution_guard_is_unknown(self):
        from wqb_agent.simulation_gateway import classify_batch_write_admission

        result = classify_batch_write_admission(
            **self._facts(execution_state_known=False, pending_entries=None),
            candidate_fingerprints=["candidate-a"],
        )

        self.assertEqual(result["write_readiness"], "UNKNOWN")
        self.assertIsNone(result["active_multi_parent_count"])
        self.assertIsNone(result["effective_max_concurrent_multi"])
        self.assertIsNone(result["available_multi_parent_slots"])
        self.assertIsNone(result["upper_bound_children_in_active_window"])

    def test_write_readiness_waits_for_missing_capability_and_unknowns_broken_guard(self):
        waiting = classify_write_readiness(**self._facts(
            client_available=False,
            authentication=None,
            simulation_capability=None,
            operator_capability=None,
        ))
        self.assertEqual(waiting["write_readiness"], "WAITING_FOR_CAPABILITY")
        self.assertIn("SIMULATION_CAPABILITY_UNKNOWN", waiting["write_blockers"])
        unknown = classify_write_readiness(**self._facts(execution_state_known=False))
        self.assertEqual(unknown["write_readiness"], "UNKNOWN")
        self.assertIn("EXECUTION_STATE_UNKNOWN", unknown["write_blockers"])

    def test_gateway_allows_disjoint_single_write_when_an_unresolved_guard_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeGatewayClient()
            gateway = SimulationGateway(client, state_dir=tmp)
            gateway.guard.register(
                "another-unresolved-fingerprint", status="SUBMIT_UNKNOWN",
                simulation_count=1, kind=ExecutionGuard.SINGLE,
            )
            result = gateway.simulate_batch([SimulationSpec("rank(close)")])[0]
            entries = gateway.guard.entries()
        self.assertEqual(result["status"], "DONE")
        self.assertEqual(len(client.submissions), 1)
        self.assertTrue(any(
            row["execution_fingerprint"] == "another-unresolved-fingerprint"
            and row["status"] == "SUBMIT_UNKNOWN"
            for row in entries
        ))

    def test_research_batch_status_admits_disjoint_specs_and_reserves_unknown_parent(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = MultiGatewayClient()
            gateway = SimulationGateway(client, state_dir=tmp)
            gateway.guard.register(
                "unknown-parent", status="SUBMIT_UNKNOWN", kind=ExecutionGuard.MULTI_PARENT,
                simulation_count=10,
            )
            unknown_child = SimulationSpec("rank(field_unknown)", {"delay": 1})
            gateway.guard.register(
                gateway.execution_fingerprint(unknown_child),
                status="SUBMIT_UNKNOWN", kind=ExecutionGuard.MULTI_CHILD,
                simulation_count=1, parent_fingerprint="unknown-parent",
            )

            result = gateway.research_batch_status([
                SimulationSpec("rank(field_a)", {"delay": 1}),
                SimulationSpec("rank(field_b)", {"delay": 1}),
            ], max_concurrent_multi=8)

        self.assertEqual(result["write_readiness"], "READY")
        self.assertEqual(result["eligible_count"], 2)
        self.assertEqual(result["blocked_count"], 0)
        self.assertEqual(result["active_multi_parent_count"], 1)
        self.assertEqual(result["effective_max_concurrent_multi"], 7)

    def test_multi_batch_continues_disjoint_children_without_reposting_unknown_child(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = MultiGatewayClient()
            gateway = SimulationGateway(client, state_dir=tmp)
            gateway.guard.register(
                "unknown-parent", status="SUBMIT_UNKNOWN", kind=ExecutionGuard.MULTI_PARENT,
                simulation_count=10,
            )
            unknown_child = SimulationSpec("rank(field_unknown)", {"delay": 1})
            unknown_fingerprint = gateway.execution_fingerprint(unknown_child)
            gateway.guard.register(
                unknown_fingerprint, status="SUBMIT_UNKNOWN",
                kind=ExecutionGuard.MULTI_CHILD, simulation_count=1,
                parent_fingerprint="unknown-parent",
            )
            specs = [unknown_child] + [
                SimulationSpec(f"rank(field_{index})", {"delay": 1})
                for index in range(9)
            ]

            results = gateway.simulate_multi_batch(
                specs, child_batch_size=10, max_concurrent_multi=8
            )
            entries = gateway.guard.entries()

        self.assertEqual(results[0]["status"], "SUBMIT_UNKNOWN")
        self.assertEqual([row["status"] for row in results[1:]], ["DONE"] * 9)
        self.assertEqual(len(client.multi_submissions), 1)
        self.assertEqual(len(client.multi_submissions[0][0]), 9)
        self.assertTrue(any(
            row["execution_fingerprint"] == unknown_fingerprint
            and row["status"] == "SUBMIT_UNKNOWN"
            for row in entries
        ))
        self.assertTrue(any(
            row["execution_fingerprint"] == "unknown-parent"
            and row["status"] == "SUBMIT_UNKNOWN"
            for row in entries
        ))

    def test_multi_batch_dispatches_ready_remainder_without_padding_to_a_minimum(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = MultiGatewayClient()
            gateway = SimulationGateway(client, state_dir=tmp)
            unknown = SimulationSpec("rank(field_unknown)", {"delay": 1})
            unknown_fp = gateway.execution_fingerprint(unknown)
            gateway.guard.register(
                unknown_fp, status="SUBMIT_UNKNOWN", kind=ExecutionGuard.SINGLE,
            )
            specs = [unknown] + [SimulationSpec(
                f"rank(field_{index})", {"delay": 1}, proposal_id=f"candidate-{index}",
            ) for index in range(4)]

            results = gateway.simulate_multi_batch(specs)
            entries = gateway.guard.entries()

        self.assertEqual(results[0]["status"], "SUBMIT_UNKNOWN")
        self.assertEqual([row["status"] for row in results[1:]], ["DONE"] * 4)
        self.assertEqual([len(payloads) for payloads, _ in client.multi_submissions], [4])
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["execution_fingerprint"], unknown_fp)

    def test_multi_batch_dispatches_nonduplicate_remainder_after_remote_scan(self):
        class DuplicateMultiClient(MultiGatewayClient):
            def get_all_user_alphas(self, **_kwargs):
                return [{
                    "id": "alpha-existing", "regular": "rank(close)",
                    "settings": {"delay": 1}, "status": "UNSUBMITTED",
                }]

        with tempfile.TemporaryDirectory() as tmp:
            client = DuplicateMultiClient()
            gateway = SimulationGateway(client, state_dir=tmp)
            results = gateway.simulate_multi_batch([
                SimulationSpec("rank(close)", {"delay": 1}),
                SimulationSpec("rank(open)", {"delay": 1}),
            ])

        self.assertEqual(results[0]["status"], "EXACT_DUPLICATE")
        self.assertEqual(results[1]["status"], "DONE")
        self.assertEqual([row[0] for row in client.submissions], ["rank(open)"])
        self.assertEqual(client.multi_submissions, [])

    def test_transport_capacity_uses_remaining_parent_slots_while_unknown_parent_is_quarantined(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = MultiGatewayClient()
            gateway = SimulationGateway(client, state_dir=tmp)
            gateway.guard.register(
                "unknown-parent", status="SUBMIT_UNKNOWN",
                kind=ExecutionGuard.MULTI_PARENT, simulation_count=10,
            )
            quarantined = SimulationSpec("rank(field_quarantined)", {"delay": 1})
            quarantined_fp = gateway.execution_fingerprint(quarantined)
            gateway.guard.register(
                quarantined_fp, status="SUBMIT_UNKNOWN",
                kind=ExecutionGuard.MULTI_CHILD, simulation_count=1,
                parent_fingerprint="unknown-parent",
            )
            specs = [SimulationSpec(
                f"rank(field_wave_{index})", {"delay": 1},
                proposal_id=f"wave-{index}",
            ) for index in range(80)]

            admission = gateway.research_batch_status(specs)
            results = gateway.simulate_multi_batch(specs)
            entries = gateway.guard.entries()

        self.assertEqual(admission["write_readiness"], "READY")
        self.assertEqual(admission["eligible_count"], 80)
        self.assertEqual(admission["effective_max_concurrent_multi"], 7)
        self.assertEqual([item["status"] for item in results], ["DONE"] * 80)
        self.assertEqual(len(client.multi_submissions), 8)
        self.assertLessEqual(client.max_active_multi, 7)
        self.assertTrue(any(
            row["execution_fingerprint"] == "unknown-parent"
            and row["status"] == "SUBMIT_UNKNOWN"
            for row in entries
        ))
        self.assertTrue(any(
            row["execution_fingerprint"] == quarantined_fp
            and row["status"] == "SUBMIT_UNKNOWN"
            for row in entries
        ))

    def test_twenty_three_meaningful_candidates_dispatch_without_a_project_minimum(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = MultiGatewayClient()
            gateway = SimulationGateway(client, state_dir=tmp)
            specs = [SimulationSpec(
                f"rank(probe_{index})", {"delay": 1}, proposal_id=f"probe-{index}",
                note="LANE:PROBE" if index < 18 else "LANE:OPTIMIZE",
            ) for index in range(23)]

            results = gateway.simulate_multi_batch(specs)

        self.assertEqual([row["status"] for row in results], ["DONE"] * 23)
        self.assertEqual([len(payloads) for payloads, _ in client.multi_submissions], [10, 10, 3])

    def test_one_hundred_meaningful_candidates_fill_safe_multi_window_and_drain_queue(self):
        class FirstWindowBarrierClient(MultiGatewayClient):
            def __init__(self):
                super().__init__()
                self._call_count = 0
                self._first_window = threading.Barrier(MULTI_MAX_CONCURRENCY)

            def submit_multi_simulation(self, payloads, **kwargs):
                with self._active_lock:
                    self._active_multi += 1
                    self._call_count += 1
                    call_index = self._call_count
                    self.max_active_multi = max(
                        self.max_active_multi, self._active_multi,
                    )
                try:
                    if call_index <= MULTI_MAX_CONCURRENCY:
                        self._first_window.wait(timeout=5)
                    self.multi_submissions.append((payloads, kwargs))
                    progress_url = f"multi-progress-{len(self.multi_submissions)}"
                    self._multi_sizes[progress_url] = len(payloads)
                    return progress_url
                finally:
                    with self._active_lock:
                        self._active_multi -= 1

        with tempfile.TemporaryDirectory() as tmp:
            client = FirstWindowBarrierClient()
            gateway = SimulationGateway(client, state_dir=tmp)
            specs = [SimulationSpec(
                f"rank(wide_probe_{index})", {"delay": 1},
                proposal_id=f"wide-probe-{index}",
            ) for index in range(100)]

            results = gateway.simulate_multi_batch(specs)

        self.assertEqual([row["status"] for row in results], ["DONE"] * 100)
        self.assertEqual([len(payloads) for payloads, _ in client.multi_submissions], [10] * 10)
        self.assertEqual(client.max_active_multi, MULTI_MAX_CONCURRENCY)

    def test_gateway_rejects_more_than_one_hundred_candidates_before_guarding(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = MultiGatewayClient()
            gateway = SimulationGateway(client, state_dir=tmp)
            specs = [SimulationSpec(
                f"rank(oversize_probe_{index})", {"delay": 1},
                proposal_id=f"oversize-probe-{index}",
            ) for index in range(101)]

            with self.assertRaisesRegex(ValueError, "at most 100 candidates"):
                gateway.simulate_multi_batch(specs)

            self.assertEqual(gateway.guard.entries(), [])
            self.assertEqual(client.multi_submissions, [])

    def test_one_field_preflight_failure_does_not_discard_valid_multi_children(self):
        class PartiallyValidClient(MultiGatewayClient):
            def get_field_capability(self, field_sources, *, scope=None):
                fields = [field for selected in field_sources.values() for field in selected]
                if "missing_field" in fields:
                    return {
                        "valid": False, "fields": [],
                        "source": "BRAIN_LIVE_ONLY",
                    }
                return {
                    "valid": True, "fields": fields,
                    "source": "BRAIN_LIVE_ONLY",
                }

        with tempfile.TemporaryDirectory() as tmp:
            client = PartiallyValidClient()
            gateway = SimulationGateway(client, state_dir=tmp)
            specs = [SimulationSpec(
                "rank(missing_field)", {"delay": 1}, fields=("missing_field",),
                field_datasets={"missing_field": "synthetic"}, proposal_id="bad",
            )] + [SimulationSpec(
                f"rank(field_{index})", {"delay": 1}, fields=(f"field_{index}",),
                field_datasets={f"field_{index}": "synthetic"}, proposal_id=f"ok-{index}",
            ) for index in range(9)]

            results = gateway.simulate_multi_batch(specs)

        self.assertEqual(results[0]["status"], "NOT_DISPATCHED")
        self.assertEqual(results[0]["reason_code"], "CAPABILITY_UNAVAILABLE")
        self.assertEqual([item["status"] for item in results[1:]], ["DONE"] * 9)
        self.assertEqual(len(client.multi_submissions), 1)
        self.assertEqual(len(client.multi_submissions[0][0]), 9)

    def test_gateway_allows_new_write_when_official_quota_is_unobserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeGatewayClient(quota_unknown=True)
            result = SimulationGateway(client, state_dir=tmp).simulate_batch([
                SimulationSpec("rank(close)"),
            ])[0]
        self.assertEqual(result["status"], "DONE")
        self.assertEqual(len(client.submissions), 1)

    def test_gateway_blocks_new_write_when_official_quota_is_exhausted(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeGatewayClient(quota_exhausted=True)
            result = SimulationGateway(client, state_dir=tmp).simulate_batch([
                SimulationSpec("rank(close)"),
            ])[0]
        self.assertEqual(result["status"], "BLOCKED_BY_REMOTE_STATE")
        self.assertEqual(result["reason_code"], "OFFICIAL_QUOTA_EXHAUSTED")
        self.assertEqual(client.submissions, [])

    def test_gateway_blocks_when_current_authentication_is_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = UnauthenticatedGatewayClient()
            result = SimulationGateway(client, state_dir=tmp).simulate(
                SimulationSpec("rank(close)")
            )
        self.assertEqual(result["status"], "BLOCKED_BY_REMOTE_STATE")
        self.assertEqual(result["reason_code"], "AUTHENTICATION_UNAVAILABLE")
        self.assertEqual(client.submissions, [])

    def test_gateway_blocks_a_vector_field_used_without_aggregation(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = VectorFieldCapabilityGatewayClient()
            results = SimulationGateway(client, state_dir=tmp).simulate_batch([
                SimulationSpec(
                    "rank(vec_field)", fields=("vec_field",),
                    field_datasets={"vec_field": "synthetic_dataset"},
                ),
            ])
        self.assertEqual(results[0]["status"], "NOT_DISPATCHED")
        self.assertEqual(results[0]["reason_code"], "FIELD_TYPE_MISMATCH")
        self.assertIn("FIELD_TYPE_MISMATCH", results[0]["error"])
        self.assertEqual(client.submissions, [])

    def test_gateway_still_runs_valid_siblings_of_a_blocked_vector_spec(self):
        # One deterministically invalid child must not cost the whole batch:
        # the platform would otherwise fail the entire Multi parent.
        with tempfile.TemporaryDirectory() as tmp:
            client = VectorFieldCapabilityGatewayClient()
            results = SimulationGateway(client, state_dir=tmp).simulate_batch([
                SimulationSpec(
                    "rank(vec_field)", fields=("vec_field",),
                    field_datasets={"vec_field": "synthetic_dataset"},
                ),
                SimulationSpec(
                    "rank(matrix_field)", fields=("matrix_field",),
                    field_datasets={"matrix_field": "synthetic_dataset"},
                ),
            ])
        self.assertEqual(results[0]["reason_code"], "FIELD_TYPE_MISMATCH")
        self.assertEqual(results[1]["status"], "DONE")
        self.assertEqual(len(client.submissions), 1)

    def test_gateway_allows_a_vector_field_aggregated_with_vec_avg(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = VectorFieldCapabilityGatewayClient()
            results = SimulationGateway(client, state_dir=tmp).simulate_batch([
                SimulationSpec(
                    "rank(vec_avg(vec_field))", fields=("vec_field",),
                    field_datasets={"vec_field": "synthetic_dataset"},
                ),
            ])
        self.assertEqual(results[0]["status"], "DONE")
        self.assertEqual(len(client.submissions), 1)

    def test_region_agnostic_fields_are_checked_in_child_regions(self):
        class RegionAwareClient(FakeGatewayClient):
            def __init__(self):
                super().__init__()
                self.field_regions = []
                self.field_scopes = []

            def get_authentication_status(self):
                return {"authenticated": True, "permissions": ["REGION_AGNOSTIC"]}

            def get_simulation_capability(self):
                return {
                    "status": "AVAILABLE", "capability_status": "AVAILABLE",
                    "simulation_type_choices": ["REGULAR", "REGION_AGNOSTIC"],
                    "settings": {}, "required_fields": [], "required_settings": [],
                }

            def get_field_capability(self, field_sources, *, scope=None):
                region = scope["region"]
                self.field_regions.append(region)
                self.field_scopes.append(dict(scope))
                fields = [field for selected in field_sources.values() for field in selected]
                available = region in {"ASI", "GLB"}
                return {
                    "valid": available,
                    "fields": fields if available else [],
                    "source": "BRAIN_LIVE_ONLY",
                    "field_types": {field: "MATRIX" for field in fields if available},
                }

        with tempfile.TemporaryDirectory() as tmp:
            client = RegionAwareClient()
            guard = ExecutionGuard(tmp, reconcile=False)
            guard.register(
                "unrelated-unknown", status="SUBMIT_UNKNOWN", kind="MULTI_PARENT",
                simulation_count=10,
            )
            result = SimulationGateway(client, state_dir=tmp).simulate(SimulationSpec(
                "rank(field_a)",
                {"region": "ALL", "universe": "MINVOL1M", "delay": 1},
                fields=("field_a",), field_datasets={"field_a": "dataset_a"},
                simulation_type="REGION_AGNOSTIC",
            ))
            unrelated_guard = ExecutionGuard(tmp, reconcile=False).find(
                "unrelated-unknown"
            )

        self.assertEqual(result["status"], "DONE")
        self.assertEqual(client.field_regions, ["USA", "EUR", "ASI", "GLB"])
        # The aggregate REGION_AGNOSTIC universe is not a field-catalog scope;
        # each child region is probed with its own catalog universe.
        self.assertEqual(
            [scope["universe"] for scope in client.field_scopes],
            ["TOP3000", "TOP2500", "MINVOL1M", "MINVOL1M"],
        )
        self.assertEqual(len(client.submissions), 1)
        self.assertIsNotNone(unrelated_guard)

    def test_region_agnostic_field_coverage_requires_two_child_regions(self):
        class OneRegionClient(FakeGatewayClient):
            def get_authentication_status(self):
                return {"authenticated": True, "permissions": ["REGION_AGNOSTIC"]}

            def get_simulation_capability(self):
                return {
                    "status": "AVAILABLE", "capability_status": "AVAILABLE",
                    "simulation_type_choices": ["REGULAR", "REGION_AGNOSTIC"],
                    "settings": {}, "required_fields": [], "required_settings": [],
                }

            def get_field_capability(self, field_sources, *, scope=None):
                fields = [field for selected in field_sources.values() for field in selected]
                available = scope["region"] == "GLB"
                return {
                    "valid": available,
                    "fields": fields if available else [],
                    "source": "BRAIN_LIVE_ONLY",
                }

        with tempfile.TemporaryDirectory() as tmp:
            client = OneRegionClient()
            result = SimulationGateway(client, state_dir=tmp).simulate(SimulationSpec(
                "rank(field_a)",
                {"region": "ALL", "universe": "MINVOL1M", "delay": 1},
                fields=("field_a",), field_datasets={"field_a": "dataset_a"},
                simulation_type="REGION_AGNOSTIC",
            ))

        self.assertEqual(result["status"], "NOT_DISPATCHED")
        self.assertEqual(result["reason_code"], "RA_FIELD_COVERAGE_UNAVAILABLE")
        self.assertEqual(client.submissions, [])

    def test_region_agnostic_fields_must_coexist_in_two_regions(self):
        class SplitRegionClient(FakeGatewayClient):
            def get_authentication_status(self):
                return {"authenticated": True, "permissions": ["REGION_AGNOSTIC"]}

            def get_operator_capability(self):
                return {
                    "valid": True, "status": "AVAILABLE",
                    "source": "BRAIN_LIVE_ONLY",
                    "operators": ["multiply", "rank"],
                }

            def get_simulation_capability(self):
                return {
                    "status": "AVAILABLE", "capability_status": "AVAILABLE",
                    "simulation_type_choices": ["REGULAR", "REGION_AGNOSTIC"],
                    "settings": {}, "required_fields": [], "required_settings": [],
                }

            def get_field_capability(self, field_sources, *, scope=None):
                region = scope["region"]
                dataset_id, requested = next(iter(field_sources.items()))
                available = {
                    ("USA", "dataset_a"): {"field_a"},
                    ("EUR", "dataset_b"): {"field_b"},
                    ("ASI", "dataset_a"): {"field_a"},
                    ("GLB", "dataset_b"): {"field_b"},
                }.get((region, dataset_id), set())
                fields = sorted(available.intersection(requested))
                return {
                    "valid": len(fields) == len(requested), "fields": fields,
                    "source": "BRAIN_LIVE_ONLY",
                    "field_types": {field: "MATRIX" for field in fields},
                }

        with tempfile.TemporaryDirectory() as tmp:
            client = SplitRegionClient()
            result = SimulationGateway(client, state_dir=tmp).simulate(SimulationSpec(
                "multiply(rank(field_a),rank(field_b))",
                {"region": "ALL", "universe": "MINVOL1M", "delay": 1},
                fields=("field_a", "field_b"),
                field_datasets={"field_a": "dataset_a", "field_b": "dataset_b"},
                simulation_type="REGION_AGNOSTIC",
            ))

        self.assertEqual(result["status"], "NOT_DISPATCHED")
        self.assertEqual(result["reason_code"], "RA_FIELD_COVERAGE_UNAVAILABLE")
        self.assertEqual(client.submissions, [])


class _EmptyQuotaRepository:
    retention_days = 7

    def list_remote_alphas(self):
        return []


class ProcessSimulationClient(FakeGatewayClient):
    def __init__(self, post_log, ready, release):
        super().__init__()
        self.post_log = post_log
        self.ready = ready
        self.release = release

    def submit_simulation(self, expression, settings, **kwargs):
        with open(self.post_log, "a", encoding="utf-8") as handle:
            handle.write("post\n")
        self.ready.set()
        self.release.wait(10)
        return "progress-1"


def _run_process_simulation(state_dir, post_log, ready, release, result_queue):
    client = ProcessSimulationClient(post_log, ready, release)
    try:
        result = SimulationGateway(client, state_dir=state_dir).simulate(
            SimulationSpec("rank(close)", {"delay": 1})
        )
        result_queue.put(result["status"])
    except OwnerBusyError:
        result_queue.put("OWNER_BUSY")


class CapabilityGatewayClient(FakeGatewayClient):
    def __init__(self):
        super().__init__()
        self.capability_calls = 0

    def get_operator_capability(self):
        self.capability_calls += 1
        return {"valid": True, "availability": "AVAILABLE", "operators": ["rank"]}


class UnavailableCapabilityGatewayClient(FakeGatewayClient):
    def get_operator_capability(self):
        return None


class MissingFieldCapabilityGatewayClient(FakeGatewayClient):
    get_field_capability = None


class UnknownFieldCapabilityGatewayClient(FakeGatewayClient):
    def get_field_capability(self, field_sources, *, scope=None):
        requested = [field for fields in field_sources.values() for field in fields]
        return {"valid": False, "fields": [], "missing": requested,
                "source": "BRAIN_LIVE_ONLY"}


class VerifiedFieldCapabilityGatewayClient(FakeGatewayClient):
    def get_field_capability(self, field_sources, *, scope=None):
        fields = [field for selected in field_sources.values() for field in selected]
        return {"valid": True, "fields": fields, "source": "BRAIN_LIVE_ONLY"}


class VectorFieldCapabilityGatewayClient(FakeGatewayClient):
    """Live field capability that reports selected fields as VECTOR-typed."""

    def __init__(self, vector_fields=("vec_field",)):
        super().__init__()
        self.vector_fields = set(vector_fields)

    def get_field_capability(self, field_sources, *, scope=None):
        fields = [field for selected in field_sources.values() for field in selected]
        return {
            "valid": True,
            "fields": fields,
            "source": "BRAIN_LIVE_ONLY",
            "field_types": {
                field: ("VECTOR" if field in self.vector_fields else "MATRIX")
                for field in fields
            },
        }

    def get_operator_capability(self):
        return {
            "valid": True, "status": "AVAILABLE", "source": "BRAIN_LIVE_ONLY",
            "operators": ["rank", "vec_avg"],
        }


class RemoteHistoryGatewayClient(FakeGatewayClient):
    def __init__(self):
        super().__init__()
        self.history_calls = 0

    def get_all_user_alphas(self, **_kwargs):
        self.history_calls += 1
        return [{
            "id": "alpha-existing", "regular": "rank(close)",
            "settings": {"delay": 1}, "status": "UNSUBMITTED",
        }]


class ShardedRemoteHistoryGatewayClient(FakeGatewayClient):
    def get_all_user_alphas(self, **_kwargs):
        from wqb_agent.client import WQBQueryTooBroadError
        raise WQBQueryTooBroadError("wide history needs date shards")

    def iter_user_alpha_history_shards(self, **_kwargs):
        for index in range(1000):
            yield {"id": f"older-alpha-{index}", "regular": "rank(open)",
                   "settings": {"delay": 1}, "status": "UNSUBMITTED"}
        yield {"id": "old-exact", "regular": "rank(close)",
               "settings": {"delay": 1}, "status": "UNSUBMITTED"}


class ConcurrentGatewayClient(FakeGatewayClient):
    def __init__(self):
        super().__init__()
        self._active = 0
        self._active_lock = threading.Lock()
        self.max_active = 0
        self.overlap_barrier = None
        self._overlap_wait_count = 0

    def submit_simulation(self, expression, settings, **kwargs):
        with self._active_lock:
            self._active += 1
            self.max_active = max(self.max_active, self._active)
            wait_for_overlap = (
                self.overlap_barrier is not None
                and self._overlap_wait_count < self.overlap_barrier.parties
            )
            if wait_for_overlap:
                self._overlap_wait_count += 1
        try:
            if not wait_for_overlap:
                time.sleep(0.03)
            else:
                self.overlap_barrier.wait(timeout=15)
            return super().submit_simulation(expression, settings, **kwargs)
        finally:
            with self._active_lock:
                self._active -= 1


class MultiGatewayClient(FakeGatewayClient):
    def __init__(self):
        super().__init__()
        self.multi_submissions = []
        self._active_multi = 0
        self._active_lock = threading.Lock()
        self.max_active_multi = 0
        self._multi_sizes = {}
        self._overlap_event = None

    def submit_multi_simulation(self, payloads, **kwargs):
        with self._active_lock:
            self._active_multi += 1
            self.max_active_multi = max(self.max_active_multi, self._active_multi)
        try:
            if self._overlap_event is None:
                time.sleep(0.03)
            else:
                with self._active_lock:
                    if self._active_multi >= 2:
                        self._overlap_event.set()
                if not self._overlap_event.wait(timeout=5):
                    raise TimeoutError("second Multi POST did not overlap")
            self.multi_submissions.append((payloads, kwargs))
            progress_url = f"multi-progress-{len(self.multi_submissions)}"
            self._multi_sizes[progress_url] = len(payloads)
            return progress_url
        finally:
            with self._active_lock:
                self._active_multi -= 1

    def poll_multi_progress(self, progress_url, **kwargs):
        return [
            f"multi-alpha-{progress_url}-{index}"
            for index in range(self._multi_sizes.get(progress_url, 2))
        ]

    def get_progress_snapshot(self, progress_url, **kwargs):
        return {"status_code": 200, "payload": {"children": ["sim-1", "sim-2"]}}


class UnknownMultiGatewayClient(MultiGatewayClient):
    def submit_multi_simulation(self, payloads, **kwargs):
        self.multi_submissions.append((payloads, kwargs))
        raise WQBSubmitUnknownError("multi response ambiguous")


class RateLimitedMultiGatewayClient(MultiGatewayClient):
    def submit_multi_simulation(self, payloads, **kwargs):
        self.multi_submissions.append((payloads, kwargs))
        raise WQBRateLimitError(
            "multi request rate limited before acceptance", status_code=429
        )


class NoMultiPermissionClient(MultiGatewayClient):
    def get_authentication_status(self):
        return {"authenticated": True, "permissions": []}


class PartialMultiGatewayClient(MultiGatewayClient):
    def poll_multi_progress(self, progress_url, **kwargs):
        return {"status": "SUCCESS", "remote_status": "COMPLETE", "children": [
            {"status": "DONE", "alpha_id": "alpha-ok"},
            {"status": "FAILED", "failure_kind": "FAILED_REMOTE", "remote_status": "FAIL",
             "error": "bad expression"},
            {"status": "FAILED", "failure_kind": "FAILED_REMOTE_TIMEOUT", "remote_status": "TIMEOUT",
             "error": "remote timeout"},
        ]}


class KnownParentReadFailureClient(MultiGatewayClient):
    def poll_multi_progress(self, progress_url, **kwargs):
        raise WQBSubmitUnknownError("parent read outcome was temporarily unknown")


class KnownParentTimeoutClient(MultiGatewayClient):
    def poll_multi_progress(self, progress_url, **kwargs):
        raise WQBTimeoutError("parent polling deadline elapsed")


class PrivateMessageMultiGatewayClient(MultiGatewayClient):
    def poll_multi_progress(self, progress_url, **kwargs):
        raise WQBSubmitUnknownError(
            "backend read failed for rank(private_secret_field)"
        )


class RemoteErrorMultiGatewayClient(MultiGatewayClient):
    def poll_multi_progress(self, progress_url, **kwargs):
        raise WQBRemoteSimulationError({
            "remote_status": "ERROR",
            "message": "invalid regular expression",
            "simulation_id": "sim-parent-error",
            "property": "regular",
            "line": 1,
            "start": 0,
            "end": 11,
        })


class RejectedMultiGatewayClient(MultiGatewayClient):
    def get_progress_snapshot(self, progress_url, **kwargs):
        return {"status_code": 200, "payload": {"status": "ERROR", "children": []}}

    def poll_multi_progress(self, progress_url, **kwargs):
        raise WQBSimulationError("Multi-Simulation rejected by platform: status=ERROR")


class TestSimulationGateway(unittest.TestCase):
    def test_independent_processes_allow_at_most_one_simulation_post(self):
        context = multiprocessing.get_context("spawn")
        with tempfile.TemporaryDirectory() as tmp:
            post_log = os.path.join(tmp, "posts.log")
            ready = context.Event()
            release = context.Event()
            result_queue = context.Queue()
            first = context.Process(
                target=_run_process_simulation,
                args=(tmp, post_log, ready, release, result_queue),
            )
            second = context.Process(
                target=_run_process_simulation,
                args=(tmp, post_log, ready, release, result_queue),
            )
            first.start()
            self.assertTrue(ready.wait(10))
            second.start()
            second.join(10)
            release.set()
            first.join(10)
            self.assertFalse(first.is_alive())
            self.assertFalse(second.is_alive())
            self.assertEqual(sorted([result_queue.get(timeout=2) for _ in range(2)]),
                             ["DONE", "OWNER_BUSY"])
            with open(post_log, encoding="utf-8") as handle:
                self.assertEqual(handle.read().splitlines(), ["post"])

    def test_public_batch_uses_bounded_gateway_concurrency(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = ConcurrentGatewayClient()
            config = {
                "simulation": {},
                "runtime": {"max_concurrent_sims": 2},
            }
            specs = [
                SimulationSpec("rank(close)", {"delay": 1}),
                SimulationSpec("rank(open)", {"delay": 1}),
                SimulationSpec("rank(high)", {"delay": 1}),
            ]

            results = research_api.simulate_batch(
                specs, client=client, config=config, state_dir=tmp
            )

            self.assertEqual([item["status"] for item in results], ["DONE"] * 3)
            self.assertEqual(len(client.submissions), 3)
            self.assertEqual(client.max_active, 2)

    def test_single_batch_defaults_to_ten_concurrent_simulations(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = ConcurrentGatewayClient()
            client.overlap_barrier = threading.Barrier(10)
            gateway = SimulationGateway(client, state_dir=tmp)
            specs = [
                SimulationSpec(f"rank(field_{index})", {"delay": 1})
                for index in range(11)
            ]

            results = gateway.simulate_batch(specs)

            self.assertEqual([item["status"] for item in results], ["DONE"] * 11)
            self.assertEqual(client.max_active, 10)

    def test_multi_batch_groups_ten_children_and_routes_one_remainder_to_single(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = MultiGatewayClient()
            client._overlap_event = threading.Event()
            gateway = SimulationGateway(client, state_dir=tmp)
            specs = [
                SimulationSpec(f"rank(field_{index})", {"delay": 1})
                for index in range(21)
            ]

            results = gateway.simulate_multi_batch(
                specs, child_batch_size=10, max_concurrent_multi=2
            )

            self.assertEqual([item["status"] for item in results], ["DONE"] * 21)
            self.assertEqual(
                [len(payloads) for payloads, _kwargs in client.multi_submissions],
                [10, 10],
            )
            self.assertEqual(len(client.submissions), 1)
            self.assertTrue(all(
                payload["type"] == "REGULAR"
                and payload["regular"].startswith("rank(field_")
                for payloads, _kwargs in client.multi_submissions
                for payload in payloads
            ))
            self.assertEqual(client.max_active_multi, 2)
            self.assertEqual(gateway.guard.entries(), [])

    def test_multi_defaults_and_bounds_have_one_gateway_owned_source(self):
        from wqb_agent import research_api

        self.assertEqual(MULTI_MIN_CHILDREN, 2)
        self.assertEqual(MULTI_MAX_CHILDREN, 10)
        self.assertEqual(MULTI_DEFAULT_CHILD_BATCH_SIZE, 10)
        self.assertEqual(MULTI_DEFAULT_CONCURRENCY, 8)
        self.assertEqual(MULTI_MAX_CONCURRENCY, 8)
        gateway_signature = inspect.signature(
            SimulationGateway.simulate_multi_batch
        ).parameters
        facade_signature = inspect.signature(
            research_api.simulate_multi_batch
        ).parameters
        self.assertEqual(
            gateway_signature["child_batch_size"].default,
            MULTI_DEFAULT_CHILD_BATCH_SIZE,
        )
        self.assertEqual(
            gateway_signature["max_concurrent_multi"].default,
            MULTI_DEFAULT_CONCURRENCY,
        )
        self.assertEqual(
            facade_signature["child_batch_size"].default,
            MULTI_DEFAULT_CHILD_BATCH_SIZE,
        )
        self.assertEqual(
            facade_signature["max_concurrent_multi"].default,
            MULTI_DEFAULT_CONCURRENCY,
        )

    def test_multi_explicit_hard_max_eight_is_valid(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = MultiGatewayClient()
            results = SimulationGateway(client, state_dir=tmp).simulate_multi_batch(
                [
                    SimulationSpec("rank(field_a)", {"delay": 1}),
                    SimulationSpec("rank(field_b)", {"delay": 1}),
                ],
                max_concurrent_multi=8,
            )
        self.assertEqual([item["status"] for item in results], ["DONE", "DONE"])
        self.assertEqual(len(client.multi_submissions), 1)

    def test_multi_concurrency_and_child_bounds_fail_before_guard_or_post(self):
        cases = (
            {"max_concurrent_multi": 0},
            {"max_concurrent_multi": True},
            {"max_concurrent_multi": "2"},
            {"max_concurrent_multi": 9},
            {"child_batch_size": 1},
            {"child_batch_size": 11},
        )
        for overrides in cases:
            with self.subTest(overrides=overrides):
                client = MultiGatewayClient()
                with tempfile.TemporaryDirectory() as tmp:
                    gateway = SimulationGateway(client, state_dir=tmp)
                    with self.assertRaises((TypeError, ValueError)):
                        gateway.simulate_multi_batch(
                            [
                                SimulationSpec("rank(field_a)", {"delay": 1}),
                                SimulationSpec("rank(field_b)", {"delay": 1}),
                            ],
                            **overrides,
                        )
                    self.assertEqual(client.multi_submissions, [])
                    self.assertEqual(client.submissions, [])
                    self.assertEqual(gateway.guard.entries(), [])

    def test_one_child_remainder_uses_single_without_one_child_multi(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = MultiGatewayClient()
            results = SimulationGateway(client, state_dir=tmp).simulate_multi_batch(
                [SimulationSpec("rank(field_a)", {"delay": 1})]
            )
        self.assertEqual(results[0]["status"], "DONE")
        self.assertEqual(client.multi_submissions, [])
        self.assertEqual(len(client.submissions), 1)

    def test_multi_batch_24_packs_as_ten_ten_four(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = MultiGatewayClient()
            client._overlap_event = threading.Event()
            gateway = SimulationGateway(client, state_dir=tmp)
            results = gateway.simulate_multi_batch([
                SimulationSpec(f"rank(field_{index})", {"delay": 1})
                for index in range(24)
            ])
            self.assertEqual([item["status"] for item in results], ["DONE"] * 24)
            self.assertEqual(
                [len(payloads) for payloads, _kwargs in client.multi_submissions],
                [10, 10, 4],
            )
            self.assertEqual(client.max_active_multi, 2)

    def test_multi_requires_live_permission_before_registering_or_posting(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = NoMultiPermissionClient()
            gateway = SimulationGateway(client, state_dir=tmp)
            with self.assertRaisesRegex(ValueError, "PERMISSION_UNAVAILABLE"):
                gateway.simulate_multi_batch([
                    SimulationSpec("rank(field_a)", {"delay": 1}),
                    SimulationSpec("rank(field_b)", {"delay": 1}),
                ])
            self.assertEqual(client.multi_submissions, [])
            self.assertEqual(client.submissions, [])
            self.assertEqual(gateway.guard.entries(), [])

    def test_multi_partial_child_results_are_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = PartialMultiGatewayClient()
            gateway = SimulationGateway(client, state_dir=tmp)
            results = gateway.simulate_multi_batch([
                SimulationSpec("rank(field_a)", {"delay": 1}),
                SimulationSpec("rank(field_b)", {"delay": 1}),
                SimulationSpec("rank(field_c)", {"delay": 1}),
            ])
            self.assertEqual(results[0]["status"], "DONE")
            self.assertEqual(results[1]["failure_kind"], "FAILED_REMOTE")
            self.assertEqual(results[2]["failure_kind"], "FAILED_REMOTE_TIMEOUT")
            self.assertEqual(results[0]["parent"]["status"], "DONE")
            self.assertEqual(results[0]["parent"]["guard_action"], "REMOVED_TERMINAL")
            self.assertEqual(results[0]["parent"]["child_count"], 3)

    def test_multi_parent_remote_error_is_projected_to_every_child(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = RemoteErrorMultiGatewayClient()
            results = SimulationGateway(client, state_dir=tmp).simulate_multi_batch([
                SimulationSpec("rank(field_a)", {"delay": 1}),
                SimulationSpec("rank(field_b)", {"delay": 1}),
            ])

            self.assertEqual([item["status"] for item in results], ["FAILED", "FAILED"])
            parents = [item["parent"] for item in results]
            self.assertEqual(parents[0], parents[1])
            self.assertEqual(parents[0]["status"], "FAILED")
            self.assertEqual(parents[0]["progress_url"], "multi-progress-1")
            self.assertEqual(parents[0]["child_count"], 2)
            self.assertEqual(parents[0]["exception_class"], "WQBRemoteSimulationError")
            self.assertEqual(parents[0]["failure_kind"], "SYNTAX")
            self.assertEqual(parents[0]["failure_scope"], "PARENT")
            self.assertEqual(parents[0]["remote_status"], "ERROR")
            self.assertEqual(parents[0]["diagnostic"]["property"], "regular")
            self.assertEqual(results[0]["failure_scope"], "PARENT")
            self.assertEqual(parents[0]["guard_action"], "REMOVED_TERMINAL")
            self.assertEqual(parents[0]["status_path"], [
                "PENDING", "SUBMITTING", "RUNNING", "FAILED",
            ])
            self.assertEqual(SimulationGateway(client, state_dir=tmp).guard.entries(), [])

    def test_multi_parent_submit_unknown_is_preserved_and_observable(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = UnknownMultiGatewayClient()
            results = SimulationGateway(client, state_dir=tmp).simulate_multi_batch([
                SimulationSpec("rank(field_a)", {"delay": 1}),
                SimulationSpec("rank(field_b)", {"delay": 1}),
                SimulationSpec("rank(field_c)", {"delay": 1}),
                SimulationSpec("rank(field_d)", {"delay": 1}),
            ], child_batch_size=4)

            parent = results[0]["parent"]
            self.assertEqual(parent["status"], "SUBMIT_UNKNOWN")
            self.assertIsNone(parent["progress_url"])
            self.assertEqual(parent["exception_class"], "WQBSubmitUnknownError")
            self.assertEqual(parent["guard_action"], "PRESERVED_SUBMIT_UNKNOWN")
            self.assertEqual(parent["status_path"], [
                "PENDING", "SUBMITTING", "SUBMIT_UNKNOWN",
            ])
            guard = SimulationGateway(client, state_dir=tmp).guard
            entries = guard.entries()
            parents = [row for row in entries if row["kind"] == "MULTI_PARENT"]
            children = [row for row in entries if row["kind"] == "MULTI_CHILD"]
            self.assertEqual(len(parents), 1)
            self.assertEqual(parents[0]["simulation_count"], 4)
            # Every child owns its own unresolved-write identity, so a later
            # reordered/split/subset/Single retry cannot re-POST it.
            self.assertEqual(len(children), 4)
            self.assertTrue(all(
                row["parent_fingerprint"] == parents[0]["execution_fingerprint"]
                for row in children
            ))
            self.assertTrue(all(row["simulation_count"] == 1 for row in children))
            guard_day = SimulationQuota._today(
                datetime.fromtimestamp(parents[0]["created_at"], tz=UTC)
            )
            snapshot = SimulationQuota(
                _EmptyQuotaRepository(), guard,
                local_date=lambda: guard_day,
            ).snapshot()
            self.assertEqual(snapshot["active_guard_count"], 1)
            self.assertEqual(snapshot["active_guard_simulation_count"], 4)
            self.assertEqual(snapshot["today_used"], 4)
            self.assertEqual(snapshot["window_used"], 4)

    def test_multi_parent_known_url_unknown_is_reconcilable_and_observable(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = KnownParentReadFailureClient()
            results = SimulationGateway(client, state_dir=tmp).simulate_multi_batch([
                SimulationSpec("rank(field_a)", {"delay": 1}),
                SimulationSpec("rank(field_b)", {"delay": 1}),
            ])

            parent = results[0]["parent"]
            self.assertEqual(parent["status"], "UNKNOWN")
            self.assertEqual(parent["progress_url"], "multi-progress-1")
            self.assertEqual(parent["exception_class"], "WQBSubmitUnknownError")
            self.assertEqual(parent["guard_action"], "PRESERVED_RUNNING")
            self.assertEqual(parent["status_path"], [
                "PENDING", "SUBMITTING", "RUNNING", "UNKNOWN",
            ])
            self.assertEqual(
                SimulationGateway(client, state_dir=tmp).guard.entries()[0]["status"],
                "RUNNING",
            )
            self.assertEqual(
                SimulationGateway(client, state_dir=tmp).guard.entries()[0]["simulation_count"],
                2,
            )

    def test_multi_parent_known_url_timeout_keeps_timeout_kind_and_guard(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = KnownParentTimeoutClient()
            results = SimulationGateway(client, state_dir=tmp).simulate_multi_batch([
                SimulationSpec("rank(field_a)", {"delay": 1}),
                SimulationSpec("rank(field_b)", {"delay": 1}),
            ])

            parent = results[0]["parent"]
            self.assertEqual(parent["status"], "UNKNOWN")
            self.assertEqual(parent["failure_kind"], "TIMEOUT")
            self.assertIsNone(parent["http_status"])
            self.assertEqual(parent["guard_action"], "PRESERVED_RUNNING")

    def test_multi_parent_diagnostic_redacts_child_expression(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = PrivateMessageMultiGatewayClient()
            results = SimulationGateway(client, state_dir=tmp).simulate_multi_batch([
                SimulationSpec("rank(private_secret_field)", {"delay": 1}),
                SimulationSpec("rank(other_private_field)", {"delay": 1}),
            ])

            error = results[0]["parent"]["error"]
            self.assertNotIn("rank(private_secret_field)", error)
            self.assertNotIn("private_secret_field", error)

    def test_parent_projection_bounds_status_path_and_diagnostic_fields(self):
        parent = SimulationGateway._parent_projection(
            fingerprint="parent-1",
            status="FAILED",
            progress_url="progress-1",
            child_count=2,
            error="x" * 1000,
            diagnostic={
                "message": "m" * 1000,
                "property": "regular",
                "private_expression": "secret",
            },
            status_path=[f"S{index}" for index in range(20)],
            guard_action="REMOVED_TERMINAL",
        )
        self.assertEqual(len(parent["status_path"]), 8)
        self.assertEqual(len(parent["error"]), 500)
        self.assertEqual(len(parent["diagnostic"]["message"]), 500)
        self.assertNotIn("private_expression", parent["diagnostic"])

    def test_known_multi_parent_read_failure_is_unknown_not_submit_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = KnownParentReadFailureClient()
            gateway = SimulationGateway(client, state_dir=tmp)
            results = gateway.simulate_multi_batch([
                SimulationSpec("rank(field_a)", {"delay": 1}),
                SimulationSpec("rank(field_b)", {"delay": 1}),
            ])
            self.assertEqual([item["status"] for item in results], ["UNKNOWN", "UNKNOWN"])
            self.assertEqual(gateway.guard.entries()[0]["status"], "RUNNING")

    def test_multi_unknown_keeps_one_parent_guard_and_never_reposts(self):
        with tempfile.TemporaryDirectory() as tmp:
            specs = [
                SimulationSpec(f"rank(field_{index})", {"delay": 1})
                for index in range(2)
            ]
            first_client = UnknownMultiGatewayClient()
            first = SimulationGateway(first_client, state_dir=tmp)
            first_results = first.simulate_multi_batch(specs)

            self.assertEqual(
                [item["status"] for item in first_results],
                ["SUBMIT_UNKNOWN", "SUBMIT_UNKNOWN"],
            )
            self.assertEqual(
                [row["kind"] for row in first.guard.entries()],
                ["MULTI_PARENT", "MULTI_CHILD", "MULTI_CHILD"],
            )

            second_client = UnknownMultiGatewayClient()
            second = SimulationGateway(second_client, state_dir=tmp)
            second_results = second.simulate_multi_batch(specs)

            self.assertEqual(
                [item["status"] for item in second_results],
                ["SUBMIT_UNKNOWN", "SUBMIT_UNKNOWN"],
            )
            self.assertEqual(second_results[0]["guard_action"], "EXISTING_GUARD")
            self.assertEqual(second_client.multi_submissions, [])

    def test_multi_parent_not_dispatched_result_explains_removed_guard(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = UnknownMultiGatewayClient()
            results = SimulationGateway(client, state_dir=tmp).simulate_multi_batch([
                SimulationSpec("rank(field_a)", {"delay": 1}),
                SimulationSpec("rank(field_b)", {"delay": 1}),
                SimulationSpec("rank(field_c)", {"delay": 1}),
                SimulationSpec("rank(field_d)", {"delay": 1}),
            ], child_batch_size=2, max_concurrent_multi=1)

            not_dispatched = results[2]
            self.assertEqual(not_dispatched["status"], "NOT_DISPATCHED")
            self.assertEqual(not_dispatched["reason_code"], "NOT_DISPATCHED")
            self.assertEqual(not_dispatched["parent"]["status"], "NOT_DISPATCHED")
            self.assertIsNone(not_dispatched["parent"]["progress_url"])
            self.assertEqual(
                not_dispatched["parent"]["guard_action"],
                "REMOVED_NOT_DISPATCHED",
            )
            self.assertEqual(
                not_dispatched["parent"]["error"],
                "dispatch paused before submission",
            )

    def test_multi_rate_limit_is_ambiguous_and_never_reposted(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = RateLimitedMultiGatewayClient()
            gateway = SimulationGateway(client, state_dir=tmp)
            specs = [
                SimulationSpec(f"rank(field_{index})", {"delay": 1})
                for index in range(2)
            ]

            results = gateway.simulate_multi_batch(specs)

            self.assertEqual(
                [item["status"] for item in results],
                ["SUBMIT_UNKNOWN", "SUBMIT_UNKNOWN"],
            )
            self.assertEqual(results[0]["parent"]["http_status"], 429)
            self.assertEqual(results[0]["parent"]["exception_class"], "WQBRateLimitError")
            self.assertEqual(
                [row["kind"] for row in gateway.guard.entries()],
                ["MULTI_PARENT", "MULTI_CHILD", "MULTI_CHILD"],
            )
            resumed = SimulationGateway(MultiGatewayClient(), state_dir=tmp)
            resumed_results = resumed.simulate_multi_batch(specs)
            self.assertEqual(
                [item["status"] for item in resumed_results],
                ["SUBMIT_UNKNOWN", "SUBMIT_UNKNOWN"],
            )
            self.assertEqual(resumed.client.multi_submissions, [])

    def test_batch_marks_unsubmitted_tail_without_creating_unknown_guard(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeGatewayClient(unknown=True)
            gateway = SimulationGateway(client, state_dir=tmp, max_concurrent=1)
            specs = [
                SimulationSpec("rank(close)", {"delay": 1}),
                SimulationSpec("rank(open)", {"delay": 1}),
            ]

            results = gateway.simulate_batch(specs)

            self.assertEqual(results[0]["status"], "SUBMIT_UNKNOWN")
            self.assertEqual(results[1]["status"], "NOT_DISPATCHED")
            self.assertEqual(
                [row["status"] for row in gateway.guard.entries()],
                ["SUBMIT_UNKNOWN"],
            )

    def test_batch_reuses_live_capability_and_history_reads(self):
        with tempfile.TemporaryDirectory() as tmp:
            capability_client = CapabilityGatewayClient()
            capability_gateway = SimulationGateway(
                capability_client, state_dir=tmp, max_concurrent=2
            )
            capability_gateway.simulate_batch([
                SimulationSpec("rank(close)", {"delay": 1}),
                SimulationSpec("rank(open)", {"delay": 1}),
            ])

            self.assertEqual(capability_client.capability_calls, 1)

        with tempfile.TemporaryDirectory() as tmp:
            history_client = RemoteHistoryGatewayClient()
            history_gateway = SimulationGateway(history_client, state_dir=tmp)
            history_gateway.simulate_batch([
                SimulationSpec("rank(close)", {"delay": 1}),
                SimulationSpec("rank(open)", {"delay": 1}),
            ])

            self.assertEqual(history_client.history_calls, 1)

    def test_batch_rejects_missing_capability_without_registering_prior_items(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = CapabilityGatewayClient()
            gateway = SimulationGateway(client, state_dir=tmp, max_concurrent=2)

            with self.assertRaisesRegex(ValueError, "OPERATOR_CAPABILITY_UNAVAILABLE"):
                gateway.simulate_batch([
                    SimulationSpec("rank(close)", {"delay": 1}),
                    SimulationSpec("ts_mean(close, 5)", {"delay": 1}),
                ])

            self.assertEqual(client.submissions, [])
            self.assertEqual(gateway.guard.entries(), [])

    def test_batch_treats_none_capability_as_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = UnavailableCapabilityGatewayClient()
            gateway = SimulationGateway(client, state_dir=tmp)

            result = gateway.simulate_batch([
                SimulationSpec("rank(close)", {"delay": 1}),
            ])[0]
            self.assertEqual(result["status"], "WAITING_FOR_CAPABILITY")
            self.assertEqual(result["reason_code"], "OPERATOR_CAPABILITY_UNKNOWN")
            self.assertEqual(client.submissions, [])

    def test_public_research_api_simulate_uses_gateway_without_agent_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeGatewayClient()
            result = research_api.simulate(
                {"expression": "rank(close)", "settings": {"delay": 1}},
                client=client, state_dir=tmp,
            )
            self.assertEqual(result["status"], "DONE")
            self.assertEqual(len(client.submissions), 1)
            self.assertEqual(research_api.get_pending_executions(state_dir=tmp), {"entries": []})

    def test_unverified_live_operator_is_rejected_before_post(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = CapabilityGatewayClient()
            with self.assertRaisesRegex(ValueError, "OPERATOR_CAPABILITY_UNAVAILABLE"):
                SimulationGateway(client, state_dir=tmp).simulate(
                    SimulationSpec("ts_mean(close, 5)", {"delay": 1})
                )
            self.assertEqual(client.submissions, [])

    def test_simulation_uses_spec_and_removes_guard_after_live_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeGatewayClient()
            gateway = SimulationGateway(client, state_dir=tmp, max_concurrent=1)

            result = gateway.simulate(SimulationSpec("rank(close)", {"delay": 1}))

            self.assertEqual(result["status"], "DONE")
            self.assertEqual(result["alpha_id"], "alpha-1")
            self.assertEqual(len(client.submissions), 1)
            self.assertFalse(os.path.exists(os.path.join(tmp, "trajectory.jsonl")))
            self.assertFalse(os.path.exists(os.path.join(tmp, "trial_ledger.jsonl")))
            self.assertEqual(ExecutionGuard(tmp).entries(), [])

    def test_active_exact_fingerprint_is_rejected_without_second_post(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeGatewayClient()
            gateway = SimulationGateway(client, state_dir=tmp, max_concurrent=1)
            spec = SimulationSpec("rank(close)", {"delay": 1})
            fingerprint = gateway.execution_fingerprint(spec)
            ExecutionGuard(tmp).register(fingerprint)

            result = gateway.simulate(spec)

            self.assertEqual(result["status"], "SUBMIT_UNKNOWN")
            self.assertEqual(result["fingerprint"], fingerprint)
            self.assertEqual(result["reason_code"], "RATE_LIMIT_OR_SUBMIT_UNKNOWN")
            self.assertEqual(client.submissions, [])

    def test_recent_remote_exact_fingerprint_is_rejected_without_post(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = RemoteHistoryGatewayClient()
            result = SimulationGateway(client, state_dir=tmp).simulate(
                SimulationSpec("rank(close)", {"delay": 1})
            )
            self.assertEqual(result["status"], "EXACT_DUPLICATE")
            self.assertEqual(result["alpha_id"], "alpha-existing")
            self.assertEqual(result["reason_code"], "EXACT_DUPLICATE")
            self.assertEqual(client.submissions, [])
            scan = result["remote_duplicate_scan"]
            self.assertEqual(scan["rows_scanned"], 1)
            self.assertEqual(scan["matched_count"], 1)
            self.assertEqual(scan["candidate_count"], 1)
            self.assertGreaterEqual(scan["elapsed_sec"], 0.0)

    def test_exact_duplicate_after_1000_rows_is_found_by_sharded_history_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = ShardedRemoteHistoryGatewayClient()
            result = SimulationGateway(client, state_dir=tmp).simulate(
                SimulationSpec("rank(close)", {"delay": 1})
            )
            self.assertEqual(result["status"], "EXACT_DUPLICATE")
            self.assertEqual(result["alpha_id"], "old-exact")
            self.assertEqual(client.submissions, [])

    def test_legacy_duplicate_scan_does_not_claim_a_recent_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = RemoteHistoryGatewayClient()
            result = SimulationGateway(client, state_dir=tmp).simulate(
                SimulationSpec("rank(open)", {"delay": 1})
            )
            scan = result["remote_duplicate_scan"]
            self.assertEqual(scan["status"], "LEGACY_SCOPE_UNKNOWN")
            self.assertIsNone(scan["lookback_days"])
            self.assertFalse(scan["complete"])
            self.assertEqual(scan["rows_scanned"], 1)
            self.assertEqual(scan["matched_count"], 0)
            self.assertEqual(scan["candidate_count"], 1)
            self.assertGreaterEqual(scan["elapsed_sec"], 0.0)

    def test_duplicate_scan_asks_the_shard_reader_for_a_bounded_window(self):
        class RecordingHistoryClient(ShardedRemoteHistoryGatewayClient):
            def __init__(self):
                super().__init__()
                self.scan_kwargs = None

            def iter_user_alpha_history_shards(self, **kwargs):
                self.scan_kwargs = dict(kwargs)
                yield from super().iter_user_alpha_history_shards(**kwargs)

        with tempfile.TemporaryDirectory() as tmp:
            client = RecordingHistoryClient()
            result = SimulationGateway(client, state_dir=tmp).simulate(
                SimulationSpec("rank(close)", {"delay": 1})
            )
            self.assertEqual(
                client.scan_kwargs["lookback_days"], 1
            )
            self.assertEqual(
                client.scan_kwargs["time_budget_sec"], 900
            )
            self.assertEqual(
                result["remote_duplicate_scan"]["status"], "BOUNDED_RECENT_WINDOW"
            )
            self.assertEqual(result["remote_duplicate_scan"]["lookback_days"], 1)

    def test_legacy_shard_reader_without_window_argument_still_scans(self):
        class LegacyHistoryClient(ShardedRemoteHistoryGatewayClient):
            def __init__(self):
                super().__init__()
                self.called = False

            def iter_user_alpha_history_shards(self):
                self.called = True
                yield {"id": "old-exact", "regular": "rank(close)",
                       "settings": {"delay": 1}, "status": "UNSUBMITTED"}

        with tempfile.TemporaryDirectory() as tmp:
            client = LegacyHistoryClient()
            result = SimulationGateway(client, state_dir=tmp).simulate(
                SimulationSpec("rank(close)", {"delay": 1})
            )
            self.assertTrue(client.called)
            self.assertEqual(result["status"], "EXACT_DUPLICATE")
            self.assertEqual(client.submissions, [])

    def test_incomplete_remote_history_scan_fails_closed_before_post(self):
        from wqb_agent.client import WQBQueryTooBroadError

        class IncompleteHistoryClient(ShardedRemoteHistoryGatewayClient):
            def iter_user_alpha_history_shards(self, **_kwargs):
                raise WQBQueryTooBroadError("bounded history scan incomplete")

        with tempfile.TemporaryDirectory() as tmp:
            client = IncompleteHistoryClient()
            with self.assertRaisesRegex(WQBQueryTooBroadError, "incomplete"):
                SimulationGateway(client, state_dir=tmp).simulate(
                    SimulationSpec("rank(close)", {"delay": 1})
                )
            self.assertEqual(client.submissions, [])

    def test_field_validation_fails_closed_when_reader_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = MissingFieldCapabilityGatewayClient()
            spec = SimulationSpec(
                "rank(close)", {"delay": 1}, fields=("close",),
                field_datasets={"close": "synthetic-dataset"},
            )
            with self.assertRaisesRegex(ValueError, "FIELD_CAPABILITY_UNAVAILABLE"):
                SimulationGateway(client, state_dir=tmp).simulate(spec)
            self.assertEqual(client.submissions, [])

    def test_unknown_field_from_live_dataset_is_rejected_before_post(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = UnknownFieldCapabilityGatewayClient()
            spec = SimulationSpec(
                "rank(close)", {"delay": 1}, fields=("close",),
                field_datasets={"close": "synthetic-dataset"},
            )
            with self.assertRaisesRegex(ValueError, "FIELD_CAPABILITY_UNAVAILABLE"):
                SimulationGateway(client, state_dir=tmp).simulate(spec)
            self.assertEqual(client.submissions, [])

    def test_expression_field_without_declared_fields_is_not_claimed_verified(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = VerifiedFieldCapabilityGatewayClient()
            requests = []

            def record_fields(fields, **kwargs):
                requests.append(fields)
                return {"valid": True, "source": "BRAIN_LIVE_ONLY", "fields": ["close"]}

            result = SimulationGateway(client, state_dir=tmp).simulate(
                SimulationSpec("rank(close)", {"delay": 1})
            )
            self.assertEqual(result["status"], "DONE")
            self.assertEqual(result["field_validation"], "UNVERIFIED")
            self.assertEqual(requests, [])

    def test_declared_fields_are_reported_live_verified(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = VerifiedFieldCapabilityGatewayClient()
            result = SimulationGateway(client, state_dir=tmp).simulate(
                SimulationSpec(
                    "rank(close)", {"delay": 1}, fields=("close",),
                    field_datasets={"close": "synthetic-dataset"},
                )
            )
            self.assertEqual(result["status"], "DONE")
            self.assertEqual(result["field_validation"], "LIVE_VERIFIED")

    def test_field_free_expression_is_rejected_before_any_post(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeGatewayClient()
            with self.assertRaisesRegex(ValueError, "identifier"):
                SimulationGateway(client, state_dir=tmp).simulate(
                    SimulationSpec("1", {"delay": 1})
                )
            self.assertEqual(client.submissions, [])

    def test_batch_and_multi_results_echo_transient_proposal_labels(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeGatewayClient()
            specs = [
                SimulationSpec(
                    "rank(close)", {"delay": 1}, note="hypothesis A control",
                    template_id="control-template", proposal_id="proposal-a",
                ),
                SimulationSpec(
                    "rank(open)", {"delay": 1}, note="hypothesis B probe",
                    template_id="probe-template", proposal_id="proposal-b",
                ),
            ]
            single_results = SimulationGateway(client, state_dir=tmp).simulate_batch(specs)
            self.assertEqual([row["proposal_id"] for row in single_results],
                             ["proposal-a", "proposal-b"])
            self.assertEqual([row["note"] for row in single_results],
                             ["hypothesis A control", "hypothesis B probe"])

        with tempfile.TemporaryDirectory() as tmp:
            client = MultiGatewayClient()
            specs = [
                SimulationSpec(
                    f"rank(field_{index})", {"delay": 1},
                    note="same hypothesis family", template_id="synthetic-template",
                    proposal_id=f"multi-{index}",
                )
                for index in range(2)
            ]
            multi_results = SimulationGateway(client, state_dir=tmp).simulate_multi_batch(specs)
            self.assertEqual([row["proposal_id"] for row in multi_results],
                             ["multi-0", "multi-1"])
            self.assertEqual([row["template_id"] for row in multi_results],
                             ["synthetic-template", "synthetic-template"])

    def test_ambiguous_submit_is_persisted_and_restart_cannot_post_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = SimulationGateway(FakeGatewayClient(unknown=True), state_dir=tmp)
            spec = SimulationSpec("rank(close)", {"delay": 1})
            result = first.simulate(spec)
            self.assertEqual(result["status"], "SUBMIT_UNKNOWN")
            with open(os.path.join(tmp, "execution_guard.json"), encoding="utf-8") as handle:
                stored = json.load(handle)
            self.assertEqual(stored["entries"][0]["status"], "SUBMIT_UNKNOWN")

            second_client = FakeGatewayClient()
            second = SimulationGateway(second_client, state_dir=tmp)
            resumed = second.simulate(spec)

            self.assertEqual(resumed["status"], "SUBMIT_UNKNOWN")
            self.assertEqual(second_client.submissions, [])

    def test_restart_promotes_submitting_to_submit_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            spec = SimulationSpec("rank(close)", {"delay": 1})
            fingerprint = ExecutionGuard.fingerprint(spec.expression, spec.settings)
            ExecutionGuard(tmp).register(fingerprint, status="SUBMITTING")

            gateway = SimulationGateway(FakeGatewayClient(), state_dir=tmp)
            result = gateway.simulate(spec)

            self.assertEqual(result["status"], "SUBMIT_UNKNOWN")
            self.assertEqual(ExecutionGuard(tmp).find(fingerprint)["status"], "SUBMIT_UNKNOWN")

    def test_execution_guard_strips_research_result_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "execution_guard.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"entries": [{
                    "execution_fingerprint": "fp",
                    "status": "RUNNING",
                    "progress_url": "progress",
                    "created_at": 1,
                    "updated_at": 2,
                    "metrics": {"sharpe": 9},
                    "checks": ["PASS"],
                    "expression": "rank(close)",
                    "lineage_id": "private-lineage",
                }]}, handle)

            entry = ExecutionGuard(tmp).entries()[0]

            self.assertEqual(
                set(entry),
                {"execution_fingerprint", "status", "progress_url",
                 "created_at", "updated_at", "simulation_count", "kind"},
            )
            self.assertNotIn("metrics", entry)

    def test_execution_guard_persists_bounded_simulation_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            guard = ExecutionGuard(tmp)
            self.assertTrue(guard.register("single"))
            self.assertTrue(guard.register("multi", simulation_count=7))
            counts = {
                row["execution_fingerprint"]: row["simulation_count"]
                for row in guard.entries()
            }
            self.assertEqual(counts["single"], 1)
            self.assertEqual(counts["multi"], 7)
            for value in (True, False, 0, -1, 11, "10"):
                with self.subTest(value=value):
                    with self.assertRaises((TypeError, ValueError)):
                        guard.register(f"invalid-{value!r}", simulation_count=value)

    def test_execution_guard_legacy_and_malformed_counts_default_to_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "execution_guard.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"entries": [
                    {"execution_fingerprint": "legacy", "status": "SUBMIT_UNKNOWN"},
                    {"execution_fingerprint": "bad-int", "status": "RUNNING", "simulation_count": "10"},
                    {"execution_fingerprint": "bad-range", "status": "RUNNING", "simulation_count": 999},
                ]}, handle)

            entries = ExecutionGuard(tmp).entries()
            snapshot = SimulationQuota(
                _EmptyQuotaRepository(), ExecutionGuard(tmp),
                local_date=lambda: "2026-09-23",
            ).snapshot()

        self.assertEqual(
            {row["execution_fingerprint"]: row["simulation_count"] for row in entries},
            {"legacy": 1, "bad-int": 1, "bad-range": 1},
        )
        self.assertEqual(snapshot["active_guard_count"], 3)
        self.assertEqual(snapshot["active_guard_simulation_count"], 3)

    def test_execution_guard_update_and_reconcile_preserve_simulation_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            guard = ExecutionGuard(tmp)
            guard.register("multi", status="SUBMITTING", simulation_count=7)
            self.assertTrue(guard.update("multi", status="RUNNING", progress_url="progress"))
            self.assertEqual(guard.find("multi")["simulation_count"], 7)
            guard.register("interrupted", status="SUBMITTING", simulation_count=6)
            reconciled = ExecutionGuard(tmp)

            entry = reconciled.find("multi")
            interrupted = reconciled.find("interrupted")

        self.assertEqual(entry["status"], "RUNNING")
        self.assertEqual(entry["simulation_count"], 7)
        self.assertEqual(interrupted["status"], "SUBMIT_UNKNOWN")
        self.assertEqual(interrupted["simulation_count"], 6)

    def test_terminal_guard_removal_removes_weighted_quota_contribution(self):
        with tempfile.TemporaryDirectory() as tmp:
            guard = ExecutionGuard(tmp)
            guard.register("multi", simulation_count=4)
            self.assertTrue(guard.remove("multi"))
            snapshot = SimulationQuota(
                _EmptyQuotaRepository(), guard,
                local_date=lambda: "2026-09-23",
            ).snapshot()

        self.assertEqual(snapshot["active_guard_count"], 0)
        self.assertEqual(snapshot["active_guard_simulation_count"], 0)
        self.assertEqual(snapshot["today_used"], 0)
        self.assertEqual(snapshot["window_used"], 0)

    def test_known_progress_url_recovery_only_polls_same_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            guard = ExecutionGuard(tmp)
            spec = SimulationSpec("rank(close)", {"delay": 1})
            fingerprint = guard.fingerprint(spec.expression, spec.settings)
            guard.register(fingerprint, progress_url="progress-known", status="RUNNING")
            client = FakeGatewayClient()
            gateway = SimulationGateway(client, state_dir=tmp)

            result = gateway.resume_execution(fingerprint)

            self.assertEqual(result["status"], "DONE")
            self.assertEqual(client.submissions, [])
            self.assertEqual(client.assert_progress_url, "progress-known")

    def test_known_multi_progress_url_recovers_children_without_repost(self):
        with tempfile.TemporaryDirectory() as tmp:
            guard = ExecutionGuard(tmp)
            fingerprint = "multi-fingerprint"
            guard.register(fingerprint, progress_url="multi-progress-known", status="SUBMIT_UNKNOWN")
            client = MultiGatewayClient()
            gateway = SimulationGateway(client, state_dir=tmp)

            result = gateway.resume_execution(fingerprint)

            self.assertEqual(result["status"], "DONE")
            self.assertEqual(result["alpha_ids"], [
                "multi-alpha-multi-progress-known-0",
                "multi-alpha-multi-progress-known-1",
            ])
            self.assertEqual(client.multi_submissions, [])
            self.assertEqual(gateway.guard.entries(), [])

    def test_known_multi_error_url_is_terminal_and_clears_guard(self):
        with tempfile.TemporaryDirectory() as tmp:
            guard = ExecutionGuard(tmp)
            fingerprint = "multi-error-fingerprint"
            guard.register(fingerprint, progress_url="multi-progress-error", status="RUNNING")
            gateway = SimulationGateway(RejectedMultiGatewayClient(), state_dir=tmp)

            result = gateway.resume_execution(fingerprint)

            self.assertEqual(result["status"], "FAILED")
            self.assertIn("status=ERROR", result["error"])
            self.assertEqual(gateway.guard.entries(), [])

    def test_multi_reordered_children_never_repost(self):
        with tempfile.TemporaryDirectory() as tmp:
            specs = [
                SimulationSpec(f"rank(field_{index})", {"delay": 1})
                for index in range(4)
            ]
            first_client = UnknownMultiGatewayClient()
            SimulationGateway(first_client, state_dir=tmp).simulate_multi_batch(
                specs, child_batch_size=4
            )

            reordered = [specs[3], specs[1], specs[0], specs[2]]
            second_client = UnknownMultiGatewayClient()
            results = SimulationGateway(second_client, state_dir=tmp).simulate_multi_batch(
                reordered, child_batch_size=4
            )

            self.assertEqual(
                [item["status"] for item in results],
                ["SUBMIT_UNKNOWN"] * 4,
            )
            self.assertTrue(all(
                item["guard_action"] == "EXISTING_GUARD" for item in results
            ))
            self.assertEqual(second_client.multi_submissions, [])

    def test_multi_split_batch_never_reposts_shared_children(self):
        with tempfile.TemporaryDirectory() as tmp:
            specs = [
                SimulationSpec(f"rank(field_{index})", {"delay": 1})
                for index in range(4)
            ]
            SimulationGateway(
                UnknownMultiGatewayClient(), state_dir=tmp
            ).simulate_multi_batch(specs, child_batch_size=4)

            split_client = UnknownMultiGatewayClient()
            results = SimulationGateway(split_client, state_dir=tmp).simulate_multi_batch(
                specs, child_batch_size=2, max_concurrent_multi=1
            )

            self.assertEqual(
                [item["status"] for item in results],
                ["SUBMIT_UNKNOWN"] * 4,
            )
            self.assertEqual(split_client.multi_submissions, [])

    def test_multi_subset_retry_never_reposts_child(self):
        with tempfile.TemporaryDirectory() as tmp:
            specs = [
                SimulationSpec(f"rank(field_{index})", {"delay": 1})
                for index in range(4)
            ]
            SimulationGateway(
                UnknownMultiGatewayClient(), state_dir=tmp
            ).simulate_multi_batch(specs, child_batch_size=4)

            subset_client = UnknownMultiGatewayClient()
            results = SimulationGateway(subset_client, state_dir=tmp).simulate_multi_batch(
                specs[:2], child_batch_size=2
            )

            self.assertEqual(
                [item["status"] for item in results],
                ["SUBMIT_UNKNOWN", "SUBMIT_UNKNOWN"],
            )
            self.assertEqual(subset_client.multi_submissions, [])

    def test_multi_child_never_reposted_as_single(self):
        with tempfile.TemporaryDirectory() as tmp:
            specs = [
                SimulationSpec("rank(field_a)", {"delay": 1}),
                SimulationSpec("rank(field_b)", {"delay": 1}),
            ]
            SimulationGateway(
                UnknownMultiGatewayClient(), state_dir=tmp
            ).simulate_multi_batch(specs)

            single_client = FakeGatewayClient()
            result = SimulationGateway(single_client, state_dir=tmp).simulate(specs[0])

            self.assertEqual(result["status"], "SUBMIT_UNKNOWN")
            self.assertEqual(single_client.submissions, [])

    def test_single_unknown_child_is_not_retried_inside_multi(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = SimulationSpec("rank(field_a)", {"delay": 1})
            second = SimulationSpec("rank(field_b)", {"delay": 1})
            third = SimulationSpec("rank(field_c)", {"delay": 1})
            SimulationGateway(
                FakeGatewayClient(unknown=True), state_dir=tmp
            ).simulate(first)

            multi_client = MultiGatewayClient()
            results = SimulationGateway(multi_client, state_dir=tmp).simulate_multi_batch(
                [first, second, third]
            )

            self.assertEqual(results[0]["status"], "SUBMIT_UNKNOWN")
            self.assertEqual(results[0]["guard_action"], "EXISTING_GUARD")
            self.assertEqual(
                [item["status"] for item in results[1:]],
                ["DONE", "DONE"],
            )
            self.assertEqual(len(multi_client.multi_submissions), 1)
            self.assertEqual(len(multi_client.multi_submissions[0][0]), 2)

    def test_multi_parent_recovers_with_multi_polling_from_persisted_kind(self):
        class ProgressOnlyMultiClient(MultiGatewayClient):
            def get_progress_snapshot(self, progress_url, **kwargs):
                return {"status_code": 200, "payload": {"progress": 0.3}}

            def poll_progress(self, progress_url, **kwargs):
                raise AssertionError("a Multi parent must not use Single polling")

        with tempfile.TemporaryDirectory() as tmp:
            client = ProgressOnlyMultiClient()
            gateway = SimulationGateway(client, state_dir=tmp)
            batch_fingerprint = ExecutionGuard.fingerprint(
                "MULTI[child-a,child-b]", {"mode": "MULTI", "children": 2}
            )
            gateway.guard.register(
                batch_fingerprint, progress_url="multi-progress-1",
                status="RUNNING", simulation_count=2,
                kind=ExecutionGuard.MULTI_PARENT,
            )

            result = gateway.resume_execution(batch_fingerprint)

            self.assertEqual(result["status"], "DONE")
            self.assertEqual(len(result["alpha_ids"]), 2)
            self.assertEqual(gateway.guard.entries(), [])

    def test_multi_parent_recovery_accepts_client_child_result_mapping(self):
        class MappingResultMultiClient(MultiGatewayClient):
            def poll_multi_progress(self, progress_url, **kwargs):
                return {
                    "status": "SUCCESS",
                    "remote_status": "COMPLETE",
                    "children": [
                        {"status": "DONE", "alpha_id": "multi-alpha-1"},
                        {"status": "DONE", "alpha_id": "multi-alpha-2"},
                    ],
                }

        with tempfile.TemporaryDirectory() as tmp:
            client = MappingResultMultiClient()
            gateway = SimulationGateway(client, state_dir=tmp)
            parent = "multi-parent-mapping"
            gateway.guard.register(
                parent, progress_url="multi-progress-mapping", status="RUNNING",
                simulation_count=2, kind=ExecutionGuard.MULTI_PARENT,
            )
            for index in range(2):
                gateway.guard.register(
                    f"child-{index}", kind=ExecutionGuard.MULTI_CHILD,
                    parent_fingerprint=parent,
                )

            result = gateway.resume_execution(parent)

            self.assertEqual(result["status"], "DONE")
            self.assertEqual(result["alpha_ids"], ["multi-alpha-1", "multi-alpha-2"])
            self.assertEqual(
                [item["id"] for item in result["evidence"]],
                ["multi-alpha-1", "multi-alpha-2"],
            )
            self.assertEqual(gateway.guard.entries(), [])
            self.assertEqual(client.multi_submissions, [])

    def test_multi_parent_terminal_failure_clears_its_unresolved_child_guards(self):
        class TerminalErrorMultiClient(MultiGatewayClient):
            def poll_multi_progress(self, progress_url, **kwargs):
                raise WQBRemoteSimulationError({
                    "remote_status": "ERROR",
                    "simulation_id": "synthetic-parent",
                    "message": "synthetic terminal parent failure",
                })

        with tempfile.TemporaryDirectory() as tmp:
            client = TerminalErrorMultiClient()
            gateway = SimulationGateway(client, state_dir=tmp)
            parent = "multi-parent-terminal-error"
            gateway.guard.register(
                parent, progress_url="multi-progress-terminal-error", status="RUNNING",
                simulation_count=2, kind=ExecutionGuard.MULTI_PARENT,
            )
            gateway.guard.register(
                "child-terminal-1", kind=ExecutionGuard.MULTI_CHILD,
                parent_fingerprint=parent,
            )
            gateway.guard.register(
                "child-terminal-2", kind=ExecutionGuard.MULTI_CHILD,
                parent_fingerprint=parent,
            )

            result = gateway.resume_execution(parent)

            self.assertEqual(result["status"], "FAILED")
            self.assertIn("synthetic terminal parent failure", result["error"])
            self.assertEqual(gateway.guard.entries(), [])
            self.assertEqual(client.multi_submissions, [])

    def test_multi_child_recovery_follows_its_parent(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = MultiGatewayClient()
            gateway = SimulationGateway(client, state_dir=tmp)
            gateway.guard.register(
                "parent-fingerprint", progress_url="multi-progress-1",
                status="RUNNING", simulation_count=2,
                kind=ExecutionGuard.MULTI_PARENT,
            )
            gateway.guard.register(
                "child-fingerprint", kind=ExecutionGuard.MULTI_CHILD,
                parent_fingerprint="parent-fingerprint",
            )

            result = gateway.resume_execution("child-fingerprint")

            self.assertEqual(result["status"], "DONE")
            self.assertEqual(result["fingerprint"], "parent-fingerprint")
            self.assertEqual(gateway.guard.entries(), [])

    def test_multi_child_guard_requires_a_parent_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            guard = ExecutionGuard(tmp)
            with self.assertRaisesRegex(ValueError, "parent fingerprint"):
                guard.register("child", kind=ExecutionGuard.MULTI_CHILD)

    def test_child_moved_into_another_multi_parent_is_not_reposted(self):
        with tempfile.TemporaryDirectory() as tmp:
            first_spec = SimulationSpec("rank(field_a)", {"delay": 1})
            second_spec = SimulationSpec("rank(field_b)", {"delay": 1})
            third_spec = SimulationSpec("rank(field_c)", {"delay": 1})
            SimulationGateway(
                UnknownMultiGatewayClient(), state_dir=tmp
            ).simulate_multi_batch([first_spec, second_spec])

            client = MultiGatewayClient()
            results = SimulationGateway(client, state_dir=tmp).simulate_multi_batch(
                [first_spec, third_spec, SimulationSpec("rank(field_d)", {"delay": 1})]
            )

            self.assertEqual(results[0]["status"], "SUBMIT_UNKNOWN")
            self.assertEqual(results[0]["guard_action"], "EXISTING_GUARD")
            self.assertEqual(
                [item["status"] for item in results[1:]],
                ["DONE", "DONE"],
            )
            self.assertEqual(len(client.multi_submissions), 1)
            self.assertEqual(len(client.multi_submissions[0][0]), 2)

    def test_timed_out_multi_parent_recovers_through_persisted_kind(self):
        with tempfile.TemporaryDirectory() as tmp:
            specs = [
                SimulationSpec("rank(field_a)", {"delay": 1}),
                SimulationSpec("rank(field_b)", {"delay": 1}),
            ]
            first = SimulationGateway(KnownParentTimeoutClient(), state_dir=tmp)
            first_results = first.simulate_multi_batch(specs)

            self.assertEqual(
                [item["status"] for item in first_results], ["UNKNOWN", "UNKNOWN"]
            )
            entries = first.guard.entries()
            parent = next(row for row in entries if row["kind"] == "MULTI_PARENT")
            self.assertTrue(parent["progress_url"])
            self.assertEqual(
                sorted(row["kind"] for row in entries),
                ["MULTI_CHILD", "MULTI_CHILD", "MULTI_PARENT"],
            )

            # A restart keeps only the durable guard state. Recovery must still
            # poll the known parent as a Multi parent and then release the
            # parent together with every child row.
            restarted = SimulationGateway(MultiGatewayClient(), state_dir=tmp)
            result = restarted.resume_execution(parent["execution_fingerprint"])

            self.assertEqual(result["status"], "DONE")
            self.assertEqual(len(result["alpha_ids"]), 2)
            self.assertEqual(restarted.guard.entries(), [])

    def test_timed_out_multi_parent_recovery_follows_a_child_fingerprint(self):
        with tempfile.TemporaryDirectory() as tmp:
            specs = [
                SimulationSpec("rank(field_a)", {"delay": 1}),
                SimulationSpec("rank(field_b)", {"delay": 1}),
            ]
            first = SimulationGateway(KnownParentTimeoutClient(), state_dir=tmp)
            first.simulate_multi_batch(specs)
            child = next(
                row for row in first.guard.entries() if row["kind"] == "MULTI_CHILD"
            )

            restarted = SimulationGateway(MultiGatewayClient(), state_dir=tmp)
            result = restarted.resume_execution(child["execution_fingerprint"])

            self.assertEqual(result["status"], "DONE")
            self.assertEqual(result["fingerprint"], child["parent_fingerprint"])
            self.assertEqual(restarted.guard.entries(), [])

    def test_public_remote_evidence_is_live_and_explicitly_sourced(self):
        evidence = research_api.get_alpha_evidence("alpha-1", client=FakeGatewayClient())
        self.assertEqual(evidence["source"], "LIVE")
        self.assertEqual(evidence["alpha_id"], "alpha-1")
        self.assertEqual(evidence["alpha"]["id"], "alpha-1")

    def test_remote_evidence_provider_exposes_named_read_operations(self):
        provider = RemoteAlphaEvidenceProvider(FakeGatewayClient())
        evidence = provider.get_alpha_evidence("alpha-1")
        self.assertEqual(evidence["source"], "LIVE")
        self.assertEqual(provider.get_alpha("alpha-1")["id"], "alpha-1")


class SimulationWriteContractTests(unittest.TestCase):
    """The production writer only admits the independently verified REGULAR schema."""

    def test_simulation_type_defaults_to_regular(self):
        spec = SimulationSpec("rank(close)", {"delay": 1})
        self.assertEqual(spec.simulation_type, "REGULAR")

    def test_canonical_validation_and_fingerprint_reject_non_regular_types(self):
        client = FakeGatewayClient()
        with tempfile.TemporaryDirectory() as state:
            gateway = SimulationGateway(client, state_dir=state)
            for simulation_type in ("SUPER", "BOGUS"):
                with self.subTest(simulation_type=simulation_type):
                    spec = SimulationSpec(
                        "rank(close)", {"delay": 1},
                        simulation_type=simulation_type,
                    )
                    with self.assertRaisesRegex(
                        ValueError, "UNSUPPORTED_SIMULATION_TYPE"
                    ):
                        gateway.validate_simulation_spec(spec)
                    with self.assertRaisesRegex(
                        ValueError, "UNSUPPORTED_SIMULATION_TYPE"
                    ):
                        gateway.execution_fingerprint(spec)

    def test_regular_fingerprint_is_unchanged_by_the_type_field(self):
        # Guards persisted before this change must still match.
        guard_material = SimulationSpec("rank(close)", {"delay": 1})
        legacy_fingerprint = "819d6287c44ea8a479470662d06428a7e4d47c74a7f3dff0875563556187be96"
        self.assertEqual(
            ExecutionGuard.fingerprint("rank(close)", {"delay": 1}),
            legacy_fingerprint,
        )
        self.assertEqual(
            ExecutionGuard.fingerprint("rank(close)", {"delay": 1}),
            ExecutionGuard.fingerprint(
                guard_material.expression, dict(guard_material.settings)
            ),
        )
        with tempfile.TemporaryDirectory() as state:
            gateway = SimulationGateway(FakeGatewayClient(), state_dir=state)
            self.assertEqual(
                gateway.execution_fingerprint(guard_material),
                ExecutionGuard.fingerprint("rank(close)", {"delay": 1}),
            )

    def test_region_agnostic_fingerprint_has_stable_type_namespace(self):
        client = FakeGatewayClient()
        with tempfile.TemporaryDirectory() as state:
            gateway = SimulationGateway(client, state_dir=state)
            regular = SimulationSpec("rank(close)", {"delay": 1})
            region_agnostic = SimulationSpec(
                "rank(close)", {"delay": 1}, simulation_type="REGION_AGNOSTIC"
            )

            regular_fingerprint = gateway.execution_fingerprint(regular)
            ra_fingerprint = gateway.execution_fingerprint(region_agnostic)

            self.assertEqual(
                regular_fingerprint,
                ExecutionGuard.fingerprint("rank(close)", {"delay": 1}),
            )
            self.assertNotEqual(regular_fingerprint, ra_fingerprint)
            self.assertEqual(
                ra_fingerprint,
                gateway.execution_fingerprint(SimulationSpec(
                    "rank(close)", {"delay": 1},
                    simulation_type="REGION_AGNOSTIC",
                )),
            )

    def test_region_agnostic_single_allows_non_client_region_scope(self):
        client = FakeGatewayClient()
        client.region = "USA"
        client.universe = "TOP3000"
        client.instrument_type = "EQUITY"
        client.get_simulation_capability = lambda: {
            "status": "AVAILABLE",
            "simulation_type_choices": ["REGULAR", "REGION_AGNOSTIC"],
            "settings": {},
            "required_settings": [],
        }
        settings = {
            "region": "GLB", "universe": "MINVOL1M",
            "instrumentType": "EQUITY", "delay": 1,
        }

        with tempfile.TemporaryDirectory() as state:
            spec = research_api.build_simulation_spec(
                "rank(close)", settings=settings, client=client,
                simulation_type="REGION_AGNOSTIC",
            )
            result = SimulationGateway(client, state_dir=state).simulate(spec)

        self.assertEqual(result["status"], "DONE")
        self.assertEqual(client.submissions[0][2]["alpha_type"], "REGION_AGNOSTIC")
        with self.assertRaisesRegex(ValueError, "does not match client scope"):
            research_api.build_simulation_spec(
                "rank(close)", settings=settings, client=client,
            )

    def test_unresolved_legacy_regular_guard_blocks_replacement_post(self):
        with tempfile.TemporaryDirectory() as state:
            client = FakeGatewayClient()
            fingerprint = ExecutionGuard.fingerprint("rank(close)", {"delay": 1})
            ExecutionGuard(state).register(fingerprint)
            gateway = SimulationGateway(client, state_dir=state)

            result = gateway.simulate(SimulationSpec("rank(close)", {"delay": 1}))

            self.assertEqual(result["status"], "SUBMIT_UNKNOWN")
            self.assertEqual(result["fingerprint"], fingerprint)
            self.assertEqual(client.submissions, [])

    def test_public_regular_build_validate_fingerprint_and_simulate_chain(self):
        spec = research_api.build_simulation_spec(
            "rank(close)", settings={"delay": 1}
        )
        client = FakeGatewayClient()
        with tempfile.TemporaryDirectory() as state:
            validated = research_api.validate_simulation_spec(
                spec, client=client, state_dir=state
            )
            fingerprint = research_api.execution_fingerprint(
                spec, client=client, state_dir=state
            )
            result = research_api.simulate(
                spec, client=client, state_dir=state
            )

        self.assertEqual(validated["status"], "VALID")
        self.assertEqual(
            fingerprint,
            ExecutionGuard.fingerprint(spec.expression, spec.settings),
        )
        self.assertEqual(result["status"], "DONE")
        self.assertEqual(client.submissions[0][2]["alpha_type"], "REGULAR")

    def test_settings_variant_preserves_fields_for_field_capability_preflight(self):
        anchor = SimulationSpec(
            "rank(field_a)", settings={"delay": 1}, fields=("field_a",),
            field_datasets={"field_a": "synthetic-dataset"},
        )
        variant = research_api.build_simulation_spec(
            "rank(field_a)", settings={"delay": 2}, anchor_spec=anchor,
        )
        client = FakeGatewayClient()
        field_requests = []

        def unavailable_fields(fields, **_kwargs):
            field_requests.append(fields)
            return {"valid": False, "fields": []}

        client.get_field_capability = unavailable_fields
        with tempfile.TemporaryDirectory() as state:
            gateway = SimulationGateway(client, state_dir=state)
            with self.assertRaisesRegex(ValueError, "FIELD_CAPABILITY_UNAVAILABLE"):
                gateway.simulate(variant)
            self.assertEqual(client.submissions, [])
            self.assertEqual(gateway.guard.entries(), [])

        self.assertEqual(field_requests, [{"synthetic-dataset": ["field_a"]}])

    def test_non_regular_types_are_rejected_before_guard_or_post(self):
        for simulation_type in ("SUPER", "BOGUS"):
            with self.subTest(simulation_type=simulation_type):
                client = FakeGatewayClient()
                with tempfile.TemporaryDirectory() as state:
                    gateway = SimulationGateway(client, state_dir=state)
                    with self.assertRaisesRegex(
                        ValueError, "UNSUPPORTED_SIMULATION_TYPE"
                    ):
                        gateway.simulate(SimulationSpec(
                            "rank(close)", {"delay": 1},
                            simulation_type=simulation_type,
                        ))
                    self.assertEqual(client.submissions, [])
                    self.assertEqual(gateway.guard.entries(), [])

    def test_multi_rejects_non_regular_type_before_parent_guard_or_post(self):
        client = MultiGatewayClient()
        with tempfile.TemporaryDirectory() as state:
            gateway = SimulationGateway(client, state_dir=state)
            with self.assertRaisesRegex(
                ValueError, "UNSUPPORTED_SIMULATION_TYPE"
            ):
                gateway.simulate_multi_batch([
                    SimulationSpec(
                        "rank(field_a)", {"delay": 1},
                        simulation_type="SUPER",
                    ),
                    SimulationSpec("rank(field_b)", {"delay": 1}),
                ])
            self.assertEqual(client.multi_submissions, [])
            self.assertEqual(client.submissions, [])
            self.assertEqual(gateway.guard.entries(), [])

    def test_multi_region_agnostic_child_is_invalid_before_any_guard_or_post(self):
        from wqb_agent.failures import ResearchReasonError

        client = MultiGatewayClient()
        with tempfile.TemporaryDirectory() as state:
            gateway = SimulationGateway(client, state_dir=state)

            with self.assertRaises(ResearchReasonError) as raised:
                gateway.simulate_multi_batch([
                    SimulationSpec(
                        "rank(field_a)", {"delay": 1},
                        simulation_type="REGION_AGNOSTIC",
                    ),
                    SimulationSpec("rank(field_b)", {"delay": 1}),
                ])

            self.assertEqual(raised.exception.reason_code, "INVALID_SPEC")
            self.assertEqual(client.multi_submissions, [])
            self.assertEqual(client.submissions, [])
            self.assertEqual(gateway.guard.entries(), [])

    def test_regular_simulation_still_posts_regular(self):
        client = FakeGatewayClient()
        with tempfile.TemporaryDirectory() as state:
            gateway = SimulationGateway(client, state_dir=state)
            gateway.simulate(SimulationSpec("rank(close)", {"delay": 1}))
        _expression, _settings, kwargs = client.submissions[0]
        self.assertEqual(kwargs.get("alpha_type"), "REGULAR")


if __name__ == "__main__":
    unittest.main()
