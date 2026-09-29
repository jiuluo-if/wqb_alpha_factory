import unittest
from unittest.mock import Mock, call

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

    def test_compare_alphas_matches_public_evidence_envelope(self):
        client = Mock()
        client.get_alpha.side_effect = [
            {"id": "a1", "is": {"sharpe": 1.2}},
            {"id": "a2", "is": {"sharpe": 0.8}},
        ]

        result = RemoteAlphaEvidenceProvider(client).compare_alphas(["a1", "a2"])

        self.assertEqual(result["source"], "LIVE")
        self.assertEqual(result["status"], "AVAILABLE")
        self.assertEqual(result["evidence_status"], "AVAILABLE")
        self.assertEqual([item["alpha_id"] for item in result["alphas"]], ["a1", "a2"])
        client.get_alpha.assert_has_calls([call("a1"), call("a2")])
        client.list_alpha_recordsets.assert_not_called()
        client.get_aggregates.assert_not_called()
        client.get_pnl.assert_not_called()
        client.get_self_correlation.assert_not_called()

    def test_pairwise_pnl_comparison_uses_overlapping_dates_and_marks_unknown(self):
        client = Mock()
        client.get_pnl.side_effect = [
            {"pnl": [
                {"date": "2024-01-01", "value": 1.0},
                {"date": "2024-01-02", "value": 2.0},
                {"date": "2024-01-03", "value": 3.0},
                {"date": "2024-01-04", "value": 4.0},
            ]},
            {"pnl": [
                {"date": "2024-01-02", "value": 4.0},
                {"date": "2024-01-03", "value": 6.0},
                {"date": "2024-01-04", "value": 8.0},
                {"date": "2024-01-05", "value": 10.0},
            ]},
            {"pnl": [
                {"date": "2024-01-01", "value": 3.0},
            ]},
            {"schema": {"properties": {"date": {}, "pnl": {}}}, "records": [
                ["2024-01-02", 7.0], ["2024-01-03", 5.0], ["2024-01-04", 3.0],
            ]},
        ]

        result = RemoteAlphaEvidenceProvider(client).compare_alphas(
            ["alpha-a", "alpha-b", "alpha-c", "alpha-d"],
            pairwise_pnl=True, max_pairs=2,
        )

        self.assertEqual(result["method"], "PEARSON_DAILY_PNL")
        self.assertEqual(result["source"], "BRAIN_LIVE")
        self.assertEqual(result["total_pair_count"], 6)
        self.assertEqual(result["available_pair_count"], 3)
        self.assertEqual(result["unknown_pair_count"], 3)
        self.assertTrue(result["pairs_truncated"])
        pair = result["strongest_pairs"][0]
        self.assertEqual((pair["alpha_id_a"], pair["alpha_id_b"]), ("alpha-a", "alpha-b"))
        self.assertEqual(pair["correlation"], 1.0)
        self.assertEqual(pair["overlap_count"], 3)
        self.assertEqual(pair["overlap_start"], "2024-01-02")
        self.assertEqual(pair["overlap_end"], "2024-01-04")
        self.assertEqual(result["unknown_pair_reasons"], {"INSUFFICIENT_OVERLAP": 3})
        per_alpha = {row["alpha_id"]: row for row in result["per_alpha_max"]}
        self.assertEqual(per_alpha["alpha-c"]["status"], "AVAILABLE")
        self.assertEqual(per_alpha["alpha-c"]["max_pairwise_status"], "UNKNOWN")
        self.assertIsNone(per_alpha["alpha-c"]["max_abs_correlation"])
        self.assertEqual(per_alpha["alpha-c"]["unknown_pair_count"], 3)
        self.assertEqual(per_alpha["alpha-d"]["max_pairwise_status"], "PARTIAL")
        self.assertIsNone(per_alpha["alpha-d"]["max_abs_correlation"])
        self.assertEqual(
            per_alpha["alpha-d"]["max_known_abs_correlation"], 1.0
        )
        self.assertEqual(
            per_alpha["alpha-d"]["max_known_correlation"], -1.0
        )
        self.assertIn(
            per_alpha["alpha-d"]["max_known_pairwise_alpha_id"],
            {"alpha-a", "alpha-b"},
        )
        self.assertIsNone(per_alpha["alpha-d"]["max_pairwise_alpha_id"])
        client.get_alpha.assert_not_called()
        self.assertEqual(client.get_pnl.call_count, 4)

    def test_pairwise_pnl_marks_constant_series_unknown(self):
        client = Mock()
        client.get_pnl.side_effect = [
            {"pnl": [
                {"date": "2024-03-01", "value": 1.0},
                {"date": "2024-03-02", "value": 2.0},
                {"date": "2024-03-03", "value": 3.0},
            ]},
            {"pnl": [
                {"date": "2024-03-01", "value": 4.0},
                {"date": "2024-03-02", "value": 4.0},
                {"date": "2024-03-03", "value": 4.0},
            ]},
        ]

        result = RemoteAlphaEvidenceProvider(client).compare_alphas(
            ["alpha-trend", "alpha-constant"], pairwise_pnl=True,
        )

        self.assertEqual(result["available_pair_count"], 0)
        self.assertEqual(result["unknown_pair_count"], 1)
        self.assertEqual(result["strongest_pairs"], [])
        self.assertEqual(result["unknown_pair_reasons"], {"ZERO_VARIANCE": 1})

    def test_pairwise_complete_per_alpha_max_names_the_exact_peer(self):
        client = Mock()
        client.get_pnl.side_effect = [
            {"pnl": [
                {"date": "2024-05-01", "value": 1.0},
                {"date": "2024-05-02", "value": 2.0},
                {"date": "2024-05-03", "value": 4.0},
            ]},
            {"pnl": [
                {"date": "2024-05-01", "value": 2.0},
                {"date": "2024-05-02", "value": 4.0},
                {"date": "2024-05-03", "value": 8.0},
            ]},
        ]

        result = RemoteAlphaEvidenceProvider(client).compare_alphas(
            ["alpha-a", "alpha-b"], pairwise_pnl=True,
        )

        rows = {row["alpha_id"]: row for row in result["per_alpha_max"]}
        self.assertEqual(rows["alpha-a"]["max_pairwise_status"], "AVAILABLE")
        self.assertEqual(rows["alpha-a"]["max_abs_correlation"], 1.0)
        self.assertEqual(rows["alpha-a"]["max_pairwise_alpha_id"], "alpha-b")
        self.assertEqual(rows["alpha-a"]["max_overlap_count"], 3)

    def test_pairwise_pnl_caps_candidate_fanout_before_remote_reads(self):
        client = Mock()

        with self.assertRaisesRegex(ValueError, "at most 40 Alpha IDs"):
            RemoteAlphaEvidenceProvider(client).compare_alphas(
                [f"alpha-{index}" for index in range(41)], pairwise_pnl=True,
            )

        client.get_pnl.assert_not_called()

if __name__ == "__main__":
    unittest.main()
