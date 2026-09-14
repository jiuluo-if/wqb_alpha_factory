import contextlib
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import patch

import main as main_entry
import wqb_agent.config as config_module
from wqb_agent.agent import Agent
from wqb_agent.audit import audit_state
from wqb_agent.behavior import extract_behavior_series
from wqb_agent.config import AppConfig, FactoryConfig, normalize_config, parse_config
from wqb_agent.diagnostics import DiagnosticEvent
from wqb_agent.doctor import run_doctor
from wqb_agent.incremental_policy import IncrementalValuePolicy
from wqb_agent.protocol import retry_after_seconds
from wqb_agent.schema import ARTIFACT_SCHEMAS, CURRENT_SCHEMA_VERSION, migrate_artifact
from wqb_agent.search_snapshot import SearchSnapshot
from wqb_agent.state import Experiment
from wqb_agent.trial_ledger import TrialLedger
from wqb_agent.workspace_snapshot import read_workspace_snapshot


class TestRuntimeSafety(unittest.TestCase):

    def test_snapshot_exposes_ledger_lifecycle_evidence_separately(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "trial_ledger.jsonl"), "w", encoding="utf-8") as handle:
                handle.write(json.dumps({"proposal_id": "p1", "phase": "simulation_committed"}) + "\n")
                handle.write(json.dumps({"proposal_id": "p1", "phase": "simulation_submitted"}) + "\n")
                handle.write(json.dumps({"proposal_id": "p1", "phase": "simulation_settled"}) + "\n")
            snapshot = read_workspace_snapshot(tmp)
        self.assertIn("p1", snapshot.ledger.committed)
        self.assertIn("p1", snapshot.ledger.submitted)
        self.assertIn("p1", snapshot.ledger.simulation_settled)

    def test_audit_reports_trajectory_lifecycle_without_ledger_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "trajectory.jsonl"), "w", encoding="utf-8") as handle:
                handle.write(json.dumps({
                    "proposal_id": "p", "phase": "simulation_committed",
                }) + "\n")
            result = audit_state(tmp)
        self.assertFalse(result["ok"])
        self.assertIn("trajectory_lifecycle_missing_ledger", result["errors"])
        self.assertNotIn("ledger_lifecycle_missing_trajectory", result["errors"])
        self.assertEqual(result["committed"], 0)
        self.assertEqual(result["trajectory_observed_committed"], 1)

    def test_audit_reports_ledger_lifecycle_without_trajectory_projection(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "trial_ledger.jsonl"), "w", encoding="utf-8") as handle:
                handle.write(json.dumps({
                    "proposal_id": "p", "phase": "simulation_committed",
                }) + "\n")
            result = audit_state(tmp)
        self.assertFalse(result["ok"])
        self.assertIn("ledger_lifecycle_missing_trajectory", result["errors"])
        self.assertNotIn("trajectory_lifecycle_missing_ledger", result["errors"])
        self.assertEqual(result["committed"], 1)
        self.assertEqual(result["trajectory_observed_committed"], 0)

    def test_audit_findings_identify_lifecycle_phase_source_and_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "trajectory.jsonl"), "w", encoding="utf-8") as handle:
                handle.write(json.dumps({
                    "proposal_id": "p", "phase": "simulation_submitted",
                }) + "\n")
            result = audit_state(tmp)
        finding = next(item for item in result["findings"]
                       if item["code"] == "trajectory_lifecycle_missing_ledger")
        self.assertTrue(result["blocking"])
        self.assertEqual(finding["source"], "trajectory→trial_ledger")
        self.assertEqual(finding["phase"], "simulation_submitted")
        self.assertEqual(finding["proposal_ids"], ["p"])

    def test_audit_compares_ledger_phase_to_same_trajectory_projection(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "trajectory.jsonl"), "w", encoding="utf-8") as handle:
                handle.write(json.dumps({"id": "trial-1", "proposal_id": "p"}) + "\n")
            with open(os.path.join(tmp, "trial_ledger.jsonl"), "w", encoding="utf-8") as handle:
                for phase in ("simulation_committed", "simulation_submitted"):
                    handle.write(json.dumps({"proposal_id": "p", "phase": phase}) + "\n")
            result = audit_state(tmp)
        findings = [item for item in result["findings"]
                    if item["code"] == "ledger_lifecycle_missing_trajectory"]
        self.assertEqual(
            [(item["phase"], item["proposal_ids"]) for item in findings],
            [("simulation_committed", ["p"]), ("simulation_submitted", ["p"])],
        )

    def test_audit_requires_exact_phase_continuity(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "trial_ledger.jsonl"), "w", encoding="utf-8") as handle:
                handle.write(json.dumps({
                    "proposal_id": "p", "phase": "simulation_settled",
                }) + "\n")
            result = audit_state(tmp)
        self.assertIn("lifecycle_order", result["errors"])

    def test_audit_findings_have_one_canonical_order_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "trial_ledger.jsonl"), "w", encoding="utf-8") as handle:
                handle.write(json.dumps({
                    "proposal_id": "p", "phase": "simulation_submitted",
                }) + "\n")
            result = audit_state(tmp)
        codes = [item["code"] for item in result["findings"]]
        self.assertEqual(codes.count("ledger_lifecycle_order"), 1)
        self.assertNotIn("lifecycle_order", codes)

    def test_audit_findings_sort_proposal_ids_deterministically(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "validation_reports.jsonl"), "w", encoding="utf-8") as handle:
                for proposal_id in ("p2", "p1"):
                    handle.write(json.dumps({"parent_id": proposal_id}) + "\n")
            result = audit_state(tmp)
        findings = [item for item in result["findings"]
                    if item["code"] == "orphan_validation_parent"]
        self.assertEqual([item["proposal_ids"] for item in findings], [["p1"], ["p2"]])

    def test_audit_and_doctor_surface_malformed_jsonl_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "trial_ledger.jsonl"), "w", encoding="utf-8") as handle:
                handle.write("not json\n")
            result = audit_state(tmp)
            doctor = run_doctor({"simulation": {}, "agent": {"state_dir": tmp}}, offline=True)
        self.assertIn("malformed_ledger_evidence", result["errors"])
        self.assertTrue(any(
            item["code"] == "LEDGER_EVIDENCE_DEGRADED"
            for item in doctor["diagnostics"]
        ))

    def test_malformed_validation_is_degraded_warning_not_lifecycle_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "validation_reports.jsonl"), "w", encoding="utf-8") as handle:
                handle.write("not json\n")
            result = audit_state(tmp)
        self.assertTrue(result["ok"])
        self.assertNotIn("malformed_validation_evidence", result["errors"])
        finding = next(item for item in result["findings"]
                       if item["code"] == "malformed_validation_evidence")
        self.assertEqual(finding["severity"], "WARN")
        self.assertIn("malformed_validation_evidence", result["warnings"])

    def test_unknown_ledger_schema_is_distinguished_from_unknown_phase(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "trial_ledger.jsonl"), "w", encoding="utf-8") as handle:
                handle.write(json.dumps({
                    "schema_version": 99,
                    "proposal_id": "p",
                    "phase": "future_phase",
                }) + "\n")
            result = audit_state(tmp)
        self.assertIn("unsupported_schema", result["errors"])
        self.assertIn("unknown_ledger_phase", result["errors"])
        finding = next(item for item in result["findings"]
                       if item["code"] == "unsupported_schema")
        self.assertEqual(finding["source"], "trial_ledger")
        self.assertEqual(finding["rows"], 1)

    def test_known_result_records_ledger_before_trajectory(self):
        """已知结果必须先写生命周期证据，再落 append-only trajectory。"""
        events = []
        agent = Agent.__new__(Agent)
        agent.trajectory = SimpleNamespace(
            experiments=[],
            add=lambda experiment: events.append("trajectory"),
        )
        agent.reflector = SimpleNamespace(
            _classify=lambda experiment: {"label": "BASELINE", "reason": "test"},
        )
        agent.quality_policy = {}
        agent._record_trial_phase = lambda experiment, phase, **kwargs: events.append(
            f"ledger:{phase}"
        )
        agent._update_search_lifecycle = lambda experiment, outcome=None: None
        agent._print_experiment = lambda experiment: None
        exp = Experiment(1, "h", "rank(field)", {}, [])
        exp.status = "DONE"
        exp.metrics = {"checks": []}
        exp.health = None
        exp.error = None
        agent._record_live_result(exp)
        self.assertLess(
            events.index("ledger:simulation_settled"),
            events.index("trajectory"),
        )

    def test_schema_migration_is_idempotent(self):
        legacy = {"schema_version": 1, "candidates": []}
        once = migrate_artifact("submission_pool", legacy)
        twice = migrate_artifact("submission_pool", once)
        self.assertEqual(once, twice)
        self.assertEqual(once["schema_version"], CURRENT_SCHEMA_VERSION)
        self.assertIn("created_by_version", once)

    def test_registry_covers_persistent_artifacts(self):
        for name in ("trajectory", "trial_ledger", "checkpoint", "validation",
                     "submission_pool", "fields_cache", "evidence_cache", "active_snapshot",
                     "simulation_results", "memory", "search_snapshot", "validation_plan"):
            self.assertIn(name, ARTIFACT_SCHEMAS)

    def test_platform_fixtures_are_small_anonymous_and_not_live_capability(self):
        fixture_dir = os.path.join(os.path.dirname(__file__), "fixtures", "brain")
        expected = {
            "authentication.json", "simulation_progress.json", "alpha.json",
            "aggregates.json", "alpha_check.json", "pnl.json",
            "data_fields.json", "data_sets.json", "operators.json",
            "self_correlation.json", "retry_after.json",
        }
        actual = {name for name in os.listdir(fixture_dir) if name.endswith(".json")}
        self.assertEqual(actual, expected)
        forbidden_keys = {"password", "secret", "authorization", "cookie"}

        def walk(value):
            if isinstance(value, dict):
                for key, nested in value.items():
                    self.assertNotIn(str(key).lower(), forbidden_keys)
                    yield from walk(nested)
            elif isinstance(value, list):
                for nested in value:
                    yield from walk(nested)

        for name in sorted(expected):
            with open(os.path.join(fixture_dir, name), encoding="utf-8") as handle:
                payload = json.load(handle)
            list(walk(payload))
        with open(os.path.join(fixture_dir, "pnl.json"), encoding="utf-8") as handle:
            pnl_payload = json.load(handle)
        self.assertEqual(
            extract_behavior_series({"pnl": pnl_payload["pnl"]})["availability"],
            "UNAVAILABLE",
        )
        with open(os.path.join(fixture_dir, "retry_after.json"), encoding="utf-8") as handle:
            retry_payload = json.load(handle)
        self.assertEqual(retry_after_seconds(retry_payload["headers"]), 2.0)

    def test_legacy_v2_and_current_migrations_are_idempotent(self):
        for payload in ({"schema_version": 2}, {"schema_version": CURRENT_SCHEMA_VERSION,
                                                  "created_by_version": "alpha-factory"}):
            migrated = migrate_artifact("checkpoint", payload)
            self.assertEqual(migrated, migrate_artifact("checkpoint", migrated))

    def test_doctor_is_local_and_reports_capability_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = run_doctor({"simulation": {}, "agent": {"state_dir": tmp}}, offline=True)
            self.assertTrue(result["config_valid"])
            self.assertFalse(result["ledger_readable"])
            self.assertEqual(result["ledger_status"], "MISSING")
            self.assertEqual(result["pnl_capability"], "UNAVAILABLE")
            self.assertEqual(result["incremental_capability"], "UNAVAILABLE")

    def test_doctor_and_audit_are_network_free(self):
        with tempfile.TemporaryDirectory() as tmp, patch(
            "requests.Session", side_effect=AssertionError("网络调用不应发生")
        ):
            result = run_doctor({"simulation": {}, "agent": {"state_dir": tmp}}, offline=True)
            self.assertTrue(result["config_valid"])
            self.assertTrue(audit_state(tmp)["ok"])

    def test_doctor_uses_example_config_on_fresh_checkout(self):
        with tempfile.TemporaryDirectory() as tmp, patch(
            "sys.argv", ["main.py", "--doctor", "--offline",
                          "--config", os.path.join(tmp, "missing.json"),
                          "--state-dir", tmp]
        ):
            output = io.StringIO()
            with redirect_stdout(output):
                main_entry.main()
            payload = json.loads(output.getvalue())
            self.assertTrue(payload["config_valid"])
            self.assertEqual(payload["state_dir"], tmp)

    def test_offline_flag_cannot_enter_production_path(self):
        with patch("sys.argv", ["main.py", "--offline", "--run-proposals"]), \
             contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                main_entry.main()
        self.assertEqual(raised.exception.code, 2)

    def test_audit_detects_orphan_submission(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "submission_pool.json"), "w", encoding="utf-8") as handle:
                json.dump({"candidates": [{"alpha_id": "orphan"}]}, handle)
            result = audit_state(tmp)
            self.assertFalse(result["ok"])
            self.assertIn("orphan_submission", result["errors"])

    def test_audit_does_not_match_alpha_id_to_proposal_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "trajectory.jsonl"), "w", encoding="utf-8") as handle:
                handle.write(json.dumps({"id": "trial-1", "proposal_id": "p1"}) + "\n")
            with open(os.path.join(tmp, "submission_pool.json"), "w", encoding="utf-8") as handle:
                json.dump({"candidates": [{"alpha_id": "p1"}]}, handle)
            result = audit_state(tmp)
        self.assertFalse(result["ok"])
        self.assertIn("orphan_submission", result["errors"])

    def test_trial_ledger_summary_separates_exact_and_cumulative_submission(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "trial_ledger.jsonl"), "w", encoding="utf-8") as handle:
                for phase in ("simulation_submitted", "simulation_settled"):
                    handle.write(json.dumps({"proposal_id": "p", "phase": phase}) + "\n")
            snapshot = read_workspace_snapshot(tmp)
        self.assertEqual(snapshot.ledger.simulation_submitted, frozenset({"p"}))
        self.assertEqual(snapshot.ledger.submitted, frozenset({"p"}))

    def test_lifecycle_reducer_keeps_early_arm_and_reports_drift(self):
        rows = {
            "p": [
                {"proposal_id": "p", "candidate_id": "c1",
                 "phase": "simulation_committed", "dataset_family": ["pv1"],
                 "template_family": "mechanism-a", "research_role": "EXPLOIT"},
                {"proposal_id": "p", "candidate_id": "c1",
                 "phase": "simulation_settled", "outcome": "SKIPPED_UNKNOWN",
                 "dataset_family": [], "template_family": "unknown"},
            ]
        }
        proposals = TrialLedger._lifecycle_proposals(rows)
        self.assertEqual(proposals["p"]["arm"], "pv1::mechanism-a")
        self.assertEqual(proposals["p"]["research_role"], "EXPLOIT")
        self.assertEqual(proposals["p"]["identity_drift"], ())

        rows["p"][1]["candidate_id"] = "c2"
        rows["p"][1]["dataset_family"] = ["pv2"]
        proposals = TrialLedger._lifecycle_proposals(rows)
        self.assertEqual(set(proposals["p"]["identity_drift"]), {
            "LIFECYCLE_ARM_DRIFT", "LIFECYCLE_CANDIDATE_ID_DRIFT",
        })

    def test_search_snapshot_uses_canonical_arm_after_sparse_terminal_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = TrialLedger(os.path.join(tmp, "trial_ledger.jsonl"))
            early = {
                "proposal_id": "p", "candidate_id": "c1",
                "dataset_family": ["pv1"], "template_family": "trend",
                "research_role": "EXPLOIT", "status": "RUNNING",
            }
            ledger.record(early, "simulation_committed", outcome="COMMITTED")
            ledger.record(
                {"proposal_id": "p", "status": "SKIPPED_UNKNOWN"},
                "simulation_settled", outcome="SKIPPED_UNKNOWN",
            )

            snapshot = SearchSnapshot.from_sources([], ledger.summarize())

        self.assertIn("pv1::trend", snapshot["arms"])
        self.assertNotIn("unknown-dataset::unknown-mechanism", snapshot["arms"])
        self.assertEqual(snapshot["proposals"]["p"]["arm"], "pv1::trend")

    def test_audit_reports_exact_parent_identity_violations(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = {
                "id": "parent-a", "round": 1, "hypothesis_id": "h",
                "expression": "rank(close)", "settings": {},
                "fields_used": ["close"], "datasets": ["pv1"],
                "status": "DONE", "metrics": {"fitness": 1.0},
            }
            child = {
                "id": "child-a", "round": 2, "hypothesis_id": "h2",
                "expression": "rank(volume)", "settings": {},
                "fields_used": ["volume"], "datasets": ["pv1"],
                "status": "DONE", "metrics": {"fitness": 0.5},
                "experiment_stage": "CHILD", "parent_id": "parent-a",
                "parent_expression": "rank(open)",
            }
            with open(os.path.join(tmp, "trajectory.jsonl"), "w", encoding="utf-8") as handle:
                handle.write(json.dumps(parent) + "\n")
                handle.write(json.dumps(child) + "\n")
            result = audit_state(tmp)
        self.assertFalse(result["ok"])
        self.assertIn("PARENT_EXPRESSION_MISMATCH", result["errors"])

    def test_audit_reports_lifecycle_candidate_and_arm_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            rows = [
                {"proposal_id": "p", "candidate_id": "c1",
                 "phase": "simulation_committed", "dataset_family": ["pv1"],
                 "template_family": "a"},
                {"proposal_id": "p", "candidate_id": "c2",
                 "phase": "simulation_settled", "outcome": "SKIPPED_STALE",
                 "dataset_family": ["pv2"], "template_family": "b"},
            ]
            with open(os.path.join(tmp, "trial_ledger.jsonl"), "w", encoding="utf-8") as handle:
                for row in rows:
                    handle.write(json.dumps(row) + "\n")
            result = audit_state(tmp)
        self.assertFalse(result["ok"])
        self.assertIn("LIFECYCLE_CANDIDATE_ID_DRIFT", result["errors"])
        self.assertIn("LIFECYCLE_ARM_DRIFT", result["errors"])

    def test_audit_reports_proposal_id_rebind_without_private_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "trial_ledger.jsonl"), "w", encoding="utf-8") as handle:
                handle.write(json.dumps({
                    "proposal_id": "p-rebind", "phase": "candidate_rejected",
                    "reason_code": "PROPOSAL_ID_REBIND",
                    "reason": "opaque binding conflict",
                }) + "\n")
            result = audit_state(tmp)
        self.assertIn("proposal_id_execution_rebind", result["errors"])
        self.assertNotIn("rank(", json.dumps(result, ensure_ascii=False))

    def test_valid_non_simulation_ledger_phases_are_not_lifecycle_degraded(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "trial_ledger.jsonl"), "w", encoding="utf-8") as handle:
                for phase in (
                    "candidate_generated", "candidate_rejected",
                    "preflight_accepted", "candidate_admitted",
                ):
                    handle.write(json.dumps({"candidate_id": "c", "phase": phase}) + "\n")
            snapshot = read_workspace_snapshot(tmp)
        self.assertEqual(snapshot.ledger.unknown_phase_rows, 0)
        self.assertEqual(snapshot.ledger.incomplete_rows, 0)

    def test_audit_detects_duplicate_settlement_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "trial_ledger.jsonl"), "w", encoding="utf-8") as handle:
                row = {"phase": "research_outcome_settled", "proposal_id": "p",
                       "settlement": {"settlement_id": "same"}}
                handle.write(json.dumps(row) + "\n")
                handle.write(json.dumps(row) + "\n")
            result = audit_state(tmp)
            self.assertFalse(result["ok"])
            self.assertIn("duplicate_settlement", result["errors"])

    def test_audit_accepts_same_settlement_in_ledger_and_trajectory(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "trajectory.jsonl"), "w", encoding="utf-8") as handle:
                handle.write(json.dumps({
                    "proposal_id": "p", "phase": "simulation_committed",
                }) + "\n")
                handle.write(json.dumps({
                    "proposal_id": "p", "phase": "simulation_submitted",
                }) + "\n")
                handle.write(json.dumps({
                    "proposal_id": "p", "phase": "simulation_settled",
                }) + "\n")
                handle.write(json.dumps({
                    "proposal_id": "p", "phase": "research_outcome_settled",
                    "settlement": {"settlement_id": "same"},
                }) + "\n")
            with open(os.path.join(tmp, "trial_ledger.jsonl"), "w", encoding="utf-8") as handle:
                for phase in ("simulation_committed", "simulation_submitted", "simulation_settled"):
                    handle.write(json.dumps({"proposal_id": "p", "phase": phase}) + "\n")
                handle.write(json.dumps({
                    "proposal_id": "p", "phase": "research_outcome_settled",
                    "settlement": {"settlement_id": "same"},
                }) + "\n")
            result = audit_state(tmp)
        self.assertTrue(result["ok"], result["errors"])

    def test_audit_detects_phantom_checkpoint_reservation(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "round_1.checkpoint.json"), "w", encoding="utf-8") as handle:
                json.dump({"complete": False, "experiments": [{"status": "PENDING"}]}, handle)
            result = audit_state(tmp)
            self.assertFalse(result["ok"])
            self.assertIn("phantom_reservation", result["errors"])

    def test_audit_detects_invalid_lifecycle_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "trial_ledger.jsonl"), "w", encoding="utf-8") as handle:
                handle.write(json.dumps({"phase": "simulation_submitted", "proposal_id": "p"}) + "\n")
            result = audit_state(tmp)
            self.assertFalse(result["ok"])
            self.assertIn("lifecycle_order", result["errors"])

    def test_audit_detects_checkpoint_ledger_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "round_1.checkpoint.json"), "w", encoding="utf-8") as handle:
                json.dump({"schema_version": 1, "round_no": 1,
                           "hypothesis": {}, "complete": True, "experiments": [{
                    "id": "e1", "round": 1, "hypothesis_id": "h",
                    "expression": "rank(low)", "settings": {},
                    "fields_used": ["low"], "status": "DONE", "proposal_id": "p"
                }]}, handle)
            with open(os.path.join(tmp, "trial_ledger.jsonl"), "w", encoding="utf-8") as handle:
                handle.write(json.dumps({
                    "phase": "simulation_committed", "proposal_id": "p"
                }) + "\n")
            result = audit_state(tmp)
            self.assertFalse(result["ok"])
            self.assertIn("checkpoint_ledger_mismatch", result["errors"])

    def test_audit_reports_missing_ledger_for_terminal_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "round_1.checkpoint.json"), "w", encoding="utf-8") as handle:
                json.dump({"complete": True, "experiments": [{
                    "status": "DONE", "proposal_id": "p"
                }]}, handle)
            result = audit_state(tmp)
            self.assertFalse(result["ok"])
            self.assertIn("ledger_missing", result["errors"])
            self.assertIn("checkpoint_ledger_mismatch", result["errors"])

    def test_audit_allows_ephemeral_lifecycle_without_local_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "round_1.checkpoint.json"), "w", encoding="utf-8") as handle:
                json.dump({"schema_version": 1, "round_no": 1,
                           "hypothesis": {}, "complete": True, "experiments": [{
                    "id": "e1", "round": 1, "hypothesis_id": "h",
                    "expression": "rank(low)", "settings": {},
                    "fields_used": ["low"], "status": "DONE", "proposal_id": "p"
                }]}, handle)
            result = audit_state(tmp, lifecycle_persistent=False)
            self.assertTrue(result["ok"])
            self.assertNotIn("ledger_missing", result["errors"])
            self.assertNotIn("checkpoint_ledger_mismatch", result["errors"])

    def test_audit_detects_orphan_validation_parent(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "validation_reports.jsonl"), "w", encoding="utf-8") as handle:
                handle.write(json.dumps({"parent_id": "missing", "plan_id": "p",
                                         "report": {"plan_id": "p"}}) + "\n")
            result = audit_state(tmp)
            self.assertFalse(result["ok"])
            self.assertIn("orphan_validation_parent", result["errors"])

    def test_audit_detects_unknown_job_released_from_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "round_1.checkpoint.json"), "w", encoding="utf-8") as handle:
                json.dump({"complete": False, "experiments": [{
                    "status": "SUBMIT_UNKNOWN", "proposal_id": "p", "budget_held": False
                }]}, handle)
            result = audit_state(tmp)
            self.assertFalse(result["ok"])
            self.assertIn("unknown_not_budget_held", result["errors"])

    def test_audit_detects_duplicate_unresolved_submission_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            row = {
                "id": "e1", "round": 1, "hypothesis_id": "h",
                "expression": "rank(low)", "settings": {},
                "fields_used": ["low"], "status": "SUBMIT_UNKNOWN",
                "proposal_id": "p1",
            }
            for round_no in (1, 2):
                with open(
                    os.path.join(tmp, f"round_{round_no}.checkpoint.json"),
                    "w", encoding="utf-8",
                ) as handle:
                    payload = {
                        "schema_version": 1, "round_no": round_no,
                        "hypothesis": {}, "complete": False,
                        "experiments": [dict(row, round=round_no)],
                    }
                    json.dump(payload, handle)
            result = audit_state(tmp)
        self.assertFalse(result["ok"])
        self.assertIn("duplicate_unresolved_submission_identity", result["errors"])

    def test_audit_detects_trajectory_execution_identity_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            rows = [
                {"id": "e1", "round": 1, "hypothesis_id": "h",
                 "expression": "rank(low)", "settings": {},
                 "fields_used": ["low"], "status": "DONE"},
                {"id": "e1", "round": 1, "hypothesis_id": "h",
                 "expression": "rank(high)", "settings": {},
                 "fields_used": ["high"], "status": "DONE"},
            ]
            with open(os.path.join(tmp, "trajectory.jsonl"), "w", encoding="utf-8") as handle:
                for row in rows:
                    handle.write(json.dumps(row) + "\n")
            result = audit_state(tmp)
        self.assertFalse(result["ok"])
        self.assertIn("trajectory_execution_identity_mismatch", result["errors"])

    def test_audit_detects_terminal_reserved_arm(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "round_1.checkpoint.json"), "w", encoding="utf-8") as handle:
                json.dump({"complete": True, "experiments": [{
                    "status": "DONE", "proposal_id": "p", "reserved": True
                }]}, handle)
            result = audit_state(tmp)
            self.assertFalse(result["ok"])
            self.assertIn("terminal_occupies_arm", result["errors"])
