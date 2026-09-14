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

class FakeClient:
    def __init__(self, latency=0.01):
        self.counter = 0
        self.latency = latency
        self._expr_by_url = {}
        self._alpha_expr = {}
        self.sim_calls = []
        self.max_active = 0
        self._active = 0
        self._lock = threading.Lock()
        self.datafield_calls = []

    def get_operator_capability(self):
        from wqb_agent.proposal_contract import load_operator_syntax_reference

        root = os.path.dirname(os.path.dirname(__file__))
        reference = load_operator_syntax_reference(
            os.path.join(root, "docs", "reference", "OPERATORS_CHEATSHEET.md")
        )
        reference.update({
            "source": "BRAIN_LIVE_ONLY", "status": "LIVE_VERIFIED",
            "availability": "AVAILABLE", "valid": True,
            "capability_fingerprint": reference["sha256"],
        })
        return reference

    def get_datafields(self, dataset_id, limit=50, offset=0, field_type=None):
        self.datafield_calls.append((dataset_id, limit, offset))
        all_fields = FAKE_FIELDS.get(dataset_id, [])
        return all_fields[offset : offset + limit], len(all_fields)

    def submit_simulation(self, expression, settings, alpha_type="REGULAR"):
        with self._lock:
            self._active += 1
            self.max_active = max(self.max_active, self._active)
        time.sleep(self.latency)
        with self._lock:
            self._active -= 1
        self.counter += 1
        url = f"progress-{self.counter}"
        self._expr_by_url[url] = expression
        self.sim_calls.append(expression)
        return url

    def poll_progress(self, progress_url, timeout_sec=900):
        time.sleep(self.latency)
        expression = self._expr_by_url[progress_url]
        alpha_id = f"alpha-{abs(hash(progress_url))}"
        self._alpha_expr[alpha_id] = expression
        return alpha_id

    def get_alpha(self, alpha_id):
        expression = self._alpha_expr.get(alpha_id, "")
        return {"is": _fake_metrics(expression), "regular": expression}


class TmpStateMixin:
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="wqb_test_")

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)


