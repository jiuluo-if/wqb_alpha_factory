import copy
import json
import tempfile
import unittest

from wqb_agent.memory import ExperienceMemory, MemorySourceReplayConflict
from wqb_agent.reflection import Reflector
from wqb_agent.research_settlement import (
    SettlementReplayConflict,
    compare_settlements,
    settlement_id,
)
from wqb_agent.state import Experiment, research_settlement_identity
from wqb_agent.trial_ledger import TrialLedger


class ReplayIdempotencyTests(unittest.TestCase):
    def test_canonical_settlement_identity_is_time_and_order_invariant(self):
        first = {
            "reward": 0.8,
            "research_evidence_bundle": {"status": "PASS", "metrics": {"sharpe": 1.2}},
            "settled_at": 1,
        }
        second = {
            "settled_at": 999,
            "research_evidence_bundle": {"metrics": {"sharpe": 1.2}, "status": "PASS"},
            "reward": 0.8,
        }

        self.assertEqual(settlement_id(first), settlement_id(second))
        self.assertEqual(compare_settlements(first, second), "NO_OP")

    def test_same_settlement_id_with_different_semantics_fails_closed(self):
        first = {"settlement_id": "s1", "reward": 0.8}
        second = {"settlement_id": "s1", "reward": 0.2}

        with self.assertRaises(SettlementReplayConflict):
            compare_settlements(first, second)

    def test_trial_ledger_uses_canonical_settlement_id(self):
        state_dir = tempfile.mkdtemp()
        ledger = TrialLedger(f"{state_dir}/trial_ledger.jsonl")
        experiment = Experiment(1, "h1", "rank(x)", {}, ["x"])
        ledger.record_outcome_settled(experiment, reward=0.8, timestamp=1)

        with open(ledger.path, encoding="utf-8") as handle:
            row = json.loads(handle.readline())
        self.assertEqual(row["settlement"]["settlement_id"], settlement_id(row["settlement"]))

    def test_interleaved_sources_remain_idempotent_after_restart(self):
        state_dir = tempfile.mkdtemp()
        memory = ExperienceMemory(state_dir=state_dir)
        memory.add_short_term(
            "observation", "shared derived observation", 7,
            detail={"evidence": "A"}, source_key="settlement:A",
        )
        memory.add_short_term(
            "observation", "shared derived observation", 8,
            detail={"evidence": "B"}, source_key="settlement:B",
        )
        memory.save()
        before = json.dumps(memory.__dict__, sort_keys=True, default=str)

        restored = ExperienceMemory(state_dir=state_dir).load()
        restored.add_short_term(
            "observation", "shared derived observation", 7,
            detail={"evidence": "A"}, source_key="settlement:A",
        )
        restored.add_short_term(
            "observation", "shared derived observation", 8,
            detail={"evidence": "B"}, source_key="settlement:B",
        )

        self.assertEqual(before, json.dumps(restored.__dict__, sort_keys=True, default=str))

    def test_memory_source_key_replay_is_byte_stable_and_conflicts_fail_closed(self):
        memory = ExperienceMemory(state_dir=tempfile.mkdtemp())
        entry = memory.add_short_term(
            "observation", "same observation", 7, detail={"x": 1},
            source_key="settlement:s1:observation",
        )
        before = json.dumps(memory.short_term, sort_keys=True, ensure_ascii=False)
        replay = memory.add_short_term(
            "observation", "same observation", 7, detail={"x": 1},
            source_key="settlement:s1:observation",
        )
        self.assertIs(replay, entry)
        self.assertEqual(before, json.dumps(memory.short_term, sort_keys=True, ensure_ascii=False))
        with self.assertRaisesRegex(MemorySourceReplayConflict, "MEMORY_SOURCE_REPLAY_CONFLICT"):
            memory.add_short_term(
                "observation", "different observation", 7,
                source_key="settlement:s1:observation",
            )

    def test_lineage_replay_does_not_double_count_and_replacement_is_exactly_once(self):
        memory = ExperienceMemory(state_dir=tempfile.mkdtemp())
        memory.record_lineage_result("l1", 1.0, "SUCCESS", 1, source_key="s1", experiment_id="e1")
        memory.record_lineage_result("l1", 1.0, "SUCCESS", 1, source_key="s1", experiment_id="e1")
        self.assertEqual(memory.lineages["l1"]["no_gain_streak"], 0)
        memory.record_lineage_result("l1", 0.8, "SUCCESS", 2, source_key="s2", experiment_id="e2")
        memory.record_lineage_result("l1", 0.7, "SUCCESS", 3, source_key="s3", experiment_id="e3")
        self.assertEqual(memory.lineages["l1"]["decision"], "STOP")
        memory.record_lineage_result("l1", 1.2, "SUCCESS", 2, source_key="s2-revised", experiment_id="e2")
        self.assertEqual(memory.lineages["l1"]["decision"], "CONTINUE")

    def test_settlement_identity_ignores_provenance_time(self):
        exp = Experiment(1, "h1", "rank(x)", {}, [])
        exp.final_outcome = {"status": "SETTLED", "settlement_id": "stable-1", "settled_at": 1}
        first = research_settlement_identity(exp)
        exp.final_outcome["settled_at"] = 999
        self.assertEqual(first, research_settlement_identity(exp))

    def test_experiment_identity_uses_canonical_settlement_reducer(self):
        exp = Experiment(1, "h1", "rank(x)", {}, [])
        exp.final_outcome = {
            "status": "SETTLED", "reward": 0.8,
            "research_evidence_bundle": {"status": "PASS"},
            "settled_at": 1,
        }
        self.assertEqual(
            research_settlement_identity(exp),
            f"settlement:{settlement_id(exp.final_outcome)}",
        )

    def test_reflector_replay_keeps_derived_memory_bounded(self):
        memory = ExperienceMemory(state_dir=tempfile.mkdtemp())
        reflector = Reflector(memory)
        exp = Experiment(1, "h1", "rank(x)", {}, ["x"])
        exp.status = "DONE"
        exp.final_outcome = {"settlement_id": "stable-1", "settled_at": 10}
        exp.metrics = {
            "sharpe": 1.2, "fitness": 1.1, "turnover": 0.3,
            "returns": 0.1, "drawdown": 0.1, "margin": 0.2,
            "checks": [{"name": "PASS", "result": "PASS"}],
        }
        exp.validation_status = "STABLE"
        exp.validation_report = {"status": "PASS", "candidate": "parent"}
        hypothesis = {"id": "h1", "statement": "test"}
        reflector.reflect(1, hypothesis, [exp])
        first = copy.deepcopy(memory.__dict__)
        reflector.reflect(1, hypothesis, [exp])
        self.assertEqual(first["short_term"], memory.short_term)
        self.assertEqual(first["lineages"], memory.lineages)
        self.assertEqual(first["next"], memory.next)


if __name__ == "__main__":
    unittest.main()
