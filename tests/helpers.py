"""Shared synthetic builders and the offline agent harness for the test suite.

Builders create synthetic evidence only: no real research exports, no network
access and no `.wqb_state` mutation.
"""

import datetime as dt
import json
import os
import shutil
import tempfile
import threading
import time

from wqb_agent.agent import Agent


def utc_timestamp(value):
    return value.replace(tzinfo=dt.UTC).timestamp()


def diversity_proposal(expression, *, concept="fundamental", measurement="level",
                       behavior="slow_moving", template_family="persistent_level",
                       dataset="fundamental6", lineage_id=None, layer="exploration",
                       relationship_type=None):
    traits = {
        "concept": concept, "measurement": measurement, "behavior": behavior,
        "status": "KNOWN" if concept != "unknown" else "UNKNOWN",
    }
    item = {
        "expression": expression,
        "template_family": template_family,
        "research_layer": layer,
        "field_analysis": {"field": {"semantic_traits": traits}},
        "field_refs": [{"id": "field", "dataset": dataset}],
        "datasets": [dataset],
    }
    if lineage_id is not None:
        item["lineage_id"] = lineage_id
    if relationship_type is not None:
        item["relationship_audit"] = {"relationship_type": relationship_type}
    return item


def proposal(index, origin="factory"):
    return {
        "expression": f"rank(field_{index})",
        "proposal_origin": origin,
        "research_layer": "exploration" if origin == "factory" else "optimization",
        "research_role": "EXPLORE" if origin == "factory" else "EXPLOIT",
        "experiment_stage": "BASELINE" if origin == "factory" else "CHILD",
        "exploration_objective": "signal_discovery" if origin == "factory" else None,
        "fields": [f"field_{index}"],
    }


def operator_reference():
    root = os.path.dirname(os.path.dirname(__file__))
    from wqb_agent.proposal_contract import _operator_reference

    reference = _operator_reference(
        os.path.join(root, "docs", "reference", "OPERATORS_CHEATSHEET.md")
    )
    reference.update({
        "source": "BRAIN_LIVE_ONLY", "status": "LIVE_VERIFIED",
        "availability": "AVAILABLE", "valid": True,
        "capability_fingerprint": reference["sha256"],
    })
    return reference


def semantic_field(field_id, description, *, dataset="research1",
                   frequency="daily", category=None):
    return {
        "id": field_id,
        "name": description,
        "description": description,
        "dataset": dataset,
        "type": "MATRIX",
        "frequency": frequency,
        "category": category or "market",
        "coverage": 0.9,
        "semantic_status": "KNOWN",
    }


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
        return operator_reference()

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
