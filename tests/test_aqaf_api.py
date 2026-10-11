import unittest
from datetime import date, timedelta

from tests.test_alpha_quality import _calibration
from wqb_agent import research_api


class _ReadOnlyClient:
    def __init__(self, submitted=True):
        self.submitted = submitted
        self.write_calls = 0

    def get_alpha(self, alpha_id):
        alpha = {
            "id": alpha_id,
            "status": "ACTIVE",
            "expression": "SYNTHETIC_EXPRESSION_SENTINEL",
            "settings": {"region": "USA", "universe": "TOP3000", "delay": 1},
            "is": {"sharpe": 1.2, "fitness": 2.0, "returns": .1,
                   "turnover": .2, "margin": 3.0,
                   "checks": [{"name": "synthetic-check", "result": "PASS"}]},
        }
        if self.submitted:
            alpha["dateSubmitted"] = "2025-01-01T00:00:00Z"
        return alpha

    def get_aggregates(self, alpha_id):
        return {"is": {"yearlyData": []}}

    def get_pnl(self, alpha_id):
        return {"pnl": []}

    def get_self_correlation(self, alpha_id):
        return {"is": {"correlation": .2}}

    def list_alpha_recordsets(self, alpha_id):
        return []

    def get_recordset(self, *args, **kwargs):
        return None


class AqafResearchApiTests(unittest.TestCase):
    def test_checks_pass_does_not_replace_submission_fact(self):
        result = research_api.evaluate_submitted_alpha(
            "synthetic-alpha", client=_ReadOnlyClient(submitted=False),
            calibration=None, as_of=date(2025, 6, 1),
        )
        self.assertEqual(result["submission_status"], "SUBMISSION_UNKNOWN")
        self.assertEqual(result["quality"]["grade"], "U")
        self.assertEqual(result["reason_code"], "SUBMISSION_UNKNOWN")

    def test_conflicting_submission_status_and_date_is_unknown(self):
        class ConflictingClient(_ReadOnlyClient):
            def get_alpha(self, alpha_id):
                alpha = super().get_alpha(alpha_id)
                alpha["status"] = "UNSUBMITTED"
                alpha["dateSubmitted"] = "2025-01-01T00:00:00Z"
                return alpha

        result = research_api.evaluate_submitted_alpha(
            "synthetic-alpha", client=ConflictingClient(submitted=False),
            calibration=None, as_of=date(2025, 6, 1),
        )
        self.assertEqual(result["submission_status"], "SUBMISSION_UNKNOWN")

    def test_date_submitted_authorizes_evaluation_but_missing_units_stay_unknown(self):
        result = research_api.evaluate_submitted_alpha(
            "synthetic-alpha", client=_ReadOnlyClient(), calibration=None,
            as_of=date(2025, 6, 1),
        )
        self.assertEqual(result["submission_status"], "SUBMITTED")
        self.assertEqual(result["quality"]["status"], "UNKNOWN")
        self.assertEqual(result["quality"]["grade"], "U")
        self.assertEqual(result["source"], "BRAIN_LIVE")
        self.assertNotIn("SYNTHETIC_EXPRESSION_SENTINEL", str(result))

    def test_live_facade_scores_complete_synthetic_evidence(self):
        class CompleteClient(_ReadOnlyClient):
            def __init__(self):
                super().__init__(submitted=True)
                self.days = [
                    (date(2024, 1, 1) + timedelta(days=index)).isoformat()
                    for index in range(252)
                ]

            def get_aggregates(self, alpha_id):
                return {"is": {"yearlyData": [
                    {"year": year, "returns": .1, "sharpe": 1.1, "complete": True}
                    for year in (2021, 2022, 2023)
                ]}}

            def get_pnl(self, alpha_id):
                return {"pnl": [
                    {"date": day, "value": (-1.0 if index > 210 else 1.0)}
                    for index, day in enumerate(self.days)
                ]}

        client = CompleteClient()
        result = research_api.evaluate_submitted_alpha(
            "synthetic-alpha", client=client, calibration=_calibration(),
            metric_units={
                "returns": {"unit": "fraction", "source": "SYNTHETIC_SCHEMA"},
                "margin": {"unit": "bp", "source": "SYNTHETIC_SCHEMA"},
                "turnover": {"unit": "fraction", "source": "SYNTHETIC_SCHEMA"},
            },
            expected_trading_dates=client.days,
            expected_trading_dates_source="SYNTHETIC_CALENDAR",
            pnl_unit="SYNTHETIC_UNITS", pnl_unit_source="SYNTHETIC_SCHEMA",
            as_of=date(2025, 6, 1),
        )
        self.assertEqual(result["submission_status"], "SUBMITTED")
        self.assertEqual(result["quality"]["coverage"], 1.0)
        self.assertTrue(all(
            metric["status"] == "AVAILABLE"
            for metric in result["metrics"].values()
        ))

    def test_batch_ids_are_bounded_and_unique(self):
        with self.assertRaisesRegex(ValueError, "unique"):
            research_api.evaluate_submitted_alphas(
                ["a", "a"], client=_ReadOnlyClient(), calibration=None,
            )
        with self.assertRaisesRegex(ValueError, "at most"):
            research_api.evaluate_submitted_alphas(
                [f"a-{index}" for index in range(101)],
                client=_ReadOnlyClient(), calibration=None,
            )

    def test_evaluation_calls_only_read_methods(self):
        client = _ReadOnlyClient()
        research_api.evaluate_submitted_alpha(
            "synthetic-alpha", client=client, calibration=None,
            as_of=date(2025, 6, 1),
        )
        self.assertEqual(client.write_calls, 0)

    def test_failed_brain_check_caps_quality_and_allocation_eligibility(self):
        class FailedCheckClient(_ReadOnlyClient):
            def get_alpha(self, alpha_id):
                alpha = super().get_alpha(alpha_id)
                alpha["is"]["checks"] = [{"name": "synthetic-check", "result": "FAIL"}]
                return alpha

        result = research_api.evaluate_submitted_alpha(
            "synthetic-alpha", client=FailedCheckClient(), calibration=None,
            as_of=date(2025, 6, 1),
        )
        self.assertEqual(result["quality"]["grade"], "U")
        self.assertIn("BRAIN_CHECK_FAILED", result["risk_alerts"])

    def test_quality_tools_are_full_profile_only(self):
        manifest = {item["name"]: item for item in research_api.research_tool_manifest("full")}
        core = {item["name"] for item in research_api.research_tool_manifest("core")}
        for name in (
            "evaluate_submitted_alpha", "evaluate_submitted_alphas",
            "preview_osmosis_allocation", "compare_osmosis_allocation_plans",
        ):
            self.assertEqual(manifest[name]["mode"], "READ_ONLY")
            self.assertNotIn(name, core)


if __name__ == "__main__":
    unittest.main()
