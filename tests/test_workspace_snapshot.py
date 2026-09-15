import json
import os
import tempfile
import unittest

from wqb_agent.workspace_snapshot import read_workspace_snapshot


class TestWorkspaceSnapshotBoundaries(unittest.TestCase):
    def test_malformed_checkpoint_and_json_remain_visible_as_unreadable(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "round_1.checkpoint.json"), "w", encoding="utf-8") as handle:
                handle.write("not json")
            with open(os.path.join(tmp, "proposals.json"), "w", encoding="utf-8") as handle:
                handle.write("{")
            snapshot = read_workspace_snapshot(tmp)
        self.assertTrue(snapshot.checkpoint_records[0]["malformed"])
        self.assertEqual(
            snapshot.inventory.info("round_1.checkpoint.json").schema_version,
            "UNREADABLE",
        )
        proposals = snapshot.inventory.info("proposals.json")
        self.assertTrue(proposals.exists)
        self.assertTrue(proposals.readable)
        self.assertEqual(proposals.schema_version, "UNREADABLE")
        self.assertIsNone(snapshot.proposal_round)
        self.assertEqual(snapshot.proposal_count, 0)

    def test_directory_named_json_is_not_reported_as_readable_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.mkdir(os.path.join(tmp, "proposals.json"))
            snapshot = read_workspace_snapshot(tmp)
        info = snapshot.inventory.info("proposals.json")
        self.assertTrue(info.exists)
        self.assertFalse(info.readable)
        self.assertEqual(info.kind, "json")
        self.assertIsNone(snapshot.proposal_round)

    def test_invalid_jsonl_row_is_skipped_without_mutating_append_only_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "trial_ledger.jsonl")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("not json\n")
                handle.write(json.dumps({
                    "proposal_id": "p1", "phase": "simulation_committed",
                }) + "\n")
            with open(path, "rb") as handle:
                before = handle.read()
            snapshot = read_workspace_snapshot(tmp)
            with open(path, "rb") as handle:
                after = handle.read()
        self.assertEqual(before, after)
        self.assertIn("p1", snapshot.ledger.committed)

    def test_jsonl_summaries_retain_invalid_row_counts_and_valid_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "trajectory.jsonl"), "w", encoding="utf-8") as handle:
                handle.write("not json\n[]\n")
                handle.write(json.dumps({
                    "proposal_id": "p1", "phase": "simulation_submitted",
                }) + "\n")
            with open(os.path.join(tmp, "trial_ledger.jsonl"), "w", encoding="utf-8") as handle:
                handle.write("not json\n")
                handle.write(json.dumps({
                    "proposal_id": "p1", "phase": "simulation_committed",
                }) + "\n")
            with open(os.path.join(tmp, "validation_reports.jsonl"), "w", encoding="utf-8") as handle:
                handle.write("not json\n")
                handle.write(json.dumps({"parent_id": "p1"}) + "\n")
            snapshot = read_workspace_snapshot(tmp)
        self.assertEqual(snapshot.trajectory.invalid_rows, 2)
        self.assertEqual(snapshot.ledger.invalid_rows, 1)
        self.assertEqual(snapshot.validation.invalid_rows, 1)
        self.assertIn("p1", snapshot.trajectory.observed_submitted)
        self.assertIn("p1", snapshot.ledger.committed)

    def test_lifecycle_summary_retains_phase_diagnostics(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "trial_ledger.jsonl"), "w", encoding="utf-8") as handle:
                for row in (
                    {"proposal_id": "p1", "phase": "simulation_submitted"},
                    {"proposal_id": "p1", "phase": "simulation_committed"},
                    {"proposal_id": "p1", "phase": "simulation_committed"},
                    {"proposal_id": "p2", "phase": "future_phase"},
                    {"proposal_id": "p1", "phase": "research_outcome_settled"},
                ):
                    handle.write(json.dumps(row) + "\n")
            snapshot = read_workspace_snapshot(tmp)
        self.assertEqual(snapshot.ledger.unknown_phase_rows, 1)
        self.assertEqual(snapshot.ledger.incomplete_rows, 1)
        self.assertEqual(snapshot.ledger.duplicate_lifecycle_phases, 1)
        self.assertEqual(
            snapshot.ledger.phase_order_violations,
            (("p1", "simulation_submitted", "simulation_committed"),),
        )

    def test_snapshot_counts_forward_ledger_schema_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "trial_ledger.jsonl"), "w", encoding="utf-8") as handle:
                handle.write(json.dumps({
                    "schema_version": 99,
                    "proposal_id": "p",
                    "phase": "future_phase",
                }) + "\n")
            snapshot = read_workspace_snapshot(tmp)
        self.assertEqual(snapshot.ledger.unsupported_schema_rows, 1)

    def test_submission_pool_does_not_fabricate_none_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "submission_pool.json"), "w", encoding="utf-8") as handle:
                json.dump({"candidates": [
                    {"alpha_id": None, "proposal_id": ""},
                    {"alpha_id": "a1", "proposal_id": None},
                ]}, handle)
            snapshot = read_workspace_snapshot(tmp)
        self.assertEqual(snapshot.submission_pool.unverifiable_candidates, 1)
        self.assertIn(frozenset(), snapshot.submission_pool.candidate_identities)
        self.assertIn(frozenset({"a1"}), snapshot.submission_pool.candidate_identities)
        self.assertIn(
            frozenset({("alpha_id", "a1")}),
            snapshot.submission_pool.candidate_identity_sources,
        )
        self.assertNotIn("None", snapshot.submission_pool.candidate_identities[0])

    def test_unexpected_json_is_inventory_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "notes.json"), "w", encoding="utf-8") as handle:
                json.dump({"round_no": 999, "proposals": [{"id": "unexpected"}]}, handle)
            snapshot = read_workspace_snapshot(tmp)
        info = snapshot.inventory.info("notes.json")
        self.assertEqual(info.schema_version, "PRESENT")
        self.assertIsNone(snapshot.proposal_round)

    def test_snapshot_safe_types_reject_untrusted_round_and_schema_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "proposals.json"), "w", encoding="utf-8") as handle:
                json.dump({"round_no": "7", "proposals": []}, handle)
            with open(os.path.join(tmp, "experience.json"), "w", encoding="utf-8") as handle:
                json.dump({"schema_version": {"unexpected": True}}, handle)
            snapshot = read_workspace_snapshot(tmp)
        self.assertIsNone(snapshot.proposal_round)
        self.assertEqual(snapshot.inventory.info("experience.json").schema_version, "INVALID")
        self.assertEqual(snapshot.proposal_count, 0)

    def test_empty_state_is_a_valid_empty_read_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            snapshot = read_workspace_snapshot(tmp)
        self.assertEqual(snapshot.checkpoint_records, ())
        self.assertEqual(snapshot.trajectory.records, 0)
        self.assertEqual(snapshot.ledger.committed, frozenset())

    def test_read_pass_inventory_is_request_local_and_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            snapshot = read_workspace_snapshot(tmp)
        self.assertEqual(dict(snapshot.read_passes), {
            "checkpoints": 1,
            "trajectory.jsonl": 1,
            "trial_ledger.jsonl": 1,
            "validation_reports.jsonl": 1,
        })

    def test_large_trajectory_uses_bounded_summary_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "trajectory.jsonl")
            with open(path, "w", encoding="utf-8") as handle:
                for index in range(2000):
                    handle.write(json.dumps({
                        "id": f"e{index}", "round": index,
                        "status": "UNKNOWN" if index % 2 else "DONE",
                    }) + "\n")
            snapshot = read_workspace_snapshot(tmp)
        self.assertEqual(snapshot.trajectory.records, 2000)
        self.assertEqual(snapshot.trajectory.latest_round, 1999)
        self.assertEqual(snapshot.trajectory.submit_unknown_count, 0)


if __name__ == "__main__":
    unittest.main()
