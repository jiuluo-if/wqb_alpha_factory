import unittest
from unittest.mock import Mock

from wqb_agent.client import (
    WQBAuthError,
    WQBClient,
    WQBCorrelationPendingError,
    WQBNotFoundError,
    WQBRateLimitError,
    WQBSimulationError,
    WQBTimeoutError,
)
from wqb_agent.optimization_interfaces import (
    ClientOptimizationEvidenceProvider,
    diagnose_optimization,
)
from wqb_agent.pnl import PnlAdapter, decode_recordset


class TestOptimizationInterfaces(unittest.TestCase):
    def test_client_exposes_only_verified_pnl_recordset(self):
        client = WQBClient.__new__(WQBClient)
        client.base_url = "https://api.worldquantbrain.com"
        response = Mock()
        response.json.return_value = {"records": [[1.0]]}
        client._request = Mock(return_value=response)
        self.assertEqual(client.get_pnl("a1"), {"records": [[1.0]]})
        client._request.assert_called_once()
        with self.assertRaises(ValueError):
            client.get_recordset("a1", "daily-pnl")

    def test_decode_recordset_uses_schema_names_not_column_positions(self):
        payload = {
            "schema": {"properties": [{"name": "pnl"}, {"name": "date"}]},
            "records": [[1.5, "2026-01-02"]],
        }
        self.assertEqual(
            decode_recordset(payload), [{"pnl": 1.5, "date": "2026-01-02"}]
        )

    def test_pnl_adapter_accepts_schema_encoded_records(self):
        payload = {
            "schema": {"properties": [{"name": "date"}, {"name": "pnl"}]},
            "records": [["2026-01-01", 0.1], ["2026-01-02", 0.2]],
        }
        result = PnlAdapter("NOT_LIVE_VERIFIED").analyze(payload)
        self.assertEqual(result["status"], "UNAVAILABLE")

    def test_client_provider_collects_only_readonly_optimization_evidence(self):
        client = Mock()
        client.get_alpha.return_value = {"is": {"sharpe": 1.2}}
        client.get_aggregates.return_value = {"is": {"yearlyData": []}}
        client.get_pnl.return_value = {"records": []}
        client.get_self_correlation.return_value = {"status": "PASS"}

        evidence = ClientOptimizationEvidenceProvider(client).collect("a1")

        self.assertEqual(evidence.alpha_id, "a1")
        self.assertEqual(evidence.alpha_detail["is"]["sharpe"], 1.2)
        client.get_alpha.assert_called_once_with("a1")
        client.get_aggregates.assert_called_once_with("a1")
        client.get_pnl.assert_called_once_with("a1")
        client.get_self_correlation.assert_called_once_with("a1")

    def test_client_provider_preserves_other_slots_when_one_capability_is_unavailable(self):
        client = Mock()
        client.get_alpha.return_value = {"is": {"sharpe": 1.2}}
        client.get_aggregates.side_effect = NotImplementedError("not supported")
        client.get_pnl.return_value = {"records": []}
        client.get_self_correlation.return_value = {"status": "PASS"}

        evidence = ClientOptimizationEvidenceProvider(client).collect("a1")

        self.assertEqual(evidence.alpha_detail["is"]["sharpe"], 1.2)
        self.assertEqual(evidence.aggregates["status"], "UNAVAILABLE")
        self.assertEqual(evidence.aggregates["availability"], "UNAVAILABLE")
        self.assertEqual(evidence.status["aggregates"], "UNAVAILABLE")
        self.assertEqual(evidence.availability["alpha_detail"], "AVAILABLE")
        self.assertEqual(evidence.pnl, {"records": []})
        self.assertEqual(evidence.self_correlation["status"], "PASS")

    def test_client_provider_rethrows_classified_transport_errors(self):
        client = Mock()
        error = RuntimeError("AUTH failed")
        error.kind = "AUTH"
        client.get_alpha.side_effect = error

        with self.assertRaises(RuntimeError) as raised:
            ClientOptimizationEvidenceProvider(client).collect("a1")
        self.assertIs(raised.exception, error)

    def test_alpha_detail_is_required_anchor_and_stops_followup_reads_on_404(self):
        client = Mock()
        client.get_alpha.side_effect = WQBNotFoundError("missing")

        with self.assertRaises(WQBNotFoundError):
            ClientOptimizationEvidenceProvider(client).collect("a1")
        client.get_aggregates.assert_not_called()
        client.get_pnl.assert_not_called()
        client.get_self_correlation.assert_not_called()

    def test_existing_alpha_allows_optional_aggregates_404_to_be_unavailable(self):
        client = Mock()
        client.get_alpha.return_value = {"id": "a1", "is": {"sharpe": 1.2}}
        client.get_aggregates.side_effect = WQBNotFoundError("optional endpoint")
        client.get_pnl.return_value = {"records": []}
        client.get_self_correlation.return_value = {"status": "PASS"}

        evidence = ClientOptimizationEvidenceProvider(client).collect("a1")

        self.assertEqual(evidence.status["aggregates"], "UNAVAILABLE")
        self.assertEqual(evidence.aggregates["reason_code"], "CAPABILITY_UNAVAILABLE")
        self.assertEqual(evidence.pnl, {"records": []})

    def test_optional_infrastructure_errors_are_not_missing_evidence(self):
        for error_type in (WQBAuthError, WQBRateLimitError, WQBSimulationError, WQBTimeoutError):
            with self.subTest(error=error_type.__name__):
                client = Mock()
                client.get_alpha.return_value = {"id": "a1"}
                client.get_aggregates.side_effect = error_type("failure")
                with self.assertRaises(error_type):
                    ClientOptimizationEvidenceProvider(client).collect("a1")

    def test_explicit_correlation_pending_is_unknown_but_capability_available(self):
        client = Mock()
        client.get_alpha.return_value = {"id": "a1"}
        client.get_aggregates.return_value = {"is": {"yearlyData": []}}
        client.get_pnl.return_value = {"records": []}
        client.get_self_correlation.side_effect = WQBCorrelationPendingError("pending")

        evidence = ClientOptimizationEvidenceProvider(client).collect("a1")

        self.assertEqual(evidence.status["self_correlation"], "UNKNOWN")
        self.assertEqual(evidence.availability["self_correlation"], "AVAILABLE")

    def test_diagnosis_distinguishes_low_sharpe_from_turnover(self):
        low_signal = diagnose_optimization(
            {"sharpe": 0.4, "returns": 0.03, "turnover": 0.08}
        )
        high_turnover = diagnose_optimization(
            {"sharpe": 1.4, "returns": 0.08, "turnover": 0.30}
        )
        self.assertEqual(low_signal["primary_problem"], "LOW_SHARPE")
        self.assertEqual(high_turnover["primary_problem"], "HIGH_TURNOVER")

if __name__ == "__main__":
    unittest.main()
