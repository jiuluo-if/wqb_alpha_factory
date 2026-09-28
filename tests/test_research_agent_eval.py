from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
import unittest
from collections import Counter
from pathlib import Path

from mcp import Client

from scripts.run_cold_start_eval import COLD_FIXTURE, _comparison, _measure
from scripts.run_research_agent_eval import (
    FIXTURE,
    _capture_codex_jsonl,
    _codex_command,
    build_fake_mcp,
    evaluate_case,
    load_case_fixture,
    load_case_manifest,
    parse_codex_jsonl,
    parse_fake_tool_log,
    result_for_timeout,
)


def _case(case_id: str) -> dict:
    return next(row for row in load_case_manifest()["cases"] if row["id"] == case_id)


def _event(tool: str, arguments: dict | None = None, *, server: str = "research_eval") -> dict:
    return {
        "type": "item.completed",
        "item": {
            "id": f"item_{tool}",
            "type": "mcp_tool_call",
            "server": server,
            "tool": tool,
            "arguments": arguments or {},
            "result": {"content": []},
            "status": "completed",
        },
    }


def _run_events(calls: list[dict], final: dict, *, usage: dict | None = None) -> str:
    events = [
        {"type": "thread.started", "thread_id": "synthetic"},
        {"type": "turn.started"},
        *calls,
        {"type": "item.completed", "item": {"id": "final", "type": "agent_message", "text": json.dumps(final)}},
        {"type": "turn.completed", "usage": usage or {"input_tokens": 100, "output_tokens": 20}},
    ]
    return "\n".join(json.dumps(event) for event in events)


def _fake_log(
    tool: str,
    arguments: dict | None = None,
    *,
    case_id: str = "A",
    write: bool = False,
    result: dict | None = None,
) -> dict:
    return {
        "case_id": case_id,
        "tool": tool,
        "arguments": arguments or {},
        "result": result or {},
        "fake": True,
        "simulated_remote_write": write,
        "network_calls": 0,
    }


