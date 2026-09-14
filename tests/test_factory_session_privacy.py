"""Synthetic regressions for the bounded Factory session control plane."""

import ast
import copy
import inspect
import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from wqb_agent.factory_control import advance_route_episode, finish_route_episode
from wqb_agent.factory_probe import route_probe_projection
from wqb_agent.factory_runner import (
    AIFactoryRunner,
    FactoryControlStateError,
)

CANARIES = (
    "rank(SYNTHETIC_FIELD_ID)",
    "SYNTHETIC_FIELD_ID",
    "SYNTHETIC_ALPHA_ID",
    "SYNTHETIC_TEMPLATE_ID",
    "SYNTHETIC_DATASET_ID",
    "SYNTHETIC_RESEARCH_QUESTION",
    "SYNTHETIC_MECHANISM",
    "SYNTHETIC_LINEAGE",
    "SYNTHETIC_ARM_ID",
    "SYNTHETIC_EXCEPTION_MESSAGE",
)


def _session_with_canaries():
    return {
        "session_id": "synthetic-session",
        "status": "STOPPED",
        "deadline": 100.0,
        "rounds_completed": 0,
        "simulations_reserved": 0,
        "simulation_cap": 10,
        "last_feasibility_check": {
            "candidate_expression_fingerprints": [CANARIES[0]],
            "field_concept_fingerprints": [CANARIES[1]],
            "failure_taxonomy": CANARIES[6],
        },
        "last_budget_probe": {
            "structural_family_fingerprints": [CANARIES[3]],
            "dataset_route": [CANARIES[4]],
            "research_question_fingerprints": [CANARIES[5]],
            "lineage": CANARIES[7],
            "arm_id": CANARIES[8],
        },
        "last_preflight_probe": {"message": CANARIES[9]},
        "last_result": {
            "best": {"expression": CANARIES[0]},
            "verdicts": [{"expression": CANARIES[0], "verdict": "PASS"}],
            "message": CANARIES[9],
        },
    }


