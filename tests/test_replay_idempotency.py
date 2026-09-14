import copy
import json
import tempfile
import unittest

from wqb_agent.memory import ExperienceMemory, MemorySourceReplayConflict
from wqb_agent.reflection import Reflector
from wqb_agent.state import Experiment, research_settlement_identity


class ReplayIdempotencyTests(unittest.TestCase):
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
