"""Proposal schema and evidence contract tests."""

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
    TmpStateMixin,
    make_agent,
)
from wqb_agent.agent import (
    SEED_HYPOTHESES,
    Agent,
)
from wqb_agent.artifacts import atomic_write_json_if_changed
from wqb_agent.discovery import FieldDiscovery
from wqb_agent.memory import ExperienceMemory
from wqb_agent.proposal_contract import validate_proposal, validate_vector_inputs
from wqb_agent.reflection import Reflector
from wqb_agent.simulator import Simulator
from wqb_agent.state import Experiment, Trajectory
from wqb_agent.submission import SubmissionPool, self_correlation_evidence


class TestProposalContract(TmpStateMixin, unittest.TestCase):

    def test_preflight_rejects_frequency_provenance_laundering(self):
        proposal = {
            "expression": "rank(field)",
            "fields": ["field"],
            "field_understanding": {"field": "model score"},
            "field_analysis": {"field": {
                "semantic": "model score", "coverage": None,
                "frequency": "daily", "frequency_evidence": {
                    "frequency": "daily", "source": "EXPLICIT_PLATFORM",
                    "status": "KNOWN", "confidence": "HIGH",
                },
                "data_type": "MATRIX",
            }},
        }
        discovered = [{
            "id": "field", "description": "model score", "type": "MATRIX",
            "frequency": "daily", "frequency_evidence": {
                "frequency": "daily", "source": "DATASET_DESCRIPTION_INFERRED",
                "status": "INFERRED", "confidence": "MEDIUM",
            },
            "semantic_status": "KNOWN",
        }]

        proposal.update({
            "datasets": ["pv1"],
            "expected_failure_modes": ["unknown"],
            "tuning_risk": False,
            "experiment_stage": "BASELINE",
        })
        ok, problems = validate_proposal(
            proposal, discovered_fields=discovered, strict_experiment=True
        )

        self.assertFalse(ok)
        self.assertTrue(any(
            "FIELD_FREQUENCY_PROVENANCE_MISMATCH" in problem
            for problem in problems
        ))

    def test_validate_proposal_allows_retired_question_fields_to_be_omitted(self):
        """旧的解释性字段不再是生产预检条件。"""
        ok, problems = validate_proposal(
            {
                "expression": "-rank(ts_zscore(returns, 20))",
                "fields": ["returns"],
            }
        )
        self.assertTrue(ok, problems)

    def test_run_proposals_accepts_traceable_proposal_without_retired_fields(self):
        agent, client = make_agent(self._tmp, rounds=1)
        field = {
            "id": "returns", "dataset": "pv1", "type": "MATRIX",
            "description": "daily returns", "semantic_status": "KNOWN",
            "coverage": None, "frequency": None,
        }
        proposal = {
            "expression": "rank(returns)",
            "fields": ["returns"], "datasets": ["pv1"],
            "field_understanding": {"returns": "daily stock returns"},
            "field_analysis": {"returns": {
                "semantic": "daily returns", "coverage": None,
                "frequency": None, "data_type": "MATRIX",
            }},
            "field_source": {
                "kind": "local_catalog", "path": "snapshot",
                "snapshot_date": "2026-09-03",
            },
            "field_hypothesis_basis": {"returns": {
                "description": "daily returns",
                "mechanism": "Tests cross-sectional return strength.",
            }},
            "operator_mapping": "rank compares cross-sectional return strength",
            "operator_evidence": {
                "sha256": agent.operator_reference["sha256"],
                "operators": ["rank"], "rationale": "rank is documented",
            },
            "experiment_question": "Does return strength carry information?",
            "expected_failure_modes": ["weak sharpe"], "tuning_risk": False,
            "experiment_stage": "BASELINE", "research_role": "EXPLORE",
        }
        path = os.path.join(self._tmp, "proposals.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({
                "round_no": 1,
                "hypothesis": {"id": "h-preflight", "statement": "traceable proposal"},
                "fields": [field], "proposals": [proposal],
            }, f, ensure_ascii=False)
        summary = agent.run_proposals(path)
        self.assertIsNotNone(summary)
        self.assertEqual(client.sim_calls, ["rank(returns)"])

    def test_vec_operators_require_vector_input(self):
        proposal = {"expression": "rank(vec_avg(news_vector))"}
        ok, problems = validate_vector_inputs(
            proposal, {"news_vector": "VECTOR"}
        )
        self.assertTrue(ok, problems)

        ok, problems = validate_vector_inputs(
            proposal, {"news_vector": "MATRIX"}
        )
        self.assertFalse(ok)
        self.assertTrue(any("只能作用于 VECTOR" in p for p in problems))

        ok, problems = validate_vector_inputs(proposal, {})
        self.assertFalse(ok)
        self.assertTrue(any("类型未知" in p for p in problems))

        # direction 现在只是可选元数据，不参与生产预检。
        ok, problems = validate_proposal(
            {
                "expression": "rank(x)",
                "hypothesis": "h",
                "rationale": "r",
                "direction": "sideways",
                "expected_horizon": "63",
                "falsification": "S<1",
                "fields": ["x"],
            }
        )
        self.assertTrue(ok, problems)

        # 同一表达式带旧元数据时仍兼容。
        ok, problems = validate_proposal(
            {
                "expression": "rank(x)",
                "hypothesis": "h",
                "rationale": "r",
                "direction": "long",
                "expected_horizon": "63",
                "falsification": "S<1",
                "fields": ["x"],
            }
        )
        self.assertTrue(ok, problems)

        # 集成：结构性证据缺失时仍不发起模拟。
        agent, client = make_agent(self._tmp, rounds=1)
        proposals = {
            "round_no": 1,
            "hypothesis": {
                "id": "h-llm-1",
                "statement": "incomplete-preflight",
                "tags": ["llm"],
                "direction": "long",
            },
            "proposals": [
                {"expression": "-rank(ts_zscore(returns, 20))", "hypothesis": "x"},
            ],
        }
        with open(os.path.join(self._tmp, "proposals.json"), "w", encoding="utf-8") as f:
            json.dump(proposals, f, ensure_ascii=False)
        summary = agent.run_proposals(os.path.join(self._tmp, "proposals.json"))
        self.assertIsNone(summary)
        self.assertEqual(len(client.sim_calls), 0)
        self.assertEqual(len(agent.trajectory.experiments), 0)

    def test_proposal_settings_merge_defaults_and_reject_unknown(self):
        agent, _client = make_agent(self._tmp, rounds=1)
        merged = agent._proposal_settings({"decay": 7})
        self.assertEqual(merged["decay"], 7)
        self.assertEqual(merged["region"], "USA")
        self.assertEqual(merged["neutralization"], "SUBINDUSTRY")
        with self.assertRaises(ValueError):
            agent._proposal_settings({"region": "EUROPE"})
        with self.assertRaises(ValueError):
            agent._proposal_settings({"decay": 11})

    def test_run_proposals_blocks_vector_input_type_mismatch(self):
        agent, client = make_agent(self._tmp, rounds=1)
        # Seed a real-looking discovery bundle so the preflight can resolve
        # the field type without contacting the platform.
        proposals = {
            "round_no": 1,
            "hypothesis": {"id": "h-vector", "statement": "vector to matrix", "direction": "long"},
            "fields": [{"id": "news_vector", "type": "MATRIX", "dataset": "news18"}],
            "proposals": [{
                "expression": "rank(vec_avg(news_vector))",
                "hypothesis": "vector input test",
                "rationale": "type gate test",
                "direction": "long",
                "expected_horizon": "63 days",
                "falsification": "S<1",
                "fields": ["news_vector"],
            }],
        }
        path = os.path.join(self._tmp, "proposals.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(proposals, f, ensure_ascii=False)
        self.assertIsNone(agent.run_proposals(path))
        self.assertEqual(len(client.sim_calls), 0)

    def test_validate_proposal_requires_declared_field_in_expression(self):
        from wqb_agent.proposal_contract import validate_proposal

        ok, problems = validate_proposal({
            "expression": "rank(returns)",
            "hypothesis": "h",
            "rationale": "evidence",
            "direction": "long",
            "expected_horizon": "63 days",
            "falsification": "sharpe < 1",
            "fields": ["close"],
        })
        self.assertFalse(ok)
        self.assertTrue(any("expression" in p for p in problems))
        ok, problems = validate_proposal({
            "expression": "rank(returns)", "hypothesis": "h",
            "rationale": "evidence", "direction": "long",
            "expected_horizon": "63 days", "falsification": "sharpe < 1",
            "fields": "returns",
        })
        self.assertFalse(ok)
        self.assertTrue(any("非空数组" in p for p in problems))

    def test_expression_identifier_allowlist_covers_cheatsheet_operators(self):
        """2026-08-22 算子放开政策：cheatsheet 收录的算子（hump/ts_corr 等）
        与常用命名参数不得被当作未知字段拦截。回归：r622 hump 误拦。"""
        from wqb_agent.proposal_contract import validate_proposal

        base = {
            "hypothesis": "h", "rationale": "e", "direction": "reversal",
            "expected_horizon": "5 days", "falsification": "S<1",
        }
        for expr in (
            "-rank(hump(ts_mean(vec_avg(two_hour_price_change_percent),5),hump=0.005))",
            "quantile(-rank(ts_av_diff(doubtful_accounts_write_offs,4)),driver=gaussian)",
            "quantile(-rank(ts_av_diff(doubtful_accounts_write_offs,4)),driver=cauchy,sigma=1)",
            "kth_element(doubtful_accounts_write_offs,10,1)",
            "trade_when(ts_rank(vec_avg(volume_at_open),63)>0.5,-rank(ts_mean(vec_avg(two_hour_price_change_percent),5)),-1)",
            "-rank(days_from_last_change(anl4_ffo_flag))",
            "-rank(last_diff_value(anl4_netdebt_flag,20))",
        ):
            ok, problems = validate_proposal({
                "expression": expr,
                "fields": ["two_hour_price_change_percent", "volume_at_open",
                            "doubtful_accounts_write_offs", "anl4_ffo_flag",
                            "anl4_netdebt_flag"],
                **base,
            })
            self.assertTrue(
                ok, f"expression blocked: {expr} -> {problems}"
            )
        # 真正的未知字段仍必须被拦截
        ok, problems = validate_proposal({
            "expression": "rank(fabricated_field_xyz)", "fields": ["close"], **base,
        })
        self.assertFalse(ok)

    def test_strict_proposal_rejects_undiscovered_or_unexplained_field(self):
        proposal = {
            "expression": "rank(returns)",
            "hypothesis": "h", "rationale": "e", "direction": "long",
            "expected_horizon": "63 days", "falsification": "S<1",
            "fields": ["returns"], "datasets": ["pv1"],
            "operator_mapping": "rank compares cross-sectional strength",
            "experiment_question": "does the effect predict returns?",
            "field_understanding": {},
        }
        ok, problems = validate_proposal(
            proposal,
            discovered_fields=[
                {"id": "returns", "description": "daily returns",
                 "semantic_status": "KNOWN", "dataset": "pv1"},
            ],
            strict_experiment=True,
        )
        self.assertFalse(ok)
        self.assertTrue(any("field_understanding" in p for p in problems))
        proposal["field_understanding"] = {"returns": "daily stock return"}
        ok, problems = validate_proposal(
            proposal,
            discovered_fields=[],
            strict_experiment=True,
        )
        self.assertFalse(ok)
        self.assertTrue(any("discovery" in p for p in problems))

        proposal["expression"] = "rank(returns + fabricated_field)"
        ok, problems = validate_proposal(
            proposal,
            discovered_fields=[
                {"id": "returns", "description": "daily returns",
                 "semantic_status": "KNOWN", "dataset": "pv1"},
            ],
            strict_experiment=True,
        )
        self.assertFalse(ok)
        self.assertTrue(any("fabricated_field" in p for p in problems))

    def test_production_contract_requires_description_and_operator_evidence(self):
        fields = [{"id": "returns", "type": "MATRIX", "description": "daily return",
                   "semantic_status": "KNOWN", "alpha_count": 2}]
        proposal = {
            "expression": "rank(returns)", "hypothesis": "return effect", "rationale": "test",
            "direction": "long", "expected_horizon": "20d", "falsification": "S<1",
            "fields": ["returns"], "datasets": ["pv1"],
            "field_understanding": {"returns": "daily return"},
            "field_analysis": {"returns": {"semantic": "daily return", "coverage": 1,
                "frequency": None, "data_type": "MATRIX"}},
            "operator_mapping": "rank compares stocks", "experiment_question": "does it work?",
            "expected_failure_modes": ["sharpe"], "tuning_risk": False,
            "experiment_stage": "BASELINE",
        }
        agent, _ = make_agent(self._tmp, rounds=1)
        ok, problems = validate_proposal(
            proposal, discovered_fields=fields, strict_experiment=True,
            operator_reference=agent.operator_reference, require_research_evidence=True,
            max_alpha_count=10,
        )
        self.assertFalse(ok)
        self.assertTrue(any("field_hypothesis_basis" in problem for problem in problems))
        self.assertTrue(any("operator_evidence" in problem for problem in problems))

    def test_verified_field_profiles_supplement_current_discovery(self):
        agent, _ = make_agent(self._tmp, rounds=1)
        cache = {
            "schema": 2,
            "saved_at": time.time(),
            "datasets": {
                "option9": [{
                    "id": "verified_option_field",
                    "description": "verified option metadata",
                    "type": "MATRIX",
                    "coverage": 0.91,
                }],
            },
        }
        with open(os.path.join(self._tmp, "fields_cache.json"), "w",
                  encoding="utf-8") as f:
            json.dump(cache, f)
        profiles = agent._verified_field_profiles()
        self.assertEqual(profiles["verified_option_field"]["dataset"], "option9")
        self.assertEqual(profiles["verified_option_field"]["semantic_status"], "KNOWN")

    def test_field_cache_reader_returns_types_and_profiles_together(self):
        agent, _ = make_agent(self._tmp, rounds=1)
        cache = {
            "schema": 2,
            "saved_at": time.time(),
            "datasets": {
                "pv1": [
                    {"id": "typed_only", "type": "MATRIX"},
                    {"id": "verified", "type": "VECTOR", "description": "vector field"},
                ]
            },
        }
        with open(os.path.join(self._tmp, "fields_cache.json"), "w", encoding="utf-8") as f:
            json.dump(cache, f)
        types, profiles = agent._read_field_cache()
        self.assertEqual(types["typed_only"], "MATRIX")
        self.assertEqual(types["verified"], "VECTOR")
        self.assertEqual(profiles["verified"]["dataset"], "pv1")
        self.assertNotIn("typed_only", profiles)

    def test_proposal_requires_field_analysis_and_one_variable_child_contract(self):
        from wqb_agent.proposal_contract import validate_proposal

        proposal = {
            "expression": "rank(returns)", "hypothesis": "return effect",
            "rationale": "test", "direction": "long", "expected_horizon": "63d",
            "falsification": "S<1", "fields": ["returns"], "datasets": ["pv1"],
            "field_understanding": {"returns": "daily return"},
            "operator_mapping": "rank creates a cross-section",
            "experiment_question": "does return predict future return?",
            "expected_failure_modes": ["sharpe"], "tuning_risk": False,
            "experiment_stage": "CHILD", "change_type": "window_change",
            "parent_expression": "rank(ts_mean(returns, 20))",
        }
        fields = [{"id": "returns", "dataset": "pv1", "type": "MATRIX",
                   "description": "daily return", "semantic_status": "KNOWN"}]
        ok, problems = validate_proposal(proposal, discovered_fields=fields,
                                         strict_experiment=True)
        self.assertFalse(ok)
        self.assertTrue(any("changed_variable" in p for p in problems))
        proposal["changed_variable"] = "window: 20 -> raw"
        proposal["field_analysis"] = {"returns": {
            "semantic": "daily return", "coverage": None, "frequency": None,
            "data_type": "MATRIX",
        }}
        ok, problems = validate_proposal(proposal, discovered_fields=fields,
                                         strict_experiment=True)
        self.assertTrue(ok, problems)

    def test_documented_ts_arg_min_is_not_treated_as_unknown_field(self):
        from wqb_agent.proposal_contract import validate_proposal

        proposal = {
            "expression": "-rank(ts_zscore(close, 7))+0.2*rank(ts_arg_min(close, 30))",
            "hypothesis": "price reversal", "rationale": "documented operator",
            "direction": "reversal", "expected_horizon": "7-30d",
            "falsification": "Sharpe<0.8", "fields": ["close"],
            "datasets": ["pv1"], "field_understanding": {"close": "daily close"},
            "field_analysis": {"close": {"semantic": "Daily close price",
                "coverage": 1.0, "frequency": None, "data_type": "MATRIX"}},
            "operator_mapping": "ts_arg_min locates the recent low",
            "experiment_question": "does price location add information?",
            "expected_failure_modes": ["sharpe"], "tuning_risk": False,
            "experiment_stage": "BASELINE",
        }
        fields = [{"id": "close", "dataset": "pv1", "type": "MATRIX",
                   "description": "Daily close price", "semantic_status": "KNOWN"}]
        ok, problems = validate_proposal(proposal, discovered_fields=fields,
                                         strict_experiment=True)
        self.assertTrue(ok, problems)

    def test_documented_ts_arg_max_is_not_treated_as_unknown_field(self):
        from wqb_agent.proposal_contract import validate_proposal

        proposal = {
            "expression": "rank(ts_zscore(close, 7))+0.2*rank(ts_arg_max(close, 30))",
            "hypothesis": "price location", "rationale": "documented operator",
            "direction": "long", "expected_horizon": "7-30d",
            "falsification": "Sharpe<0.8", "fields": ["close"],
            "datasets": ["pv1"], "field_understanding": {"close": "daily close"},
            "field_analysis": {"close": {"semantic": "Daily close price",
                "coverage": 1.0, "frequency": None, "data_type": "MATRIX"}},
            "operator_mapping": "ts_arg_max locates the recent high",
            "experiment_question": "does price location add information?",
            "expected_failure_modes": ["sharpe"], "tuning_risk": False,
            "experiment_stage": "BASELINE",
        }
        fields = [{"id": "close", "dataset": "pv1", "type": "MATRIX",
                   "description": "Daily close price", "semantic_status": "KNOWN"}]
        ok, problems = validate_proposal(proposal, discovered_fields=fields,
                                         strict_experiment=True)
        self.assertTrue(ok, problems)

    def test_documented_ts_decay_linear_is_not_treated_as_unknown_field(self):
        from wqb_agent.proposal_contract import validate_proposal

        proposal = {
            "expression": "rank(ts_decay_linear(ts_zscore(close, 20), 5))",
            "hypothesis": "smoothed price effect", "rationale": "documented operator",
            "direction": "long", "expected_horizon": "20d",
            "falsification": "Sharpe<0.8", "fields": ["close"],
            "datasets": ["pv1"], "field_understanding": {"close": "daily close"},
            "field_analysis": {"close": {"semantic": "Daily close price",
                "coverage": 1.0, "frequency": None, "data_type": "MATRIX"}},
            "operator_mapping": "decay linear smooths the signal",
            "experiment_question": "does smoothing add information?",
            "expected_failure_modes": ["sharpe"], "tuning_risk": False,
            "experiment_stage": "BASELINE",
        }
        fields = [{"id": "close", "dataset": "pv1", "type": "MATRIX",
                   "description": "Daily close price", "semantic_status": "KNOWN"}]
        ok, problems = validate_proposal(proposal, discovered_fields=fields,
                                         strict_experiment=True)
        self.assertTrue(ok, problems)
