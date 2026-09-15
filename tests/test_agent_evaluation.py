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

FAKE_FIELDS = {
    "pv1": [
        {"id": "close", "name": "Close price", "description": "daily close price of the stock"},
        {"id": "returns", "name": "Returns", "description": "daily simple returns"},
        {"id": "volume", "name": "Volume", "description": "daily trading volume"},
        {"id": "adv20", "name": "Average daily volume 20d", "description": "20 day average trading volume"},
        {"id": "high", "name": "High price", "description": "daily high price"},
        {"id": "low", "name": "Low price", "description": "daily low price"},
        {"id": "open", "name": "Open price", "description": "daily open price"},
        {"id": "vwap", "name": "VWAP", "description": "volume weighted average price"},
    ],
    "pv13": [
        {"id": "sector", "name": "Sector", "description": "sector classification"},
        {"id": "market_cap", "name": "Market cap", "description": "market capitalization"},
        {"id": "spread", "name": "Bid-ask spread", "description": "liquidity spread"},
    ],
    "analyst4": [
        {"id": "target_price", "name": "Analyst target price", "description": "consensus analyst target price"},
        {"id": "recommendation", "name": "Recommendation", "description": "analyst recommendation rating"},
        {"id": "eps_estimate", "name": "EPS estimate", "description": "analyst eps estimate"},
        {"id": "num_analysts", "name": "Number of analysts", "description": "analyst coverage count"},
    ],
    "option8": [
        {"id": "implied_vol", "name": "Implied volatility", "description": "option implied volatility"},
        {"id": "put_call_ratio", "name": "Put call ratio", "description": "option put call volume ratio"},
        {"id": "iv_skew", "name": "IV skew", "description": "implied volatility skew"},
        {"id": "option_volume", "name": "Option volume", "description": "total option trading volume"},
    ],
    "model16": [
        {"id": "risk_score", "name": "Model risk score", "description": "composite model risk score"},
        {"id": "model_factor", "name": "Model factor", "description": "model factor loading"},
        {"id": "pred_ret", "name": "Predicted return", "description": "model predicted return"},
    ],
    "news12": [
        {"id": "news_sentiment", "name": "News sentiment", "description": "news sentiment score"},
        {"id": "news_count", "name": "News count", "description": "number of news articles"},
        {"id": "headline_buzz", "name": "Headline buzz", "description": "headline attention score"},
    ],
}
def _fake_metrics(expression):
    sharpe = 0.1
    turnover = 1.8
    if "ts_mean" in expression:
        sharpe += 1.0
        turnover = 0.4
    if "ts_rank" in expression:
        sharpe += 0.3
    if "group_neutralize" in expression:
        sharpe += 0.2
    if "zscore" in expression:
        sharpe -= 0.1
    if expression.startswith("rank(close") or expression.startswith("-rank(close"):
        sharpe = -0.3
    if "badfield" in expression:
        return {
            "sharpe": 0.0,
            "fitness": 0.0,
            "turnover": 0.5,
            "margin": 0.0,
            "returns": 0.0,
            "drawdown": 0.0,
            "checks": [{"name": "syntax", "pass": False}],
        }
    return {
        "sharpe": sharpe,
        "fitness": sharpe,
        "turnover": turnover,
        "margin": sharpe * 0.1,
        "returns": sharpe * 0.05,
        "drawdown": 0.1,
        "checks": [{"name": "limitations", "pass": True}],
    }
BASE_CONFIG = {
    "simulation": {
        "instrumentType": "EQUITY",
        "region": "USA",
        "universe": "TOP3000",
        "delay": 1,
        "decay": 0,
        "neutralization": "SUBINDUSTRY",
        "truncation": 0.08,
        "pasteurization": "ON",
        "unitHandling": "VERIFY",
        "nanHandling": "ON",
    },
    "agent": {
        "max_rounds": 2,
        "candidates_per_round": 6,
        "max_concurrent_sims": 3,
        "fields_per_discovery": 6,
        "pagination_limit": 50,
        "max_pagination_pages": 20,
        "state_dir": None,
        "poll_timeout_sec": 30,
    },
}
def make_agent(tmpdir, rounds=2):
    config = json.loads(json.dumps(BASE_CONFIG))
    config["agent"]["state_dir"] = str(tmpdir)
    config["agent"]["max_rounds"] = rounds
    client = FakeClient()
    return Agent(client, config), client

class TmpStateMixin:
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="wqb_test_")

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)


