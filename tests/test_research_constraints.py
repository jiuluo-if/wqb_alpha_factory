import os
import tempfile
import unittest

from scripts.refresh_self_correlation import _timestamp, select_alpha_ids
from wqb_agent.alpha_factory import AlphaFactory
from wqb_agent.metrics import checks_ready_for_self_correlation_refresh
from wqb_agent.preflight import run_takeover_preflight
from wqb_agent.proposal_contract import _operator_reference, validate_proposal
from wqb_agent.research_guard import (
    is_direction_only_change,
    overfit_expression_reason,
)


def _correlation_row(alpha_id, *, sharpe=1.60, fitness=1.30, returns=0.05,
                     turnover=0.25, drawdown=0.05, checks=None):
    """A complete DONE row: the shared pre-correlation gate needs full evidence."""
    return {
        "status": "DONE", "alpha_id": alpha_id, "created_at": 10,
        "metrics": {
            "sharpe": sharpe, "fitness": fitness, "returns": returns,
            "turnover": turnover, "drawdown": drawdown, "margin": 0.001,
            "checks": checks if checks is not None else [
                {"name": "LOW_SHARPE", "pass": True},
                {"name": "SELF_CORRELATION", "pass": None, "result": "PENDING"},
            ],
        },
        "health": {"ok": True},
    }


