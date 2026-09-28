"""Run synthetic cold-start and progressive-disclosure cases via Codex JSONL."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import uuid
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.run_research_agent_eval import (  # noqa: E402
    ROOT,
    WRITE_TOOLS,
    _run_one_case,
    load_case_fixture,
    parse_codex_jsonl,
    parse_fake_tool_log,
    parse_final_json,
)

COLD_FIXTURE = ROOT / "tests" / "fixtures" / "research_agent_eval" / "cold_start.json"


def _measure(case: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    fake_path_value = result.get("fake_tool_log_path")
    fake_path = Path(fake_path_value) if fake_path_value else None
    fake_log = parse_fake_tool_log(fake_path.read_text(encoding="utf-8")) if fake_path and fake_path.exists() else []
    final = result.get("final_payload") or {}
    trace = {}
    if result.get("trace_path"):
        trace_path = Path(result["trace_path"])
        if trace_path.exists():
            trace = parse_codex_jsonl(trace_path.read_text(encoding="utf-8"))
    if not final and trace:
        final = parse_final_json(trace["messages"]) or {}
    token_usage = result.get("token_usage") or trace.get("token_usage", {})
    run_started = float(result.get("run_started_monotonic_s", 0.0))
    useful_tools = set(case.get("USEFUL_ACTION_TOOLS", []))
    useful_rows = [row for row in fake_log if row.get("tool") in useful_tools]
    first_useful = min(
        (
            max(0.0, float(row["monotonic_s"]) - run_started)
            for row in useful_rows
            if row.get("monotonic_s") is not None
        ),
        default=None,
    )
    context_files = result.get("context_files", [])
    context_file_set = set(context_files)
    reference_rows = [row for row in fake_log if row.get("tool") == "read_workspace_reference"]
    reference_paths = [str(row.get("arguments", {}).get("path", "")) for row in reference_rows]
    reference_counts = Counter(reference_paths)
    duplicate_reads = sum(max(0, count - 1) for count in reference_counts.values())
    duplicate_reads += sum(1 for path in reference_paths if path in context_file_set)
    reference_bytes = sum(int(row.get("result", {}).get("bytes", 0)) for row in reference_rows)
    hint_bytes = len(str(case.get("DISCOVERY_HINT", "")).encode("utf-8"))
    expected = case.get("EXPECTED_ACTION", {})
    writes = [row for row in fake_log if row.get("simulated_remote_write")]
    current_status = case.get("CURRENT_EVIDENCE", {}).get("research_status")
    if current_status is None and case.get("CURRENT_EVIDENCE", {}).get("research_status_sequence"):
        current_status = case["CURRENT_EVIDENCE"]["research_status_sequence"][0]
    status_payload_bytes = (
        len(json.dumps(current_status, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        if current_status is not None else 0
    )
    status_calls = max(
        sum(row.get("tool") == "research_status" for row in fake_log),
        sum(call.get("tool") == "research_status" for call in trace.get("tool_calls", [])),
    )
    decision_ok = result.get("status") == "PASS"
    unknown_preserved = final.get("unknown_quota_preserved") is True if final and status_calls else None
    stale_override = (
        bool(case.get("DISCOVERY_HINT"))
        and status_calls > 0
        and final.get("status") == "BLOCKED_BY_REMOTE_STATE"
        and final.get("stale_handoff_overridden") is True
        if final and status_calls else None
    )
    write_tool_calls = sum(call.get("tool") in WRITE_TOOLS for call in trace.get("tool_calls", []))
    write_attempts = max(len(writes), write_tool_calls)
    write_violations = max(0, write_attempts - int(expected.get("expected_write_count", 0)))
    safe_time = float(result.get("elapsed_sec", 0.0)) if decision_ok else None
    return {
        "case_id": case["id"],
        "variant": case.get("CONTEXT_FILES", []),
        "PASS_FAIL_NOT_RUN": result.get("status"),
        "BLOCKER": result.get("blocker"),
        "RUNNER_PROCESS_STATUS": result.get("runner_process_status"),
        "issues": result.get("issues", []),
        "RUNTIME_ERROR_COUNT": len(trace.get("runtime_errors", [])),
        "TIME_TO_SAFE_STATE_SEC": safe_time,
        "TIME_TO_SAFE_STATE_MEASURE": "elapsed to completed structured response; conservative upper bound",
        "TIME_TO_FIRST_USEFUL_ACTION_SEC": first_useful,
        "DOC_BYTES_READ_BEFORE_SAFE_STATE": int(result.get("context_source_bytes", 0)) + hint_bytes + reference_bytes if decision_ok else None,
        "FILES_READ_BEFORE_SAFE_STATE": len(context_files) + len(set(reference_paths) - context_file_set) if decision_ok else None,
        "TOOL_CALLS_BEFORE_SAFE_STATE": int(result.get("total_tool_calls", len(fake_log))) if decision_ok else None,
        "DUPLICATE_DOC_READS": duplicate_reads,
        "RESEARCH_STATUS_CALL_COUNT": status_calls,
        "WRITE_VIOLATIONS": write_violations,
        "STALE_HANDOFF_OVERRIDE": stale_override if case.get("DISCOVERY_HINT") else None,
        "UNKNOWN_PRESERVATION": unknown_preserved if current_status and current_status.get("quota", {}).get("status") == "UNKNOWN" else None,
        "INPUT_TOKENS": token_usage.get("input_tokens"),
        "CACHED_INPUT_TOKENS": token_usage.get("cached_input_tokens"),
        "OUTPUT_TOKENS": token_usage.get("output_tokens"),
        "STATUS_PAYLOAD_BYTES": status_payload_bytes,
        "CONTEXT_SOURCE_BYTES": int(result.get("context_source_bytes", 0)),
        "CONTEXT_SOURCE_FILES": context_files,
        "REFERENCE_READS": reference_paths,
        "FAKE_TOOL_CALLS": dict(Counter(str(row.get("tool")) for row in fake_log)),
        "SIMULATED_WRITE_COUNT": len(writes),
        "TRACE": result.get("trace_path"),
        "FAKE_TOOL_LOG": result.get("fake_tool_log_path"),
    }


def _comparison(rows: list[dict[str, Any]], left_id: str, right_id: str, name: str) -> dict[str, Any]:
    by_id = {row["case_id"]: row for row in rows}
    left, right = by_id.get(left_id), by_id.get(right_id)
    if left is None or right is None:
        return {"comparison": name, "status": "NOT_RUN", "cases": [left_id, right_id]}
    states = [left.get("PASS_FAIL_NOT_RUN"), right.get("PASS_FAIL_NOT_RUN")]
    comparison_status = "FAIL" if "FAIL" in states else "PASS" if states == ["PASS", "PASS"] else "NOT_RUN"
    metrics = (
        "PASS_FAIL_NOT_RUN",
        "TIME_TO_SAFE_STATE_SEC",
        "TIME_TO_FIRST_USEFUL_ACTION_SEC",
        "DOC_BYTES_READ_BEFORE_SAFE_STATE",
        "FILES_READ_BEFORE_SAFE_STATE",
        "TOOL_CALLS_BEFORE_SAFE_STATE",
        "DUPLICATE_DOC_READS",
        "WRITE_VIOLATIONS",
        "STATUS_PAYLOAD_BYTES",
    )
    return {
        "comparison": name,
        "cases": [left_id, right_id],
        "status": comparison_status,
        **{metric: [left.get(metric), right.get(metric)] for metric in metrics},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-agent", action="store_true", help="Run local Codex JSONL cases; may use model tokens")
    parser.add_argument("--cases", nargs="+", help="Optional cold-start case ids; defaults to all")
    parser.add_argument("--codex", help="Codex executable; defaults to PATH discovery")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "tmp" / "logs")
    parser.add_argument("--timeout", type=int, default=240)
    args = parser.parse_args(argv)
    manifest = load_case_fixture(COLD_FIXTURE)
    cases = manifest["cases"]
    if args.cases:
        selected = set(args.cases)
        cases = [case for case in cases if case["id"] in selected]
        if len(cases) != len(selected):
            parser.error("unknown cold-start case id")
    if not args.run_agent:
        for case in cases:
            print(f"{case['id']}\t{case['title']}")
        print("LOCAL_AGENT_LANE=NOT_RUN (pass --run-agent to execute synthetic Codex cases)")
        return 0
    codex = args.codex or shutil.which("codex")
    if not codex:
        for case in cases:
            print(f"{case['id']} NOT_RUN blocker=EXECUTABLE_AGENT_TRACE_UNAVAILABLE")
        return 2
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    rows = []
    for case in cases:
        raw = _run_one_case(
            case,
            codex=codex,
            run_id=run_id,
            output_dir=args.output_dir,
            timeout=args.timeout,
            fixture_path=COLD_FIXTURE,
        )
        rows.append(_measure(case, raw))
    summary = {
        "runner": "Codex JSONL / synthetic MCP",
        "runtime_status_source": "SYNTHETIC_FIXTURE; live BRAIN status not fetched because real credentials are prohibited",
        "run_id": run_id,
        "cases": rows,
        "A_B_RESULTS": {
            "CURRENT_FULL_vs_CURRENT_SUMMARY": _comparison(rows, "CS1_CURRENT_FULL", "CS2_CURRENT_SUMMARY", "pending payload progressive disclosure under current prompt"),
            "CURRENT_SUMMARY_vs_CANDIDATE_SUMMARY": _comparison(rows, "CS2_CURRENT_SUMMARY", "CS3_CANDIDATE_SUMMARY", "current prompt versus candidate inline contract under summary status"),
        },
        "SKILL_LOADING": {
            "CURRENT_RULE": next((row for row in rows if row["case_id"] == "SK1_CURRENT_TWO_BATCHES"), None),
            "CANDIDATE_RULE": next((row for row in rows if row["case_id"] == "SK2_CANDIDATE_TRIGGERED_LOADING"), None),
        },
    }
    summary_path = args.output_dir / f"{run_id}_cold_start_summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for row in rows:
        print(
            f"{row['case_id']} {row['PASS_FAIL_NOT_RUN']} safe={row['TIME_TO_SAFE_STATE_SEC']}s "
            f"first_useful={row['TIME_TO_FIRST_USEFUL_ACTION_SEC']}s docs={row['DOC_BYTES_READ_BEFORE_SAFE_STATE']}B "
            f"files={row['FILES_READ_BEFORE_SAFE_STATE']} tools={row['TOOL_CALLS_BEFORE_SAFE_STATE']} writes={row['WRITE_VIOLATIONS']}"
        )
    print(f"SUMMARY={summary_path}")
    return 0 if all(row["PASS_FAIL_NOT_RUN"] == "PASS" for row in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
