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


class TestSubmissionPool(TmpStateMixin, unittest.TestCase):
    def test_pool_records_only_platform_self_correlation_evidence(self):
        exp = Experiment(1, "h", "rank(field)", {}, ["field"])
        exp.alpha_id = "alpha-1"
        exp.proposal_id = "p-1"
        exp.metrics = {
            "sharpe": 2.0, "fitness": 2.0, "turnover": 0.1, "margin": 0.01,
            "checks": [{"name": "SELF_CORRELATION", "pass": True}], "passed": True,
        }
        exp.health = {"ok": True}
        exp.validation_status = "STABLE"
        evidence = self_correlation_evidence(exp.metrics)
        self.assertEqual(evidence["status"], "PASS")
        pool = SubmissionPool(self._tmp)
        pool.upsert(exp, "EXCELLENT", evidence, {"total": 1})
        with open(os.path.join(self._tmp, "submission_pool.json"), encoding="utf-8") as f:
            entry = json.load(f)["candidates"][0]
        self.assertEqual(entry["submission"], "MANUAL_REQUIRED")
        self.assertEqual(entry["self_correlation"]["status"], "PASS")

    def test_batch_upsert_writes_review_queue_once(self):
        pool = SubmissionPool(self._tmp)
        experiments = []
        for index in range(2):
            exp = Experiment(1, "h", f"rank(field_{index})", {}, [f"field_{index}"])
            exp.alpha_id = f"alpha-{index}"
            exp.proposal_id = f"proposal-{index}"
            exp.metrics = {"sharpe": 2.0}
            exp.health = {"ok": True}
            exp.validation_status = "STABLE"
            experiments.append((exp, "EXCELLENT", {"status": "PASS"}, None))
        with mock.patch("wqb_agent.submission.atomic_write_json_if_changed", wraps=atomic_write_json_if_changed) as writer:
            records = pool.upsert_many(experiments)
        self.assertEqual(len(records), 2)
        self.assertEqual(writer.call_count, 1)

    def test_pool_recovers_from_malformed_candidates_shape(self):
        path = os.path.join(self._tmp, "submission_pool.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"schema_version": 1, "candidates": ["bad", 3, {"alpha_id": "old"}]}, handle)
        exp = Experiment(1, "h", "rank(field)", {}, ["field"])
        exp.alpha_id = "new"
        exp.metrics = {"sharpe": 2.0}
        record = SubmissionPool(self._tmp).upsert(
            exp, "EXCELLENT", {"status": "PASS"}, None
        )
        self.assertEqual(record["alpha_id"], "new")
        with open(path, encoding="utf-8") as handle:
            self.assertEqual([item["alpha_id"] for item in json.load(handle)["candidates"]], ["old", "new"])

    def test_pending_self_correlation_is_not_failure(self):
        metrics = {
            "checks": [{
                "name": "SELF_CORRELATION",
                "pass": None,
                "result": "PENDING",
            }]
        }
        self.assertEqual(self_correlation_evidence(metrics)["status"], "PENDING")

    def test_result_only_self_correlation_is_normalized(self):
        metrics = {
            "checks": [{
                "name": "SELF_CORRELATION",
                "result": "PASS",
                "value": 0.2,
            }]
        }
        self.assertEqual(self_correlation_evidence(metrics)["status"], "PASS")



if __name__ == "__main__":
    unittest.main()
