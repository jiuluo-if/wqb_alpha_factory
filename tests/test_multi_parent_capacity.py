"""Multi parent capacity is reserved until the guarded remote write is resolved."""

import time
import unittest

from wqb_agent.simulation_gateway import (
    MULTI_MAX_CONCURRENCY,
    ExecutionGuard,
    classify_batch_write_admission,
    multi_parent_holds_slot,
)


def _facts(**overrides):
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


def _parent(index, updated_at=None, status="SUBMIT_UNKNOWN"):
    row = {
        "execution_fingerprint": f"parent-{index}",
        "kind": ExecutionGuard.MULTI_PARENT,
        "status": status,
        "simulation_count": 10,
    }
    if updated_at is not None:
        row["updated_at"] = updated_at
    return row


class TestMultiParentCapacity(unittest.TestCase):
    def test_stale_unknown_parents_keep_capacity_and_exact_once(self):
        now = time.time()
        pending = [
            _parent(index, updated_at=now - 86400)
            for index in range(MULTI_MAX_CONCURRENCY)
        ]
        result = classify_batch_write_admission(
            **_facts(pending_entries=pending),
            candidate_fingerprints=["parent-0", "new-child"],
            requested_max_concurrent_multi=MULTI_MAX_CONCURRENCY,
        )

        self.assertEqual(result["active_multi_parent_count"], MULTI_MAX_CONCURRENCY)
        self.assertEqual(result["available_multi_parent_slots"], 0)
        self.assertEqual(result["effective_max_concurrent_multi"], 0)
        self.assertEqual(result["write_readiness"], "BLOCKED_BY_REMOTE_STATE")
        self.assertIn("MULTI_PARENT_CAPACITY_RESERVED", result["write_blockers"])
        self.assertEqual(result["eligible_count"], 1)
        self.assertEqual(result["blocked_count"], 1)

    def test_recent_unknown_parents_still_reserve_every_slot(self):
        pending = [
            _parent(index, updated_at=time.time() - 30)
            for index in range(MULTI_MAX_CONCURRENCY)
        ]
        result = classify_batch_write_admission(
            **_facts(pending_entries=pending),
            candidate_fingerprints=["new-child"],
            requested_max_concurrent_multi=MULTI_MAX_CONCURRENCY,
        )

        self.assertEqual(
            result["active_multi_parent_count"], MULTI_MAX_CONCURRENCY)
        self.assertEqual(result["effective_max_concurrent_multi"], 0)
        self.assertIn("MULTI_PARENT_CAPACITY_RESERVED", result["write_blockers"])

    def test_slot_hold_rules_fail_closed_without_a_usable_timestamp(self):
        now = time.time()
        cases = [
            ("running parent", _parent(0, status="RUNNING"), True),
            ("submitting parent", _parent(0, status="SUBMITTING"), True),
            ("recent unknown", _parent(0, updated_at=now - 60), True),
            ("stale unknown", _parent(0, updated_at=now - 86400), True),
            ("unknown without timestamp", _parent(0), True),
            ("unknown with unusable timestamp",
             _parent(0, updated_at="yesterday"), True),
            ("unknown with boolean timestamp",
             _parent(0, updated_at=True), True),
            ("unknown with NaN timestamp",
             _parent(0, updated_at=float("nan")), True),
            ("unknown with negative infinity timestamp",
             _parent(0, updated_at=float("-inf")), True),
            ("child row",
             {"kind": ExecutionGuard.MULTI_CHILD, "status": "SUBMIT_UNKNOWN"},
             False),
            ("single row", {"kind": "SINGLE", "status": "SUBMIT_UNKNOWN"}, False),
            ("terminal parent", _parent(0, status="DONE"), False),
            ("non-mapping row", None, True),
        ]
        for label, row, expected in cases:
            with self.subTest(label=label):
                self.assertIs(multi_parent_holds_slot(row), expected)


if __name__ == "__main__":
    unittest.main()