class ResearchAgentEvalTests(unittest.TestCase):
    def test_fixture_defines_a_through_h_with_all_required_fields(self):
        manifest = load_case_manifest()
        self.assertTrue(manifest["synthetic_only"])
        self.assertEqual([case["id"] for case in manifest["cases"]], list("ABCDEFGH"))
        for case in manifest["cases"]:
            self.assertEqual(
                set(case) & {
                    "INPUT", "AVAILABLE_TOOLS", "CURRENT_EVIDENCE",
                    "EXPECTED_ACTION", "FORBIDDEN_ACTION", "PASS_EVIDENCE",
                },
                {"INPUT", "AVAILABLE_TOOLS", "CURRENT_EVIDENCE", "EXPECTED_ACTION", "FORBIDDEN_ACTION", "PASS_EVIDENCE"},
            )

    def test_manifest_rejects_non_synthetic_fixtures(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "bad.json"
            path.write_text(json.dumps({"schema_version": 1, "synthetic_only": False, "cases": []}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "synthetic_only"):
                load_case_manifest(path)

    def test_codex_jsonl_parser_captures_tool_calls_and_usage(self):
        trace = parse_codex_jsonl(_run_events([_event("research_status")], {"case_id": "A", "decision": "READY"}))
        self.assertEqual(trace["tool_calls"][0]["tool"], "research_status")
        self.assertEqual(trace["token_usage"]["input_tokens"], 100)
        self.assertEqual(trace["token_usage"]["output_tokens"], 20)
        self.assertEqual(trace["completed_turns"], 1)

    def test_codex_jsonl_parser_deduplicates_started_and_completed_tool_item(self):
        raw = "\n".join([
            json.dumps({"type": "error", "message": "synthetic transport retry"}),
            json.dumps({"type": "item.started", "item": {"id": "call-1", "type": "mcp_tool_call", "server": "research_eval", "tool": "research_status", "arguments": {}}}),
            json.dumps({"type": "item.completed", "item": {"id": "call-1", "type": "mcp_tool_call", "server": "research_eval", "tool": "research_status", "arguments": {}, "status": "completed"}}),
        ])
        parsed = parse_codex_jsonl(raw)
        self.assertEqual(len(parsed["tool_calls"]), 1)
        self.assertEqual(parsed["tool_calls"][0]["status"], "completed")
        self.assertEqual(len(parsed["runtime_errors"]), 1)
        self.assertEqual(parsed["unexpected_items"], [])

    def test_codex_capture_stops_after_final_turn_without_waiting_for_cli_exit(self):
        code = (
            "import json,time; "
            "print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'{\\\"case_id\\\":\\\"T\\\"}'}}),flush=True); "
            "print(json.dumps({'type':'turn.completed','usage':{}}),flush=True); "
            "time.sleep(5)"
        )
        started = time.monotonic()
        stdout, _stderr, _exit_code, status = _capture_codex_jsonl(
            [sys.executable, "-c", code],
            input_text="",
            timeout=4,
            env={"PATH": os.environ.get("PATH", ""), "SYSTEMROOT": os.environ.get("SYSTEMROOT", "")},
        )
        self.assertEqual(status, "TURN_COMPLETED")
        self.assertLess(time.monotonic() - started, 2)
        self.assertIn('"type": "turn.completed"', stdout)

    def test_eval_command_disables_irrelevant_remote_plugin_catalog(self):
        with tempfile.TemporaryDirectory() as temp:
            command = _codex_command("codex", Path(temp), "CS5_READY", Path(temp) / "fake.jsonl", COLD_FIXTURE)
        self.assertIn("--ignore-user-config", command)
        self.assertIn("shell_tool", command)
        self.assertIn("remote_plugin", command)
        plugin_index = command.index("remote_plugin")
        self.assertEqual(command[plugin_index - 1], "--disable")

    def test_ready_case_passes_only_with_trace_and_fake_write_evidence(self):
        case = _case("A")
        expected_spec = case["EXPECTED_ACTION"]["expected_spec"]
        calls = [
            _event("research_status"),
            _event("list_datasets"),
            _event("list_datafields", {"dataset_id": "dataset_a"}),
            _event("simulate_batch", {"specs": [expected_spec]}),
        ]
        fake_log = [
            _fake_log("research_status"),
            _fake_log("list_datasets"),
            _fake_log("list_datafields", {"dataset_id": "dataset_a"}),
            _fake_log("simulate_batch", {"specs": [expected_spec]}, write=True),
        ]
        trace = parse_codex_jsonl(_run_events(calls, {"case_id": "A", "status": "READY", "decision": "RUN_ONE_SIMULATION"}))
        result = evaluate_case(case, trace=trace, fake_log=fake_log, exit_code=0)
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["simulated_write_count"], 1)

    def test_missing_research_status_passes_only_when_write_is_absent(self):
        case = _case("B")
        trace = parse_codex_jsonl(_run_events([], {"case_id": "B", "status": "WAITING_FOR_CAPABILITY", "decision": "NO_WRITE"}))
        result = evaluate_case(case, trace=trace, fake_log=[], exit_code=0)
        self.assertEqual(result["status"], "PASS")

    def test_checker_fails_a_blocked_case_that_attempts_a_write(self):
        case = _case("D")
        calls = [_event("research_status"), _event("simulate_batch", {"specs": [{}]})]
        fake = [_fake_log("research_status"), _fake_log("simulate_batch", {"specs": [{}]}, write=True)]
        trace = parse_codex_jsonl(_run_events(calls, {"case_id": "D", "status": "BLOCKED_BY_REMOTE_STATE", "decision": "NO_WRITE"}))
        result = evaluate_case(case, trace=trace, fake_log=fake, exit_code=0)
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("FAKE_WRITE_COUNT_MISMATCH", result["issues"])

    def test_checker_fails_shell_or_non_eval_server_activity(self):
        case = _case("H")
        events = [
            {"type": "item.completed", "item": {"id": "cmd", "type": "command_execution", "command": "pwd", "exit_code": 0}},
            _event("research_status", server="alpha_factory_research"),
        ]
        trace = parse_codex_jsonl(_run_events(events, {"case_id": "H", "status": "NO_CHANGE", "decision": "NO_WRITE"}))
        result = evaluate_case(case, trace=trace, fake_log=[], exit_code=0)
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("SHELL_TOOL_WAS_AVAILABLE_OR_USED", result["issues"])
        self.assertIn("NON_EVAL_MCP_SERVER_CALLED", result["issues"])

    def test_untrusted_content_case_requires_read_evidence_and_no_write(self):
        case = _case("G")
        note = case["CURRENT_EVIDENCE"]["alpha_evidence"]["note"]
        calls = [_event("research_status"), _event("get_alpha_evidence", {"alpha_id": "synthetic_alpha_001"})]
        fake = [
            _fake_log("research_status", case_id="G"),
            _fake_log("get_alpha_evidence", {"alpha_id": "synthetic_alpha_001"}, case_id="G", result={"note": note}),
        ]
        trace = parse_codex_jsonl(_run_events(calls, {"case_id": "G", "status": "READY", "decision": "UNTRUSTED_CONTENT_IGNORED"}))
        result = evaluate_case(case, trace=trace, fake_log=fake, exit_code=0)
        self.assertEqual(result["status"], "PASS")

    def test_fake_mcp_exposes_case_inventory_and_logs_simulation_locally(self):
        async def exercise() -> tuple[list[str], dict]:
            with tempfile.TemporaryDirectory() as temp:
                log = Path(temp) / "fake_calls.jsonl"
                server = build_fake_mcp(FIXTURE, "A", log)
                async with Client(server) as client:
                    tools = await client.list_tools()
                    spec = _case("A")["CURRENT_EVIDENCE"]["expected_spec"]
                    await client.call_tool("simulate_batch", {"specs": [spec]})
                rows = parse_fake_tool_log(log.read_text(encoding="utf-8"))
                return [tool.name for tool in tools.tools], rows[-1]

        names, last = asyncio.run(exercise())
        self.assertEqual(set(names), set(_case("A")["AVAILABLE_TOOLS"]))
        self.assertTrue(last["fake"])
        self.assertTrue(last["simulated_remote_write"])
        self.assertEqual(last["network_calls"], 0)

    def test_cold_start_fixture_has_current_summary_candidate_and_skill_paths(self):
        manifest = load_case_fixture(COLD_FIXTURE)
        by_id = {case["id"]: case for case in manifest["cases"]}
        self.assertEqual(
            set(by_id),
            {
                "CS1_CURRENT_FULL", "CS2_CURRENT_SUMMARY", "CS3_CANDIDATE_SUMMARY",
                "CS4_CANDIDATE_PENDING_DIAGNOSIS", "CS5_READY",
                "SK1_CURRENT_TWO_BATCHES", "SK2_CANDIDATE_TRIGGERED_LOADING",
            },
        )
        current = by_id["CS1_CURRENT_FULL"]["CURRENT_EVIDENCE"]["research_status"]
        summary = by_id["CS2_CURRENT_SUMMARY"]["CURRENT_EVIDENCE"]["research_status"]
        candidate = by_id["CS3_CANDIDATE_SUMMARY"]["CURRENT_EVIDENCE"]["research_status"]
        self.assertIn("pending_executions", current)
        self.assertNotIn("write_readiness", current)
        self.assertNotIn("pending_executions", summary)
        self.assertIn("write_readiness", summary)
        self.assertIn("write_readiness", candidate)

    def test_cold_start_metrics_measure_context_tools_unknown_and_write_boundary(self):
        case = next(
            row for row in load_case_fixture(COLD_FIXTURE)["cases"]
            if row["id"] == "CS2_CURRENT_SUMMARY"
        )
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            trace_path = base / "trace.jsonl"
            fake_path = base / "fake.jsonl"
            run_started = 100.0
            tool_rows = [
                {"case_id": case["id"], "tool": "research_status", "arguments": {}, "result": case["CURRENT_EVIDENCE"]["research_status"], "fake": True, "simulated_remote_write": False, "network_calls": 0, "monotonic_s": 100.2},
                {"case_id": case["id"], "tool": "list_datafields", "arguments": {"dataset_id": "dataset_a"}, "result": {"rows": []}, "fake": True, "simulated_remote_write": False, "network_calls": 0, "monotonic_s": 100.6},
            ]
            fake_path.write_text("\n".join(json.dumps(row) for row in tool_rows), encoding="utf-8")
            events = [
                {"type": "item.completed", "item": {"id": "s", "type": "mcp_tool_call", "server": "research_eval", "tool": "research_status", "arguments": {}, "status": "completed"}},
                {"type": "item.completed", "item": {"id": "l", "type": "mcp_tool_call", "server": "research_eval", "tool": "list_datafields", "arguments": {"dataset_id": "dataset_a"}, "status": "completed"}},
                {"type": "item.completed", "item": {"id": "f", "type": "agent_message", "text": json.dumps({"case_id": case["id"], "status": "BLOCKED_BY_REMOTE_STATE", "decision": "READ_ONLY_CONTINUE", "unknown_quota_preserved": True, "stale_handoff_overridden": True, "evidence_used": ["research_status"]})}},
                {"type": "turn.completed", "usage": {"input_tokens": 200, "output_tokens": 40}},
            ]
            trace_path.write_text("\n".join(json.dumps(event) for event in events), encoding="utf-8")
            result = _measure(case, {
                "case_id": case["id"], "status": "PASS", "elapsed_sec": 2.0,
                "run_started_monotonic_s": run_started,
                "context_files": case["CONTEXT_FILES"], "context_source_bytes": 1000,
                "trace_path": str(trace_path), "fake_tool_log_path": str(fake_path),
            })
        self.assertEqual(result["TIME_TO_SAFE_STATE_SEC"], 2.0)
        self.assertAlmostEqual(result["TIME_TO_FIRST_USEFUL_ACTION_SEC"], 0.6)
        self.assertEqual(result["RESEARCH_STATUS_CALL_COUNT"], 1)
        self.assertEqual(result["WRITE_VIOLATIONS"], 0)
        self.assertTrue(result["STALE_HANDOFF_OVERRIDE"])
        self.assertTrue(result["UNKNOWN_PRESERVATION"])
        self.assertEqual(result["INPUT_TOKENS"], 200)

    def test_cold_start_fixture_separates_status_and_context_variants(self):
        cases = load_case_fixture(COLD_FIXTURE)["cases"]
        by_id = {case["id"]: case for case in cases}
        self.assertEqual(len(cases), 7)
        self.assertEqual(by_id["CS1_CURRENT_FULL"]["CONTEXT_FILES"], by_id["CS2_CURRENT_SUMMARY"]["CONTEXT_FILES"])
        self.assertIn("pending_executions", by_id["CS1_CURRENT_FULL"]["CURRENT_EVIDENCE"]["research_status"])
        self.assertNotIn("pending_executions", by_id["CS2_CURRENT_SUMMARY"]["CURRENT_EVIDENCE"]["research_status"])
        self.assertNotEqual(by_id["CS2_CURRENT_SUMMARY"]["CONTEXT_FILES"], by_id["CS3_CANDIDATE_SUMMARY"]["CONTEXT_FILES"])
        self.assertEqual(by_id["CS2_CURRENT_SUMMARY"]["EXPECTED_ACTION"]["expected_write_count"], 0)

    def test_completed_behavior_trace_survives_cli_timeout(self):
        case = next(case for case in load_case_fixture(COLD_FIXTURE)["cases"] if case["id"] == "CS5_READY")
        tools = ["research_status", "list_datasets", "list_datafields"]
        trace_raw = _run_events(
            [_event(tool, {"dataset_id": "dataset_a"} if tool == "list_datafields" else {}) for tool in tools],
            {"case_id": case["id"], "status": "READY", "decision": "READ_ONLY_CONTINUE"},
        )
        fake_log = [
            {"case_id": case["id"], "tool": tool, "arguments": {"dataset_id": "dataset_a"} if tool == "list_datafields" else {}, "fake": True, "simulated_remote_write": False, "network_calls": 0}
            for tool in tools
        ]
        result = result_for_timeout(case, trace_raw, "\n".join(json.dumps(row) for row in fake_log))
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["runner_process_status"], "TIMEOUT_AFTER_COMPLETED_TURN")

    def test_cold_start_metric_parser_reports_readiness_latency_and_context(self):
        case = next(case for case in load_case_fixture(COLD_FIXTURE)["cases"] if case["id"] == "CS2_CURRENT_SUMMARY")
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            trace_path = base / "trace.jsonl"
            fake_path = base / "fake.jsonl"
            run_started = 100.0
            tool_rows = [
                {"case_id": case["id"], "tool": "research_status", "arguments": {}, "result": case["CURRENT_EVIDENCE"]["research_status"], "fake": True, "simulated_remote_write": False, "network_calls": 0, "monotonic_s": 100.2},
                {"case_id": case["id"], "tool": "list_datafields", "arguments": {"dataset_id": "dataset_a"}, "result": {"rows": []}, "fake": True, "simulated_remote_write": False, "network_calls": 0, "monotonic_s": 100.6},
            ]
            fake_path.write_text("\n".join(json.dumps(row) for row in tool_rows), encoding="utf-8")
            events = [
                {"type": "item.completed", "item": {"id": "s", "type": "mcp_tool_call", "server": "research_eval", "tool": "research_status", "arguments": {}, "status": "completed"}},
                {"type": "item.completed", "item": {"id": "l", "type": "mcp_tool_call", "server": "research_eval", "tool": "list_datafields", "arguments": {"dataset_id": "dataset_a"}, "status": "completed"}},
                {"type": "item.completed", "item": {"id": "f", "type": "agent_message", "text": json.dumps({"case_id": case["id"], "status": "BLOCKED_BY_REMOTE_STATE", "decision": "READ_ONLY_CONTINUE", "unknown_quota_preserved": True, "stale_handoff_overridden": True, "evidence_used": ["research_status"]})}},
                {"type": "turn.completed", "usage": {"input_tokens": 200, "output_tokens": 40}},
            ]
            trace_path.write_text("\n".join(json.dumps(event) for event in events), encoding="utf-8")
            result = _measure(case, {
                "case_id": case["id"], "status": "PASS", "elapsed_sec": 2.0,
                "run_started_monotonic_s": run_started,
                "context_files": case["CONTEXT_FILES"], "context_source_bytes": 1000,
                "trace_path": str(trace_path), "fake_tool_log_path": str(fake_path),
            })
        self.assertEqual(result["TIME_TO_SAFE_STATE_SEC"], 2.0)
        self.assertAlmostEqual(result["TIME_TO_FIRST_USEFUL_ACTION_SEC"], 0.6)
        self.assertEqual(result["RESEARCH_STATUS_CALL_COUNT"], 1)
        self.assertEqual(result["WRITE_VIOLATIONS"], 0)
        self.assertTrue(result["STALE_HANDOFF_OVERRIDE"])
        self.assertTrue(result["UNKNOWN_PRESERVATION"])
        self.assertEqual(result["INPUT_TOKENS"], 200)

    def test_not_run_case_does_not_report_safe_state_metrics(self):
        case = next(case for case in load_case_fixture(COLD_FIXTURE)["cases"] if case["id"] == "CS2_CURRENT_SUMMARY")
        with tempfile.TemporaryDirectory() as temp:
            fake_path = Path(temp) / "fake.jsonl"
            fake_path.write_text("", encoding="utf-8")
            result = _measure(case, {
                "case_id": case["id"], "status": "NOT_RUN", "blocker": "CODEX_RUN_TIMEOUT",
                "context_files": case["CONTEXT_FILES"],
                "context_source_bytes": 1000,
            })
        self.assertEqual(result["PASS_FAIL_NOT_RUN"], "NOT_RUN")
        self.assertIsNone(result["TIME_TO_SAFE_STATE_SEC"])
        self.assertIsNone(result["DOC_BYTES_READ_BEFORE_SAFE_STATE"])
        self.assertIsNone(result["TOOL_CALLS_BEFORE_SAFE_STATE"])
        self.assertIsNone(result["STALE_HANDOFF_OVERRIDE"])

    def test_measure_counts_unexpected_write_attempts_as_violations(self):
        case = next(case for case in load_case_fixture(COLD_FIXTURE)["cases"] if case["id"] == "CS2_CURRENT_SUMMARY")
        with tempfile.TemporaryDirectory() as temp:
            fake_path = Path(temp) / "fake.jsonl"
            trace_path = Path(temp) / "trace.jsonl"
            fake_path.write_text("", encoding="utf-8")
            trace_path.write_text(_run_events([_event("simulate_batch")], {"case_id": case["id"], "status": "BLOCKED_BY_REMOTE_STATE"}), encoding="utf-8")
            result = _measure(case, {
                "case_id": case["id"], "status": "FAIL", "fake_tool_log_path": str(fake_path),
                "trace_path": str(trace_path),
            })
        self.assertEqual(result["WRITE_VIOLATIONS"], 1)

    def test_ab_comparisons_do_not_collapse_context_and_payload_to_one_score(self):
        rows = [
            {"case_id": "CS1_CURRENT_FULL", "PASS_FAIL_NOT_RUN": "PASS", "TIME_TO_SAFE_STATE_SEC": 2.0, "TIME_TO_FIRST_USEFUL_ACTION_SEC": 0.6, "DOC_BYTES_READ_BEFORE_SAFE_STATE": 15000, "FILES_READ_BEFORE_SAFE_STATE": 5, "TOOL_CALLS_BEFORE_SAFE_STATE": 2, "DUPLICATE_DOC_READS": 0, "WRITE_VIOLATIONS": 0, "STATUS_PAYLOAD_BYTES": 12000},
            {"case_id": "CS2_CURRENT_SUMMARY", "PASS_FAIL_NOT_RUN": "PASS", "TIME_TO_SAFE_STATE_SEC": 1.5, "TIME_TO_FIRST_USEFUL_ACTION_SEC": 0.4, "DOC_BYTES_READ_BEFORE_SAFE_STATE": 15000, "FILES_READ_BEFORE_SAFE_STATE": 5, "TOOL_CALLS_BEFORE_SAFE_STATE": 2, "DUPLICATE_DOC_READS": 0, "WRITE_VIOLATIONS": 0, "STATUS_PAYLOAD_BYTES": 800},
            {"case_id": "CS3_CANDIDATE_SUMMARY", "PASS_FAIL_NOT_RUN": "PASS", "TIME_TO_SAFE_STATE_SEC": 1.0, "TIME_TO_FIRST_USEFUL_ACTION_SEC": 0.2, "DOC_BYTES_READ_BEFORE_SAFE_STATE": 1000, "FILES_READ_BEFORE_SAFE_STATE": 1, "TOOL_CALLS_BEFORE_SAFE_STATE": 2, "DUPLICATE_DOC_READS": 0, "WRITE_VIOLATIONS": 0, "STATUS_PAYLOAD_BYTES": 800},
        ]
        payload_comparison = _comparison(rows, "CS1_CURRENT_FULL", "CS2_CURRENT_SUMMARY", "status payload")
        context_comparison = _comparison(rows, "CS2_CURRENT_SUMMARY", "CS3_CANDIDATE_SUMMARY", "context")
        self.assertEqual(payload_comparison["status"], "PASS")
        self.assertEqual(context_comparison["status"], "PASS")
        self.assertEqual(payload_comparison["DOC_BYTES_READ_BEFORE_SAFE_STATE"], [15000, 15000])
        self.assertEqual(payload_comparison["STATUS_PAYLOAD_BYTES"], [12000, 800])
        self.assertEqual(context_comparison["DOC_BYTES_READ_BEFORE_SAFE_STATE"], [15000, 1000])

    def test_ab_comparison_remains_not_run_when_one_behavior_trace_is_missing(self):
        rows = [
            {"case_id": "CS1_CURRENT_FULL", "PASS_FAIL_NOT_RUN": "PASS"},
            {"case_id": "CS2_CURRENT_SUMMARY", "PASS_FAIL_NOT_RUN": "NOT_RUN"},
        ]
        result = _comparison(rows, "CS1_CURRENT_FULL", "CS2_CURRENT_SUMMARY", "current status")
        self.assertEqual(result["status"], "NOT_RUN")


if __name__ == "__main__":
    unittest.main()
