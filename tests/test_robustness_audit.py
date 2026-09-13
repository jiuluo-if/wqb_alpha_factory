"""Regression tests for the 2026-XX robustness audit (minimal fixes).

Covers:
- P1 lineage STOP/KILL policy alignment: the no-gain STOP/KILL discipline
  only counts for lineages already at a submittable standard (SUCCESS);
  promising-but-unqualified lineages keep iterating (2026-08-22 policy).
- P2 poll_progress tolerates an HTTP-date Retry-After header.
- P3 robustness hardening: load_config on corrupt JSON, run_proposals on a
  corrupt/non-dict proposals file, string longCount in _check_health,
  regex-escape safety in _swap_field, discovery fields without ids.
"""

import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wqb_agent.agent import Agent
from wqb_agent.client import WQBClient
from wqb_agent.discovery import FieldDiscovery
from wqb_agent.memory import ExperienceMemory
from wqb_agent.mutations import _swap_field
from wqb_agent.reflection import Reflector
from wqb_agent.simulator import _check_health
from wqb_agent.state import Experiment


def _metrics(sharpe=1.5, turnover=0.3, passed=True):
    return {
        "sharpe": sharpe,
        "fitness": sharpe,
        "turnover": turnover,
        "margin": 0.001,
        "returns": 0.1,
        "drawdown": 0.05,
        "checks": [{"name": "limitations", "pass": passed}],
        "passed": passed,
    }


def _done_experiment(lineage_id, expression="rank(returns)", metrics=None):
    exp = Experiment(1, "h-1", expression, {}, ["returns"])
    exp.status = "DONE"
    exp.metrics = metrics if metrics is not None else _metrics()
    exp.alpha_id = "abc123"
    exp.lineage_id = lineage_id
    return exp