class TestAgentLoop(TmpStateMixin, unittest.TestCase):
    def test_current_discovery_type_overlays_cached_type(self):
        agent, _ = make_agent(self._tmp, rounds=1)
        result = agent._known_field_types(
            {"fields": [{"id": "shared", "type": "VECTOR"}]},
            cached_field_types={"shared": "MATRIX", "cached_only": "MATRIX"},
        )
        self.assertEqual(result["shared"], "VECTOR")
        self.assertEqual(result["cached_only"], "MATRIX")

    def test_malformed_numeric_metrics_fail_closed(self):
        agent, _ = make_agent(self._tmp, rounds=1)
        self.assertEqual(
            agent._alpha_rating({
                "sharpe": "not-a-number", "fitness": 2,
                "turnover": 0.1, "margin": 0.01,
            }),
            "UNRATED",
        )

    def test_alpha_rating_shares_the_pre_correlation_turnover_bounds(self):
        agent, _ = make_agent(self._tmp, rounds=1)
        metrics = {
            "sharpe": 1.5, "fitness": 1.1, "turnover": 0.5, "margin": 0.01,
        }
        self.assertEqual(agent._alpha_rating(metrics), "GOOD")
        agent.quality_policy = dict(agent.quality_policy, max_turnover=0.3)
        self.assertEqual(agent._alpha_rating(metrics), "BELOW_GOOD")

    def test_legacy_entrypoints_are_blocked_without_side_effects(self):
        for entrypoint, args in (("run", ()), ("run_one_round", (1,))):
            with self.subTest(entrypoint=entrypoint):
                agent, client = make_agent(self._tmp, rounds=2)
                with self.assertRaises(RuntimeError):
                    getattr(agent, entrypoint)(*args)
                self.assertEqual(client.sim_calls, [])
                self.assertFalse(
                    os.path.exists(os.path.join(self._tmp, "context.md"))
                )

    def test_checkpoint_is_durable_before_submit_on_agent_path(self):
        from wqb_agent.client import WQBSubmitUnknownError

        class DurableAmbiguousClient(FakeClient):
            def __init__(self, state_dir):
                super().__init__(latency=0)
                self.state_dir = state_dir
                self.checkpoint_status = None
                self.checkpoint_proposal_id = None

            def submit_simulation(self, expression, settings, alpha_type="REGULAR",
                                  idempotency_key=None):
                with open(os.path.join(self.state_dir, "round_1.checkpoint.json"),
                          encoding="utf-8") as handle:
                    checkpoint = json.load(handle)
                row = checkpoint["experiments"][0]
                self.checkpoint_status = row["status"]
                self.checkpoint_proposal_id = row["proposal_id"]
                self.sim_calls.append(expression)
                raise WQBSubmitUnknownError("response lost after POST")

        config = json.loads(json.dumps(BASE_CONFIG))
        config["agent"]["state_dir"] = self._tmp
        client = DurableAmbiguousClient(self._tmp)
        agent = Agent(client, config)
        hypothesis = {"id": "h", "statement": "test"}
        exp = Experiment(1, "h", "rank(field)", {}, [], ["pv1"])
        exp.proposal_id = "p1"
        exp.allocation_key = "p1"
        agent._write_proposal_checkpoint(1, hypothesis, [exp], complete=False)
        agent.simulator.run(
            [exp],
            on_update=lambda item: agent._on_simulation_update(
                item, 1, hypothesis, [exp]
            ),
        )
        self.assertEqual(client.checkpoint_status, "SUBMITTING")
        self.assertEqual(client.checkpoint_proposal_id, "p1")
        self.assertEqual(exp.status, "SUBMIT_UNKNOWN")

    def test_reloaded_submit_unknown_checkpoint_never_posts(self):
        class CountingClient(FakeClient):
            def submit_simulation(self, expression, settings, alpha_type="REGULAR",
                                  idempotency_key=None):
                self.sim_calls.append(expression)
                raise AssertionError("SUBMIT_UNKNOWN recovery must not POST")

        config = json.loads(json.dumps(BASE_CONFIG))
        config["agent"]["state_dir"] = self._tmp
        first = Agent(CountingClient(), config)
        exp = Experiment(1, "h", "rank(field)", {}, [], ["pv1"])
        exp.proposal_id = "p1"
        exp.status = "SUBMIT_UNKNOWN"
        exp.error = "WQBSubmitUnknownError: response lost after POST"
        first._write_proposal_checkpoint(
            1, {"id": "h", "statement": "test"}, [exp], complete=False
        )

        restarted_client = CountingClient()
        restarted = Agent(restarted_client, config)
        checkpoint = restarted._load_proposal_checkpoint(1)
        self.assertIsNotNone(checkpoint)
        self.assertIsNone(restarted._resume_proposal_checkpoint(checkpoint))
        self.assertEqual(restarted_client.sim_calls, [])
        self.assertFalse(restarted._load_proposal_checkpoint(1)["complete"])

    def test_submit_unknown_pauses_same_checkpoint_pending_work(self):
        client = FakeClient(latency=0)
        config = json.loads(json.dumps(BASE_CONFIG))
        config["agent"]["state_dir"] = self._tmp
        agent = Agent(client, config)
        unknown = Experiment(1, "h", "rank(unknown_field)", {}, [], ["pv1"])
        unknown.proposal_id = "p-unknown"
        unknown.status = "SUBMIT_UNKNOWN"
        pending = Experiment(1, "h", "rank(pending_field)", {}, [], ["pv1"])
        pending.proposal_id = "p-pending"
        pending.status = "PENDING"
        agent._write_proposal_checkpoint(
            1, {"id": "h", "statement": "test"}, [unknown, pending], complete=False
        )

        checkpoint = agent._load_proposal_checkpoint(1)
        self.assertIsNone(agent._resume_proposal_checkpoint(checkpoint))
        self.assertEqual(client.sim_calls, [])
        restored = agent._load_proposal_checkpoint(1)
        self.assertEqual(
            {row["status"] for row in restored["experiments"]},
            {"SUBMIT_UNKNOWN", "PENDING"},
        )
        self.assertFalse(restored["complete"])

    def test_submit_unknown_still_polls_known_progress_urls(self):
        client = FakeClient(latency=0)
        client._expr_by_url["progress-known"] = "rank(known_field)"
        config = json.loads(json.dumps(BASE_CONFIG))
        config["agent"]["state_dir"] = self._tmp
        agent = Agent(client, config)
        unknown = Experiment(1, "h", "rank(unknown_field)", {}, [], ["pv1"])
        unknown.status = "SUBMIT_UNKNOWN"
        known = Experiment(1, "h", "rank(known_field)", {}, [], ["pv1"])
        known.status = "RUNNING"
        known.progress_url = "progress-known"
        pending = Experiment(1, "h", "rank(pending_field)", {}, [], ["pv1"])
        pending.status = "PENDING"
        agent._write_proposal_checkpoint(
            1, {"id": "h", "statement": "test"},
            [unknown, known, pending], complete=False,
        )

        checkpoint = agent._load_proposal_checkpoint(1)
        self.assertIsNone(agent._resume_proposal_checkpoint(checkpoint))
        self.assertEqual(client.sim_calls, [])
        restored = agent._load_proposal_checkpoint(1)
        statuses = {row["expression"]: row["status"] for row in restored["experiments"]}
        self.assertEqual(statuses["rank(known_field)"], "DONE")
        self.assertEqual(statuses["rank(pending_field)"], "PENDING")
        self.assertEqual(statuses["rank(unknown_field)"], "SUBMIT_UNKNOWN")

    def test_agent_submit_unknown_update_keeps_budget_and_arm_occupied(self):
        config = json.loads(json.dumps(BASE_CONFIG))
        config["agent"]["state_dir"] = self._tmp
        config["agent"]["search_policy"] = {
            "enabled": True,
            "max_simulations": 2,
            "max_pending_per_arm": 2,
        }
        agent = Agent(FakeClient(), config)
        proposal = {
            "proposal_id": "p1",
            "expression": "rank(field)",
            "datasets": ["pv1"],
            "template_family": "trend",
        }
        self.assertTrue(agent.search_policy.accept(proposal))
        self.assertTrue(agent.search_policy.commit(proposal))
        exp = Experiment(1, "h", "rank(field)", {}, [], ["pv1"])
        exp.proposal_id = "p1"
        exp.allocation_key = "p1"
        exp.status = "SUBMIT_UNKNOWN"
        agent._on_simulation_update(exp, 1, {"id": "h"}, [exp])
        state = agent.search_policy.allocator.proposals["p1"]
        arm = agent.search_policy.allocator.arms[state["arm"]]
        self.assertEqual(agent.search_policy.allocator.consumed_budget, 1)
        self.assertEqual(state["status"], "UNKNOWN")
        self.assertEqual(arm["unknown"], 1)

    def test_suggestion_exports_real_fields_bundle(self):
        agent, client = make_agent(self._tmp, rounds=1)
        agent.run_suggestion_round(round_no=1)
        path = os.path.join(self._tmp, "suggestions.json")
        self.assertTrue(os.path.exists(path))
        with open(path, encoding="utf-8") as f:
            bundle = json.load(f)
        self.assertEqual(bundle["round_no"], 1)
        self.assertGreater(len(bundle["fields"]), 0)
        self.assertIn("context", bundle)
        self.assertIn("current_best", bundle["context"])
        self.assertGreaterEqual(len(bundle["alpha_templates"]), 6)
        self.assertIn("research_guard", bundle)
        self.assertIn("optimizer_context", bundle)
        self.assertEqual(bundle["research_guard"]["policy"], "same_change_type_no_gain_guard")

    def test_stalled_dataset_rotates_to_an_unseen_family(self):
        agent, _ = make_agent(self._tmp, rounds=1)
        for index in range(12):
            experiment = Experiment(
                10 + index, "h-analyst", f"rank(field_{index})", {},
                [f"field_{index}"], ["analyst4"],
            )
            experiment.status = "DONE"
            agent.trajectory.add(experiment)

        rotated = agent.suggestion_workflow.rotate_stalled_research_space(
            {"id": "analyst", "statement": "test", "datasets": ["analyst4"]},
            round_no=11,
        )

        self.assertNotIn("analyst4", rotated["datasets"])
        self.assertTrue(rotated.get("rotation_reason"))

    def test_suggestion_falls_back_after_empty_discovery(self):
        agent, client = make_agent(self._tmp, rounds=1)
        calls = []

        def discover(space, target_count):
            calls.append(space["datasets"])
            if len(calls) == 1:
                return []
            return [{
                "id": "cashflow_op",
                "name": "Operating cash flow",
                "description": "Operating cash flow generated by the business",
                "dataset": "fundamental6",
                "type": "MATRIX",
                "coverage": 0.9,
                "frequency": None,
                "alpha_count": 1,
                "semantic_status": "KNOWN",
                "field_source": agent.discovery.source_provenance(),
            }]

        agent.discovery.discover = discover
        bundle = agent.run_suggestion_round(round_no=1)

        self.assertEqual(len(calls), 2)
        self.assertEqual(bundle["research_space"]["datasets"], ["fundamental6", "fundamental2"])
        self.assertEqual(bundle["fields"][0]["id"], "cashflow_op")

    def test_suggestion_keeps_previous_bundle_fields(self):
        """2026-08-22 政策：字段级全量排除已删除，上轮字段可再次进入 discovery；
        重复防护收敛到 run_proposals 的表达式级去重。"""
        agent, client = make_agent(self._tmp, rounds=1)
        previous = os.path.join(self._tmp, "suggestions.json")
        with open(previous, "w", encoding="utf-8") as f:
            json.dump({"round_no": 0, "fields": [{"id": "old_field"}]}, f)

        def discover(space, target_count):
            return [
                {"id": "old_field", "description": "old", "type": "MATRIX"},
                {"id": "new_field", "description": "new", "type": "MATRIX"},
            ]

        agent.discovery.discover = discover
        bundle = agent.run_suggestion_round(round_no=1)

        self.assertEqual(
            [field["id"] for field in bundle["fields"]], ["old_field", "new_field"]
        )

    def test_suggestion_keeps_fields_outside_trajectory_window(self):
        """2026-08-22 政策：trajectory 历史字段不再被 discovery 排除。"""
        agent, client = make_agent(self._tmp, rounds=1)
        with open(agent.trajectory.path, "w", encoding="utf-8") as f:
            f.write(json.dumps({"round": 1, "fields_used": ["old_field"]}) + "\n")

        def discover(space, target_count):
            return [
                {"id": "old_field", "description": "old", "type": "MATRIX"},
                {"id": "new_field", "description": "new", "type": "MATRIX"},
            ]

        agent.discovery.discover = discover
        bundle = agent.run_suggestion_round(round_no=1)

        self.assertEqual(
            [field["id"] for field in bundle["fields"]], ["old_field", "new_field"]
        )

    def test_epoch_label_hex(self):
        """rb 纪元标签：r619->rb0, r620->rb1, r634->rbf, r635->rb10。"""
        agent, _ = make_agent(self._tmp, rounds=1)
        self.assertEqual(agent.epoch_label(619), "rb0")
        self.assertEqual(agent.epoch_label(620), "rb1")
        self.assertEqual(agent.epoch_label(634), "rbf")
        self.assertEqual(agent.epoch_label(635), "rb10")



if __name__ == "__main__":
    unittest.main()
