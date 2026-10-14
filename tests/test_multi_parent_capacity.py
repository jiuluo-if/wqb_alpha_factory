"""Multi parent capacity accounting: unresolved parents must not hold slots forever.

An ExecutionGuard ``MULTI_PARENT`` row that stayed ``SUBMIT_UNKNOWN`` past the
hold window cannot still be running on BRAIN (its children finish in minutes and
the poll timeout is 30 minutes), so it must stop consuming one of the
``MULTI_MAX_CONCURRENCY`` slots.  Its exact-once record stays authoritative: the
same fingerprint is still refused.
"""

import time
import unittest

from wqb_agent.simulation_gateway import (
    MULTI_MAX_CONCURRENCY,
    MULTI_UNKNOWN_SLOT_HOLD_SEC,
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
    def test_stale_unknown_parents_release_capacity_but_keep_exact_once(self):
        now = time.time()
        pending = [
            _parent(index, updated_at=now - MULTI_UNKNOWN_SLOT_HOLD_SEC - 60)
            for index in range(MULTI_MAX_CONCURRENCY)
        ]
        result = classify_batch_write_admission(
            **_facts(pending_entries=pending),
            candidate_fingerprints=["new-child"],
            requested_max_concurrent_multi=MULTI_MAX_CONCURRENCY,
        )

        self.assertEqual(result["active_multi_parent_count"], 0)
        self.assertEqual(
            result["available_multi_parent_slots"], MULTI_MAX_CONCURRENCY)
        self.assertEqual(
            result["effective_max_concurrent_multi"], MULTI_MAX_CONCURRENCY)
        self.assertEqual(result["write_readiness"], "READY")
        self.assertEqual(result["write_blockers"], [])
        # Exact-once is untouched: the old fingerprints still occupy the guard.
        self.assertEqual(result["eligible_count"], 1)
        self.assertEqual(result["blocked_count"], 0)

    def test_recent_unknown_parents_still_reserve_every_slot(self):
        now = time.time()
        pending = [
            _parent(index, updated_at=now - 30)
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
            ("stale unknown",
             _parent(0, updated_at=now - MULTI_UNKNOWN_SLOT_HOLD_SEC - 1), False),
            ("unknown without timestamp", _parent(0), True),
            ("unknown with unusable timestamp",
             _parent(0, updated_at="yesterday"), True),
            ("unknown with boolean timestamp",
             _parent(0, updated_at=True), True),
            ("child row",
             {"kind": ExecutionGuard.MULTI_CHILD, "status": "SUBMIT_UNKNOWN"},
             False),
            ("single row", {"kind": "SINGLE", "status": "SUBMIT_UNKNOWN"}, False),
            ("terminal parent", _parent(0, status="DONE"), False),
            ("non-mapping row", None, True),
        ]
        for label, row, expected in cases:
            with self.subTest(label=label):
                self.assertIs(multi_parent_holds_slot(row, now=now), expected)


if __name__ == "__main__":
    unittest.main()