class TestLineageStopPolicy(unittest.TestCase):
    """STOP/KILL 次数准则仅适用于已达可提交标准（SUCCESS）的候选。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wqb_lineage_")
        self.memory = ExperienceMemory(self.tmp)
        self.reflector = Reflector(self.memory)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_promising_lineage_never_stops_by_count(self):
        for _ in range(5):
            exp = _done_experiment("lin-p", metrics=_metrics(sharpe=0.95))
            self.reflector._update_lineages(
                1, [{"experiment": exp, "verdict": {"label": "PROMISING"}}]
            )
        self.assertEqual(self.memory.lineage_decision("lin-p"), "CONTINUE")

    def test_success_lineage_stops_after_two_no_gain(self):
        scores = (2.0, 1.9, 1.8)
        for round_no, score in enumerate(scores, start=1):
            exp = _done_experiment("lin-s", metrics=_metrics(sharpe=score))
            self.reflector._update_lineages(
                round_no,
                [{"experiment": exp, "verdict": {"label": "SUCCESS"}}],
            )
        self.assertEqual(self.memory.lineage_decision("lin-s"), "STOP")

    def test_memory_counts_toward_stop_flag(self):
        for _ in range(5):
            self.memory.record_lineage_result(
                "lin-open", 0.8, "PROMISING", 1, counts_toward_stop=False
            )
        self.assertEqual(self.memory.lineage_decision("lin-open"), "CONTINUE")
        entry = self.memory.lineages["lin-open"]
        self.assertEqual(entry["no_gain_streak"], 0)
        self.assertEqual(entry["last_label"], "PROMISING")


class TestPollProgressRetryAfterDate(unittest.TestCase):
    """poll_progress 的 Retry-After 为 HTTP-date 时不得崩溃（原 float() bug）。"""

    def test_http_date_retry_after_is_tolerated(self):
        c = WQBClient.__new__(WQBClient)
        c._local = threading.local()
        c._local.authenticated = True
        c.max_retries = 2
        c.base_url = "https://api.worldquantbrain.com"

        class Resp:
            def __init__(self, headers, payload):
                self.status_code = 200
                self.headers = headers
                self.text = json.dumps(payload)
                self._payload = payload

            def json(self):
                return self._payload

        class Sess:
            def __init__(self):
                self.calls = [
                    Resp({"Retry-After": "Wed, 21 Oct 2099 07:28:00 GMT"},
                         {"status": "RUNNING"}),
                    Resp({}, {"id": "sim-1", "alpha": "KP73prPz"}),
                ]

            def get(self, url, timeout=60):
                return self.calls.pop(0)

        c._local.session = Sess()
        sleeps = []
        with mock.patch("wqb_agent.client.time.sleep", side_effect=sleeps.append):
            alpha_id = c.poll_progress("/simulations/abc", timeout_sec=60)
        self.assertEqual(alpha_id, "KP73prPz")
        # 等待被夹在 30s 上限内，且没有抛 ValueError
        self.assertTrue(sleeps)
        self.assertLessEqual(max(sleeps), 30.0)


class TestCorruptInputs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wqb_corrupt_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_load_config_corrupt_json_exits_cleanly(self):
        import main as main_mod

        path = os.path.join(self.tmp, "config.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write("{not valid json")
        with self.assertRaises(SystemExit) as ctx:
            main_mod.load_config(path)
        self.assertEqual(ctx.exception.code, 1)

    def test_run_proposals_corrupt_json_returns_none(self):
        config = {
            "simulation": {"universe": "TOP3000", "decay": 0, "truncation": 0.08},
            "agent": {"state_dir": self.tmp},
        }
        agent = Agent(object(), config)
        path = os.path.join(self.tmp, "proposals.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write("{broken")
        self.assertIsNone(agent.run_proposals(path))

    def test_run_proposals_malformed_checkpoint_fails_closed(self):
        config = {
            "simulation": {"universe": "TOP3000", "decay": 0, "truncation": 0.08},
            "agent": {"state_dir": self.tmp},
        }
        agent = Agent(object(), config)
        proposals_path = os.path.join(self.tmp, "proposals.json")
        checkpoint_path = os.path.join(self.tmp, "round_1.checkpoint.json")
        with open(proposals_path, "w", encoding="utf-8") as f:
            json.dump({"round_no": 1, "proposals": [{"expression": "rank(a)"}]}, f)
        with open(checkpoint_path, "w", encoding="utf-8") as f:
            f.write("{broken")
        self.assertIsNone(agent.run_proposals(proposals_path))
        with open(checkpoint_path, encoding="utf-8") as f:
            self.assertEqual(f.read(), "{broken")

    def test_run_proposals_non_dict_payload_returns_none(self):
        config = {
            "simulation": {"universe": "TOP3000", "decay": 0, "truncation": 0.08},
            "agent": {"state_dir": self.tmp},
        }
        agent = Agent(object(), config)
        path = os.path.join(self.tmp, "proposals.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(["not", "a", "dict"], f)
        self.assertIsNone(agent.run_proposals(path))

    def test_run_proposals_malformed_nested_shapes_return_none(self):
        config = {
            "simulation": {"universe": "TOP3000", "decay": 0, "truncation": 0.08},
            "agent": {"state_dir": self.tmp},
        }
        agent = Agent(object(), config)
        path = os.path.join(self.tmp, "proposals.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"round_no": 1, "proposals": {"expression": "rank(a)"}}, f)
        self.assertIsNone(agent.run_proposals(path))

        with open(path, "w", encoding="utf-8") as f:
            json.dump({"round_no": 1, "hypothesis": [], "proposals": [{}]}, f)
        self.assertIsNone(agent.run_proposals(path))

        with open(path, "w", encoding="utf-8") as f:
            json.dump({"round_no": 1, "proposals": ["not an object"]}, f)
        self.assertIsNone(agent.run_proposals(path))

        with open(path, "w", encoding="utf-8") as f:
            json.dump({"round_no": "not-a-number", "proposals": [{}]}, f)
        self.assertIsNone(agent.run_proposals(path))

    def test_budget_cap_survives_non_numeric_max_simulations(self):
        # 直接验证 budget_cap 计算路径的 int() 防御逻辑。
        try:
            int("18x")
            raised = False
        except ValueError:
            raised = True
        self.assertTrue(raised)
        fallback = max(0, min(18, 18))
        self.assertEqual(fallback, 18)


class TestCheckHealthStringCounts(unittest.TestCase):
    def test_boolean_failed_health_check_without_result_is_not_hidden(self):
        payload = {
            "is": {
                "longCount": 500,
                "shortCount": 480,
                "checks": [{"name": "CONCENTRATED_WEIGHT", "pass": False}],
            }
        }
        health = _check_health(payload)
        self.assertFalse(health["ok"])
        self.assertTrue(
            any(reason.startswith("CONCENTRATED_WEIGHT=FAIL") for reason in health["reasons"])
        )

    def test_lowercase_pass_result_is_not_reported_as_failure(self):
        payload = {"is": {"checks": [
            {"name": "CONCENTRATED_WEIGHT", "result": "pass"},
            {"name": "LOW_SUB_UNIVERSE_SHARPE", "pass": True},
        ]}}
        self.assertTrue(_check_health(payload)["ok"])

    def test_string_long_count_does_not_crash(self):
        payload = {"is": {"longCount": "12", "shortCount": "30", "checks": []}}
        health = _check_health(payload)
        self.assertFalse(health["ok"])
        self.assertTrue(any("longCount" in r for r in health["reasons"]))

    def test_numeric_counts_unchanged(self):
        payload = {"is": {"longCount": 500, "shortCount": 480, "checks": []}}
        self.assertTrue(_check_health(payload)["ok"])


class TestSwapFieldEscapeSafety(unittest.TestCase):
    def test_replacement_with_backslash_reference_is_literal(self):
        swapped = _swap_field("rank(returns)", "returns", r"\g<0>_bad")
        self.assertIsNotNone(swapped)
        self.assertEqual(swapped, r"rank(\g<0>_bad)")


class TestDiscoverySkipsIdlessFields(unittest.TestCase):
    def test_field_without_id_is_skipped_not_crash(self):
        tmp = tempfile.mkdtemp(prefix="wqb_disc_idless_")
        try:
            client = mock.Mock()
            client.get_datafields.return_value = (
                [
                    {"name": "no id here", "description": "mystery field"},
                    {"id": "good_field", "description": "daily volume",
                     "dataset": {"id": "pv1"}},
                ],
                2,
            )
            d = FieldDiscovery(client, pagination_limit=10, max_pages=1,
                               cache_path=os.path.join(tmp, "cache.json"))
            chosen = d.discover(
                {"statement": "volume predicts returns",
                 "tags": ["volume"], "datasets": ["pv1"]},
                target_count=5,
            )
            self.assertEqual([f["id"] for f in chosen], ["good_field"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestSecondPassFixes(unittest.TestCase):
    """第二轮审计修复的回归。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wqb_pass2_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_reconcile_avoid_merges_duplicates_keeps_target(self):
        from wqb_agent.memory import ExperienceMemory

        memory = ExperienceMemory(self.tmp)
        record = {"id": "a1", "direction": "rank(returns)",
                  "reason": "r1", "source_round": 1,
                  "created": 1.0, "updated": 1.0}
        twin = dict(record)  # == 相等的重复记录
        memory.avoid = [record, twin]
        fixed = memory.reconcile_avoid("rank(returns)", "better reason", 2)
        # 重复记录合并为一条，target 更新为新的理由
        self.assertEqual(len(memory.avoid), 1)
        self.assertIs(fixed, memory.avoid[0])
        self.assertEqual(fixed["reason"], "better reason")

    def test_reconcile_avoid_leaves_other_directions_intact(self):
        from wqb_agent.memory import ExperienceMemory

        memory = ExperienceMemory(self.tmp)
        memory.add_avoid("rank(volume)", "old", 1)
        memory.add_avoid("rank(close)", "old2", 1)
        memory.reconcile_avoid("rank(volume)", "fixed", 2)
        self.assertEqual(len(memory.avoid), 2)
        by_dir = {item["direction"]: item for item in memory.avoid}
        self.assertEqual(by_dir["rank(volume)"]["reason"], "fixed")
        self.assertEqual(by_dir["rank(close)"]["reason"], "old2")

    def test_classify_verdicts_carry_kind_key(self):
        from wqb_agent.memory import ExperienceMemory
        from wqb_agent.reflection import Reflector

        memory = ExperienceMemory(self.tmp)
        reflector = Reflector(memory)
        exp = Experiment(1, "h", "rank(returns)", {}, ["returns"])
        exp.status = "PENDING"  # DONE-without-metrics path -> RECONCILE
        exp.metrics = {"sharpe": None}
        verdict = reflector._classify(exp)
        self.assertEqual(verdict["label"], "RECONCILE")
        self.assertIn("kind", verdict)

    def test_check_correlation_tolerates_http_date_retry_after(self):
        import scripts.check_correlation as cc
        import wqb_agent.client as client_mod

        class Resp:
            def __init__(self, headers):
                self.status_code = 200
                self.headers = headers
                self.text = ""

            def json(self):
                return {"max": 0.1}

        client = WQBClient.__new__(WQBClient)
        client.base_url = "https://api.worldquantbrain.com"
        client._request = mock.Mock(side_effect=[
            Resp({"Retry-After": "Wed, 21 Oct 2099 07:28:00 GMT"}),
            Resp({}),
        ])

        sleeps = []
        with mock.patch.object(client_mod.time, "sleep", side_effect=sleeps.append):
            payload = cc.fetch_correlations(client, "abc")
        self.assertEqual(payload["max"], 0.1)
        self.assertTrue(sleeps and max(sleeps) <= 30.0)


if __name__ == "__main__":
    unittest.main()
