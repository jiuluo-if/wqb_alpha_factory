"""Synthetic regressions for the bounded Factory session control plane."""

import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from wqb_agent.factory_runner import AIFactoryRunner

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
        AIFactoryRunner._finish_route_episode(session, 3)
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
        AIFactoryRunner._advance_route_episode(session)
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
        projection = getattr(AIFactoryRunner, "_route_probe_projection", None)
        self.assertTrue(callable(projection))
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
        projection = AIFactoryRunner._route_probe_projection
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


if __name__ == "__main__":
    unittest.main()
