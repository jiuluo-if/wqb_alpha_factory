import json
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from wqb_agent.trial_ledger import TrialLedger


def _trial(candidate_id):
    return {"candidate_id": candidate_id, "proposal_id": candidate_id,
            "expression": f"rank({candidate_id})", "round": 1}


def _event_ids(path):
    with open(path, encoding="utf-8") as handle:
        return [json.loads(line)["event_id"] for line in handle if line.strip()]


class TrialLedgerIOTests(unittest.TestCase):
    def test_membership_open_failure_writes_no_duplicate_on_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "trial_ledger.jsonl")
            with patch(
                "wqb_agent.trial_ledger.sqlite3.connect",
                side_effect=sqlite3.OperationalError("open failed"),
            ):
                with self.assertRaises(sqlite3.OperationalError):
                    TrialLedger(path).record(_trial("open"), "candidate_generated")
            ledger = TrialLedger(path)
            self.assertTrue(ledger.record(_trial("open"), "candidate_generated"))
            self.assertEqual(len(_event_ids(path)), 1)

    def test_membership_insert_failure_keeps_one_canonical_append(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "trial_ledger.jsonl")
            ledger = TrialLedger(path)
            self.assertTrue(ledger.record(_trial("before"), "candidate_generated"))
            database = ledger._membership_db

            class InsertFailingDatabase:
                def execute(self, sql, parameters=()):
                    if sql.startswith("INSERT INTO event_ids"):
                        raise sqlite3.OperationalError("insert failed")
                    return database.execute(sql, parameters)

                def commit(self):
                    return database.commit()

                def rollback(self):
                    return database.rollback()

                def close(self):
                    return database.close()

            ledger._membership_db = InsertFailingDatabase()
            self.assertTrue(ledger.record(_trial("insert"), "candidate_generated"))
            self.assertEqual(len(_event_ids(path)), 2)

    def test_membership_commit_failure_keeps_one_canonical_append(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "trial_ledger.jsonl")
            ledger = TrialLedger(path)
            self.assertTrue(ledger.record(_trial("before"), "candidate_generated"))
            database = ledger._membership_db

            class CommitFailingDatabase:
                def execute(self, sql, parameters=()):
                    return database.execute(sql, parameters)

                def commit(self):
                    raise sqlite3.OperationalError("commit failed")

                def rollback(self):
                    return database.rollback()

                def close(self):
                    return database.close()

            ledger._membership_db = CommitFailingDatabase()
            self.assertTrue(ledger.record(_trial("commit"), "candidate_generated"))
            self.assertEqual(len(_event_ids(path)), 2)

    def test_membership_remove_failure_is_disposable(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "trial_ledger.jsonl")
            ledger = TrialLedger(path)
            self.assertTrue(ledger.record(_trial("remove"), "candidate_generated"))
            membership_path = ledger._membership_db_path
            database = ledger._membership_db

            class CloseFailingDatabase:
                def close(self):
                    raise sqlite3.OperationalError("close failed")

            ledger._membership_db = CloseFailingDatabase()
            with patch(
                "wqb_agent.trial_ledger.os.remove",
                side_effect=OSError("remove failed"),
            ):
                ledger._close_membership_db_unlocked()
            self.assertIsNone(ledger._membership_db)
            self.assertIsNone(ledger._membership_db_path)
            self.assertTrue(os.path.exists(membership_path))

    def test_one_owner_reconciles_once_for_many_appends(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "trial_ledger.jsonl")
            with open(path, "w", encoding="utf-8") as handle:
                for index in range(1000):
                    handle.write(json.dumps({"event_id": f"old-{index}", "phase": "generated"}) + "\n")
            ledger = TrialLedger(path)
            self.assertTrue(ledger.record(_trial("new-a"), "candidate_generated"))
            self.assertEqual(ledger._reconciliation_count, 1)
            self.assertTrue(ledger.record(_trial("new-b"), "candidate_generated"))
            self.assertTrue(ledger.record(_trial("new-c"), "candidate_generated"))
            self.assertEqual(ledger._reconciliation_count, 1)
            self.assertEqual(len(_event_ids(path)), 1003)

    def test_restart_duplicate_is_exact_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "trial_ledger.jsonl")
            trial = _trial("same")
            self.assertTrue(TrialLedger(path).record(trial, "candidate_generated"))
            before = _event_ids(path)
            self.assertFalse(TrialLedger(path).record(trial, "candidate_generated"))
            self.assertEqual(_event_ids(path), before)

    def test_membership_projection_is_disposable_and_not_python_history_set(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "trial_ledger.jsonl")
            ledger = TrialLedger(path)
            self.assertTrue(ledger.record(_trial("bounded"), "candidate_generated"))
            self.assertFalse(hasattr(ledger, "_event_ids"))
            self.assertTrue(os.path.exists(ledger._membership_db_path))

    def test_external_owner_append_invalidates_and_rebuilds_membership(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "trial_ledger.jsonl")
            first = TrialLedger(path)
            second = TrialLedger(path)
            self.assertTrue(first.record(_trial("first"), "candidate_generated"))
            self.assertTrue(second.record(_trial("external"), "candidate_generated"))
            self.assertFalse(first.record(_trial("external"), "candidate_generated"))
            self.assertEqual(first._reconciliation_count, 2)

    def test_duplicate_and_new_events_mix_without_reordering_existing_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "trial_ledger.jsonl")
            first = TrialLedger(path)
            self.assertTrue(first.record(_trial("a"), "candidate_generated"))
            self.assertTrue(first.record(_trial("b"), "candidate_generated"))
            second = TrialLedger(path)
            self.assertFalse(second.record(_trial("a"), "candidate_generated"))
            self.assertTrue(second.record(_trial("c"), "candidate_generated"))
            self.assertFalse(second.record(_trial("b"), "candidate_generated"))
            self.assertTrue(second.record(_trial("d"), "candidate_generated"))
            self.assertEqual(len(_event_ids(path)), 4)

    def test_history_completeness_boundary_is_written_once_across_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger_path = os.path.join(tmp, "trial_ledger.jsonl")
            trajectory_path = os.path.join(tmp, "trajectory.jsonl")
            with open(trajectory_path, "w", encoding="utf-8") as handle:
                handle.write('{"id":"legacy"}\n')
            trial = _trial("settled")
            ledger = TrialLedger(ledger_path, trajectory_path=trajectory_path)
            self.assertTrue(ledger.record(trial, "candidate_generated"))
            self.assertEqual(ledger.summarize()["history_completeness"], "INCOMPLETE_LEGACY")
            rows_after_first = _event_ids(ledger_path)
            self.assertFalse(ledger.record(trial, "candidate_generated"))
            self.assertEqual(len(_event_ids(ledger_path)), len(rows_after_first))
            restarted = TrialLedger(ledger_path, trajectory_path=trajectory_path)
            self.assertFalse(restarted.record(trial, "candidate_generated"))
            self.assertEqual(len(_event_ids(ledger_path)), len(rows_after_first))
            self.assertEqual(restarted.summarize()["history_completeness"], "INCOMPLETE_LEGACY")


if __name__ == "__main__":
    unittest.main()
