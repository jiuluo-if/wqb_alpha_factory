import unittest
from unittest.mock import Mock

from wqb_agent.client import WQBNotFoundError
from wqb_agent.remote_evidence import RemoteAlphaEvidenceProvider, decode_recordset


class TestRemoteEvidence(unittest.TestCase):
    def test_client_provider_default_collection_is_cheap_alpha_detail_only(self):
        client = Mock()
        client.get_alpha.return_value = {"is": {"sharpe": 1.2}}

        evidence = RemoteAlphaEvidenceProvider(client).collect("a1")

        self.assertEqual(evidence.alpha_id, "a1")
        self.assertEqual(evidence.alpha_detail["is"]["sharpe"], 1.2)
        client.get_alpha.assert_called_once_with("a1")
        client.list_alpha_recordsets.assert_not_called()
        client.get_aggregates.assert_not_called()
        client.get_pnl.assert_not_called()
        client.get_self_correlation.assert_not_called()

    def test_explicit_recordsets_discover_once_and_decode_without_recalculation(self):
        client = Mock()
        client.get_alpha.return_value = {"is": {"sharpe": 1.2}}
        client.list_alpha_recordsets.return_value = [
            {"name": "yearly-stats", "title": "Yearly Stats"},
            {"name": "coverage", "title": "Coverage"},
        ]
        client.get_recordset.side_effect = [
            {"schema": {"properties": {"year": {}, "sharpe": {}}},
             "records": [[2025, 1.2]]},
            {"schema": {"properties": {"date": {}, "coverage": {}}},
             "records": []},
        ]

        evidence = RemoteAlphaEvidenceProvider(client).collect(
            "a1", recordsets=["yearly-stats", "coverage"],
        )

        self.assertEqual(evidence.recordsets["yearly-stats"]["columns"], ["year", "sharpe"])
        self.assertEqual(evidence.recordsets["yearly-stats"]["rows"], [{"year": 2025, "sharpe": 1.2}])
        self.assertEqual(evidence.recordsets["coverage"]["status"], "EMPTY")
        client.list_alpha_recordsets.assert_called_once_with("a1")
        self.assertEqual(client.get_recordset.call_count, 2)
        self.assertTrue(all(call.kwargs["available"] for call in client.get_recordset.call_args_list))

    def test_decode_recordset_preserves_official_numeric_values_and_empty_states(self):
        decoded = decode_recordset({
            "schema": {"properties": {"metric": {}, "value": {}}},
            "records": [["sharpe", 1.23456789]],
        })
        self.assertEqual(decoded["status"], "AVAILABLE")
        self.assertEqual(decoded["rows"], [{"metric": "sharpe", "value": 1.23456789}])
        self.assertEqual(decode_recordset({
            "schema": {"properties": {"metric": {}}}, "records": [],
        })["status"], "EMPTY")
        self.assertEqual(decode_recordset(None)["status"], "PENDING_DATA")

    def test_selected_recordset_not_discovered_is_unavailable_without_read(self):
        client = Mock()
        client.get_alpha.return_value = {"id": "a1"}
        client.list_alpha_recordsets.return_value = []
        evidence = RemoteAlphaEvidenceProvider(client).collect(
            "a1", recordsets=["coverage"],
        )
        self.assertEqual(evidence.recordsets["coverage"]["status"], "UNAVAILABLE")
        client.get_recordset.assert_not_called()

    def test_client_provider_rethrows_classified_transport_errors(self):
        client = Mock()
        error = RuntimeError("AUTH failed")
        error.kind = "AUTH"
        client.get_alpha.side_effect = error

        with self.assertRaises(RuntimeError) as raised:
            RemoteAlphaEvidenceProvider(client).collect("a1")
        self.assertIs(raised.exception, error)

    def test_alpha_detail_is_required_anchor_and_stops_followup_reads_on_404(self):
        client = Mock()
        client.get_alpha.side_effect = WQBNotFoundError("missing")

        with self.assertRaises(WQBNotFoundError):
            RemoteAlphaEvidenceProvider(client).collect("a1")
        client.get_aggregates.assert_not_called()
        client.get_pnl.assert_not_called()
        client.get_self_correlation.assert_not_called()

    def test_explicit_recordset_transport_error_is_not_silently_missing(self):
        client = Mock()
        client.get_alpha.return_value = {"id": "a1"}
        client.list_alpha_recordsets.return_value = [{"name": "coverage", "title": "Coverage"}]
        error = RuntimeError("rate limit")
        client.get_recordset.side_effect = error
        with self.assertRaises(RuntimeError) as raised:
            RemoteAlphaEvidenceProvider(client).collect("a1", recordsets=["coverage"])
        self.assertIs(raised.exception, error)

if __name__ == "__main__":
    unittest.main()