class TestReflection(TmpStateMixin, unittest.TestCase):
    def test_reflect_normalizes_effective_evidence_once_per_experiment(self):
        from wqb_agent import reflection_evaluation

        memory = ExperienceMemory(state_dir=self._tmp)
        reflector = Reflector(memory)
        exp = Experiment(1, "h", "rank(x)", {}, ["x"])
        exp.status = "DONE"
        exp.metrics = _fake_metrics("rank(x)")
        with mock.patch.object(
            reflection_evaluation,
            "normalized_metrics",
            wraps=reflection_evaluation.normalized_metrics,
        ) as normalize:
            reflector.reflect(1, {"id": "h", "direction": "long"}, [exp])
        self.assertEqual(normalize.call_count, 1)

    def test_failed_high_score_does_not_replace_best(self):
        memory = ExperienceMemory(state_dir=self._tmp)
        reflector = Reflector(memory)
        good = Experiment(1, "h", "rank(good)", {}, ["good"])
        good.metrics = {
            "sharpe": 1.1, "fitness": 1.1, "turnover": 0.2,
            "returns": 0.1, "drawdown": 0.1, "margin": 0.01,
            "checks": [{"name": "LIMITS", "pass": True}],
            "passed": True,
        }
        good.status = "DONE"
        bad = Experiment(1, "h", "rank(noise)", {}, ["noise"])
        bad.metrics = {
            "sharpe": 9.0, "fitness": 20.0, "turnover": 0.1,
            "returns": 0.5, "drawdown": 0.1, "margin": 0.02,
            "checks": [{"name": "LIMITS", "pass": True}],
            "passed": True,
        }
        bad.status = "DONE"
        bad.health = {"ok": False, "reasons": ["CONCENTRATED_WEIGHT=FAIL"]}
        reflector.reflect(1, {"id": "h", "direction": "long"}, [good, bad])
        self.assertIsNone(memory.current_best)

    def test_sub_universe_health_failure_keeps_its_real_diagnosis(self):
        memory = ExperienceMemory(state_dir=self._tmp)
        reflector = Reflector(memory)
        exp = Experiment(1, "h", "rank(x)", {}, ["x"])
        exp.status = "DONE"
        exp.metrics = {
            "sharpe": -1.0, "fitness": -0.4, "turnover": 0.2,
            "returns": -0.03, "drawdown": 0.16, "margin": -0.0003,
            "checks": [{"name": "LOW_SUB_UNIVERSE_SHARPE", "pass": False}],
            "passed": False,
        }
        exp.health = {"ok": False, "reasons": ["LOW_SUB_UNIVERSE_SHARPE=FAIL v=-1.12"]}
        verdict = reflector._classify(exp)
        self.assertIn("sub_universe", verdict["diagnosis"])
        self.assertNotIn("weight_concentration", verdict["diagnosis"])

    def test_suspicious_high_signal_is_not_promoted(self):
        memory = ExperienceMemory(state_dir=self._tmp)
        reflector = Reflector(memory)
        exp = Experiment(1, "h", "rank(x)", {}, ["x"])
        exp.status = "DONE"
        exp.metrics = {
            "sharpe": 3.2, "fitness": 8.1, "turnover": 0.2,
            "returns": 0.2, "drawdown": 0.1, "margin": 0.01,
            "checks": [{"name": "LIMITS", "pass": True}], "passed": True,
        }
        exp.health = {"ok": True, "reasons": []}
        summary = reflector.reflect(1, {"id": "h", "direction": "long"}, [exp])
        self.assertEqual(summary["verdicts"]["SUSPICIOUS_HIGH_SIGNAL"], 1)
        self.assertIsNone(memory.current_best)
        self.assertEqual(memory.lessons, [])
        self.assertIsNone(memory.next_with_fields())

    def test_unverified_checks_and_missing_metrics_cannot_succeed(self):
        memory = ExperienceMemory(state_dir=self._tmp)
        reflector = Reflector(memory)
        exp = Experiment(1, "h", "rank(x)", {}, ["x"])
        exp.status = "DONE"
        exp.metrics = {
            "sharpe": 2.0, "fitness": None, "turnover": 0.2,
            "returns": 0.1, "drawdown": 0.1, "margin": 0.01,
            "checks": [], "passed": None,
        }
        verdict = reflector._classify(exp)
        self.assertNotEqual(verdict["label"], "SUCCESS")
        self.assertIn("missing_metrics", verdict["diagnosis"])

    def test_result_only_checks_are_accepted_when_resolved(self):
        memory = ExperienceMemory(state_dir=self._tmp)
        reflector = Reflector(memory)
        exp = Experiment(1, "h", "rank(x)", {}, ["x"])
        exp.status = "DONE"
        exp.metrics = {
            "sharpe": 1.1, "fitness": 0.8, "turnover": 0.2,
            "returns": 0.1, "drawdown": 0.1, "margin": 0.01,
            "checks": [{"name": "LIMITS", "result": "PASS"}],
            "passed": True,
        }
        verdict = reflector._classify(exp)
        self.assertNotEqual(verdict["label"], "RECONCILE")

    def test_single_success_is_observation_not_best_or_lesson(self):
        memory = ExperienceMemory(state_dir=self._tmp)
        reflector = Reflector(memory)
        exps = []
        for _, expr in enumerate(
            ["rank(close)", "rank(ts_mean(returns, 5))", "rank(ts_rank(returns, 20))"]
        ):
            e = Experiment(1, "h", expr, {}, ["returns"])
            e.metrics = _fake_metrics(expr)
            e.status = "DONE"
            exps.append(e)
        summary = reflector.reflect(1, {"tags": ["return"], "direction": "long"}, exps)
        self.assertIsNone(memory.current_best)
        self.assertEqual(memory.lessons, [])
        self.assertTrue(any(x["kind"] == "observation" for x in memory.short_term))
        self.assertIn("SUCCESS", summary["verdicts"])

    def test_fail_diagnosis_adds_avoid(self):
        memory = ExperienceMemory(state_dir=self._tmp)
        reflector = Reflector(memory)
        e = Experiment(1, "h", "rank(close)", {}, ["close"])
        e.status = "FAILED"
        e.error = "Simulation rejected (422): syntax error"
        reflector.reflect(1, {"tags": ["price"], "direction": "long"}, [e])
        self.assertEqual(len(memory.avoid), 1)
        self.assertIn("syntax", memory.avoid[0]["reason"])

    def test_marks_hypothesis_outcome(self):
        memory = ExperienceMemory(state_dir=self._tmp)
        reflector = Reflector(memory)
        e = Experiment(1, "h-1", "rank(ts_mean(returns, 5))", {}, ["returns"])
        e.metrics = _fake_metrics(e.expression)
        e.status = "DONE"
        hypothesis = {"id": "h-1", "tags": ["return"], "direction": "long", "_round": 1}
        memory.register_hypothesis(hypothesis)
        reflector.reflect(1, hypothesis, [e])
        active = {h["id"]: h for h in memory.active_hypotheses}
        self.assertEqual(active["h-1"]["status"], "success")

    def test_promising_next_carries_fields_and_datasets(self):
        memory = ExperienceMemory(state_dir=self._tmp)
        reflector = Reflector(memory)
        e = Experiment(1, "h", "rank(returns)", {}, ["returns"], ["pv1"])
        e.metrics = {
            "sharpe": 0.8, "fitness": 0.6, "turnover": 0.2,
            "returns": 0.03, "drawdown": 0.1, "margin": 0.003,
            "checks": [{"name": "LIMITS", "pass": True}], "passed": True,
        }
        e.status = "DONE"
        reflector.reflect(1, {
            "tags": ["return"], "direction": "long",
            "next_experiment": {
                "idea": "test a distinct mechanism",
                "expression": "rank(group_neutralize(returns, SUBINDUSTRY))",
                "change_reason": "separate raw strength from group-relative strength",
                "unresolved_question": "is the effect absolute or relative?",
                "competing_explanations": ["absolute", "relative"],
                "next_discriminating_question": "does group normalization preserve the effect?",
                "evidence_needed": ["same horizon"],
            },
        }, [e])
        top = memory.next_with_fields()
        self.assertIsNotNone(top)
        self.assertIn("returns", top.get("fields", []))
        self.assertIn("pv1", top.get("datasets", []))

    # ---- six-metric comprehensive gate --------------------------------

    @staticmethod
    def _done_experiment(expression, metrics):
        e = Experiment(1, "h", expression, {}, ["f"])
        e.metrics = metrics
        e.status = "DONE"
        return e

    def test_gate_promising_on_deep_drawdown(self):
        memory = ExperienceMemory(state_dir=self._tmp)
        reflector = Reflector(memory)
        exp = self._done_experiment(
            "rank(f)",
            {"sharpe": 1.5, "fitness": 1.5, "turnover": 0.4, "margin": 0.15,
             "returns": 0.1, "drawdown": 0.8,
             "checks": [{"name": "limitations", "pass": True}]},
        )
        result = reflector._classify(exp)
        self.assertEqual(result["label"], "PROMISING")
        self.assertIn("drawdown", result["diagnosis"])

    def test_gate_promising_on_nonpositive_margin(self):
        memory = ExperienceMemory(state_dir=self._tmp)
        reflector = Reflector(memory)
        exp = self._done_experiment(
            "rank(f)",
            {"sharpe": 1.5, "fitness": 1.5, "turnover": 0.4, "margin": -0.01,
             "returns": 0.1, "drawdown": 0.1,
             "checks": [{"name": "limitations", "pass": True}]},
        )
        result = reflector._classify(exp)
        self.assertEqual(result["label"], "PROMISING")
        self.assertIn("margin", result["diagnosis"])

    def test_gate_fail_on_low_sharpe_plus_deep_drawdown(self):
        memory = ExperienceMemory(state_dir=self._tmp)
        reflector = Reflector(memory)
        exp = self._done_experiment(
            "rank(f)",
            {"sharpe": 0.3, "fitness": 0.3, "turnover": 0.4, "margin": 0.03,
             "returns": 0.02, "drawdown": 0.8,
             "checks": [{"name": "limitations", "pass": True}]},
        )
        result = reflector._classify(exp)
        self.assertEqual(result["label"], "FAIL")
        self.assertIn("drawdown", result["diagnosis"])

    def test_gate_soft_low_turnover_still_success(self):
        memory = ExperienceMemory(state_dir=self._tmp)
        reflector = Reflector(memory)
        exp = self._done_experiment(
            "rank(f)",
            {"sharpe": 1.5, "fitness": 1.5, "turnover": 0.005, "margin": 0.15,
             "returns": 0.1, "drawdown": 0.1,
             "checks": [{"name": "limitations", "pass": True}]},
        )
        result = reflector._classify(exp)
        self.assertEqual(result["label"], "SUCCESS")
        self.assertTrue(any("turnover" in note for note in result["notes"]))

    def test_unknown_not_learned_or_avoided(self):
        memory = ExperienceMemory(state_dir=self._tmp)
        reflector = Reflector(memory)
        e = Experiment(1, "h", "rank(returns)", {}, ["returns"])
        e.status = "UNKNOWN"
        e.error = "UNKNOWN_LOCAL ConnectionError: proxy down"
        hypothesis = {"tags": ["return"], "direction": "long", "id": "h", "_round": 1}
        memory.register_hypothesis(hypothesis)
        reflector.reflect(1, hypothesis, [e])
        self.assertEqual(len(memory.avoid), 0)
        self.assertEqual(len(memory.lessons), 0)
        active = {h["id"]: h for h in memory.active_hypotheses}
        self.assertEqual(active["h"]["status"], "active")

    def test_unknown_and_system_failure_are_removed_from_persisted_dedupe(self):
        memory = ExperienceMemory(state_dir=self._tmp)
        reflector = Reflector(memory)
        unknown = Experiment(1, "h", "rank(unknown_field)", {}, ["unknown_field"])
        unknown.status = "UNKNOWN"
        unknown.error = "UNKNOWN_LOCAL ConnectionError: proxy down"
        failed = Experiment(1, "h", "rank(rate_limited)", {}, ["rate_limited"])
        failed.status = "FAILED"
        failed.error = "WQBRateLimitError: 429"
        memory.remember_expression(unknown.expression)
        memory.remember_expression(failed.expression)
        hypothesis = {"id": "h", "direction": "long", "_round": 1}
        memory.register_hypothesis(hypothesis)
        reflector.reflect(1, hypothesis, [unknown, failed])
        loaded = ExperienceMemory(state_dir=self._tmp).load()
        self.assertNotIn(unknown.expression, loaded.seen_expressions)
        self.assertNotIn(failed.expression, loaded.seen_expressions)

    @staticmethod
    def _interpreted_hypothesis(hypothesis_id, outcome, experiment_id):
        return {
            "id": hypothesis_id,
            "statement": "the field carries incremental information",
            "direction": "long",
            "falsification": "a resolved low-signal result falsifies the prediction",
            "_round": 1,
            "agent_interpretation": {
                "outcome": outcome,
                "mechanism_learning": "the evidence distinguishes the proposed mechanism",
                "evidence_refs": [experiment_id],
                "direct_relevance": True,
            },
        }

    @staticmethod
    def _full_metrics(sharpe=1.2, checks=None):
        return {
            "sharpe": sharpe,
            "fitness": sharpe,
            "turnover": 0.2,
            "returns": sharpe * 0.05,
            "drawdown": 0.1,
            "margin": 0.01,
            "checks": checks or [{"name": "LIMITS", "pass": True}],
        }

    def test_failed_mandatory_check_cannot_support_hypothesis(self):
        memory = ExperienceMemory(state_dir=self._tmp)
        reflector = Reflector(memory)
        exp = Experiment(1, "h-check", "rank(field)", {}, ["field"])
        exp.status = "DONE"
        exp.metrics = self._full_metrics(
            3.0, checks=[{"name": "SELF_CORRELATION", "pass": False}]
        )
        exp.health = {"ok": True, "reasons": []}
        hypothesis = self._interpreted_hypothesis("h-check", "SUPPORTED", exp.id)
        memory.register_hypothesis(hypothesis)

        reflector.reflect(1, hypothesis, [exp])

        active = {item["id"]: item for item in memory.active_hypotheses}
        self.assertEqual(active["h-check"]["outcome"], "INCONCLUSIVE")
        self.assertEqual(memory.lessons, [])

    def test_suspicious_signal_without_independent_validation_is_inconclusive(self):
        memory = ExperienceMemory(state_dir=self._tmp)
        reflector = Reflector(memory)
        exp = Experiment(1, "h-suspicious", "rank(field)", {}, ["field"])
        exp.status = "DONE"
        exp.metrics = self._full_metrics(3.2)
        exp.health = {"ok": True, "reasons": []}
        hypothesis = self._interpreted_hypothesis("h-suspicious", "SUPPORTED", exp.id)
        memory.register_hypothesis(hypothesis)

        reflector.reflect(1, hypothesis, [exp])

        active = {item["id"]: item for item in memory.active_hypotheses}
        self.assertEqual(active["h-suspicious"]["outcome"], "INCONCLUSIVE")
        self.assertEqual(memory.lessons, [])

    def test_reconcile_evidence_cannot_write_durable_mechanism_learning(self):
        memory = ExperienceMemory(state_dir=self._tmp)
        reflector = Reflector(memory)
        exp = Experiment(1, "h-reconcile", "rank(field)", {}, ["field"])
        exp.status = "DONE"
        exp.metrics = {"sharpe": 1.5}
        hypothesis = self._interpreted_hypothesis("h-reconcile", "SUPPORTED", exp.id)
        memory.register_hypothesis(hypothesis)

        reflector.reflect(1, hypothesis, [exp])

        active = {item["id"]: item for item in memory.active_hypotheses}
        self.assertEqual(active["h-reconcile"]["outcome"], "INCONCLUSIVE")
        self.assertEqual(memory.lessons, [])
        self.assertFalse(any(item.get("kind") == "mechanism" for item in memory.short_term))

    def test_system_failure_cannot_contradict_hypothesis_or_pollute_avoid(self):
        cases = (
            ("h-auth", "FAILED", "WQBAuthError: 401 unauthorized"),
            ("h-timeout", "FAILED", "WQBTimeoutError: Simulation polling timed out"),
            ("h-rate-limit", "FAILED", "WQBRateLimitError: 429 retry later"),
            ("h-submit-unknown", "SUBMIT_UNKNOWN", "POST result unknown"),
        )
        for hypothesis_id, status, error in cases:
            with self.subTest(status=status):
                memory = ExperienceMemory(state_dir=self._tmp + status)
                reflector = Reflector(memory)
                exp = Experiment(1, hypothesis_id, "rank(field)", {}, ["field"])
                exp.status = status
                exp.error = error
                hypothesis = self._interpreted_hypothesis(
                    hypothesis_id, "CONTRADICTED", exp.id
                )
                memory.register_hypothesis(hypothesis)

                reflector.reflect(1, hypothesis, [exp])

                active = {item["id"]: item for item in memory.active_hypotheses}
                self.assertEqual(active[hypothesis_id]["outcome"], "INCONCLUSIVE")
                self.assertEqual(memory.avoid, [])

    def test_single_complete_negative_evidence_is_not_contradiction(self):
        memory = ExperienceMemory(state_dir=self._tmp)
        reflector = Reflector(memory)
        exp = Experiment(1, "h-contradicted", "rank(field)", {}, ["field"])
        exp.status = "DONE"
        exp.metrics = self._full_metrics(0.2)
        hypothesis = self._interpreted_hypothesis("h-contradicted", "CONTRADICTED", exp.id)
        memory.register_hypothesis(hypothesis)

        reflector.reflect(1, hypothesis, [exp])

        active = {item["id"]: item for item in memory.active_hypotheses}
        self.assertEqual(active["h-contradicted"]["outcome"], "INCONCLUSIVE")

    def test_supported_observation_does_not_become_durable_lesson_once(self):
        memory = ExperienceMemory(state_dir=self._tmp)
        reflector = Reflector(memory)
        exp = Experiment(1, "h-supported", "rank(field)", {}, ["field"])
        exp.status = "DONE"
        exp.metrics = self._full_metrics(1.2)
        hypothesis = self._interpreted_hypothesis("h-supported", "SUPPORTED", exp.id)
        memory.register_hypothesis(hypothesis)

        reflector.reflect(1, hypothesis, [exp])

        active = {item["id"]: item for item in memory.active_hypotheses}
        self.assertEqual(active["h-supported"]["outcome"], "INCONCLUSIVE")
        self.assertEqual(memory.lessons, [])
        self.assertTrue(any(item.get("mechanism_learning") for item in memory.short_term))
        self.assertFalse(memory.context()["supported_mechanisms"])
        self.assertTrue(memory.context()["unresolved_mechanisms"])

    def test_mechanism_learning_promotes_only_after_independent_lineages(self):
        memory = ExperienceMemory(state_dir=self._tmp, short_term_window=5)
        reflector = Reflector(memory)
        first = Experiment(1, "h-independent", "rank(field_a)", {}, ["field_a"])
        second = Experiment(1, "h-independent", "rank(field_b)", {}, ["field_b"])
        for exp, lineage in ((first, "lineage-a"), (second, "lineage-b")):
            exp.status = "DONE"
            exp.metrics = self._full_metrics(1.2)
            exp.lineage_id = lineage
        hypothesis = self._interpreted_hypothesis(
            "h-independent", "SUPPORTED", first.id
        )
        hypothesis["agent_interpretation"]["evidence_refs"] = [first.id, second.id]
        memory.register_hypothesis(hypothesis)

        reflector.reflect(1, hypothesis, [first, second])
        active = {item["id"]: item for item in memory.active_hypotheses}
        self.assertEqual(active["h-independent"]["outcome"], "SUPPORTED")
        self.assertEqual(
            active["h-independent"]["confirmation_status"],
            "INDEPENDENT_CONFIRMED",
        )
        self.assertEqual(
            set(active["h-independent"]["independent_lineages"]),
            {"lineage-a", "lineage-b"},
        )
        self.assertEqual(memory.lessons, [])
        mechanism_entries = [
            item for item in memory.short_term if item.get("mechanism_learning")
        ]
        self.assertEqual(len(mechanism_entries), 1)
        self.assertEqual(
            set(mechanism_entries[0]["lineages"]), {"lineage-a", "lineage-b"}
        )

        memory.expire_short_term(now_round=6)
        self.assertEqual(len(memory.lessons), 1)

    def test_existing_independent_validation_can_confirm_one_experiment(self):
        memory = ExperienceMemory(state_dir=self._tmp)
        reflector = Reflector(memory)
        exp = Experiment(1, "h-validated", "rank(field)", {}, ["field"])
        exp.status = "DONE"
        exp.metrics = self._full_metrics(1.2)
        exp.validation_status = "STABLE"
        exp.validation_report = {"status": "PASS", "candidate": "parent"}
        hypothesis = self._interpreted_hypothesis("h-validated", "SUPPORTED", exp.id)
        memory.register_hypothesis(hypothesis)

        reflector.reflect(1, hypothesis, [exp])

        active = {item["id"]: item for item in memory.active_hypotheses}
        self.assertEqual(active["h-validated"]["outcome"], "SUPPORTED")
        self.assertEqual(
            active["h-validated"]["confirmation_status"],
            "INDEPENDENT_CONFIRMED",
        )
        self.assertFalse(active["h-validated"]["independent_lineages"])
        self.assertTrue(memory.context()["supported_mechanisms"])

        memory.expire_short_term(now_round=6)
        self.assertEqual(len(memory.lessons), 1)

    def test_same_lineage_repetition_is_not_independent_confirmation(self):
        memory = ExperienceMemory(state_dir=self._tmp)
        reflector = Reflector(memory)
        experiments = []
        for expression in ("rank(field_a)", "rank(field_b)"):
            exp = Experiment(1, "h-same-lineage", expression, {}, ["field"])
            exp.status = "DONE"
            exp.metrics = self._full_metrics(1.2)
            exp.lineage_id = "lineage-a"
            experiments.append(exp)
        hypothesis = self._interpreted_hypothesis(
            "h-same-lineage", "SUPPORTED", experiments[0].id
        )
        hypothesis["agent_interpretation"]["evidence_refs"] = [
            exp.id for exp in experiments
        ]
        memory.register_hypothesis(hypothesis)

        reflector.reflect(1, hypothesis, experiments)

        active = {item["id"]: item for item in memory.active_hypotheses}
        self.assertEqual(active["h-same-lineage"]["outcome"], "INCONCLUSIVE")
        self.assertEqual(memory.lessons, [])

    def test_parameter_only_variants_are_not_independent_confirmation(self):
        memory = ExperienceMemory(state_dir=self._tmp)
        reflector = Reflector(memory)
        first = Experiment(
            1, "h-parameter-confirmation", "rank(ts_zscore(field, 20))", {}, ["field"]
        )
        second = Experiment(
            1, "h-parameter-confirmation", "rank(ts_zscore(field, 21))", {}, ["field"]
        )
        for exp, lineage in ((first, "lineage-a"), (second, "lineage-b")):
            exp.status = "DONE"
            exp.metrics = self._full_metrics(1.2)
            exp.lineage_id = lineage
        hypothesis = self._interpreted_hypothesis(
            "h-parameter-confirmation", "SUPPORTED", first.id
        )
        hypothesis["agent_interpretation"]["evidence_refs"] = [first.id, second.id]
        memory.register_hypothesis(hypothesis)

        reflector.reflect(1, hypothesis, [first, second])

        active = {item["id"]: item for item in memory.active_hypotheses}
        self.assertEqual(
            active["h-parameter-confirmation"]["outcome"], "INCONCLUSIVE"
        )

    def test_invalid_evidence_member_blocks_confirmation(self):
        memory = ExperienceMemory(state_dir=self._tmp)
        reflector = Reflector(memory)
        first = Experiment(1, "h-invalid-evidence", "rank(field_a)", {}, ["field_a"])
        second = Experiment(1, "h-invalid-evidence", "rank(field_b)", {}, ["field_b"])
        first.status = "DONE"
        first.metrics = self._full_metrics(1.2)
        first.lineage_id = "lineage-a"
        second.status = "DONE"
        second.metrics = {"sharpe": 1.2}
        second.lineage_id = "lineage-b"
        hypothesis = self._interpreted_hypothesis(
            "h-invalid-evidence", "SUPPORTED", first.id
        )
        hypothesis["agent_interpretation"]["evidence_refs"] = [first.id, second.id]
        memory.register_hypothesis(hypothesis)

        reflector.reflect(1, hypothesis, [first, second])

        active = {item["id"]: item for item in memory.active_hypotheses}
        self.assertEqual(active["h-invalid-evidence"]["outcome"], "INCONCLUSIVE")

    def test_fake_evidence_ref_blocks_confirmation(self):
        memory = ExperienceMemory(state_dir=self._tmp)
        reflector = Reflector(memory)
        exp = Experiment(1, "h-fake-ref", "rank(field)", {}, ["field"])
        exp.status = "DONE"
        exp.metrics = self._full_metrics(1.2)
        hypothesis = self._interpreted_hypothesis("h-fake-ref", "SUPPORTED", exp.id)
        hypothesis["agent_interpretation"]["evidence_refs"] = ["does-not-exist"]
        memory.register_hypothesis(hypothesis)

        reflector.reflect(1, hypothesis, [exp])

        active = {item["id"]: item for item in memory.active_hypotheses}
        self.assertEqual(active["h-fake-ref"]["outcome"], "INCONCLUSIVE")

    def test_cross_hypothesis_evidence_cannot_confirm_current_hypothesis(self):
        memory = ExperienceMemory(state_dir=self._tmp)
        reflector = Reflector(memory)
        first = Experiment(1, "h-current", "rank(field_a)", {}, ["field_a"])
        second = Experiment(1, "h-other", "rank(field_b)", {}, ["field_b"])
        for exp, lineage in ((first, "lineage-a"), (second, "lineage-b")):
            exp.status = "DONE"
            exp.metrics = self._full_metrics(1.2)
            exp.lineage_id = lineage
        hypothesis = self._interpreted_hypothesis("h-current", "SUPPORTED", first.id)
        hypothesis["agent_interpretation"]["evidence_refs"] = [first.id, second.id]
        memory.register_hypothesis(hypothesis)

        reflector.reflect(1, hypothesis, [first, second])

        active = {item["id"]: item for item in memory.active_hypotheses}
        self.assertEqual(active["h-current"]["outcome"], "INCONCLUSIVE")

    def test_unvalidated_suspicious_evidence_cannot_confirm_with_normal_success(self):
        memory = ExperienceMemory(state_dir=self._tmp)
        reflector = Reflector(memory)
        suspicious = Experiment(1, "h-suspicious-pair", "rank(field_a)", {}, ["field_a"])
        normal = Experiment(1, "h-suspicious-pair", "rank(field_b)", {}, ["field_b"])
        suspicious.status = "DONE"
        suspicious.metrics = self._full_metrics(3.2)
        suspicious.lineage_id = "lineage-a"
        normal.status = "DONE"
        normal.metrics = self._full_metrics(1.2)
        normal.lineage_id = "lineage-b"
        hypothesis = self._interpreted_hypothesis(
            "h-suspicious-pair", "SUPPORTED", suspicious.id
        )
        hypothesis["agent_interpretation"]["evidence_refs"] = [
            suspicious.id, normal.id
        ]
        memory.register_hypothesis(hypothesis)

        reflector.reflect(1, hypothesis, [suspicious, normal])

        active = {item["id"]: item for item in memory.active_hypotheses}
        self.assertEqual(active["h-suspicious-pair"]["outcome"], "INCONCLUSIVE")

    def test_two_independent_negative_results_can_contradict(self):
        memory = ExperienceMemory(state_dir=self._tmp)
        reflector = Reflector(memory)
        experiments = []
        for expression, lineage in (
            ("rank(field_a)", "lineage-a"),
            ("rank(field_b)", "lineage-b"),
        ):
            exp = Experiment(1, "h-negative-pair", expression, {}, ["field"])
            exp.status = "DONE"
            exp.metrics = self._full_metrics(0.2)
            exp.lineage_id = lineage
            experiments.append(exp)
        hypothesis = self._interpreted_hypothesis(
            "h-negative-pair", "CONTRADICTED", experiments[0].id
        )
        hypothesis["agent_interpretation"]["evidence_refs"] = [
            exp.id for exp in experiments
        ]
        memory.register_hypothesis(hypothesis)

        reflector.reflect(1, hypothesis, experiments)

        active = {item["id"]: item for item in memory.active_hypotheses}
        self.assertEqual(active["h-negative-pair"]["outcome"], "CONTRADICTED")

    def test_negative_pair_without_direct_relevance_stays_inconclusive(self):
        memory = ExperienceMemory(state_dir=self._tmp)
        reflector = Reflector(memory)
        experiments = []
        for expression, lineage in (
            ("rank(field_a)", "lineage-a"),
            ("rank(field_b)", "lineage-b"),
        ):
            exp = Experiment(1, "h-negative-not-direct", expression, {}, ["field"])
            exp.status = "DONE"
            exp.metrics = self._full_metrics(0.2)
            exp.lineage_id = lineage
            experiments.append(exp)
        hypothesis = self._interpreted_hypothesis(
            "h-negative-not-direct", "CONTRADICTED", experiments[0].id
        )
        hypothesis["agent_interpretation"]["evidence_refs"] = [
            exp.id for exp in experiments
        ]
        hypothesis["agent_interpretation"]["direct_relevance"] = False
        memory.register_hypothesis(hypothesis)

        reflector.reflect(1, hypothesis, experiments)

        active = {item["id"]: item for item in memory.active_hypotheses}
        self.assertEqual(
            active["h-negative-not-direct"]["outcome"], "INCONCLUSIVE"
        )

    def test_next_experiment_keeps_discriminating_metadata_and_rejects_parameter_only(self):
        memory = ExperienceMemory(state_dir=self._tmp)
        reflector = Reflector(memory)
        exp = Experiment(1, "h-next", "rank(field)", {}, ["field"], ["pv1"])
        exp.status = "DONE"
        exp.metrics = self._full_metrics(1.2)
        hypothesis = self._interpreted_hypothesis("h-next", "SUPPORTED", exp.id)
        hypothesis["next_experiment"] = {
            "idea": "separate persistence from magnitude",
            "expression": "rank(group_neutralize(field, SUBINDUSTRY))",
            "change_reason": "distinguish cross-sectional normalization from raw level",
            "unresolved_question": "is the effect caused by persistence or magnitude?",
            "competing_explanations": ["persistence", "magnitude"],
            "next_discriminating_question": "does normalization preserve the effect?",
            "evidence_needed": ["same horizon", "independent lineage"],
        }
        memory.register_hypothesis(hypothesis)

        reflector.reflect(1, hypothesis, [exp])

        next_item = memory.next_with_fields()
        self.assertIsNotNone(next_item)
        self.assertEqual(next_item["parent_hypothesis"], "h-next")
        self.assertEqual(next_item["unresolved_question"], "is the effect caused by persistence or magnitude?")
        self.assertEqual(next_item["competing_explanations"], ["persistence", "magnitude"])
        self.assertEqual(next_item["next_discriminating_question"], "does normalization preserve the effect?")
        self.assertIn("field", next_item["fields"])
        self.assertIn("pv1", next_item["datasets"])

        parameter_memory = ExperienceMemory(state_dir=self._tmp + "-parameter")
        parameter_reflector = Reflector(parameter_memory)
        parameter_exp = Experiment(1, "h-parameter", "rank(ts_zscore(field, 20))", {}, ["field"])
        parameter_exp.status = "DONE"
        parameter_exp.metrics = self._full_metrics(1.2)
        parameter_hypothesis = self._interpreted_hypothesis(
            "h-parameter", "SUPPORTED", parameter_exp.id
        )
        parameter_hypothesis["next_experiment"] = {
            "idea": "change only the window",
            "expression": "rank(ts_zscore(field, 60))",
            "change_reason": "window change",
            "unresolved_question": "does the window matter?",
            "competing_explanations": ["short", "long"],
            "next_discriminating_question": "does a longer window work?",
            "evidence_needed": ["same field"],
        }
        parameter_memory.register_hypothesis(parameter_hypothesis)

        parameter_reflector.reflect(1, parameter_hypothesis, [parameter_exp])

        self.assertIsNone(parameter_memory.next_with_fields())

        overfit_memory = ExperienceMemory(state_dir=self._tmp + "-overfit")
        overfit_reflector = Reflector(overfit_memory)
        overfit_exp = Experiment(1, "h-overfit", "rank(field)", {}, ["field"])
        overfit_exp.status = "DONE"
        overfit_exp.metrics = self._full_metrics(1.2)
        overfit_hypothesis = self._interpreted_hypothesis(
            "h-overfit", "SUPPORTED", overfit_exp.id
        )
        overfit_hypothesis["next_experiment"] = {
            "idea": "stack fixed weighted legs",
            "expression": (
                "0.2*rank(ts_decay_linear(ts_zscore(a, 5), 3)) + "
                "0.3*rank(ts_decay_linear(ts_zscore(b, 10), 5)) + "
                "0.5*rank(ts_decay_linear(ts_zscore(c, 20), 10))"
            ),
            "change_reason": "more tuned legs",
            "unresolved_question": "which blend scores best?",
            "competing_explanations": ["leg a", "leg b"],
            "next_discriminating_question": "does the blend score best?",
            "evidence_needed": ["independent lineage"],
        }
        overfit_memory.register_hypothesis(overfit_hypothesis)

        overfit_reflector.reflect(1, overfit_hypothesis, [overfit_exp])

        self.assertIsNone(overfit_memory.next_with_fields())



if __name__ == "__main__":
    unittest.main()
