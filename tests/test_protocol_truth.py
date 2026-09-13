import json
import os
import tempfile
import threading
import unittest

from wqb_agent.locking import OwnerBusyError, single_instance_scope
from wqb_agent.protocol import (
    CapabilityStatus,
    endpoint_catalog,
    fixture_capability,
    probe_capability_response,
    retry_after_seconds,
)
from wqb_agent.simulator import Simulator
from wqb_agent.state import Experiment
from wqb_agent.trial_ledger import TrialLedger
from wqb_agent.yearly import build_yearly_evidence

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "brain")


class TestProtocolTruth(unittest.TestCase):
    def test_catalog_separates_official_and_community_capabilities(self):
        catalog = {row["key"]: row for row in endpoint_catalog()}
        self.assertEqual(catalog["authentication"]["status"], "OFFICIAL")
        self.assertEqual(catalog["aggregates"]["status"], "OFFICIAL")
        self.assertEqual(catalog["operators"]["status"], "COMMUNITY_OBSERVED")

    def test_fixture_validation_is_not_live_verification(self):
        with open(os.path.join(FIXTURES, "aggregates.json"), encoding="utf-8") as handle:
            payload = json.load(handle)
        result = fixture_capability("aggregates", payload)
        self.assertEqual(result["status"], CapabilityStatus.FIXTURE_VERIFIED.value)
        self.assertNotEqual(result["status"], CapabilityStatus.LIVE_VERIFIED.value)

    def test_retry_after_accepts_seconds_and_invalid_fails_closed(self):
        self.assertEqual(retry_after_seconds({"Retry-After": "2"}), 2.0)
        self.assertEqual(retry_after_seconds({"Retry-After": "bad"}), 5.0)
        self.assertGreaterEqual(retry_after_seconds({"Retry-After": "-3"}), 1.0)

    def test_probe_response_can_verify_live_shape_without_calling_transport(self):
        result = probe_capability_response(
            "operators", 200, {"operators": [{"name": "rank"}]}
        )
        self.assertEqual(result["status"], "LIVE_VERIFIED")
        unavailable = probe_capability_response("pnl", 404, {})
        self.assertEqual(unavailable["status"], "COMMUNITY_OBSERVED")


class TestYearlyEvidence(unittest.TestCase):
    def test_builds_compact_stability_evidence(self):
        with open(os.path.join(FIXTURES, "aggregates.json"), encoding="utf-8") as handle:
            evidence = build_yearly_evidence(
                json.load(handle), min_sharpe=0.9, min_fitness=0.5, max_turnover=0.7
            )
        self.assertEqual(evidence["status"], "VERIFIED")
        self.assertTrue(evidence["stable"])
        self.assertEqual(evidence["year_count"], 2)
        self.assertEqual(evidence["summary"]["min_sharpe"], 1.1)

    def test_missing_yearly_data_is_unknown(self):
        evidence = build_yearly_evidence({"is": {"yearlyData": []}})
        self.assertEqual(evidence["status"], "UNKNOWN")
        self.assertIsNone(evidence["stable"])

    def test_simulator_consumes_aggregates_after_alpha_completion(self):
        with open(os.path.join(FIXTURES, "aggregates.json"), encoding="utf-8") as handle:
            aggregates = json.load(handle)

        class Client:
            def submit_simulation(self, expression, settings, idempotency_key=None):
                return "/simulations/fixture"

            def poll_progress(self, progress_url, timeout_sec=0, progress_callback=None):
                return "alpha-fixture-1"

            def get_alpha(self, alpha_id):
                return {"is": {"sharpe": 1.2, "fitness": 0.8, "checks": []}}

            def get_aggregates(self, alpha_id):
                return aggregates

        exp = Experiment(1, "h1", "rank(signal)", {}, ["signal"])
        exp.submission_fingerprint = "fp"
        Simulator(Client(), max_concurrent=1, replace_attempts=1,
                  yearly_policy={"min_sharpe": 0.9, "min_fitness": 0.5,
                                 "max_turnover": 0.7}).run([exp])
        self.assertEqual(exp.status, "DONE")
        self.assertEqual(exp.yearly_evidence["status"], "VERIFIED")
        self.assertTrue(exp.yearly_evidence["stable"])


class TestTrialLedger(unittest.TestCase):
    def test_durable_append_requires_explicit_worker_delegation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "trial_ledger.jsonl")
            ledger = TrialLedger(path)
            trial = {"id": "trial-1", "round": 1, "expression": "rank(field)"}
            outcomes = []
            with single_instance_scope(tmp, operation="outer") as owner:
                delegation = owner.delegate_simulation_worker()

                def attempt(capability=None):
                    try:
                        ledger.record(trial, "submitted", delegation=capability)
                    except OwnerBusyError:
                        outcomes.append("busy")
                    else:
                        outcomes.append("written")

                thread = threading.Thread(target=attempt)
                thread.start()
                thread.join()
                thread = threading.Thread(target=attempt, args=(delegation,))
                thread.start()
                thread.join()

            self.assertEqual(outcomes, ["busy", "written"])
    def test_lifecycle_and_group_counts_are_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = TrialLedger(os.path.join(tmp, "trial_ledger.jsonl"))
            exp = Experiment(1, "h1", "rank(signal)", {}, ["signal"], ["dataset"])
            exp.template_family = "quality"
            exp.template_id = "quality_level"
            exp.lineage_id = "lineage-1"
            exp.status = "PENDING"
            for phase in ("generated", "preflight"):
                ledger.record(exp, phase, outcome="ACCEPTED")
            exp.status = "RUNNING"
            ledger.record(exp, "submitted")
            exp.status = "DONE"
            exp.metrics = {"sharpe": 1.25}
            ledger.record(exp, "completed")
            ledger.record(exp, "completed")
            summary = ledger.summarize()
            self.assertEqual(summary["events"], 4)
            self.assertEqual(summary["phase_counts"]["completed"], 1)
            self.assertEqual(summary["trial_counts"]["template_family"]["quality"]["completed"], 1)
            self.assertEqual(summary["trial_counts"]["field"]["signal"]["generated"], 1)
            self.assertEqual(summary["trial_sharpe_count"], 1)

    def test_legacy_experiment_row_without_yearly_evidence_loads(self):
        exp = Experiment.from_dict({
            "id": "old", "round": 1, "hypothesis_id": "h1",
            "expression": "rank(x)", "settings": {}, "fields_used": ["x"],
            "status": "DONE", "metrics": {},
        })
        self.assertIsNone(exp.yearly_evidence)
