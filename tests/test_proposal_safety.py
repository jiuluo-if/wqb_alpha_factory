"""Proposal execution safety tests (dedupe, budget, checkpoint, SUBMIT_UNKNOWN)."""

import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts.reconcile_pending import collect
from tests.helpers import (
    BASE_CONFIG,
    TmpStateMixin,
    make_agent,
)
from wqb_agent.agent import (
    SEED_HYPOTHESES,
    Agent,
)
from wqb_agent.artifacts import atomic_write_json_if_changed, iter_jsonl_objects
from wqb_agent.candidate import CandidateBuilder
from wqb_agent.discovery import FieldDiscovery
from wqb_agent.expression import submission_fingerprint
from wqb_agent.memory import ExperienceMemory
from wqb_agent.proposal_contract import validate_proposal, validate_vector_inputs
from wqb_agent.reflection import Reflector
from wqb_agent.simulator import Simulator
from wqb_agent.state import Experiment, Trajectory
from wqb_agent.submission import SubmissionPool, self_correlation_evidence


class TestProposalExecutionSafety(TmpStateMixin, unittest.TestCase):

    @staticmethod
    def _force_round_proposal(agent, *, settings=None):
        proposal = {
            "expression": "rank(returns)",
            "hypothesis": "return signal",
            "rationale": "execution identity fence regression",
            "direction": "long",
            "expected_horizon": "63 days",
            "falsification": "S<1",
            "fields": ["returns"],
            "datasets": ["pv1"],
            "field_understanding": {"returns": "daily return"},
            "field_hypothesis_basis": {"returns": {
                "description": "daily return", "mechanism": "return signal",
            }},
            "operator_mapping": "rank creates a cross-section",
            "operator_evidence": {
                "sha256": agent.operator_reference["sha256"],
                "operators": ["rank"], "rationale": "documented rank",
            },
            "experiment_question": "does return predict future return?",
            "field_analysis": {"returns": {"semantic": "daily return",
                "coverage": None, "frequency": None, "data_type": "MATRIX"}},
            "expected_failure_modes": ["sharpe"], "tuning_risk": False,
            "experiment_stage": "BASELINE",
        }
        if settings is not None:
            proposal["settings"] = settings
        return proposal

    def test_run_proposals_executes_llm_candidates(self):
        agent, client = make_agent(self._tmp, rounds=1)
        proposals = {
            "round_no": 1,
            "hypothesis": {
                "id": "h-llm-1",
                "statement": "LLM proposed reversal with ts_zscore smoothing.",
                "tags": ["llm"],
                "direction": "long",
            },
            "fields": [
                {"id": "returns", "dataset": "pv1", "type": "MATRIX", "description": "daily simple returns", "semantic_status": "KNOWN"},
                {"id": "volume", "dataset": "pv1", "type": "MATRIX", "description": "daily trading volume", "semantic_status": "KNOWN"},
            ],
            "proposals": [
                {
                    "expression": "-rank(ts_zscore(returns, 20))",
                    "hypothesis": "短期收益反转：5 日大涨后均值回归。",
                    "rationale": "r84 后反转信号在短窗有效（记忆证据）。",
                    "direction": "reversal",
                    "expected_horizon": "20 天 z-score 反转，短窗生效（63 内）",
                    "falsification": "S<0.5 或方向反号即放弃",
                    "fields": ["returns"],
                    "datasets": ["pv1"],
                    "field_understanding": {"returns": "每日简单收益率，代表近期价格变化。"},
                    "operator_mapping": "ts_zscore 衡量异常涨跌，rank 形成横截面对比。",
                    "experiment_question": "异常短期收益是否会在随后一个月反转？",
                    "field_analysis": {"returns": {"semantic": "daily simple returns", "coverage": None, "frequency": None, "data_type": "MATRIX"}},
                    "expected_failure_modes": ["sharpe", "turnover"],
                    "tuning_risk": False,
                    "experiment_stage": "BASELINE",
                },
                {
                    "expression": "-rank(ts_zscore(returns, 20))",
                    "hypothesis": "duplicate of above.",
                },
                {
                    "expression": "rank(ts_mean(volume, 5))",
                    "hypothesis": "成交量 5 日均值：放量预示持续关注。",
                    "rationale": "second structure",
                    "direction": "long",
                    "expected_horizon": "5 天均值，短期",
                    "falsification": "S<0.5 即放弃",
                    "fields": ["volume"],
                    "datasets": ["pv1"],
                    "field_understanding": {"volume": "每日成交量，代表市场注意力与交易参与。"},
                    "operator_mapping": "短期均值降低单日成交量噪声，rank 比较相对注意力。",
                    "experiment_question": "持续放量是否对应后续收益延续？",
                    "field_analysis": {"volume": {"semantic": "daily trading volume", "coverage": None, "frequency": None, "data_type": "MATRIX"}},
                    "expected_failure_modes": ["sharpe"],
                    "tuning_risk": False,
                    "experiment_stage": "BASELINE",
                },
            ],
        }
        with open(os.path.join(self._tmp, "proposals.json"), "w", encoding="utf-8") as f:
            json.dump(proposals, f, ensure_ascii=False)
        summary = agent.run_proposals(os.path.join(self._tmp, "proposals.json"))
        self.assertIsNotNone(summary)
        # 重复提案被去重：只模拟 2 个
        self.assertEqual(len(client.sim_calls), 2)
        self.assertEqual(summary["experiment_count"], 2)
        self.assertIsNone(agent.memory.current_best)
        self.assertTrue(any(x["kind"] == "observation" for x in agent.memory.short_term))
        # trajectory 完整记录
        self.assertEqual(len(agent.trajectory.experiments), 2)
        for exp in agent.trajectory.experiments:
            self.assertTrue(exp.proposal_id.startswith("p-"))
            self.assertEqual(len(exp.submission_fingerprint), 64)
            self.assertIsNotNone(exp.submission_started_at)

    def test_run_proposals_caps_same_structural_family_without_template_metadata(self):
        """Sibling fields must not multiply one operator/window skeleton."""
        agent, client = make_agent(self._tmp, rounds=1)
        field_rows = [
            {
                "id": f"analyst_field_{i}",
                "dataset": "analyst4",
                "type": "MATRIX",
                "description": f"verified analyst field {i}",
                "semantic_status": "KNOWN",
            }
            for i in range(4)
        ]
        proposals = {
            "round_no": 1,
            "hypothesis": {"id": "h-structural", "statement": "same skeleton"},
            "fields": field_rows,
            "proposals": [
                {
                    "expression": f"rank(ts_delta(analyst_field_{i}, 5))",
                    "fields": [f"analyst_field_{i}"],
                    "datasets": ["analyst4"],
                    "field_understanding": {
                        f"analyst_field_{i}": "verified analyst signal",
                    },
                    "field_analysis": {
                        f"analyst_field_{i}": {
                            "semantic": "verified analyst signal",
                            "coverage": None,
                            "frequency": None,
                            "data_type": "MATRIX",
                        },
                    },
                    "operator_mapping": "ts_delta measures the field change; rank compares it cross-sectionally",
                    "experiment_question": "Does the five-day field change carry information?",
                    "expected_failure_modes": ["weak signal"],
                    "tuning_risk": False,
                    "experiment_stage": "BASELINE",
                }
                for i in range(4)
            ],
        }
        path = os.path.join(self._tmp, "proposals.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(proposals, handle, ensure_ascii=False)

        summary = agent.run_proposals(path)

        self.assertIsNotNone(summary)
        self.assertEqual(len(client.sim_calls), 2)
        self.assertEqual(summary["experiment_count"], 2)

    def test_other_unfinished_checkpoint_blocks_new_round(self):
        agent, client = make_agent(self._tmp, rounds=1)
        checkpoint = {
            "round_no": 7,
            "complete": False,
            "experiments": [],
            "hypothesis": {"id": "h-7"},
        }
        with open(os.path.join(self._tmp, "round_7.checkpoint.json"), "w",
                  encoding="utf-8") as f:
            json.dump(checkpoint, f)
        payload = {"round_no": 8, "proposals": []}
        path = os.path.join(self._tmp, "proposals.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        self.assertIsNone(agent.run_proposals(path))
        self.assertEqual(client.sim_calls, [])

    def test_malformed_checkpoint_fails_closed_before_resume(self):
        agent, client = make_agent(self._tmp, rounds=1)
        checkpoint_path = os.path.join(self._tmp, "round_1.checkpoint.json")
        with open(checkpoint_path, "w", encoding="utf-8") as handle:
            json.dump({
                "round_no": 1,
                "complete": False,
                "hypothesis": {"id": "h-1"},
                "experiments": [{"id": "only-partial-row"}],
            }, handle)
        proposals_path = os.path.join(self._tmp, "proposals.json")
        with open(proposals_path, "w", encoding="utf-8") as handle:
            json.dump({"round_no": 1, "proposals": []}, handle)
        self.assertIsNone(agent.run_proposals(proposals_path))
        self.assertEqual(client.sim_calls, [])
        with open(checkpoint_path, encoding="utf-8") as handle:
            self.assertFalse(json.load(handle)["complete"])

    def test_skip_stale_requires_three_reconciliations_and_records_terminal_state(self):
        agent, client = make_agent(self._tmp, rounds=1)
        exp = Experiment(1797, "h-1797", "rank(put_iv)", BASE_CONFIG["simulation"], ["put_iv"], ["option8"])
        exp.status = "UNKNOWN"
        exp.proposal_id = "p-stale-1797"
        exp.progress_url = "https://api.worldquantbrain.com/simulations/remote-1797"
        agent._write_proposal_checkpoint(1797, {"id": "h-1797"}, [exp], complete=False)
        history_path = os.path.join(self._tmp, "reconcile_history.jsonl")
        rows = [{"simulation_id": "remote-1797", "outcome": "STALE", "reconciled_at": str(i)}
                for i in range(2)]
        with open(history_path, "w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row) + "\n")
        with self.assertRaises(ValueError):
            agent.skip_stale_reconciled(1797, "remote-1797")
        with open(history_path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"simulation_id": "remote-1797", "outcome": "STALE",
                                "reconciled_at": "2"}) + "\n")
        skipped = agent.skip_stale_reconciled(1797, "remote-1797")
        self.assertEqual(skipped.status, "SKIPPED_STALE")
        self.assertEqual(skipped.skip_record["attempts"], 3)
        with open(os.path.join(self._tmp, "round_1797.checkpoint.json"), encoding="utf-8") as f:
            self.assertTrue(json.load(f)["complete"])
        self.assertTrue(os.path.exists(os.path.join(self._tmp, "stale_skip_log.jsonl")))
        self.assertEqual(client.sim_calls, [])
        lifecycle = agent.trial_ledger.summarize()["lifecycle_proposals"]
        self.assertEqual(lifecycle[skipped.proposal_id]["status"], "SKIPPED_STALE")
        before = len(list(iter_jsonl_objects(agent.trial_ledger.path)))
        self.assertEqual(
            agent.skip_stale_reconciled(1797, "remote-1797").status,
            "SKIPPED_STALE",
        )
        self.assertEqual(len(list(iter_jsonl_objects(agent.trial_ledger.path))), before)

    def test_skip_submit_unknown_rejects_known_url_unknown_and_pending(self):
        """A URL-bearing UNKNOWN is read-only recoverable and a PENDING item was
        never submitted; neither may be user-authorized skipped."""
        agent, client = make_agent(self._tmp, rounds=1)
        exp_url = Experiment(1799, "h-1799", "rank(put_iv)", BASE_CONFIG["simulation"], ["put_iv"], ["option8"])
        exp_url.status = "UNKNOWN"
        exp_url.progress_url = "https://api.worldquantbrain.com/simulations/remote-1799"
        exp_url.proposal_id = "p-known-url-unknown"
        exp_pending = Experiment(1799, "h-1799", "rank(put_iv)", BASE_CONFIG["simulation"], ["put_iv"], ["option8"])
        exp_pending.id = "other"
        exp_pending.status = "PENDING"
        exp_pending.proposal_id = "p-pending"
        agent._write_proposal_checkpoint(1799, {"id": "h-1799"}, [exp_url, exp_pending], complete=False)
        with self.assertRaises(ValueError):
            agent.skip_submit_unknown_authorized(1799, "p-known-url-unknown")
        with self.assertRaises(ValueError):
            agent.skip_submit_unknown_authorized(1799, "p-pending")
        self.assertEqual(client.sim_calls, [])

    def test_skip_submit_unknown_authorized_accepts_url_less_unknown(self):
        """A progress-URL-less UNKNOWN (ambiguous POST, no remote identity) is the
        same recovery-evidence gap as SUBMIT_UNKNOWN; user-authorized skip is the
        only terminal disposition, with its own audit trail."""
        agent, client = make_agent(self._tmp, rounds=1)
        exp = Experiment(1800, "h-1800", "rank(put_iv)", BASE_CONFIG["simulation"], ["put_iv"], ["option8"])
        exp.status = "UNKNOWN"
        exp.progress_url = None
        exp.proposal_id = "p-no-url-unknown"
        agent._write_proposal_checkpoint(1800, {"id": "h-1800"}, [exp], complete=False)
        skipped = agent.skip_submit_unknown_authorized(1800, "p-no-url-unknown")
        self.assertEqual(skipped.status, "SKIPPED_UNKNOWN")
        self.assertEqual(skipped.skip_record["reason"], "user_authorized_skip_unknown_no_progress_url")
        self.assertEqual(skipped.error, "SKIPPED_AFTER_USER_AUTHORIZED_UNKNOWN_NO_URL")
        with open(os.path.join(self._tmp, "round_1800.checkpoint.json"), encoding="utf-8") as f:
            self.assertTrue(json.load(f)["complete"])
        self.assertTrue(os.path.exists(os.path.join(self._tmp, "stale_skip_log.jsonl")))
        self.assertEqual(client.sim_calls, [])
        lifecycle = agent.trial_ledger.summarize()["lifecycle_proposals"]
        self.assertEqual(lifecycle[skipped.proposal_id]["status"], "SKIPPED_UNKNOWN")

    def test_skip_submit_unknown_authorized_still_accepts_submit_unknown(self):
        """SUBMIT_UNKNOWN keeps its legacy audit semantics unchanged."""
        agent, client = make_agent(self._tmp, rounds=1)
        exp = Experiment(1801, "h-1801", "rank(put_iv)", BASE_CONFIG["simulation"], ["put_iv"], ["option8"])
        exp.status = "SUBMIT_UNKNOWN"
        exp.proposal_id = "p-submit-unknown"
        agent._write_proposal_checkpoint(1801, {"id": "h-1801"}, [exp], complete=False)
        skipped = agent.skip_submit_unknown_authorized(1801, "p-submit-unknown")
        self.assertEqual(skipped.status, "SKIPPED_UNKNOWN")
        self.assertEqual(
            skipped.skip_record["reason"],
            "user_authorized_skip_after_repeated_read_only_reconciliation",
        )
        self.assertEqual(skipped.error, "SKIPPED_AFTER_USER_AUTHORIZED_SUBMIT_UNKNOWN")
        self.assertEqual(client.sim_calls, [])

    def test_reconcile_collect_includes_known_url_from_unfinished_checkpoint(self):
        exp = Experiment(1798, "h-1798", "rank(put_iv)", BASE_CONFIG["simulation"], ["put_iv"], ["option8"])
        exp.status = "UNKNOWN"
        exp.progress_url = "https://api.worldquantbrain.com/simulations/remote-1798"
        checkpoint_path = os.path.join(self._tmp, "round_1798.checkpoint.json")
        with open(checkpoint_path, "w", encoding="utf-8") as handle:
            json.dump({"round_no": 1798, "complete": False,
                       "hypothesis": {"id": "h-1798"},
                       "experiments": [exp.to_dict()]}, handle)
        targets = collect(self._tmp)
        self.assertEqual([row["progress_url"] for row in targets], [exp.progress_url])

    def test_explicit_force_new_round_preserves_old_checkpoint(self):
        agent, client = make_agent(self._tmp, rounds=1)
        checkpoint = {
            "round_no": 7,
            "complete": False,
            "experiments": [],
            "hypothesis": {"id": "h-7"},
        }
        with open(os.path.join(self._tmp, "round_7.checkpoint.json"), "w",
                  encoding="utf-8") as f:
            json.dump(checkpoint, f)
        payload = {"round_no": 8, "fields": [{
            "id": "returns", "dataset": "pv1", "type": "MATRIX",
            "description": "daily return", "semantic_status": "KNOWN",
        }], "proposals": [{
            "expression": "rank(returns)", "hypothesis": "return signal",
            "rationale": "explicit user-authorized new round", "direction": "long",
            "expected_horizon": "63 days", "falsification": "S<1",
            "fields": ["returns"], "datasets": ["pv1"],
            "field_understanding": {"returns": "daily return"},
            "field_hypothesis_basis": {"returns": {
                "description": "daily return", "mechanism": "return signal",
            }},
            "operator_mapping": "rank creates a cross-section",
            "operator_evidence": {
                "sha256": agent.operator_reference["sha256"],
                "operators": ["rank"], "rationale": "documented rank",
            },
            "experiment_question": "does return predict future return?",
            "field_analysis": {"returns": {"semantic": "daily return",
                "coverage": None, "frequency": None, "data_type": "MATRIX"}},
            "expected_failure_modes": ["sharpe"], "tuning_risk": False,
            "experiment_stage": "BASELINE",
        }]}
        path = os.path.join(self._tmp, "proposals.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        agent.run_proposals(path, allow_unresolved_checkpoint=True)
        self.assertEqual(client.sim_calls, ["rank(returns)"])
        with open(os.path.join(self._tmp, "round_7.checkpoint.json"),
                  encoding="utf-8") as f:
            self.assertFalse(json.load(f)["complete"])

    def test_force_new_round_blocks_unresolved_submission_fingerprint(self):
        agent, client = make_agent(self._tmp, rounds=1)
        unresolved = Experiment(
            7, "h-7", "rank(returns)", BASE_CONFIG["simulation"],
            ["returns"], ["pv1"],
        )
        unresolved.status = "SUBMIT_UNKNOWN"
        unresolved.progress_url = "https://api.worldquantbrain.com/simulations/remote-7"
        unresolved.submission_fingerprint = submission_fingerprint(
            unresolved.expression, unresolved.settings
        )
        agent._write_proposal_checkpoint(7, {"id": "h-7"}, [unresolved], complete=False)
        path = os.path.join(self._tmp, "proposals.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({
                "round_no": 8,
                "fields": [{"id": "returns", "dataset": "pv1", "type": "MATRIX",
                            "description": "daily return", "semantic_status": "KNOWN"}],
                "proposals": [self._force_round_proposal(agent)],
            }, handle)

        self.assertIsNone(agent.run_proposals(path, allow_unresolved_checkpoint=True))
        self.assertEqual(client.sim_calls, [])
        rows = list(agent.trial_ledger._events) if not agent.trial_ledger.path else list(
            iter_jsonl_objects(agent.trial_ledger.path)
        )
        self.assertTrue(any(
            row.get("reason_code") == "UNRESOLVED_SUBMISSION_IDENTITY"
            for row in rows
        ))

    def test_force_new_round_allows_same_expression_with_different_settings(self):
        agent, client = make_agent(self._tmp, rounds=1)
        unresolved = Experiment(
            7, "h-7", "rank(returns)", BASE_CONFIG["simulation"],
            ["returns"], ["pv1"],
        )
        unresolved.status = "UNKNOWN"
        unresolved.submission_fingerprint = submission_fingerprint(
            unresolved.expression, unresolved.settings
        )
        agent._write_proposal_checkpoint(7, {"id": "h-7"}, [unresolved], complete=False)
        path = os.path.join(self._tmp, "proposals.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({
                "round_no": 8,
                "fields": [{"id": "returns", "dataset": "pv1", "type": "MATRIX",
                            "description": "daily return", "semantic_status": "KNOWN"}],
                "proposals": [self._force_round_proposal(agent, settings={"decay": 1})],
            }, handle)

        agent.run_proposals(path, allow_unresolved_checkpoint=True)
        self.assertEqual(client.sim_calls, ["rank(returns)"])

    def test_proposal_priority_cap_never_spends_over_budget(self):
        agent, client = make_agent(self._tmp, rounds=1)
        agent.candidates_per_round = 1
        fields = [
            {"id": f, "dataset": "pv1", "type": "MATRIX", "description": f"{f} profile",
             "semantic_status": "KNOWN"}
            for f in ("returns", "close", "volume")
        ]
        proposals = []
        for field, quality in (("returns", 1), ("close", 3), ("volume", 2)):
            proposals.append({
                "expression": f"rank({field})", "hypothesis": f"{field} effect",
                "rationale": "test evidence", "direction": "long",
                "expected_horizon": "63 days", "falsification": "S<1",
                "fields": [field], "datasets": ["pv1"],
                "field_understanding": {field: f"{field} meaning"},
                "operator_mapping": "rank converts the field to a cross-section",
                "experiment_question": f"does {field} predict returns?",
                "field_analysis": {field: {"semantic": f"{field} profile", "coverage": None, "frequency": None, "data_type": "MATRIX"}},
                "expected_failure_modes": ["sharpe"],
                "tuning_risk": False,
                "experiment_stage": "BASELINE",
                "expected_quality": quality,
            })
        path = os.path.join(self._tmp, "proposals.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"round_no": 1, "fields": fields, "proposals": proposals}, f)
        agent.run_proposals(path)
        self.assertEqual(client.sim_calls, ["rank(close)"])

    def test_research_allocation_allows_cold_start_explore(self):
        agent, client = make_agent(self._tmp, rounds=1)
        agent.candidates_per_round = 18
        agent.research_allocation = {
            "max_simulations": 18,
            "maximum": {"EXPLOIT": 4, "VALIDATION": 4},
        }
        proposal = {
            "expression": "rank(returns)", "hypothesis": "one candidate",
            "rationale": "single candidate test", "direction": "long",
            "expected_horizon": "63 days", "falsification": "S<1",
            "fields": ["returns"], "datasets": ["pv1"],
            "field_understanding": {"returns": "daily return"},
            "operator_mapping": "rank creates a cross-section",
            "field_hypothesis_basis": {"returns": {
                "description": "daily return", "mechanism": "cross-sectional return contains the tested signal",
            }},
            "operator_evidence": {
                "sha256": agent.operator_reference["sha256"], "operators": ["rank"],
                "rationale": "rank is documented for cross-sectional comparison",
            },
            "experiment_question": "does return predict future return?",
            "field_analysis": {"returns": {"semantic": "daily return",
                "coverage": None, "frequency": None, "data_type": "MATRIX"}},
            "expected_failure_modes": ["sharpe"], "tuning_risk": False,
            "experiment_stage": "BASELINE", "research_role": "EXPLORE",
            "field_source": {"kind": "local_catalog", "path": "snapshot", "snapshot_date": "2026-08-22"},
        }
        path = os.path.join(self._tmp, "proposals.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"round_no": 1,
                       "fields": [{"id": "returns", "dataset": "pv1",
                                   "type": "MATRIX", "description": "daily return",
                                   "semantic_status": "KNOWN"}],
                       "proposals": [proposal]}, f)
        self.assertIsNotNone(agent.run_proposals(path))
        self.assertEqual(client.sim_calls, ["rank(returns)"])

    def test_stopped_lineage_cannot_spend_more_budget(self):
        agent, client = make_agent(self._tmp, rounds=1)
        for score in (1.0, 0.9, 0.8):
            agent.memory.record_lineage_result("lineage-a", score, "FAIL", 1)
        self.assertEqual(agent.memory.lineage_decision("lineage-a"), "STOP")
        proposal = {
            "expression": "rank(returns)", "hypothesis": "return effect",
            "rationale": "last permitted variant already failed", "direction": "long",
            "expected_horizon": "63d", "falsification": "S<1",
            "fields": ["returns"], "datasets": ["pv1"],
            "field_understanding": {"returns": "daily return"},
            "field_analysis": {"returns": {"semantic": "daily return",
                "coverage": None, "frequency": None, "data_type": "MATRIX"}},
            "operator_mapping": "rank creates a cross-section",
            "experiment_question": "does a final window change help?",
            "expected_failure_modes": ["sharpe"], "tuning_risk": False,
            "experiment_stage": "CHILD", "change_type": "window_change",
            "parent_expression": "rank(ts_mean(returns, 20))",
            "changed_variable": "window: 20 -> raw", "lineage_id": "lineage-a",
        }
        path = os.path.join(self._tmp, "proposals.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"round_no": 1, "fields": [{"id": "returns", "dataset": "pv1",
                "type": "MATRIX", "description": "daily return", "semantic_status": "KNOWN"}],
                       "proposals": [proposal]}, f)
        self.assertIsNone(agent.run_proposals(path))
        self.assertEqual(client.sim_calls, [])
