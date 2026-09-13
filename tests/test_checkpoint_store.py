import json
import os
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from wqb_agent.checkpoints import CheckpointStore
from wqb_agent.locking import OwnerBusyError, single_instance_scope


class TestCheckpointStore(unittest.TestCase):
    def test_write_reuses_outer_state_owner_for_execution_worker(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CheckpointStore(tmp)
            outcomes = []
            with single_instance_scope(tmp, operation="outer"):
                thread = threading.Thread(
                    target=lambda: self._write_from_thread(store, outcomes)
                )
                thread.start()
                thread.join()

            self.assertEqual(outcomes, ["written"])
            self.assertTrue(os.path.exists(store.path(1)))

    @staticmethod
    def _write_from_thread(store, outcomes):
        try:
            store.write(1, {"id": "h"}, [], complete=False)
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
