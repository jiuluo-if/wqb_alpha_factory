"""Focused control-plane tests for durable factory blockers."""

import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from wqb_agent.factory_runner import AIFactoryRunner


class TestFactoryBlockerControl(unittest.TestCase):
    def test_signature_ignores_round_probe_and_private_payload(self):
        base = {
            "failure_taxonomy": "FREQUENCY_EVIDENCE_INSUFFICIENT",
            "budget_shortage_count": 2,
            "budget_shortage_reason": "candidate shortage",
        }
        changed = dict(base, probe_offset=99, round_no=22, seed="new")
        self.assertEqual(
            AIFactoryRunner._blocker_signature("FEASIBILITY", base),
            AIFactoryRunner._blocker_signature("FEASIBILITY", changed),
        )
        private = dict(base, expression="rank(fake_private_field)", alpha_id="fake-alpha")
        self.assertEqual(
            AIFactoryRunner._blocker_signature("FEASIBILITY", base),
            AIFactoryRunner._blocker_signature("FEASIBILITY", private),
        )

    def test_active_stopped_blocker_cross_session_skips_remote_work(self):
        with tempfile.TemporaryDirectory() as directory:
            blocker = AIFactoryRunner._blocker_projection(
                "PREFLIGHT", {"failure_taxonomy": "PREFLIGHT_BLOCKED"},
                now=10.0, recheck_sec=100.0,
            )
            with open(os.path.join(directory, "factory_session.json"), "w", encoding="utf-8") as handle:
                json.dump({
                    "session_id": "session-1", "status": "STOPPED", "deadline": 1000,
                    "rounds_completed": 0, "simulations_reserved": 0, "simulation_cap": 10,
                    "blocker": blocker,
                }, handle)
            agent = SimpleNamespace(
                state_dir=directory, factory_config={}, alpha_factory=Mock(), run_suggestion_round=Mock(),
                run_proposals=Mock(), checkpoints=Mock(),
            )
            runner = AIFactoryRunner(agent, clock=lambda: 20.0, sleeper=Mock())
            result = runner.run(duration_sec=100, max_simulations=10)
            self.assertEqual(result["session_id"], "session-1")
            self.assertEqual(result["last_action"], "BLOCKER_COOLDOWN")
            agent.run_suggestion_round.assert_not_called()
            agent.run_proposals.assert_not_called()

    def test_repeated_preflight_reason_reaches_stop_through_existing_route_primitive(self):
        probe = {
            "failure_taxonomy": "PREFLIGHT_BLOCKED",
            "rejection_reason_counts": {"PREFLIGHT_REJECTED": 3},
        }
        first = AIFactoryRunner.route_decision(
            None, probe, route_attempt=0, no_gain_attempts=0,
            max_route_attempts=3, max_no_gain_attempts=2,
        )
        second = AIFactoryRunner.route_decision(
            probe, dict(probe), route_attempt=1,
            no_gain_attempts=first["no_gain_attempts"],
            max_route_attempts=3, max_no_gain_attempts=2,
        )
        self.assertEqual(first["action"], "REROUTE")
        self.assertEqual(second["action"], "STOP")
        self.assertEqual(second["reason"], "NO_INFORMATION_GAIN")

    def test_due_blocker_performs_one_unchanged_recheck(self):
        with tempfile.TemporaryDirectory() as directory:
            blocker = AIFactoryRunner._blocker_projection(
                "FEASIBILITY", {"failure_taxonomy": "READY"},
                now=10.0, recheck_sec=0,
            )
            with open(os.path.join(directory, "factory_session.json"), "w", encoding="utf-8") as handle:
                json.dump({
                    "session_id": "session-2", "status": "STOPPED", "deadline": 1000,
                    "rounds_completed": 0, "simulations_reserved": 0, "simulation_cap": 10,
                    "blocker": blocker,
                }, handle)
            recheck = Mock(return_value={"changed": False, "probe": {}})
            agent = SimpleNamespace(
                state_dir=directory, factory_config={}, alpha_factory=Mock(),
                recheck_factory_blocker=recheck, checkpoints=Mock(),
            )
            result = AIFactoryRunner(agent, clock=lambda: 20.0, sleeper=Mock()).run(
                duration_sec=100, max_simulations=10
            )
            self.assertEqual(result["status"], "STOPPED")
            self.assertEqual(result["last_result"]["status"], "BLOCKER_UNCHANGED")
            recheck.assert_called_once()


if __name__ == "__main__":
    unittest.main()
