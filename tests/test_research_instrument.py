import json
import os
import tempfile
import unittest
from types import SimpleNamespace

import wqb_agent.research_api as research_api
from wqb_agent.research_api import (
    assess_execution_round,
    assess_experiment,
    execute_pending_round,
    inspect_pending_work,
    inspect_research_context,
    inspect_runtime_context,
    materialize_targeted_batch,
    research_tool_manifest,
)
from wqb_agent.research_cursor import (
    build_research_cursor,
    cycle_projection,
    research_cycle_id,
)
from wqb_agent.research_quality import assess_experiment as pure_assess


def _settled(round_no=3, settlement_id="s1", experiment_id="exp-1", **extra):
    return {
        "id": experiment_id, "round": round_no, "status": "DONE",
        "trajectory_revision": "RESEARCH_SETTLED",
        "final_outcome": {"settlement_id": settlement_id, "base_quality": "STABLE"},
        "research_classification": "STABLE", "metrics": {"sharpe": 1.2,
        "checks": [{"name": "A", "pass": True}]},
        "validation_report": {"status": "PASS"}, "health": {"ok": True},
        **extra,
    }


class TestResearchInstrument(unittest.TestCase):
    def test_cursor_is_stable_for_timestamp_refresh_and_changes_for_settlement(self):
        first = build_research_cursor([_settled()], ledger_summary={
            "history_completeness": "COMPLETE_FROM_START", "effective_trial_count": 3,
        })
        refreshed = build_research_cursor([_settled(updated_at=999, recorded_at=123)],
                                          ledger_summary={
            "history_completeness": "COMPLETE_FROM_START", "effective_trial_count": 3,
        })
        changed = build_research_cursor([_settled(settlement_id="s2")], ledger_summary={
            "history_completeness": "COMPLETE_FROM_START", "effective_trial_count": 3,
        })
        self.assertEqual(first["research_cursor"], refreshed["research_cursor"])
        self.assertNotEqual(first["research_cursor"], changed["research_cursor"])

    def test_cursor_blocks_advancement_for_unresolved_checkpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            with open(os.path.join(directory, "round_4.checkpoint.json"), "w", encoding="utf-8") as handle:
                json.dump({"schema_version": 1, "round_no": 4, "hypothesis": {"id": "h"},
                           "experiments": [], "complete": False}, handle)
            context = inspect_runtime_context(state_dir=directory)
            pending = inspect_pending_work(state_dir=directory)
        self.assertEqual(context["runtime_state"], "BLOCKED")
        self.assertEqual(context["pending_execution_round"], 4)
        self.assertEqual(pending["next_safe_action"], "READ_ONLY_RECONCILE")

    def test_cycle_identity_is_stable_and_not_execution_fingerprint(self):
        cursor = "sha256:" + "a" * 64
        one = research_cycle_id(cursor, ["d2", "d1"])
        two = research_cycle_id(cursor, ["d1", "d2"])
        self.assertEqual(one, two)
        self.assertNotEqual(one, "sha256:" + "a" * 64)

    def test_one_cycle_maps_to_multiple_execution_rounds(self):
        rows = [
            {"id": "e1", "round": 4, "research_cycle_id": "cycle-1",
             "source_research_cursor": "sha256:old", "optimization_decision_id": "d1"},
            {"id": "e2", "round": 5, "research_cycle_id": "cycle-1",
             "source_research_cursor": "sha256:old", "optimization_decision_id": "d1"},
        ]
        projection = cycle_projection(rows, "cycle-1")
        self.assertEqual(projection["execution_rounds"], [4, 5])
        self.assertEqual(projection["decision_identities"], ["d1"])

    def test_quality_distinguishes_unresolved_provisional_and_final(self):
        unresolved = pure_assess({"id": "u", "round": 1, "status": "SUBMIT_UNKNOWN"})
        provisional = pure_assess({"id": "p", "round": 1, "status": "DONE",
                                   "metrics": {"sharpe": 1}})
        final = pure_assess(_settled())
        self.assertEqual(unresolved.finality, "UNRESOLVED")
        self.assertEqual(unresolved.quality_status, "UNKNOWN")
        self.assertEqual(provisional.finality, "PROVISIONAL")
        self.assertEqual(final.finality, "FINAL")
        self.assertNotEqual(unresolved.execution_status, "FAIL")

    def test_quality_uses_ledger_denominator_and_legacy_is_unavailable(self):
        row = _settled()
        assessment = pure_assess(row, trial_summary={
            "trial_count": 7, "effective_trial_count": 5,
            "history_completeness": "COMPLETE_FROM_START",
        })
        legacy = pure_assess(row, trial_summary={
            "trial_count": 7, "effective_trial_count": 5,
            "history_completeness": "INCOMPLETE_LEGACY",
        })
        self.assertEqual(assessment.effective_trial_count, 5)
        self.assertEqual(legacy.effective_trial_count, "UNAVAILABLE")

    def test_context_is_bounded_and_tool_manifest_is_explicit(self):
        with tempfile.TemporaryDirectory() as directory:
            for index in range(20):
                path = os.path.join(directory, "trajectory.jsonl")
                with open(path, "a", encoding="utf-8") as handle:
                    handle.write(json.dumps(_settled(settlement_id=f"s{index}", experiment_id=f"e{index}")) + "\n")
            context = inspect_research_context(state_dir=directory, limit=8)
        self.assertLessEqual(len(context["quality_summaries"]), 8)
        self.assertNotIn("expression", json.dumps(context))
        names = {item["name"] for item in research_tool_manifest()}
        self.assertIn("simulate", names)
        self.assertIn("get_alpha_evidence", names)

    def test_stale_cursor_rejects_materialization_before_write(self):
        agent = SimpleNamespace(state_dir=tempfile.mkdtemp(), next_round_no=lambda: 1)
        result = materialize_targeted_batch([], agent=agent,
                                            expected_research_cursor="sha256:stale")
        self.assertEqual(result["status"], "RESEARCH_CONTEXT_STALE")
        self.assertFalse(os.path.exists(os.path.join(agent.state_dir, "proposals.json")))

    def test_execute_pending_round_routes_only_through_agent_facade(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "proposals.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"round_no": 1, "proposals": []}, handle)
            agent = SimpleNamespace(state_dir=directory,
                                   run_proposals=lambda value: {"path": value, "status": "OK"})
            result = execute_pending_round(agent=agent)
        self.assertEqual(result["status"], "OK")
        self.assertTrue(result["path"].endswith("proposals.json"))

    def test_public_assessment_api_is_read_only_projection(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "trajectory.jsonl")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(_settled()) + "\n")
            result = assess_experiment("exp-1", state_dir=directory)
            round_result = assess_execution_round(3, state_dir=directory)
        self.assertEqual(result["finality"], "FINAL")
        self.assertEqual(round_result["settled_experiments"], 1)

    def test_cycle_provenance_is_carried_without_changing_execution_identity(self):
        from wqb_agent.research_api import ExperimentSpec

        proposal = ExperimentSpec(
            "hypothesis", "rank(close)", fields=("close",),
            source_research_cursor="sha256:" + "a" * 64,
            research_cycle_id="research-cycle:1",
        ).to_proposal()
        self.assertEqual(proposal["source_research_cursor"], "sha256:" + "a" * 64)
        self.assertEqual(proposal["research_cycle_id"], "research-cycle:1")
        self.assertNotIn("research_cycle_id", research_api.ExperimentSpec.__dataclass_fields__
                         ["settings"].default_factory())

    def test_current_docs_and_public_exports_match_remote_first_surface(self):
        root = os.path.dirname(os.path.dirname(__file__))
        from pathlib import Path

        readme = Path(root, "README.md").read_text(encoding="utf-8")
        testing = Path(root, "docs", "TESTING.md").read_text(encoding="utf-8")
        architecture = Path(root, "docs", "ARCHITECTURE_AGENT.md").read_text(encoding="utf-8")
        self.assertIn("SimulationGateway", readme)
        self.assertIn("get_alpha_evidence", readme)
        self.assertIn("remote evidence", architecture)
        self.assertIn("python -m unittest discover -s tests", testing)
        self.assertNotIn("Phase VIII", architecture)
        self.assertTrue({"simulate", "get_alpha_evidence", "find_similar_alphas"}.issubset(
            set(research_api.__all__)
        ))


if __name__ == "__main__":
    unittest.main()
