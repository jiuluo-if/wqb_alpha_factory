import ast
import os
import unittest

from wqb_agent.heartbeat import HeartbeatSink


class TestHeartbeatSink(unittest.TestCase):
    def test_heartbeat_module_has_no_concrete_research_or_transport_owner(self):
        path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "wqb_agent", "heartbeat.py")
        with open(path, encoding="utf-8") as handle:
            tree = ast.parse(handle.read(), filename=path)
        imports = {
            ("." * node.level + (node.module or ""))
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        }
        self.assertNotIn(".agent", imports)
        self.assertNotIn(".simulator", imports)
        self.assertNotIn(".client", imports)

    def test_fake_clock_throttles_but_stage_change_emits_immediately(self):
        now = [100.0]
        events = []
        sink = HeartbeatSink(emit=lambda stage, **meta: events.append({"stage": stage, **meta}), clock=lambda: now[0], interval_sec=10)
        self.assertTrue(sink.emit_stage("DISCOVERY", progress=1))
        self.assertFalse(sink.emit_stage("DISCOVERY", progress=1))
        now[0] = 105.0
        self.assertFalse(sink.emit_stage("DISCOVERY", progress=1))
        self.assertTrue(sink.emit_stage("DISCOVERY", progress=2))
        self.assertTrue(sink.emit_stage("FEASIBILITY", progress=2))
        self.assertEqual([item["stage"] for item in events], [
            "DISCOVERY", "DISCOVERY", "FEASIBILITY"
        ])

    def test_stall_is_observation_only(self):
        now = [0.0]
        events = []
        sink = HeartbeatSink(emit=lambda stage, **meta: events.append({"stage": stage, **meta}), clock=lambda: now[0], interval_sec=10)
        sink.emit_stage("SIMULATION_EXECUTION", done=0)
        now[0] = 20.0
        self.assertTrue(sink.emit_stage("SIMULATION_EXECUTION", done=0, stalled=True))
        self.assertEqual(events[-1]["stalled"], True)

    def test_elapsed_metadata_does_not_bypass_aggregate_throttle(self):
        now = [0.0]
        events = []
        sink = HeartbeatSink(
            emit=lambda stage, **meta: events.append({"stage": stage, **meta}),
            clock=lambda: now[0], interval_sec=10,
        )
        sink.emit_stage("SIMULATION_EXECUTION", done=1, elapsed_sec=1)
        self.assertFalse(
            sink.emit_stage("SIMULATION_EXECUTION", done=1, elapsed_sec=2)
        )
        self.assertEqual(len(events), 1)


if __name__ == "__main__":
    unittest.main()