class TestResearchConstraints(unittest.TestCase):
    def _proposal(self, **updates):
        proposal = {
            "expression": "rank(ts_delta(cashflow_op, 5))",
            "fields": ["cashflow_op"],
            "datasets": ["fundamental6"],
            "field_understanding": {"cashflow_op": "经营现金流"},
            "field_analysis": {"cashflow_op": {
                "semantic": "经营现金流", "coverage": 1.0,
                "frequency": None, "data_type": "MATRIX",
            }},
            "field_hypothesis_basis": {"cashflow_op": {
                "description": "经营现金流",
                "mechanism": "经营现金流改善代表经营质量改善，预期未来收益更高。",
                "independent_increment": "单字段 baseline",
                "direction": "long",
            }},
            "operator_mapping": "ts_delta 捕捉经营质量更新，rank 做横截面比较。",
            "operator_evidence": {"sha256": "sha", "operators": ["rank", "ts_delta"],
                                  "rationale": "算子对应信息更新机制。"},
            "experiment_question": "经营现金流变化是否带来未来收益增量？",
            "expected_failure_modes": ["现金流覆盖不足", "信号换手过高"],
            "tuning_risk": False,
            "experiment_stage": "BASELINE",
            "change_type": "baseline",
            "economic_mechanism": "经营现金流改善意味着盈利质量和可持续经营能力改善。",
            "direction": "long",
            "direction_transform": {"applied": False, "reason": "直接使用经济方向，不做符号翻转。"},
            "expected_horizon": "20-60 trading days",
            "falsification": "若独立样本 Sharpe<0.8 或健康检查失败则停止。",
            "self_correlation_impact": {
                "expected_effect": "UNKNOWN",
                "basis": "no_live_behavior_series",
                "rationale": "模拟前没有平台结算值，不把结构差异冒充为低自相关。",
                "admission": "REVIEW",
            },
        }
        proposal.update(updates)
        return proposal

    def test_known_multi_leg_decay_blend_is_blocked(self):
        expression = (
            "0.25 * rank(ts_decay_linear(ts_zscore(cashflow_op / enterprise_value, 63), 7))"
            " + 0.55 * rank(ts_decay_linear(ts_zscore(operating_income / equity, 84), 15))"
            " + 0.2 * rank(-ts_decay_linear(ts_zscore(bookvalue_ps, 252), 15))"
        )
        self.assertIsNotNone(overfit_expression_reason(expression))

    def test_pure_direction_change_is_not_a_new_experiment(self):
        self.assertTrue(is_direction_only_change(
            "rank(ts_mean(cashflow_op, 20))",
            "-rank(ts_mean(cashflow_op, 20))",
        ))
        self.assertFalse(is_direction_only_change(
            "rank(ts_mean(cashflow_op, 20))",
            "rank(ts_delta(cashflow_op, 5))",
        ))

    def test_economic_integrity_requires_mechanism_direction_and_corr_impact(self):
        proposal = self._proposal(economic_mechanism=None)
        ok, problems = validate_proposal(
            proposal,
            strict_experiment=True,
            require_economic_integrity=True,
        )
        self.assertFalse(ok)
        self.assertTrue(any("economic_mechanism" in item for item in problems))

    def test_high_predicted_correlation_is_not_admitted(self):
        proposal = self._proposal(self_correlation_impact={
            "expected_effect": "HIGHER",
            "basis": "structural_overlap",
            "rationale": "与 parent 使用相同三腿字段与窗口。",
            "admission": "BLOCK",
        })
        ok, problems = validate_proposal(
            proposal,
            strict_experiment=True,
            require_economic_integrity=True,
        )
        self.assertFalse(ok)
        self.assertTrue(any("self_correlation_impact" in item for item in problems))

    def test_similar_predicted_correlation_requires_review(self):
        proposal = self._proposal(self_correlation_impact={
            "expected_effect": "SIMILAR",
            "basis": "same_behavioral_horizon",
            "rationale": "结构可能与既有 Alpha 使用相同收益来源。",
            "admission": "ALLOW",
        })
        ok, problems = validate_proposal(
            proposal,
            strict_experiment=True,
            require_economic_integrity=True,
        )
        self.assertFalse(ok)
        self.assertTrue(any("self_correlation_impact" in item for item in problems))

    def test_pending_self_correlation_does_not_block_read_only_refresh(self):
        metrics = {"checks": [
            {"name": "LOW_SHARPE", "pass": True},
            {"name": "SELF_CORRELATION", "pass": None, "result": "PENDING"},
        ]}
        self.assertTrue(checks_ready_for_self_correlation_refresh(metrics))

    def test_unresolved_non_correlation_check_still_blocks_refresh(self):
        metrics = {"checks": [
            {"name": "LOW_SHARPE", "pass": None, "result": "PENDING"},
            {"name": "SELF_CORRELATION", "pass": None, "result": "PENDING"},
        ]}
        self.assertFalse(checks_ready_for_self_correlation_refresh(metrics))

    def test_takeover_preflight_is_local_and_reports_ready_state(self):
        with tempfile.TemporaryDirectory() as state_dir:
            result = run_takeover_preflight({
                "simulation": {},
                "agent": {"state_dir": state_dir},
            })
        self.assertEqual(result["status"], "READY")
        self.assertTrue(result["network_write"] is False)
        self.assertIn("doctor", result)
        self.assertIn("state", result)

    def test_takeover_preflight_accepts_ephemeral_terminal_checkpoint(self):
        with tempfile.TemporaryDirectory() as state_dir:
            with open(os.path.join(state_dir, "round_1.checkpoint.json"), "w", encoding="utf-8") as handle:
                import json
                json.dump({"schema_version": 1, "round_no": 1,
                           "hypothesis": {}, "complete": True, "experiments": [{
                    "id": "e1", "round": 1, "hypothesis_id": "h",
                    "expression": "rank(low)", "settings": {},
                    "fields_used": ["low"], "status": "DONE", "proposal_id": "p"
                }]}, handle)
            result = run_takeover_preflight({
                "simulation": {},
                "agent": {"state_dir": state_dir},
            })
        self.assertEqual(result["status"], "READY")
        self.assertNotIn("ledger_missing", result["blocking"])

    def test_economic_factory_proposals_carry_mechanism_and_corr_forecast(self):
        root = os.path.dirname(os.path.dirname(__file__))
        reference = _operator_reference(
            os.path.join(root, "docs", "reference", "OPERATORS_CHEATSHEET.md")
        )
        proposals = AlphaFactory().assemble_proposals(
            {"id": "h", "datasets": ["fundamental6"], "template_ids": ["toy_pair_spread"]},
            [{"id": "cashflow_op", "description": "daily close price",
              "type": "MATRIX", "semantic_status": "KNOWN", "frequency": "daily",
              "category": "market", "dataset": "pv1"},
             {"id": "cashflow_fin", "description": "daily high price",
              "type": "MATRIX", "semantic_status": "KNOWN", "frequency": "daily",
              "category": "market", "dataset": "pv1"}],
            reference,
            max_candidates=1,
        )
        self.assertEqual(len(proposals), 1)
        self.assertTrue(proposals[0]["economic_mechanism"])
        self.assertIn("direction_transform", proposals[0])
        self.assertEqual(proposals[0]["self_correlation_impact"]["admission"], "REVIEW")

    def test_correlation_backfill_selects_only_real_quality_rows_in_window(self):
        rows = [
            _correlation_row("good"),
            _correlation_row("bad-check", checks=[
                {"name": "LOW_SHARPE", "pass": False},
                {"name": "SELF_CORRELATION", "pass": None},
            ]),
            _correlation_row("negative-returns", returns=-0.01),
        ]
        self.assertEqual(select_alpha_ids(rows, 0, 20, delay=1), ["good"])
        borderline = [_correlation_row("delay-sensitive", sharpe=1.26, fitness=1.01)]
        self.assertEqual(
            select_alpha_ids(borderline, 0, 20, delay=1), ["delay-sensitive"]
        )
        self.assertEqual(select_alpha_ids(borderline, 0, 20, delay=0), [])

    def test_correlation_backfill_until_date_is_exclusive_midnight(self):
        self.assertLess(
            _timestamp("2026-09-09"),
            _timestamp("2026-09-09", end=True),
        )
        self.assertEqual(
            _timestamp("2026-09-09", end=True),
            _timestamp("2026-09-10"),
        )


if __name__ == "__main__":
    unittest.main()
