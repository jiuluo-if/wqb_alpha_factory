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


class TestSimulator(unittest.TestCase):
    def test_terminal_complete_callback_is_acknowledged_before_terminal_update(self):
        """终态 checkpoint 通知必须晚于 canonical evidence 回调。"""
        events = []
        client = FakeClient(latency=0)
        exp = Experiment(1, "h", "rank(field)", {}, [])

        def on_complete(item):
            events.append(("complete", item.status))
            return True

        def on_update(item):
            events.append(("update", item.status))

        Simulator(client, max_concurrent=1, poll_timeout_sec=30).run(
            [exp], on_complete=on_complete, on_update=on_update
        )

        self.assertEqual(
            events[-2:], [("complete", "DONE"), ("update", "DONE")]
        )

    def test_terminal_complete_callback_failure_is_not_swallowed(self):
        """canonical evidence 失败时不得发出终态 checkpoint 更新。"""
        events = []
        client = FakeClient(latency=0)
        exp = Experiment(1, "h", "rank(field)", {}, [])

        def on_complete(_item):
            raise RuntimeError("trajectory unavailable")

        def on_update(item):
            events.append(item.status)

        with self.assertRaisesRegex(RuntimeError, "trajectory unavailable"):
            Simulator(client, max_concurrent=1, poll_timeout_sec=30).run(
                [exp], on_complete=on_complete, on_update=on_update
            )

        self.assertNotIn("DONE", events)

    def test_concurrency_limited(self):
        client = FakeClient(latency=0.05)
        sim = Simulator(client, max_concurrent=3, poll_timeout_sec=30)
        exps = [Experiment(1, "h", f"rank(field{i})", {}, []) for i in range(7)]
        sim.run(exps)
        self.assertEqual(len(exps), 7)
        self.assertTrue(all(e.status == "DONE" for e in exps))
        self.assertLessEqual(client.max_active, 3)
        self.assertEqual(len(client.sim_calls), 7)

    def test_failure_recorded(self):
        class FailingClient(FakeClient):
            def submit_simulation(self, expression, settings, alpha_type="REGULAR"):
                raise Exception("Simulation rejected (422): bad expression")

        sim = Simulator(FailingClient(), max_concurrent=3, poll_timeout_sec=5)
        exp = Experiment(1, "h", "badfield(x)", {}, [])
        sim.run([exp])
        self.assertEqual(exp.status, "FAILED")
        self.assertIn("Simulation rejected", exp.error)

    def test_extracts_drawdown_and_six_metrics(self):
        client = FakeClient()
        sim = Simulator(client, max_concurrent=1, poll_timeout_sec=30)
        exp = Experiment(1, "h", "rank(ts_mean(returns, 5))", {}, ["returns"])
        sim.run([exp])
        m = exp.metrics
        for key in ("sharpe", "fitness", "turnover", "margin", "returns", "drawdown"):
            self.assertIsNotNone(m.get(key), f"missing metric {key}")
        self.assertEqual(m["drawdown"], 0.1)
        self.assertIn("value", m["checks"][0])
        self.assertIn("limit", m["checks"][0])
        self.assertIn("result", m["checks"][0])

    def test_unknown_pauses_dispatch(self):
        class NetworkFailingClient(FakeClient):
            def submit_simulation(self, expression, settings, alpha_type="REGULAR"):
                import requests
                raise requests.exceptions.ConnectionError("proxy down")

        client = NetworkFailingClient(latency=0.01)
        sim = Simulator(client, max_concurrent=3, poll_timeout_sec=30)
        exps = [Experiment(1, "h", f"rank(field{i})", {}, []) for i in range(6)]
        sim.run(exps)
        # 请求在传输层失败时无法证明 BRAIN 没有接收 POST；按 exactly-once
        # 语义保留为 SUBMIT_UNKNOWN，禁止下一轮自动重发。
        self.assertEqual(exps[0].status, "SUBMIT_UNKNOWN")
        self.assertTrue(sim.paused_reason)
        self.assertEqual(len(client.sim_calls), 0)  # 连接层失败未发出任何 POST
        # 3 个已 in-flight 的不回收；暂停后其余 3 个未派发
        self.assertEqual(
            sum(1 for e in exps if e.status == "PENDING"), 3
        )

    def test_ambiguous_submit_is_never_reposted(self):
        """丢失 POST 响应必须保留 SUBMIT_UNKNOWN，而非重复烧预算。"""
        from wqb_agent.client import WQBSubmitUnknownError

        class AmbiguousClient(FakeClient):
            def submit_simulation(self, expression, settings, alpha_type="REGULAR"):
                self.sim_calls.append(expression)  # BRAIN may already have accepted it
                raise WQBSubmitUnknownError("response lost after POST")

        client = AmbiguousClient()
        checkpoints = []
        exp = Experiment(1, "h", "rank(field)", {}, [])
        Simulator(client, max_concurrent=1, poll_timeout_sec=30).run(
            [exp], on_update=lambda item: checkpoints.append(item.status)
        )
        self.assertEqual(exp.status, "SUBMIT_UNKNOWN")
        self.assertEqual(len(client.sim_calls), 1)
        self.assertEqual(checkpoints, ["SUBMITTING", "SUBMIT_UNKNOWN"])

    def test_submit_unknown_second_run_does_not_post_again(self):
        """恢复扫描再次遇到 SUBMIT_UNKNOWN 时只能保留对账状态。"""
        from wqb_agent.client import WQBSubmitUnknownError

        class AmbiguousClient(FakeClient):
            def submit_simulation(self, expression, settings, alpha_type="REGULAR"):
                self.sim_calls.append(expression)
                raise WQBSubmitUnknownError("response lost after POST")

        client = AmbiguousClient()
        sim = Simulator(client, max_concurrent=1, poll_timeout_sec=30)
        exp = Experiment(1, "h", "rank(field)", {}, [])
        sim.run([exp])
        sim.run([exp])
        self.assertEqual(exp.status, "SUBMIT_UNKNOWN")
        self.assertEqual(len(client.sim_calls), 1)

    def test_checkpoint_update_precedes_first_submit(self):
        """本地 SUBMITTING 证据必须先于第一次 POST。"""
        events = []

        class OrderedClient(FakeClient):
            def submit_simulation(self, expression, settings, alpha_type="REGULAR"):
                events.append("post")
                return super().submit_simulation(expression, settings, alpha_type)

        client = OrderedClient(latency=0)
        sim = Simulator(client, max_concurrent=1, poll_timeout_sec=30)
        exp = Experiment(1, "h", "rank(field)", {}, [])
        sim.run([exp], on_update=lambda item: events.append(item.status))
        self.assertLess(events.index("SUBMITTING"), events.index("post"))

    def test_rolling_window_stays_full(self):
        client = FakeClient(latency=0.05)
        sim = Simulator(client, max_concurrent=3, poll_timeout_sec=30)
        exps = [Experiment(1, "h", f"rank(field{i})", {}, []) for i in range(9)]
        sim.run(exps)
        self.assertEqual(len(exps), 9)
        self.assertTrue(all(e.status == "DONE" for e in exps))
        self.assertLessEqual(client.max_active, 3)
        self.assertEqual(len(client.sim_calls), 9)

    def test_rate_limit_without_contract_is_submit_unknown(self):
        """没有 API 明确写入保证的 429 不能自动重发。"""
        from wqb_agent.client import WQBRateLimitError

        class FlakyClient(FakeClient):
            def __init__(self, failures=2):
                super().__init__(latency=0.01)
                self.failures = failures

            def submit_simulation(self, expression, settings, alpha_type="REGULAR"):
                if self.failures > 0:
                    self.failures -= 1
                    raise WQBRateLimitError("rate-limited after 5 attempts (429).")
                return super().submit_simulation(expression, settings, alpha_type)

        client = FlakyClient(failures=2)
        sim = Simulator(client, max_concurrent=1, poll_timeout_sec=30,
                        replace_attempts=3, replace_backoff_sec=0)
        exp = Experiment(1, "h", "rank(ts_mean(returns, 5))", {}, [])
        sim.run([exp])
        self.assertEqual(exp.status, "SUBMIT_UNKNOWN", exp.error)
        self.assertEqual(len(client.sim_calls), 0)
        self.assertTrue(sim.paused_reason)
        self.assertIsNotNone(exp.elapsed_sec)
        self.assertGreaterEqual(exp.elapsed_sec, 0.0)

    def test_rate_limit_never_marks_failed_without_write_contract(self):
        """429 没有幂等/拒绝契约时保留对账状态和预算槽。"""
        from wqb_agent.client import WQBRateLimitError

        class AlwaysLimitedClient(FakeClient):
            def submit_simulation(self, expression, settings, alpha_type="REGULAR"):
                raise WQBRateLimitError("rate-limited after 5 attempts (429).")

        client = AlwaysLimitedClient()
        sim = Simulator(client, max_concurrent=1, poll_timeout_sec=30,
                        replace_attempts=3, replace_backoff_sec=0)
        exp = Experiment(1, "h", "rank(ts_mean(returns, 5))", {}, [])
        sim.run([exp])
        self.assertEqual(exp.status, "SUBMIT_UNKNOWN")
        self.assertIn("WQBRateLimitError", exp.error)
        self.assertTrue(sim.paused_reason)
        self.assertIsNotNone(exp.elapsed_sec)

    def test_poll_timeout_reuses_known_progress_url(self):
        """轮询超时只重试同一 BRAIN job，不可重复提交。"""
        from wqb_agent.client import WQBTimeoutError

        class TimeoutOnceClient(FakeClient):
            def __init__(self):
                super().__init__(latency=0.01)
                self._once = True

            def poll_progress(self, progress_url, timeout_sec=900,
                              progress_callback=None):
                if self._once:
                    self._once = False
                    raise WQBTimeoutError("Simulation polling timed out.")
                return super().poll_progress(progress_url, timeout_sec)

        client = TimeoutOnceClient()
        sim = Simulator(client, max_concurrent=1, poll_timeout_sec=30,
                        replace_attempts=3, replace_backoff_sec=0)
        exp = Experiment(1, "h", "rank(ts_mean(returns, 5))", {}, [])
        sim.run([exp])
        self.assertEqual(exp.status, "DONE", exp.error)
        self.assertEqual(len(client.sim_calls), 1)

    def test_known_progress_unknown_does_not_pause_new_submissions(self):
        """已知远程作业只需只读恢复，不应阻塞独立待派发题案。"""
        import requests

        class PollFailingClient(FakeClient):
            def poll_progress(self, progress_url, timeout_sec=900,
                              progress_callback=None):
                if progress_url.endswith("known"):
                    raise requests.exceptions.ConnectionError("proxy down")
                return super().poll_progress(progress_url, timeout_sec)

        client = PollFailingClient(latency=0)
        known = Experiment(1, "h", "rank(known_field)", {}, [])
        known.progress_url = "https://api.worldquantbrain.com/simulations/known"
        pending = Experiment(1, "h", "rank(new_field)", {}, [])

        Simulator(
            client, max_concurrent=1, poll_timeout_sec=30,
            replace_attempts=1, replace_backoff_sec=0,
        ).run([known, pending])

        self.assertEqual(known.status, "UNKNOWN")
        self.assertEqual(pending.status, "DONE")
        self.assertEqual(len(client.sim_calls), 1)



if __name__ == "__main__":
    unittest.main()
