"""Deterministic offline benchmark harness for local state/IO hot paths.

The harness is offline only: synthetic rows live in a temporary directory and
timing uses the standard library.  It never constructs a ``Client`` or
``Simulator``, never touches the network and never reads or writes
``.wqb_state``; the point is to measure local JSON/JSONL and canonical-merge
cost on representative sizes, not platform latency.

Usage:
    python scripts/benchmark_local_io.py
    python scripts/benchmark_local_io.py --rows 1000,10000 --repeat 7
    python scripts/benchmark_local_io.py --workloads trajectory_load
    python scripts/benchmark_local_io.py --codecs json,orjson --output research_data/bench.txt
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import sys
import tempfile
import time
import tracemalloc
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from wqb_agent import artifacts  # noqa: E402
from wqb_agent.discovery import FieldDiscovery  # noqa: E402
from wqb_agent.state import Experiment, Trajectory  # noqa: E402
from wqb_agent.trial_ledger import TrialLedger  # noqa: E402

DEFAULT_ROWS = (1000, 10000, 50000)
DEFAULT_REPEAT = 5
DEFAULT_CODEC = "json"
FIND_ROW_BATCH = 32
WRITE_ROWS = 512


class SkipWorkload(RuntimeError):
    """Raised when a workload cannot be built offline (e.g. missing codec)."""


def _synthetic_experiments(count, seed=20260912):
    """Generate representative trajectory rows without any private data."""
    rng = random.Random(seed)
    for index in range(count):
        experiment = Experiment(
            round=1,
            hypothesis_id=f"h{index % 97}",
            expression=f"rank(ts_delta(close, {index % 40 + 1}))",
            settings={"region": "USA", "universe": "TOP3000"},
            fields_used=["close"],
            datasets=["pv1"],
        )
        experiment.id = f"exp{index:08d}"
        experiment.proposal_id = f"prop{index:08d}"
        experiment.status = "PENDING" if index % 4 == 0 else "DONE"
        if experiment.status == "DONE":
            experiment.metrics = {
                "sharpe": round(1.0 + rng.random(), 4),
                "fitness": round(0.4 + rng.random(), 4),
                "turnover": 0.1,
            }
        experiment.created_at = 1_700_000_000 + index
        yield experiment


_FIXTURES: dict[tuple, object] = {}


def _write_trajectory(path, count):
    with open(path, "w", encoding="utf-8") as handle:
        for experiment in _synthetic_experiments(count):
            handle.write(json.dumps(experiment.to_dict(), ensure_ascii=False))
            handle.write("\n")


def _state_dir(root, rows):
    key = ("state", rows)
    if key not in _FIXTURES:
        directory = os.path.join(root, f"state_{rows}")
        os.makedirs(directory, exist_ok=True)
        _write_trajectory(os.path.join(directory, "trajectory.jsonl"), rows)
        _FIXTURES[key] = directory
    return _FIXTURES[key]


def _trajectory_path(root, rows):
    return os.path.join(_state_dir(root, rows), "trajectory.jsonl")


def _sample_ids(rows, count):
    step = max(1, rows // max(1, count * 4))
    candidates = [f"exp{index:08d}" for index in range(0, rows, step)]
    return candidates[:count]


def _load_catalog(root, rows):
    key = ("catalog", rows)
    if key not in _FIXTURES:
        base = os.path.join(root, f"discovery_{rows}")
        catalog = os.path.join(base, "platform_field_catalog_20260912")
        os.makedirs(catalog, exist_ok=True)
        fields = [
            {
                "id": f"synthetic_field_{index:05d}",
                "description": "synthetic benchmark field",
                "type": "MATRIX" if index % 2 else "VECTOR",
            }
            for index in range(rows)
        ]
        with open(os.path.join(catalog, "pv1.json"), "w", encoding="utf-8") as handle:
            json.dump({"fields": fields}, handle)
        with open(
            os.path.join(catalog, "manifest.json"), "w", encoding="utf-8"
        ) as handle:
            json.dump(
                {
                    "fetched_at": "2026-09-12T00:00:00+0800",
                    "datasets": {"pv1": {"file": "pv1.json"}},
                },
                handle,
            )
        _FIXTURES[key] = base
    return _FIXTURES[key]


def _build_disk_cache(root, rows):
    key = ("disk_cache", rows)
    if key not in _FIXTURES:
        base = os.path.join(root, f"cache_only_{rows}")
        os.makedirs(base, exist_ok=True)
        cache_path = os.path.join(base, "fields_cache.json")
        datasets = {
            f"ds{index:03d}": [
                {"id": f"field_{index}_{field}", "type": "MATRIX"}
                for field in range(8)
            ]
            for index in range(max(1, rows // 200))
        }
        with open(cache_path, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "schema": FieldDiscovery.CACHE_SCHEMA,
                    "saved_at": time.time(),
                    "datasets": datasets,
                },
                handle,
            )
        _FIXTURES[key] = cache_path
    return _FIXTURES[key]


def _decoder(codec):
    if codec == DEFAULT_CODEC:
        return json.loads
    if codec == "orjson":
        try:
            import orjson
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise SkipWorkload(f"orjson unavailable: {exc}") from exc
        return orjson.loads
    raise SkipWorkload(f"unknown codec: {codec}")


def workload_trajectory_load(rows, root, codec):
    path = _trajectory_path(root, rows)

    def run():
        Trajectory(max_len=100, path=path, persist=True).load()

    return run


def workload_trajectory_iter_canonical(rows, root, codec):
    path = _trajectory_path(root, rows)

    def run():
        for _row in Trajectory(path=path, persist=True).iter_canonical_rows():
            pass

    return run


def workload_trajectory_iter_rows(rows, root, codec):
    path = _trajectory_path(root, rows)

    def run():
        for _row in Trajectory(path=path, persist=True).iter_rows():
            pass

    return run


def workload_trajectory_find_row_batch(rows, root, codec):
    path = _trajectory_path(root, rows)
    targets = _sample_ids(rows, FIND_ROW_BATCH)

    def run():
        trajectory = Trajectory(path=path, persist=True)
        for target in targets:
            trajectory.find_row(target)

    return run


def workload_trajectory_contains_ids(rows, root, codec):
    path = _trajectory_path(root, rows)
    targets = _sample_ids(rows, FIND_ROW_BATCH)

    def run():
        Trajectory(path=path, persist=True).contains_ids(targets)

    return run


def workload_trajectory_find_completed_expressions(rows, root, codec):
    path = _trajectory_path(root, rows)
    expressions = [
        f"rank(ts_delta(close, {index % 40 + 1}))" for index in range(FIND_ROW_BATCH)
    ]

    def run():
        Trajectory(path=path, persist=True).find_completed_expressions(expressions)

    return run


def workload_artifacts_iter_jsonl(rows, root, codec):
    path = _trajectory_path(root, rows)

    def run():
        for _row in artifacts.iter_jsonl_objects(path):
            pass

    return run


def workload_artifacts_write_json_unchanged(rows, root, codec):
    payload = {
        "schema": 2,
        "datasets": {
            f"ds{index:03d}": {"fields": index}
            for index in range(max(1, rows // 200))
        },
    }
    path = os.path.join(root, f"write_json_{rows}.json")

    def run():
        artifacts.atomic_write_json_if_changed(path, payload, indent=None)

    return run


def workload_artifacts_write_jsonl_unchanged(rows, root, codec):
    rows_payload = [
        {"id": f"row{index:06d}", "value": index}
        for index in range(min(rows, WRITE_ROWS))
    ]
    path = os.path.join(root, f"write_jsonl_{rows}.jsonl")

    def run():
        artifacts.atomic_write_jsonl_if_changed(path, rows_payload)

    return run


def workload_trial_ledger_append(rows, root, codec):
    """Append a small batch after one synthetic historical ledger scan."""
    directory = os.path.join(root, f"trial_ledger_{rows}")
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, "trial_ledger.jsonl")
    with open(path, "w", encoding="utf-8") as handle:
        for index in range(rows):
            handle.write(json.dumps({
                "event_id": f"synthetic-{index:08d}",
                "phase": "candidate_generated",
                "candidate_id": f"candidate-{index:08d}",
            }) + "\n")
    ledger = TrialLedger(path)
    counter = {"value": 0}

    def run():
        index = counter["value"]
        counter["value"] += 1
        candidate = f"benchmark-new-{index:06d}"
        ledger.record({
            "candidate_id": candidate,
            "proposal_id": candidate,
            "expression": "rank(synthetic_field)",
            "round": 1,
        }, "candidate_generated", outcome="CONSIDERED")

    return run


def workload_trial_ledger_startup(rows, root, codec):
    """Measure rebuilding the transient membership index from JSONL."""
    directory = os.path.join(root, f"trial_ledger_startup_{rows}")
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, "trial_ledger.jsonl")
    with open(path, "w", encoding="utf-8") as handle:
        for index in range(rows):
            handle.write(json.dumps({
                "event_id": f"synthetic-{index:08d}",
                "phase": "candidate_generated",
                "candidate_id": f"candidate-{index:08d}",
            }) + "\n")

    def run():
        ledger = TrialLedger(path)
        ledger.initialize_history_completeness(path)
        ledger._close_membership_db_unlocked()

    return run


def workload_discovery_local_catalog(rows, root, codec):
    base = _load_catalog(root, rows)
    cache_path = os.path.join(base, "fields_cache.json")

    def run():
        FieldDiscovery(None, cache_path=cache_path)

    return run


def workload_discovery_disk_cache(rows, root, codec):
    cache_path = _build_disk_cache(root, rows)

    def run():
        FieldDiscovery(None, cache_path=cache_path)

    return run


def workload_jsonl_decode(rows, root, codec):
    loader = _decoder(codec)
    path = _trajectory_path(root, rows)
    with open(path, encoding="utf-8") as handle:
        lines = handle.read().splitlines()

    def run():
        for line in lines:
            loader(line)

    return run


WORKLOADS = {
    "trajectory_load": workload_trajectory_load,
    "trajectory_iter_canonical": workload_trajectory_iter_canonical,
    "trajectory_iter_rows": workload_trajectory_iter_rows,
    "trajectory_find_row_batch": workload_trajectory_find_row_batch,
    "trajectory_contains_ids": workload_trajectory_contains_ids,
    "trajectory_find_completed_expressions": workload_trajectory_find_completed_expressions,
    "artifacts_iter_jsonl": workload_artifacts_iter_jsonl,
    "artifacts_write_json_unchanged": workload_artifacts_write_json_unchanged,
    "artifacts_write_jsonl_unchanged": workload_artifacts_write_jsonl_unchanged,
    "trial_ledger_append": workload_trial_ledger_append,
    "trial_ledger_startup": workload_trial_ledger_startup,
    "discovery_local_catalog": workload_discovery_local_catalog,
    "discovery_disk_cache": workload_discovery_disk_cache,
    "jsonl_decode": workload_jsonl_decode,
}


def _p95(samples):
    ordered = sorted(samples)
    index = max(0, math.ceil(0.95 * len(ordered)) - 1)
    return ordered[index]


def _measure(run, repeat):
    run()  # warmup, never reported
    samples = []
    peak_bytes = 0
    for _ in range(repeat):
        tracemalloc.start()
        tracemalloc.reset_peak()
        start = time.perf_counter()
        run()
        samples.append((time.perf_counter() - start) * 1000.0)
        _, current_peak = tracemalloc.get_traced_memory()
        peak_bytes = max(peak_bytes, current_peak)
        tracemalloc.stop()
    return samples, peak_bytes / 1024.0


def _parse_rows(raw):
    rows = []
    for chunk in str(raw or "").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        value = int(chunk)
        if value <= 0:
            raise SystemExit(f"rows must be positive: {chunk}")
        rows.append(value)
    return rows or list(DEFAULT_ROWS)


def _parse_workloads(raw):
    names = [chunk.strip() for chunk in str(raw or "").split(",") if chunk.strip()]
    unknown = [name for name in names if name not in WORKLOADS]
    if unknown:
        raise SystemExit(f"unknown workloads: {', '.join(unknown)}")
    return names or list(WORKLOADS)


def _parse_codecs(raw):
    names = [chunk.strip() for chunk in str(raw or "").split(",") if chunk.strip()]
    return names or [DEFAULT_CODEC]


def run_benchmarks(*, rows_list, workloads, codecs, repeat):
    results = []
    with tempfile.TemporaryDirectory(prefix="wqb_benchmark_") as root:
        for rows in rows_list:
            for name in workloads:
                factory = WORKLOADS[name]
                candidates = codecs if name == "jsonl_decode" else [DEFAULT_CODEC]
                for codec in candidates:
                    try:
                        run = factory(rows, root, codec)
                    except SkipWorkload as exc:
                        print(f"skip {name} rows={rows} codec={codec}: {exc}", file=sys.stderr)
                        continue
                    samples, peak_kb = _measure(run, repeat)
                    results.append(
                        {
                            "workload": name,
                            "rows": rows,
                            "implementation": codec,
                            "median_ms": statistics_median(samples),
                            "p95_ms": _p95(samples),
                            "python_peak_kb": peak_kb,
                        }
                    )
    return results


def statistics_median(samples):
    ordered = sorted(samples)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def _format_table(results):
    lines = [
        f"{'workload':<34}{'rows':>8}{'implementation':>16}{'median_ms':>12}{'p95_ms':>10}{'peak_kb':>12}"
    ]
    for row in results:
        lines.append(
            f"{row['workload']:<34}{row['rows']:>8}{row['implementation']:>16}"
            f"{row['median_ms']:>12.3f}{row['p95_ms']:>10.3f}{row['python_peak_kb']:>12.1f}"
        )
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", default=",".join(str(row) for row in DEFAULT_ROWS))
    parser.add_argument("--workloads", default="")
    parser.add_argument("--codecs", default=DEFAULT_CODEC)
    parser.add_argument("--repeat", type=int, default=DEFAULT_REPEAT)
    parser.add_argument("--output", default="")
    args = parser.parse_args(argv)

    repeat = max(1, int(args.repeat))
    results = run_benchmarks(
        rows_list=_parse_rows(args.rows),
        workloads=_parse_workloads(args.workloads),
        codecs=_parse_codecs(args.codecs),
        repeat=repeat,
    )
    table = _format_table(results)
    print(table)
    if args.output:
        target = Path(args.output)
        target.parent.mkdir(parents=True, exist_ok=True)
        header = f"# repeat={repeat} rows={args.rows} codecs={args.codecs}\n"
        target.write_text(header + table + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
