"""Reconcile historical UNKNOWN/PENDING simulations against the platform (read-only).

§6 纪律：已知 simulation URL 的重试只允许轮询同一 job，绝不重新 POST。
本脚本对 trajectory 中带 progress_url 的非终态实验逐一只读轮询：
  - COMPLETE -> 抓取 alpha payload 指标，写入 .wqb_state/reconcile_report.json
  - ERROR/FAIL -> 记录终态失败（表达式可安全标记为已证伪）
  - 仍在跑/无进展 -> 记录 stale
不修改 trajectory.jsonl / checkpoint / experience.json。

Usage:
  python scripts/reconcile_pending.py [--state-dir .wqb_state] [--timeout 120]
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wqb_agent.artifacts import atomic_write_json_if_changed, iter_jsonl_objects
from wqb_agent.checkpoints import CheckpointStore
from wqb_agent.client import WQBClient
from wqb_agent.locking import acquire_os_owner_lock, release_os_owner_lock
from wqb_agent.metrics import extract_metrics as _extract_metrics
from wqb_agent.state import RECOVERABLE_STATUSES, Trajectory


def _scalar_key(value):
    """Return a stable key fragment for legacy JSONL shapes."""
    if isinstance(value, (str, int, float, bool)) and str(value).strip():
        return str(value)
    return None


def _history_key(entry):
    """Normalize a reconciliation identity without admitting list/dict keys."""
    outcome = entry.get("outcome")
    keys = ("simulation_id", "outcome", "alpha_id")
    if outcome in {"STALE", "UNKNOWN"}:
        # Repeated stale observations are evidence for the explicit
        # skip-stale threshold; their timestamp makes each observation
        # auditable while COMPLETE/ERROR states remain idempotent.
        keys += ("reconciled_at",)
    raw = tuple(entry.get(key) for key in keys)
    normalized = tuple(
        _scalar_key(value) if value is not None else None for value in raw
    )
    if any(value is not None and normalized[index] is None
           for index, value in enumerate(raw)):
        return None
    return normalized if any(value is not None for value in normalized) else None


def append_unique_history(path, entries):
    """Append only new reconciliation states to the single audit log.

    Re-polling an unchanged remote job is expected during a long run; the
    same stale/complete observation must not inflate the audit file forever.
    A later state or alpha id remains a new audit event.
    """
    entries = [
        entry for entry in (entries or [])
        if isinstance(entry, dict)
        and _history_key(entry) is not None
    ]
    if not entries:
        return 0
    parent = os.path.dirname(os.path.abspath(path))
    if not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)
    owner_lock = acquire_os_owner_lock(os.path.join(parent, "run.lock"))
    if owner_lock is None:
        raise RuntimeError("active research owner exists; reconciliation history append refused")
    try:
        return _append_unique_history_locked(path, entries)
    finally:
        release_os_owner_lock(owner_lock)


def _append_unique_history_locked(path, entries):
    """Append a batch after the caller has acquired the shared owner lock."""
    # The audit log is append-only and can span a full day's run.  Only
    # retain keys that could be emitted by this invocation; loading every
    # historical key would turn an idempotency check into an unbounded index.
    candidate_keys = {
        _history_key(entry)
        for entry in entries
    }
    existing = set()
    for row in iter_jsonl_objects(path):
        key = _history_key(row)
        if key in candidate_keys:
            existing.add(key)
    new_entries = []
    for entry in entries:
        key = _history_key(entry)
        if key in existing:
            continue
        existing.add(key)
        new_entries.append(entry)
    # Do not create an empty audit file, open it, or fsync it when this
    # invocation contributes no new evidence.  Re-polling an unchanged job is
    # a normal unattended path and must be a true no-op at the artifact layer.
    if not new_entries:
        return 0
    added = 0
    with open(path, "a", encoding="utf-8") as handle:
        for entry in new_entries:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
            added += 1
        handle.flush()
        os.fsync(handle.fileno())
    return added


def collect(state_dir, *, blockers=None):
    path = os.path.join(state_dir, "trajectory.jsonl")
    # Deduplicate while streaming instead of retaining every historical
    # active row and then materialising a second list.  This matters after a
    # day-long run with repeated UNKNOWN/PENDING observations.
    uniq = {}
    accepted = []
    sources = []
    trajectory = Trajectory(path=path)
    if os.path.exists(path):
        sources.append(trajectory.iter_canonical_rows())
    checkpoint_store = CheckpointStore(state_dir)
    for record in checkpoint_store.scan():
        if record["malformed"]:
            if blockers is not None:
                blockers.append({
                    "code": "RECONCILE_CHECKPOINT_UNVERIFIABLE",
                    "round": record["round_no"],
                })
            continue
        if record["checkpoint"].get("complete") is True:
            continue
        sources.append(iter(record["checkpoint"].get("experiments") or []))
    for e in (item for source in sources for item in source):
        if not isinstance(e, dict):
            continue
        if e.get("status") not in RECOVERABLE_STATUSES:
            continue
        progress_url = e.get("progress_url")
        if not isinstance(progress_url, str) or not progress_url.strip():
            continue
        round_value = _scalar_key(e.get("round"))
        identity = _scalar_key(e.get("id"))
        fingerprint = _scalar_key(e.get("submission_fingerprint"))
        proposal_id = _scalar_key(e.get("proposal_id"))
        if round_value is None or not any((identity, fingerprint, proposal_id)):
            continue
        aliases = []
        if identity:
            aliases.append(("id", identity))
        if fingerprint:
            aliases.append(("remote", fingerprint, progress_url))
        if proposal_id:
            aliases.append(("proposal", proposal_id, progress_url))
        if not any(alias in uniq for alias in aliases):
            accepted.append(e)
            for alias in aliases:
                uniq[alias] = e
    return accepted


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--state-dir", default=".wqb_state")
    ap.add_argument("--timeout", type=int, default=120,
                    help="per-alpha polling timeout in seconds")
    ap.add_argument("--simulation-id", default=None,
                    help="only reconcile this known simulation id")
    ap.add_argument("--commit", action="store_true",
                    help="retired; canonical recovery is owned by run-proposals")
    args = ap.parse_args(argv)
    if args.commit:
        print("RECONCILE_COMMIT_RETIRED: use canonical checkpoint recovery via run-proposals")
        return 2

    blockers = []
    targets = collect(args.state_dir, blockers=blockers)
    for blocker in blockers:
        print(f"{blocker['code']} round={blocker['round']}")
    if args.simulation_id:
        targets = [e for e in targets
                   if e.get("progress_url", "").rstrip("/").split("/")[-1]
                   == args.simulation_id]
    print(f"[SCAN] {len(targets)} reconcilable experiments with progress_url")
    if not targets:
        return

    client = WQBClient()
    reconciled_at = time.strftime("%Y-%m-%dT%H:%M:%S")
    report = {"reconciled_at": reconciled_at, "results": []}
    for exp in targets:
        url = exp["progress_url"]
        rid = url.rstrip("/").split("/")[-1]
        entry = {
            "round": exp.get("round"),
            "simulation_id": rid,
            "expression": exp.get("expression"),
            "old_status": exp.get("status"),
        }
        start = time.time()
        outcome = None
        while time.time() - start < args.timeout:
            snapshot = client.get_progress_snapshot(url, timeout=60)
            status_code = snapshot["status_code"]
            headers = snapshot["headers"]
            retry_after = headers.get("Retry-After")
            if retry_after:
                # Retry-After 可能是秒数或 HTTP-date；统一走 client 解析器。
                time.sleep(min(snapshot.get("retry_after_seconds", 1), 30))
                continue
            data = snapshot["payload"]
            if not isinstance(data, dict):
                entry["outcome"] = "ERROR"
                entry["message"] = f"non-JSON poll response ({status_code})"
                outcome = "ERROR"
                break
            status = (data.get("status") or "").upper()
            alpha_id = data.get("alpha")
            if status in ("COMPLETE", "WARNING") and alpha_id:
                payload = client.get_alpha(alpha_id)
                metrics = _extract_metrics(payload)
                entry.update({"outcome": "COMPLETE", "alpha_id": alpha_id,
                              "metrics": {k: metrics.get(k) for k in
                                          ("sharpe", "fitness", "turnover", "returns",
                                           "drawdown", "margin", "passed")},
                              "checks": [
                                  {"name": c.get("name"), "pass": c.get("pass"),
                                   "value": c.get("value")}
                                  for c in metrics.get("checks") or []]})
                outcome = "COMPLETE"
                break
            if status in ("ERROR", "FAIL", "FAILED"):
                entry.update({"outcome": "ERROR",
                              "message": str(data.get("message") or "")[:200]})
                outcome = "ERROR"
                break
            time.sleep(10)
        if outcome is None:
            entry["outcome"] = "STALE"
        entry["reconciled_at"] = reconciled_at
        report["results"].append(entry)
        print("[{}] r{} {} | S={} F={} TO={}".format(
            entry.get("outcome"), entry.get("round"), rid[:12],
            (entry.get("metrics") or {}).get("sharpe"),
            (entry.get("metrics") or {}).get("fitness"),
            (entry.get("metrics") or {}).get("turnover")))

    out_path = os.path.join(args.state_dir, "reconcile_report.json")
    atomic_write_json_if_changed(out_path, report, ignored_keys=("reconciled_at",))
    history_path = os.path.join(args.state_dir, "reconcile_history.jsonl")
    added = append_unique_history(history_path, report["results"])
    print(f"[DONE] report -> {out_path}; history_added={added}")


if __name__ == "__main__":
    raise SystemExit(main())
