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
from wqb_agent.client import WQBClient
from wqb_agent.evidence import load_evidence_cache
from wqb_agent.locking import acquire_os_owner_lock, release_os_owner_lock
from wqb_agent.metrics import (
    check_pass,
    checks_passed,
)
from wqb_agent.metrics import (
    extract_metrics as _extract_metrics,
)
from wqb_agent.state import RECOVERABLE_STATUSES, Experiment, Trajectory


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


def collect(state_dir):
    path = os.path.join(state_dir, "trajectory.jsonl")
    # Deduplicate while streaming instead of retaining every historical
    # active row and then materialising a second list.  This matters after a
    # day-long run with repeated UNKNOWN/PENDING observations.
    uniq = {}
    sources = []
    if os.path.exists(path):
        sources.append(iter_jsonl_objects(path))
    # A checkpoint may predate the first trajectory append.  Include its
    # recoverable, known-URL experiments so read-only reconciliation can
    # produce the audit evidence required by skip-stale.
    for checkpoint_path in sorted(
        os.path.join(state_dir, name)
        for name in os.listdir(state_dir)
        if name.endswith(".checkpoint.json")
    ):
        try:
            with open(checkpoint_path, encoding="utf-8") as handle:
                checkpoint = json.load(handle)
        except (OSError, ValueError):
            continue
        if checkpoint.get("complete") is True:
            continue
        sources.append(iter(checkpoint.get("experiments") or []))
    for e in (item for source in sources for item in source):
        if e.get("status") not in RECOVERABLE_STATUSES:
            continue
        progress_url = e.get("progress_url")
        if not isinstance(progress_url, str) or not progress_url.strip():
            continue
        round_value = _scalar_key(e.get("round"))
        identity = _scalar_key(e.get("submission_fingerprint"))
        if identity is None:
            identity = _scalar_key(e.get("expression"))
        if round_value is None or identity is None:
            continue
        uniq[(round_value, identity)] = e
    return list(uniq.values())


def commit_reconciled(state_dir, exp, alpha_id, metrics, known_ids=None,
                      trajectory=None):
    """把对账确认的 DONE 结果以新记录追加进 trajectory（append-only）。

    - 新 id（原 UNKNOWN 记录保留作审计线索，不重写历史）；
    - 表达式不变 → 终态表达式去重集合自动纳入，防止预算重放；
    - SELF_CORRELATION 以 correlations/self 的结算值叠加。
    """
    settled = load_evidence_cache(state_dir).get(alpha_id)
    if settled:
        by_name = {c.get("name"): c for c in settled.get("checks") or []}
        for check in metrics.get("checks") or []:
            rep = by_name.get(check.get("name"))
            if check_pass(check) is None and rep and check_pass(rep) is not None:
                check["pass"] = check_pass(rep)
                check["result"] = rep.get("result", check.get("result"))
                check["value"] = rep.get("value", check.get("value"))
                check["limit"] = rep.get("limit", check.get("limit"))
        metrics["passed"] = checks_passed(metrics)
    rec = Experiment.from_dict(exp)
    rec.id = (rec.id or "x") + "rc"  # 新 id：同一实验的对账更新记录
    rec.status = "DONE"
    rec.alpha_id = alpha_id
    rec.metrics = metrics
    path = os.path.join(state_dir, "trajectory.jsonl")
    lock_handle = acquire_os_owner_lock(os.path.join(state_dir, "run.lock"))
    if lock_handle is None:
        raise RuntimeError("active research owner exists; trajectory reconciliation refused")
    try:
        writer = trajectory or Trajectory(max_len=1, path=path)
        if known_ids is not None:
            # Compatibility for older callers.  New callers should pass the
            # bounded writer so it can reconcile a batch without an
            # unbounded process-wide ID set.
            if rec.id in known_ids:
                return rec
            writer.add(rec)
            known_ids.add(rec.id)
        else:
            writer.add(rec)
    finally:
        release_os_owner_lock(lock_handle)
    return rec


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--state-dir", default=".wqb_state")
    ap.add_argument("--timeout", type=int, default=120,
                    help="per-alpha polling timeout in seconds")
    ap.add_argument("--simulation-id", default=None,
                    help="only reconcile this known simulation id")
    ap.add_argument("--commit", action="store_true",
                    help="append COMPLETE outcomes into trajectory.jsonl (append-only)")
    args = ap.parse_args()

    targets = collect(args.state_dir)
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
    trajectory_writer = None
    trajectory_batch_started = False
    if args.commit:
        # Keep only this reconciliation batch's bounded IDs.  The first
        # COMPLETE result performs one full-history check; later rows in the
        # same batch are protected by the writer without materializing every
        # historical ID in memory.
        trajectory_writer = Trajectory(
            max_len=max(1, len(targets)),
            path=os.path.join(args.state_dir, "trajectory.jsonl"),
        )
        trajectory_batch = []
        for target in targets:
            planned = Experiment.from_dict(target)
            planned.id = (planned.id or "x") + "rc"
            trajectory_batch.append(planned)

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

        if outcome == "COMPLETE" and args.commit:
            metrics_full = _extract_metrics(client.get_alpha(entry["alpha_id"]))
            if not trajectory_batch_started:
                trajectory_writer.begin_append_batch(trajectory_batch)
                trajectory_batch_started = True
            rec = commit_reconciled(
                args.state_dir, exp, entry["alpha_id"], metrics_full,
                trajectory=trajectory_writer,
            )
            print(f"[COMMIT] {rec.id} status=DONE appended")

    if trajectory_batch_started:
        trajectory_writer.end_append_batch()

    out_path = os.path.join(args.state_dir, "reconcile_report.json")
    atomic_write_json_if_changed(out_path, report, ignored_keys=("reconciled_at",))
    history_path = os.path.join(args.state_dir, "reconcile_history.jsonl")
    added = append_unique_history(history_path, report["results"])
    print(f"[DONE] report -> {out_path}; history_added={added}")


if __name__ == "__main__":
    main()