class TestFactorySessionPrivacy(unittest.TestCase):
    def _runner(self, directory):
        return AIFactoryRunner(
            SimpleNamespace(state_dir=directory, alpha_factory=Mock()),
            clock=lambda: 20.0,
            sleeper=lambda _seconds: None,
        )

    def test_save_session_persists_only_bounded_control_plane(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = self._runner(directory)
            runner._save_session(_session_with_canaries())
            with open(runner.session_path, encoding="utf-8") as handle:
                raw = handle.read()
            loaded = AIFactoryRunner.read_session(directory)

        for canary in CANARIES:
            self.assertNotIn(canary, raw)
            self.assertNotIn(canary, json.dumps(loaded, ensure_ascii=False))

    def test_status_view_does_not_expose_private_session_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = self._runner(directory)
            runner._save_session(_session_with_canaries())
            view = AIFactoryRunner.status_view(directory)

        serialized = json.dumps(view, ensure_ascii=False)
        self.assertNotIn("best", view.get("last_result", {}))
        self.assertNotIn("message", view.get("last_result", {}))
        for canary in CANARIES:
            self.assertNotIn(canary, serialized)

    def test_factory_run_return_does_not_expose_private_session_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = self._runner(directory)
            runner._save_session(_session_with_canaries())
            result = runner.run(duration_sec=0, max_simulations=10)

        serialized = json.dumps(result, ensure_ascii=False)
        for canary in CANARIES:
            self.assertNotIn(canary, serialized)

    def test_successful_episode_clears_route_state_but_keeps_quota(self):
        session = {
            "route_episode_round": 3,
            "route_attempt": 2,
            "no_gain_attempts": 1,
            "last_feasibility_check": {"candidate_expression_fingerprints": ["x"]},
            "last_budget_probe": {"dataset_route": ["d"]},
            "last_preflight_probe": {"failure_taxonomy": "READY"},
            "quota": {"weekly_reserved": 7},
        }
        finish_route_episode(session, 3)
        self.assertEqual(session["route_attempt"], 0)
        self.assertEqual(session["no_gain_attempts"], 0)
        self.assertIsNone(session["last_feasibility_check"])
        self.assertEqual(session["quota"]["weekly_reserved"], 7)

    def test_budget_shortage_episode_advance_is_bounded_and_does_not_release_quota(self):
        session = {
            "session_id": "s",
            "last_round": 4,
            "probe_offset": 2,
            "route_attempt": 3,
            "no_gain_attempts": 2,
            "simulations_reserved": 9,
            "last_budget_probe": {"dataset_route": ["private-dataset"]},
        }
        advance_route_episode(session)
        self.assertEqual(session["last_action"], "ADVANCE_ROUTE_EPISODE")
        self.assertEqual(session["probe_offset"], 2)
        self.assertEqual(session["simulations_reserved"], 9)
        self.assertEqual(session["route_attempt"], 0)
        self.assertEqual(session["no_gain_attempts"], 0)
        self.assertIsNone(session["last_budget_probe"])

    def test_route_projection_uses_order_invariant_session_bound_digests(self):
        probe = {
            "candidate_expression_fingerprints": ["expr-b", "expr-a", "expr-a"],
            "semantic_mechanism_fingerprints": ["mechanism-a"],
            "structural_family_fingerprints": ["family-a"],
            "field_concept_fingerprints": ["concept-a"],
            "relationship_fingerprints": ["relationship-a"],
            "dataset_route": ["dataset-a"],
            "research_question_fingerprints": ["question-a"],
        }
        projection = route_probe_projection
        first = projection(probe, session_id="session-a")
        second = projection(
            {key: list(reversed(value)) for key, value in probe.items()},
            session_id="session-a",
        )
        other = projection(probe, session_id="session-b")

        for dimension in (
            "candidate_expression", "semantic_mechanism", "structural_family",
            "field_concept", "relationship", "dataset_route", "research_question",
        ):
            self.assertEqual(first[f"{dimension}_count"], 1 if dimension != "candidate_expression" else 2)
            self.assertEqual(first[f"{dimension}_set_digest"], second[f"{dimension}_set_digest"])
            self.assertNotEqual(first[f"{dimension}_set_digest"], other[f"{dimension}_set_digest"])
        self.assertNotIn("expr-a", json.dumps(first))
        self.assertNotIn("dataset-a", json.dumps(first))

    def test_route_decision_projection_matches_raw_decision(self):
        base = {
            "candidate_expression_fingerprints": ["expr-a"],
            "semantic_mechanism_fingerprints": ["mechanism-a"],
            "structural_family_fingerprints": ["family-a"],
            "field_concept_fingerprints": ["concept-a"],
            "relationship_fingerprints": ["relationship-a"],
            "dataset_route": ["dataset-a"],
            "research_question_fingerprints": ["question-a"],
        }
        projection = route_probe_projection
        changes = {
            "candidate_expression_fingerprints": ["expr-b"],
            "semantic_mechanism_fingerprints": ["mechanism-b"],
            "relationship_fingerprints": ["relationship-b"],
            "dataset_route": ["dataset-b"],
            "research_question_fingerprints": ["question-b"],
        }
        for key, value in changes.items():
            changed = dict(base, **{key: value})
            expected = AIFactoryRunner.route_decision(
                base, changed, route_attempt=0, no_gain_attempts=1
            )
            actual = AIFactoryRunner.route_decision(
                projection(base, session_id="session-a"),
                projection(changed, session_id="session-a"),
                route_attempt=0,
                no_gain_attempts=1,
            )
            self.assertEqual(
                {key: actual[key] for key in ("action", "reason", "information_gain", "change_type", "route_index")},
                {key: expected[key] for key in ("action", "reason", "information_gain", "change_type", "route_index")},
            )
        expected = AIFactoryRunner.route_decision(
            base, base, route_attempt=0, no_gain_attempts=1
        )
        actual = AIFactoryRunner.route_decision(
            projection(base, session_id="session-a"),
            projection(base, session_id="session-a"),
            route_attempt=0,
            no_gain_attempts=1,
        )
        self.assertEqual(
            {key: actual[key] for key in ("action", "reason", "information_gain", "change_type", "route_index")},
            {key: expected[key] for key in ("action", "reason", "information_gain", "change_type", "route_index")},
        )

    def test_control_plane_projection_is_pure(self):
        source = _session_with_canaries()
        original = copy.deepcopy(source)
        AIFactoryRunner._control_plane_session(source)
        self.assertEqual(source, original)

    def test_save_session_does_not_mutate_live_runtime_state(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = self._runner(directory)
            source = _session_with_canaries()
            original = copy.deepcopy(source)
            runner._save_session(source)
        self.assertEqual(source, original)

    def test_new_undeclared_internal_token_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = self._runner(directory)
            source = {"session_id": "s", "status": "RUNNING", "last_action": "NEW_ACTION"}
            with self.assertRaisesRegex(
                FactoryControlStateError, "FACTORY_CONTROL_STATE_UNDECLARED"
            ):
                runner._save_session(source)
            self.assertFalse(os.path.exists(runner.session_path))

    def test_legacy_unknown_token_is_tolerated_for_inspection(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, AIFactoryRunner.SESSION_FILE)
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({
                    "session_id": "legacy",
                    "status": "LEGACY_STATUS",
                    "last_action": "LEGACY_ACTION",
                }, handle)
            view = AIFactoryRunner.status_view(directory)
        self.assertEqual(view["status"], "UNKNOWN")
        self.assertEqual(view["last_action"], "UNKNOWN")

    def test_legacy_unknown_token_fails_closed_before_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, AIFactoryRunner.SESSION_FILE)
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({
                    "session_id": "legacy",
                    "status": "LEGACY_STATUS",
                    "last_action": "LEGACY_ACTION",
                    "deadline": 1000.0,
                }, handle)
            suggestion = Mock(side_effect=AssertionError("legacy execution"))
            agent = SimpleNamespace(
                state_dir=directory,
                alpha_factory=Mock(),
                run_suggestion_round=suggestion,
            )
            with self.assertRaisesRegex(
                FactoryControlStateError, "FACTORY_CONTROL_STATE_UNDECLARED"
            ):
                AIFactoryRunner(agent, clock=lambda: 0.0).run(
                    duration_sec=1, max_simulations=1
                )
        suggestion.assert_not_called()

    def test_durable_runtime_vocabulary_is_ast_closed(self):
        source_path = inspect.getsourcefile(AIFactoryRunner)
        with open(source_path, encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        action_literals = set()
        status_literals = set()
        retry_literals = set()
        dynamic_action_nodes = []

        class VocabularyAudit(ast.NodeVisitor):
            @staticmethod
            def literal_tokens(node):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    return {node.value}
                if isinstance(node, ast.IfExp):
                    return (
                        VocabularyAudit.literal_tokens(node.body)
                        | VocabularyAudit.literal_tokens(node.orelse)
                    )
                return set()

            def visit_Assign(self, node):
                for target in node.targets:
                    if not isinstance(target, ast.Subscript):
                        continue
                    if not isinstance(target.value, ast.Name):
                        continue
                    key = target.slice.value if isinstance(target.slice, ast.Constant) else None
                    if key not in {"status", "last_action"}:
                        continue
                    values = VocabularyAudit.literal_tokens(node.value)
                    if key == "status":
                        status_literals.update(values)
                    else:
                        action_literals.update(values)
                        if any(isinstance(item, ast.JoinedStr) for item in ast.walk(node.value)):
                            dynamic_action_nodes.append(node)
                self.generic_visit(node)

            def visit_Call(self, node):
                if (
                    isinstance(node.func, ast.Name)
                    and node.func.id == "_record_retry"
                    and len(node.args) > 1
                    and isinstance(node.args[1], ast.Constant)
                    and isinstance(node.args[1].value, str)
                ):
                    retry_literals.add(node.args[1].value)
                self.generic_visit(node)

        VocabularyAudit().visit(tree)
        self.assertFalse(dynamic_action_nodes)
        self.assertTrue(action_literals)
        self.assertTrue(status_literals)
        self.assertTrue(
            action_literals | retry_literals <= AIFactoryRunner._SESSION_ACTIONS,
            sorted((action_literals | retry_literals) - AIFactoryRunner._SESSION_ACTIONS),
        )
        self.assertTrue(
            status_literals <= AIFactoryRunner._SESSION_STATUSES,
            sorted(status_literals - AIFactoryRunner._SESSION_STATUSES),
        )
        self.assertTrue(
            set(AIFactoryRunner._BLOCKER_STOP_ACTIONS.values())
            <= AIFactoryRunner._SESSION_ACTIONS
        )
        self.assertTrue(
            set(AIFactoryRunner._TERMINAL_RECONCILE_ACTIONS.values())
            <= AIFactoryRunner._SESSION_ACTIONS
        )

    def test_runtime_boundary_actions_round_trip_through_json(self):
        actions = (
            "RUN_PROPOSALS", "RECOVER_PROPOSALS", "RECOVER_CHECKPOINT_ERROR",
            "RUN_PROPOSALS_ERROR", "RECOVER_PROPOSALS_ERROR", "SUGGEST",
            "SUGGEST_ERROR", "WAIT_AGENT_DECISION",
        )
        with tempfile.TemporaryDirectory() as directory:
            runner = self._runner(directory)
            for action in actions:
                source = {
                    "session_id": f"session-{action}",
                    "status": "RUNNING",
                    "last_action": action,
                    "last_result": {"status": "TARGETED_BATCH_INVALID"},
                }
                runner._save_session(source)
                loaded = AIFactoryRunner.read_session(directory)
                self.assertEqual(loaded["last_action"], action)
                self.assertEqual(loaded["last_result"]["status"], "TARGETED_BATCH_INVALID")

    def test_orphaned_inbox_recovery_survives_json_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            proposals_path = os.path.join(directory, "proposals.json")
            with open(proposals_path, "w", encoding="utf-8") as handle:
                json.dump({
                    "round_no": 7,
                    "factory_session_id": "session-orphan",
                    "proposals": [{"expression": "synthetic"}],
                }, handle)
            agent = SimpleNamespace(
                state_dir=directory,
                alpha_factory=Mock(),
                checkpoints=SimpleNamespace(load=lambda _round: None),
            )
            runner = AIFactoryRunner(agent)
            source = {
                "session_id": "session-orphan",
                "status": "RUNNING",
                "last_round": 7,
                "last_action": "RUN_PROPOSALS",
                "simulations_reserved": 1,
            }
            runner._save_session(source)
            resumed = AIFactoryRunner(agent)
            session = AIFactoryRunner.read_session(directory)
            self.assertEqual(resumed._orphaned_canonical_proposals(session), [{"expression": "synthetic"}])
            self.assertTrue(resumed._recovery_already_reserved(session, 7))

    def test_run_proposals_orphan_restart_skips_suggestion_and_reuses_inbox(self):
        with tempfile.TemporaryDirectory() as directory:
            with open(os.path.join(directory, "proposals.json"), "w", encoding="utf-8") as handle:
                json.dump({
                    "round_no": 7,
                    "factory_session_id": "session-restart",
                    "proposals": [{"expression": "synthetic"}],
                }, handle)
            source = {
                "session_id": "session-restart",
                "status": "RUNNING",
                "deadline": 100.0,
                "last_round": 7,
                "last_action": "RUN_PROPOSALS",
                "simulations_reserved": 1,
                "quota": {
                    "schema_version": 1,
                    "timezone": "America/New_York",
                    "local_date": "1970-01-01",
                    "week_start": "1969-12-29",
                    "daily_cap": 10,
                    "weekly_cap": 10,
                    "daily_reserved": 1,
                    "weekly_reserved": 1,
                },
            }
            AIFactoryRunner(
                SimpleNamespace(state_dir=directory, alpha_factory=Mock())
            )._save_session(source)
            clock_values = iter([0.0] * 10 + [100.0])

            def clock():
                return next(clock_values, 100.0)

            events = []

            def recover(_path):
                events.append("recover")
                return None

            def suggest(_round):
                events.append("suggest")
                return None

            agent = SimpleNamespace(
                state_dir=directory,
                alpha_factory=Mock(),
                factory_config={},
                checkpoints=SimpleNamespace(
                    load=lambda _round: None,
                    unfinished_except=lambda _round: None,
                ),
                run_proposals=Mock(side_effect=recover),
                run_suggestion_round=Mock(side_effect=suggest),
                next_round_no=Mock(return_value=8),
            )
            probe_runner = AIFactoryRunner(agent, clock=lambda: 0.0)
            self.assertEqual(
                probe_runner._orphaned_canonical_proposals(
                    AIFactoryRunner.read_session(directory)
                ),
                [{"expression": "synthetic"}],
            )
            result = AIFactoryRunner(
                agent, clock=clock, sleeper=lambda _seconds: None
            ).run(duration_sec=1, max_simulations=10, idle_sleep_sec=1)

        self.assertEqual(result["session_id"], "session-restart")
        self.assertEqual(events[0], "recover")
        agent.run_proposals.assert_called_once()
        self.assertEqual(result["rounds_completed"], 1)

    def test_recover_checkpoint_error_round_trip_keeps_recovery_reservation(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = self._runner(directory)
            runner._save_session({
                "session_id": "session-recovery-error",
                "status": "RUNNING",
                "last_round": 9,
                "last_action": "RECOVER_CHECKPOINT_ERROR",
                "simulations_reserved": 3,
                "quota": {"weekly_reserved": 3},
            })
            resumed = AIFactoryRunner.read_session(directory)
        self.assertEqual(resumed["last_action"], "RECOVER_CHECKPOINT_ERROR")
        self.assertTrue(AIFactoryRunner._recovery_already_reserved(resumed, 9))


if __name__ == "__main__":
    unittest.main()
