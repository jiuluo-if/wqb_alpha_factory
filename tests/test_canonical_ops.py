import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import patch

from scripts import archive_completed_rounds, reconcile_pending
from wqb_agent.audit import audit_state
from wqb_agent.checkpoints import CheckpointStore
from wqb_agent.expression import submission_fingerprint
from wqb_agent.workspace_snapshot import read_workspace_snapshot


class TestCanonicalOperations(unittest.TestCase):
    @staticmethod
    def _experiment(**overrides):
        row = {
            "id": "e1", "round": 4, "hypothesis_id": "h1",
            "expression": "rank(close)", "settings": {"decay": 4},
            "fields_used": ["close"], "status": "RUNNING",
            "proposal_id": "p1", "progress_url": "/simulations/s1",
        }
        row.update(overrides)
        return SimpleNamespace(to_dict=lambda: dict(row))

    def test_maintenance_scripts_keep_canonical_owner_boundaries(self):
        with open("scripts/archive_completed_rounds.py", encoding="utf-8") as handle:
            archive_source = handle.read()
        with open("scripts/reconcile_pending.py", encoding="utf-8") as handle:
            reconcile_source = handle.read()
        self.assertIn("CheckpointStore", archive_source)
        self.assertIn(".scan()", archive_source)
        self.assertNotIn("json.load", archive_source)
        self.assertNotIn("_checkpoint_complete", archive_source)
        self.assertNotIn("commit_reconciled", reconcile_source)
        self.assertNotIn("+ \"rc\"", reconcile_source)
        self.assertNotIn("trajectory.add", reconcile_source)
        self.assertNotIn("submit_simulation", reconcile_source)

    def test_archive_plan_uses_canonical_scan_and_reports_unverifiable(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = os.path.join(tmp, "state")
            archive_dir = os.path.join(tmp, "archive")
            os.makedirs(state_dir)
            store = CheckpointStore(state_dir)
            store.write(4, {"id": "h1"}, [self._experiment(status="DONE")], complete=True)
            with open(os.path.join(state_dir, "round_5.checkpoint.json"), "w", encoding="utf-8") as handle:
                handle.write('{"round_no": 5, "complete": true, "experiments": [')
            output = io.StringIO()
            with redirect_stdout(output):
                result = archive_completed_rounds.main([
                    "--state-dir", state_dir, "--archive-dir", archive_dir,
                ])
            self.assertEqual(result, 0)
            text = output.getvalue()
            self.assertIn("valid-complete candidate count: 1", text)
            self.assertIn("unverifiable protected count: 1", text)
            self.assertTrue(os.path.exists(os.path.join(state_dir, "round_5.checkpoint.json")))
            self.assertFalse(os.path.exists(os.path.join(archive_dir, "checkpoints", "round_4.checkpoint.json")))

    def test_archive_apply_moves_nothing_when_unverifiable_checkpoint_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = os.path.join(tmp, "state")
            archive_dir = os.path.join(tmp, "archive")
            os.makedirs(state_dir)
            store = CheckpointStore(state_dir)
            store.write(4, {"id": "h1"}, [self._experiment(status="DONE")], complete=True)
            with open(os.path.join(state_dir, "round_5.checkpoint.json"), "w", encoding="utf-8") as handle:
                handle.write("not json")
            result = archive_completed_rounds.main([
                "--state-dir", state_dir, "--archive-dir", archive_dir, "--apply",
            ])
            self.assertEqual(result, 2)
            self.assertTrue(os.path.exists(os.path.join(state_dir, "round_4.checkpoint.json")))
            self.assertTrue(os.path.exists(os.path.join(state_dir, "round_5.checkpoint.json")))

    def test_archive_apply_moves_only_canonically_valid_complete_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = os.path.join(tmp, "state")
            archive_dir = os.path.join(tmp, "archive")
            os.makedirs(state_dir)
            CheckpointStore(state_dir).write(
                4, {"id": "h1"}, [self._experiment(status="DONE")], complete=True
            )
            self.assertEqual(archive_completed_rounds.main([
                "--state-dir", state_dir, "--archive-dir", archive_dir, "--apply",
            ]), 0)
            self.assertFalse(os.path.exists(os.path.join(state_dir, "round_4.checkpoint.json")))
            self.assertTrue(os.path.exists(os.path.join(archive_dir, "checkpoints", "round_4.checkpoint.json")))

    def test_archive_apply_blocks_future_or_identity_invalid_checkpoint(self):
        for payload in (
            {"schema_version": 999, "round_no": 4, "complete": True,
             "hypothesis": {}, "experiments": []},
            {"round_no": 4, "complete": True, "hypothesis": {"id": "h1"},
             "experiments": [{**self._experiment(status="DONE").to_dict(),
                              "submission_fingerprint": "bad"}]},
        ):
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as tmp:
                state_dir = os.path.join(tmp, "state")
                archive_dir = os.path.join(tmp, "archive")
                os.makedirs(state_dir)
                with open(os.path.join(state_dir, "round_4.checkpoint.json"), "w", encoding="utf-8") as handle:
                    json.dump(payload, handle)
                self.assertEqual(archive_completed_rounds.main([
                    "--state-dir", state_dir, "--archive-dir", archive_dir, "--apply",
                ]), 2)
                self.assertTrue(os.path.exists(os.path.join(state_dir, "round_4.checkpoint.json")))

    def test_reconcile_collect_uses_validated_checkpoint_and_deduplicates_trajectory(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CheckpointStore(tmp)
            exp = self._experiment()
            store.write(4, {"id": "h1"}, [exp], complete=False)
            with open(os.path.join(tmp, "trajectory.jsonl"), "w", encoding="utf-8") as handle:
                handle.write(json.dumps({**exp.to_dict(), "submission_fingerprint": submission_fingerprint(
                    exp.to_dict()["expression"], exp.to_dict()["settings"]
                )}) + "\n")
            with open(os.path.join(tmp, "round_5.checkpoint.json"), "w", encoding="utf-8") as handle:
                json.dump({"round_no": 5, "complete": False, "hypothesis": {"id": "h1"},
                           "experiments": [{**exp.to_dict(), "submission_fingerprint": "bad"}]}, handle)
            targets = reconcile_pending.collect(tmp)
            self.assertEqual(len(targets), 1)
            self.assertEqual(targets[0]["id"], "e1")

    def test_reconcile_ignores_complete_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            CheckpointStore(tmp).write(
                4, {"id": "h1"}, [self._experiment(status="DONE")], complete=True
            )
            self.assertEqual(reconcile_pending.collect(tmp), [])

    def test_reconcile_known_url_observes_only_without_post_or_canonical_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            CheckpointStore(tmp).write(
                4, {"id": "h1"}, [self._experiment(status="UNKNOWN")], complete=False
            )

            class ReadOnlyClient:
                def get_progress_snapshot(self, url, timeout):
                    return {"status_code": 200, "headers": {}, "payload": {
                        "status": "COMPLETE", "alpha": "alpha-1",
                    }}

                def get_alpha(self, alpha_id):
                    return {"alpha": alpha_id, "metrics": {}}

                def submit_simulation(self, *_args, **_kwargs):
                    raise AssertionError("reconciliation must never POST")

            with patch("scripts.reconcile_pending.WQBClient", ReadOnlyClient):
                self.assertIsNone(reconcile_pending.main([
                    "--state-dir", tmp, "--timeout", "1",
                ]))
            self.assertFalse(os.path.exists(os.path.join(tmp, "trajectory.jsonl")))
            self.assertFalse(os.path.exists(os.path.join(tmp, "trial_ledger.jsonl")))

    def test_reconcile_reports_unverifiable_checkpoint_without_consuming_raw_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "round_5.checkpoint.json"), "w", encoding="utf-8") as handle:
                json.dump({"round_no": 5, "complete": False, "experiments": [self._experiment().to_dict()]}, handle)
            blockers = []
            self.assertEqual(reconcile_pending.collect(tmp, blockers=blockers), [])
            self.assertEqual(blockers, [{"code": "RECONCILE_CHECKPOINT_UNVERIFIABLE", "round": 5}])

    def test_reconcile_commit_is_retired_before_state_or_network_access(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = io.StringIO()
            with redirect_stdout(output):
                result = reconcile_pending.main(["--state-dir", tmp, "--commit"])
            self.assertEqual(result, 2)
            self.assertIn("RECONCILE_COMMIT_RETIRED", output.getvalue())
            self.assertEqual(os.listdir(tmp), [])

    def test_audit_reports_duplicate_remote_execution_projection_without_expression(self):
        with tempfile.TemporaryDirectory() as tmp:
            fingerprint = submission_fingerprint("rank(secret_expression)", {"decay": 4})
            rows = [
                {"id": "e1", "round": 1, "expression": "rank(secret_expression)",
                 "settings": {"decay": 4}, "fields_used": ["secret"], "status": "DONE",
                 "submission_fingerprint": fingerprint, "progress_url": "/simulations/s1",
                 "alpha_id": "alpha-1"},
                {"id": "e2", "round": 2, "expression": "rank(secret_expression)",
                 "settings": {"decay": 4}, "fields_used": ["secret"], "status": "DONE",
                 "submission_fingerprint": fingerprint, "progress_url": "/simulations/s1",
                 "alpha_id": "alpha-1"},
            ]
            with open(os.path.join(tmp, "trajectory.jsonl"), "w", encoding="utf-8") as handle:
                for row in rows:
                    handle.write(json.dumps(row) + "\n")
            result = audit_state(tmp, snapshot=read_workspace_snapshot(tmp), lifecycle_persistent=False)
            self.assertIn("DUPLICATE_REMOTE_EXECUTION_PROJECTION", result["errors"])
            finding = next(item for item in result["findings"] if item["code"] == "DUPLICATE_REMOTE_EXECUTION_PROJECTION")
            self.assertEqual(finding["collision_count"], 1)
            self.assertNotIn("secret_expression", json.dumps(result))

    def test_audit_reports_same_fingerprint_and_alpha_with_different_urls(self):
        with tempfile.TemporaryDirectory() as tmp:
            fingerprint = submission_fingerprint("rank(close)", {"decay": 4})
            rows = [
                {"id": "e1", "round": 1, "expression": "rank(close)", "settings": {"decay": 4},
                 "fields_used": ["close"], "status": "DONE", "submission_fingerprint": fingerprint,
                 "alpha_id": "alpha-1", "progress_url": "/simulations/s1"},
                {"id": "e2", "round": 2, "expression": "rank(close)", "settings": {"decay": 4},
                 "fields_used": ["close"], "status": "DONE", "submission_fingerprint": fingerprint,
                 "alpha_id": "alpha-1", "progress_url": "/simulations/s2"},
            ]
            with open(os.path.join(tmp, "trajectory.jsonl"), "w", encoding="utf-8") as handle:
                for row in rows:
                    handle.write(json.dumps(row) + "\n")
            result = audit_state(tmp, snapshot=read_workspace_snapshot(tmp), lifecycle_persistent=False)
            self.assertIn("DUPLICATE_REMOTE_EXECUTION_PROJECTION", result["errors"])


if __name__ == "__main__":
    unittest.main()
