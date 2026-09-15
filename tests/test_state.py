import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import is_dataclass
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
from wqb_agent.state import Experiment, Trajectory, TrajectoryIntegrityError
from wqb_agent.submission import SubmissionPool, self_correlation_evidence


class TestTrajectoryIntegrity(unittest.TestCase):
    def test_new_and_legacy_ids_and_identity_replay_contract(self):
        experiment = Experiment(1, "h", "rank(x)", {}, ["x"])
        self.assertRegex(experiment.id, r"^[0-9a-f]{32}$")
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "trajectory.jsonl")
            legacy = experiment.to_dict()
            legacy["id"] = "legacy123456"
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(legacy) + "\n")
            trajectory = Trajectory(path=path).load()
            self.assertEqual(trajectory.experiments[0].id, "legacy123456")
            self.assertEqual(trajectory.add_many([Experiment.from_dict(legacy)]), [])
            with self.assertRaises(TrajectoryIntegrityError):
                trajectory.add_many([Experiment.from_dict(dict(legacy, expression="rank(y)"))])

    def test_strict_corruption_blocks_but_torn_final_tail_is_recoverable(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "trajectory.jsonl")
            good = Experiment(1, "h", "rank(x)", {}, ["x"]).to_dict()
            later = dict(good, id="later", expression="rank(y)")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("\n".join([json.dumps(good), "{broken", json.dumps(later)]) + "\n")
            with self.assertRaises(TrajectoryIntegrityError):
                list(Trajectory(path=path).iter_rows(strict=True))
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(good) + "\n{\"partial\":")
            stats = {}
            rows = list(Trajectory(path=path).iter_rows(strict=True, stats=stats))
            self.assertEqual([row["id"] for row in rows], [good["id"]])
            self.assertEqual(stats["torn_tail"], 1)

    def test_non_object_canonical_row_is_strictly_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "trajectory.jsonl")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("{}\n[]\n")
            with self.assertRaises(TrajectoryIntegrityError):
                list(Trajectory(path=path).iter_rows(strict=True))

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


class TestMemory(TmpStateMixin, unittest.TestCase):
    def test_persistence_roundtrip(self):
        memory = ExperienceMemory(state_dir=self._tmp)
        memory.add_lesson("Smoothing lowers turnover", 1, evidence=2)
        memory.add_avoid("rank(close)", "low sharpe", 1)
        memory.remember_expression("rank(close)")
        memory.save()
        loaded = ExperienceMemory(state_dir=self._tmp).load()
        self.assertEqual(len(loaded.lessons), 1)
        self.assertEqual(loaded.avoid[0]["direction"], "rank(close)")
        self.assertIn("rank(close)", loaded.seen_expressions)

    def test_reconcile_avoid_merges_truncated_and_full_keys(self):
        memory = ExperienceMemory(state_dir=self._tmp)
        expression = "x" * 100
        memory.add_avoid(expression[:80], "wrong diagnosis", 1)
        memory.add_avoid(expression, "duplicate", 1)
        memory.reconcile_avoid(expression, "correct diagnosis", 2)
        self.assertEqual(len(memory.avoid), 1)
        self.assertEqual(memory.avoid[0]["direction"], expression[:80])
        self.assertEqual(memory.avoid[0]["reason"], "correct diagnosis")

    def test_compress_dedupes_lessons(self):
        memory = ExperienceMemory(state_dir=self._tmp)
        memory.add_lesson("Rank fields on returns is weak", 1, evidence=2)
        memory.add_lesson("Rank fields on returns is weak", 2, evidence=1)
        memory.compress()
        self.assertEqual(len(memory.lessons), 1)

    def test_context_shape(self):
        memory = ExperienceMemory(state_dir=self._tmp)
        memory.add_lesson("Some lesson", 1, evidence=1)
        ctx = memory.context(recent_experiments=[{"expression": "rank(x)"}])
        for key in (
            "current_best",
            "active_hypotheses",
            "recent_key_experiments",
            "lessons",
            "avoid",
            "next",
        ):
            self.assertIn(key, ctx)

    def test_used_hypotheses_persisted(self):
        memory = ExperienceMemory(state_dir=self._tmp)
        memory.register_hypothesis({"id": "h-1", "statement": "s", "_round": 1})
        memory.save()
        loaded = ExperienceMemory(state_dir=self._tmp).load()
        self.assertIn("h-1", loaded.used_hypotheses)


