import json
import os
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from wqb_agent.checkpoints import CheckpointStore, CheckpointWriteError
from wqb_agent.expression import submission_fingerprint
from wqb_agent.locking import OwnerBusyError, single_instance_scope


class TestCheckpointStore(unittest.TestCase):
    @staticmethod
    def _row(**overrides):
        row = {
            "id": "e1", "round": 4, "hypothesis_id": "h1",
            "expression": "rank(close)", "settings": {"decay": 4},
            "fields_used": ["close"], "status": "PENDING",
        }
        row.update(overrides)
        return row

    def _write_raw(self, store, payload):
        with open(store.path(4), "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        record = store.scan()[0]
        return record["validation_code"]

    def test_write_from_unrelated_thread_is_blocked_by_outer_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CheckpointStore(tmp)
            outcomes = []
            with single_instance_scope(tmp, operation="outer"):
                thread = threading.Thread(
                    target=lambda: self._write_from_thread(store, outcomes)
                )
                thread.start()
                thread.join()

            self.assertEqual(outcomes, ["busy"])
            self.assertFalse(os.path.exists(store.path(1)))

    def test_write_from_delegated_simulation_worker_succeeds(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CheckpointStore(tmp)
            outcomes = []
            with single_instance_scope(tmp, operation="outer") as owner:
                delegation = owner.delegate_simulation_worker()
                thread = threading.Thread(
                    target=lambda: self._write_from_thread(store, outcomes, delegation)
                )
                thread.start()
                thread.join()

            self.assertEqual(outcomes, ["written"])
            self.assertTrue(os.path.exists(store.path(1)))

    def test_released_owner_delegation_cannot_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CheckpointStore(tmp)
            with single_instance_scope(tmp, operation="outer") as owner:
                delegation = owner.delegate_simulation_worker()
            outcomes = []
            thread = threading.Thread(
                target=lambda: self._write_from_thread(store, outcomes, delegation)
            )
            thread.start()
            thread.join()
            self.assertEqual(outcomes, ["busy"])
            self.assertFalse(os.path.exists(store.path(1)))

    @staticmethod
    def _write_from_thread(store, outcomes, delegation=None):
        try:
            store.write(1, {"id": "h"}, [], complete=False, delegation=delegation)
        except OwnerBusyError:
            outcomes.append("busy")
        else:
            outcomes.append("written")

    def test_write_and_load_preserve_checkpoint_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CheckpointStore(f"{tmp}/new-state")
            experiment = SimpleNamespace(to_dict=lambda: {
                "id": "e1", "round": 4, "hypothesis_id": "h1",
                "expression": "rank(close)", "settings": {},
                "fields_used": ["close"], "status": "SUBMIT_UNKNOWN",
            })
            self.assertTrue(store.write(4, {"id": "h1"}, [experiment], complete=False))
            loaded = store.load(4)
            self.assertEqual(loaded["round_no"], 4)
            self.assertFalse(loaded["complete"])
            self.assertEqual(loaded["experiments"][0]["status"], "SUBMIT_UNKNOWN")

    def test_write_rejects_non_bool_complete_without_changing_existing_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CheckpointStore(tmp)
            experiment = SimpleNamespace(to_dict=lambda: self._row(status="DONE"))
            store.write(4, {"id": "h1"}, [experiment], complete=True)
            path = store.path(4)
            with open(path, "rb") as handle:
                before = handle.read()
            for complete in ("false", "true", 0, 1, None):
                with self.subTest(complete=complete):
                    with self.assertRaises(CheckpointWriteError) as raised:
                        store.write(4, {"id": "h1"}, [experiment], complete=complete)
                    self.assertEqual(raised.exception.code, "CHECKPOINT_COMPLETE_TYPE_INVALID")
                    with open(path, "rb") as handle:
                        self.assertEqual(handle.read(), before)

    def test_write_projection_uses_read_validator_and_preserves_existing_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CheckpointStore(tmp)
            valid = SimpleNamespace(to_dict=lambda: self._row(status="DONE"))
            store.write(4, {"id": "h1"}, [valid], complete=True)
            path = store.path(4)
            with open(path, "rb") as handle:
                before = handle.read()
            invalid = SimpleNamespace(to_dict=lambda: self._row(status="SUBMIT_UNKNOWN"))
            with self.assertRaises(CheckpointWriteError) as raised:
                store.write(4, {"id": "h1"}, [invalid], complete=True)
            self.assertEqual(raised.exception.code, "CHECKPOINT_COMPLETE_WITH_UNRESOLVED_EXECUTION")
            with open(path, "rb") as handle:
                self.assertEqual(handle.read(), before)

    def test_write_projection_rejects_execution_set_collisions(self):
        cases = (
            ([self._row(id="e1"), self._row(id="e1", expression="rank(open)")],
             "CHECKPOINT_DUPLICATE_EXPERIMENT_ID"),
            ([self._row(id="e1"), self._row(id="e2")],
             "CHECKPOINT_DUPLICATE_SUBMISSION_IDENTITY"),
            ([self._row(id="e1", proposal_id="p1"),
              self._row(id="e2", expression="rank(open)", proposal_id="p1")],
             "CHECKPOINT_DUPLICATE_PROPOSAL_ID"),
            ([self._row(id="e1", progress_url="/simulations/1"),
              self._row(id="e2", expression="rank(open)", progress_url="/simulations/1")],
             "CHECKPOINT_PROGRESS_IDENTITY_COLLISION"),
            ([self._row(id="e1", submission_fingerprint="bad")],
             "CHECKPOINT_SUBMISSION_IDENTITY_MISMATCH"),
        )
        for rows, code in cases:
            with self.subTest(code=code), tempfile.TemporaryDirectory() as tmp:
                store = CheckpointStore(tmp)
                with self.assertRaises(CheckpointWriteError) as raised:
                    store.write(
                        4,
                        {"id": "h1"},
                        [SimpleNamespace(to_dict=lambda row=row: dict(row)) for row in rows],
                        complete=False,
                    )
                self.assertEqual(raised.exception.code, code)
                self.assertFalse(os.path.exists(store.path(4)))

    def test_checkpoint_excludes_result_evidence_for_restart_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CheckpointStore(tmp)
            experiment = SimpleNamespace(to_dict=lambda: {
                "id": "e1", "round": 4, "hypothesis_id": "h1",
                "expression": "rank(close)", "settings": {},
                "fields_used": ["close"], "status": "RUNNING",
                "progress_url": "/simulations/s1", "alpha_id": "alpha-1",
                "metrics": {"sharpe": 2.0}, "checks": [{"name": "SELF_CORRELATION"}],
                "yearly_evidence": {"status": "VERIFIED"},
            })

            store.write(4, {"id": "h1"}, [experiment], complete=False)
            row = store.load(4)["experiments"][0]

            self.assertEqual(row["progress_url"], "/simulations/s1")
            for forbidden in ("alpha_id", "metrics", "checks", "yearly_evidence"):
                self.assertNotIn(forbidden, row)

    def test_checkpoint_preserves_recovery_identity_envelope(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CheckpointStore(tmp)
            experiment = SimpleNamespace(to_dict=lambda: {
                "id": "e1", "round": 4, "hypothesis_id": "h1",
                "expression": "rank(close)", "settings": {"decay": 4},
                "fields_used": ["close"], "datasets": ["pv1"],
                "candidate_id": "candidate-1", "proposal_id": "proposal-1",
                "submission_fingerprint": submission_fingerprint(
                    "rank(close)", {"decay": 4}
                ), "submission_started_at": 12.5,
                "parent_expression": "rank(open)", "parent_id": "parent-1",
                "lineage_id": "lineage-1", "created_at": 10.0,
                "optimization_decision_id": "decision-1",
                "template_mode": "PARTIAL_OPERATOR",
                "operator_role_mapping": {"x": "documented"},
                "status": "RUNNING", "progress_url": "/simulations/s1",
            })
            store.write(4, {"id": "h1"}, [experiment], complete=False)
            row = store.load(4)["experiments"][0]
            for key in (
                "datasets", "candidate_id", "proposal_id", "submission_started_at",
                "parent_expression", "parent_id", "lineage_id", "created_at",
                "optimization_decision_id", "template_mode", "operator_role_mapping",
            ):
                self.assertIn(key, row)
            for forbidden in ("metrics", "checks", "pnl_evidence", "validation_report"):
                self.assertNotIn(forbidden, row)

    def test_persisted_fingerprint_mismatch_is_invalid_not_repaired(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CheckpointStore(tmp)
            row = {
                "id": "e1", "round": 4, "hypothesis_id": "h1",
                "expression": "rank(close)", "settings": {"decay": 4},
                "fields_used": ["close"], "status": "SUBMIT_UNKNOWN",
                "submission_fingerprint": "corrupt",
            }
            with open(store.path(4), "w", encoding="utf-8") as handle:
                json.dump({"round_no": 4, "complete": False,
                           "hypothesis": {}, "experiments": [row]}, handle)
            self.assertIsNone(store.load(4))
            record = store.scan()[0]
            self.assertEqual(record["validation_code"], "CHECKPOINT_SUBMISSION_IDENTITY_MISMATCH")

    def test_future_checkpoint_schema_is_unverifiable(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CheckpointStore(tmp)
            with open(store.path(4), "w", encoding="utf-8") as handle:
                json.dump({"schema_version": 999, "round_no": 4,
                           "complete": False, "hypothesis": {}, "experiments": []}, handle)
            record = store.scan()[0]
            self.assertEqual(record["validation_code"], "UNSUPPORTED_FUTURE_CHECKPOINT_SCHEMA")

    def test_checkpoint_completion_and_status_semantics_are_strict(self):
        cases = (
            ("false", "CHECKPOINT_COMPLETE_TYPE_INVALID", "PENDING"),
            (1, "CHECKPOINT_COMPLETE_TYPE_INVALID", "PENDING"),
            (True, "CHECKPOINT_COMPLETE_WITH_UNRESOLVED_EXECUTION", "SUBMIT_UNKNOWN"),
            (True, "CHECKPOINT_COMPLETE_WITH_UNRESOLVED_EXECUTION", "RUNNING"),
            (True, None, "DONE"),
            (False, None, "DONE"),
            (False, "CHECKPOINT_STATUS_INVALID", "NOT_A_STATUS"),
        )
        for complete, code, status in cases:
            with self.subTest(complete=complete, status=status):
                with tempfile.TemporaryDirectory() as tmp:
                    store = CheckpointStore(tmp)
                    actual = self._write_raw(store, {
                        "round_no": 4, "complete": complete,
                        "hypothesis": {"id": "h1"},
                        "experiments": [self._row(status=status)],
                    })
                    if code is None:
                        self.assertIsNotNone(store.load(4))
                    else:
                        self.assertEqual(actual, code)

    def test_checkpoint_round_and_hypothesis_referential_integrity(self):
        cases = (
            (self._row(round=5), "CHECKPOINT_ROUND_IDENTITY_MISMATCH"),
            (self._row(round=True), "CHECKPOINT_ROUND_IDENTITY_MISMATCH"),
            (self._row(hypothesis_id="h2"), "CHECKPOINT_HYPOTHESIS_IDENTITY_MISMATCH"),
        )
        for row, code in cases:
            with self.subTest(code=code):
                with tempfile.TemporaryDirectory() as tmp:
                    store = CheckpointStore(tmp)
                    self.assertEqual(self._write_raw(store, {
                        "round_no": 4, "complete": False,
                        "hypothesis": {"id": "h1"}, "experiments": [row],
                    }), code)

        with tempfile.TemporaryDirectory() as tmp:
            store = CheckpointStore(tmp)
            code = self._write_raw(store, {
                "round_no": 4, "complete": False, "hypothesis": {},
                "experiments": [self._row(hypothesis_id="legacy-h")],
            })
            self.assertIsNone(code)
            self.assertIsNotNone(store.load(4))

    def test_checkpoint_execution_set_uniqueness_is_fail_closed(self):
        cases = (
            ([self._row(id="e1"), self._row(id="e1", expression="rank(open)")],
             "CHECKPOINT_DUPLICATE_EXPERIMENT_ID"),
            ([self._row(id="e1"), self._row(id="e2")],
             "CHECKPOINT_DUPLICATE_SUBMISSION_IDENTITY"),
            ([self._row(id="e1", proposal_id="p1"), self._row(id="e2", expression="rank(open)", proposal_id="p1")],
             "CHECKPOINT_DUPLICATE_PROPOSAL_ID"),
            ([self._row(id="e1", progress_url="/simulations/1"),
              self._row(id="e2", expression="rank(open)", progress_url="/simulations/1")],
             "CHECKPOINT_PROGRESS_IDENTITY_COLLISION"),
        )
        for rows, code in cases:
            with self.subTest(code=code):
                with tempfile.TemporaryDirectory() as tmp:
                    store = CheckpointStore(tmp)
                    self.assertEqual(self._write_raw(store, {
                        "round_no": 4, "complete": False,
                        "hypothesis": {"id": "h1"}, "experiments": rows,
                    }), code)

    def test_legacy_missing_fingerprints_still_collide_after_recomputation(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CheckpointStore(tmp)
            rows = [self._row(id="e1"), self._row(id="e2")]
            self.assertEqual(self._write_raw(store, {
                "round_no": 4, "complete": False, "hypothesis": {"id": "h1"},
                "experiments": rows,
            }), "CHECKPOINT_DUPLICATE_SUBMISSION_IDENTITY")

    def test_malformed_or_mismatched_checkpoint_is_not_treated_as_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CheckpointStore(tmp)
            with open(store.path(4), "w", encoding="utf-8") as handle:
                json.dump({"round_no": 3, "experiments": []}, handle)
            self.assertIsNone(store.load(4))
            self.assertIsNone(store.unfinished_except(4))
            with open(store.path(5), "w", encoding="utf-8") as handle:
                handle.write("not json")
            self.assertEqual(store.unfinished_except(4), store.path(5))

    def test_unfinished_except_returns_lowest_blocking_round(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CheckpointStore(tmp)
            for round_no in (6, 8):
                with open(store.path(round_no), "w", encoding="utf-8") as handle:
                    json.dump({"round_no": round_no, "complete": False,
                               "hypothesis": {}, "experiments": []}, handle)
            with open(store.path(7), "w", encoding="utf-8") as handle:
                json.dump({"round_no": 7, "complete": True,
                           "hypothesis": {}, "experiments": []}, handle)
            self.assertEqual(store.unfinished_except(9), store.path(6))
            self.assertEqual(store.unfinished_except(6), store.path(8))

    def test_unresolved_submission_identity_reconstructs_legacy_fingerprint(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CheckpointStore(tmp)
            row = {
                "id": "e1", "round": 4, "hypothesis_id": "h1",
                "expression": "rank(close)", "settings": {"decay": 4},
                "fields_used": ["close"], "status": "SUBMIT_UNKNOWN",
            }
            with open(store.path(4), "w", encoding="utf-8") as handle:
                json.dump({"round_no": 4, "complete": False,
                           "hypothesis": {}, "experiments": [row]}, handle)
            identities = store.unresolved_submission_identities()
            self.assertEqual(len(identities), 1)
            self.assertEqual(identities[next(iter(identities))][0]["round_no"], 4)

    def test_scan_is_authoritative_for_valid_and_malformed_checkpoints(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CheckpointStore(tmp)
            with open(store.path(4), "w", encoding="utf-8") as handle:
                json.dump({"round_no": 4, "complete": False, "hypothesis": {}, "experiments": []}, handle)
            with open(store.path(5), "w", encoding="utf-8") as handle:
                handle.write("not json")
            records = store.scan()
            self.assertEqual([record["round_no"] for record in records], [4, 5])
            self.assertFalse(records[0]["malformed"])
            self.assertTrue(records[1]["malformed"])
            self.assertEqual(store.unfinished_except(9), records[0]["path"])

    def test_scan_parses_each_checkpoint_json_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CheckpointStore(tmp)
            with open(store.path(4), "w", encoding="utf-8") as handle:
                json.dump({
                    "round_no": 4, "complete": False, "hypothesis": {},
                    "experiments": [],
                }, handle)
            with patch("wqb_agent.checkpoints.json.load",
                       wraps=json.load) as load:
                records = store.scan()
        self.assertEqual(len(records), 1)
        self.assertEqual(load.call_count, 1)

    def test_load_accepts_utf8_bom_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CheckpointStore(tmp)
            payload = json.dumps({
                "round_no": 4, "complete": False, "hypothesis": {},
                "experiments": [],
            }).encode("utf-8-sig")
            with open(store.path(4), "wb") as handle:
                handle.write(payload)
            checkpoint = store.load(4)
        self.assertIsNotNone(checkpoint)
        self.assertEqual(checkpoint["round_no"], 4)


if __name__ == "__main__":
    unittest.main()