class TestTrajectory(TmpStateMixin, unittest.TestCase):
    def test_experiment_is_dataclass_and_preserves_legacy_round_trip(self):
        experiment = Experiment(
            3, "h", "rank(close)", {"decay": 4}, ["close"], ["pv1"]
        )
        experiment.self_correlation = {
            "status": "PENDING", "source": "BRAIN alpha payload",
        }
        restored = Experiment.from_dict(experiment.to_dict())
        self.assertTrue(is_dataclass(Experiment))
        self.assertEqual(restored.round, 3)
        self.assertEqual(restored.datasets, ["pv1"])
        self.assertEqual(restored.self_correlation["status"], "PENDING")
        self.assertEqual(restored.to_dict(), experiment.to_dict())

    def test_legacy_unknown_experiment_metadata_is_ignored_on_read(self):
        experiment = Experiment(1, "h", "rank(close)", {"decay": 4}, ["close"])
        experiment.proposal_id = "proposal-1"
        experiment.submission_fingerprint = "fingerprint-1"
        experiment.status = "DONE"
        row = experiment.to_dict()
        row["external_evidence_refs"] = ["legacy"]
        restored = Experiment.from_dict(row)
        self.assertEqual(restored.id, experiment.id)
        self.assertEqual(restored.proposal_id, experiment.proposal_id)
        self.assertEqual(restored.submission_fingerprint, experiment.submission_fingerprint)
        self.assertEqual(restored.status, experiment.status)
        self.assertNotIn("external_evidence_refs", restored.to_dict())

    def test_datasets_dict_entries_normalized(self):
        """proposal datasets 允许 {"id":...} 字典形态；Experiment 入口必须
        归一化为字符串 id，保证 trajectory/ResearchState 聚合可哈希。"""
        from wqb_agent.state import Experiment as Exp
        from wqb_agent.state import dataset_ref
        exp = Exp(1, "h", "rank(x)", {}, ["x"],
                  datasets=[{"id": "pv1", "name": "Price Volume"},
                            "option8", {"name": "news18"}, None])
        self.assertEqual(exp.datasets, ["pv1", "option8", "news18"])
        self.assertEqual(dataset_ref({"id": "f2"}), "f2")
        self.assertIsNone(dataset_ref(None))
        # 聚合场景不再触发 unhashable dict
        pooled = sorted({d for d in exp.datasets})
        self.assertEqual(pooled, ["news18", "option8", "pv1"])

    def test_jsonl_append_only(self):
        path = os.path.join(self._tmp, "trajectory.jsonl")
        traj = Trajectory(max_len=100, path=path)
        e1 = Experiment(1, "h", "rank(a)", {}, ["a"])
        e1.status = "DONE"
        traj.add(e1)
        e2 = Experiment(2, "h", "rank(b)", {}, ["b"])
        e2.status = "DONE"
        traj.add(e2)
        with open(path, encoding="utf-8") as f:
            lines = [line for line in f if line.strip()]
        self.assertEqual(len(lines), 2)

        traj2 = Trajectory(max_len=100, path=path).load()
        self.assertEqual(len(traj2.experiments), 2)
        self.assertEqual(traj2.experiments[1].expression, "rank(b)")
        self.assertTrue(traj2.contains_id(e1.id))
        self.assertFalse(traj2.contains_id("missing-id"))

    def test_old_completed_parent_is_streamed_without_large_memory_window(self):
        path = os.path.join(self._tmp, "trajectory.jsonl")
        traj = Trajectory(max_len=10, path=path)
        old = Experiment(1, "h", "rank(old_signal)", {}, ["old_signal"])
        old.status = "DONE"
        old.metrics = {"sharpe": 1.0, "fitness": 1.0}
        recent = Experiment(2, "h", "rank(recent_signal)", {}, ["recent_signal"])
        recent.status = "DONE"
        recent.metrics = {"sharpe": 0.5, "fitness": 0.5}
        traj.add(old)
        traj.add(recent)

        bounded = Trajectory(max_len=1, path=path).load()

        self.assertEqual(len(bounded.experiments), 1)
        restored = bounded.find_completed_expression("rank( old_signal )")
        self.assertIsNotNone(restored)
        self.assertEqual(restored.id, old.id)
        self.assertIs(restored, bounded.find_completed_expression("rank(old_signal)"))

    def test_multiple_old_parents_share_one_streaming_lookup(self):
        path = os.path.join(self._tmp, "trajectory.jsonl")
        traj = Trajectory(max_len=1, path=path)
        expected_ids = set()
        for number, expression in ((1, "rank(old_a)"), (2, "rank(old_b)")):
            exp = Experiment(number, "h", expression, {}, [expression])
            exp.status = "DONE"
            exp.metrics = {"sharpe": 1.0, "fitness": 1.0}
            traj.add(exp)
            expected_ids.add(exp.id)

        with mock.patch("builtins.open", wraps=open) as opened:
            resolved = traj.find_completed_expressions(
                ["rank(old_a)", "rank( old_b )"]
            )
        self.assertEqual({item.id for item in resolved.values()}, expected_ids)
        trajectory_reads = [
            call for call in opened.call_args_list
            if str(call.args[0]).endswith("trajectory.jsonl")
        ]
        self.assertEqual(len(trajectory_reads), 1)

    def test_completed_parent_cache_is_bounded(self):
        traj = Trajectory(max_len=1)
        traj.find_completed_expressions(
            [f"rank(cache_{index})" for index in range(600)]
        )
        self.assertLessEqual(
            len(traj._completed_expression_cache),
            Trajectory.COMPLETED_EXPRESSION_CACHE_MAX,
        )



if __name__ == "__main__":
    unittest.main()
