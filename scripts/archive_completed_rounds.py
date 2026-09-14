"""Archive completed round state without touching live recovery state.

Default is dry-run. Use --apply only after reviewing the planned moves.
Incomplete checkpoints and the newest ``--keep`` round summaries stay in
``.wqb_state``; completed older checkpoints go to docs/archive/checkpoints.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from wqb_agent.locking import acquire_os_owner_lock, release_os_owner_lock

SUMMARY_RE = re.compile(r"round_(\d+)\.json$")
CHECKPOINT_RE = re.compile(r"round_(\d+)\.checkpoint\.json$")


def _round_files(state_dir: Path):
    summaries = {}
    checkpoints = {}
    for path in state_dir.iterdir():
        if not path.is_file():
            continue
        match = SUMMARY_RE.fullmatch(path.name)
        if match:
            summaries[int(match.group(1))] = path
            continue
        match = CHECKPOINT_RE.fullmatch(path.name)
        if match:
            checkpoints[int(match.group(1))] = path
    return summaries, checkpoints


def _checkpoint_complete(path: Path) -> bool:
    try:
        with path.open(encoding="utf-8") as handle:
            return json.load(handle).get("complete") is True
    except (OSError, ValueError, TypeError):
        return False


def ensure_archive_dir_safe(state_dir: Path, archive_dir: Path):
    """Reject an archive target inside live state before any planning/apply."""
    try:
        archive_dir.resolve().relative_to(state_dir.resolve())
    except ValueError:
        return
    raise ValueError(
        f"archive directory must be outside live state directory: {archive_dir}"
    )


def plan(state_dir: Path, archive_dir: Path, keep: int):
    summaries, checkpoints = _round_files(state_dir)
    newest = sorted(summaries)[-keep:] if keep else []
    incomplete = {
        round_no
        for round_no, path in checkpoints.items()
        if not _checkpoint_complete(path)
    }
    protected = set(newest) | incomplete
    summary_moves = [
        (path, archive_dir / "rounds" / path.name)
        for round_no, path in sorted(summaries.items())
        if round_no not in protected
    ]
    checkpoint_moves = [
        (path, archive_dir / "checkpoints" / path.name)
        for round_no, path in sorted(checkpoints.items())
        if round_no not in protected and _checkpoint_complete(path)
    ]
    return summary_moves, checkpoint_moves, protected


def _move(source: Path, target: Path):
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if source.read_bytes() == target.read_bytes():
            source.unlink()
            return "deduplicated"
        raise RuntimeError(f"archive target differs: {target}")
    shutil.move(str(source), str(target))
    return "moved"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--state-dir", type=Path, default=Path(".wqb_state"))
    parser.add_argument(
        "--archive-dir", type=Path, default=None,
        help="归档目录；省略时使用 state-dir 同级项目目录下的 docs/archive",
    )
    parser.add_argument("--keep", type=int, default=10)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    if args.keep < 0:
        parser.error("--keep must be non-negative")
    state_dir = args.state_dir.resolve()
    if not state_dir.is_dir():
        raise SystemExit(f"state directory not found: {state_dir}")
    if args.archive_dir is None:
        archive_dir = state_dir.parent / "docs" / "archive"
    else:
        archive_dir = args.archive_dir
        if not archive_dir.is_absolute():
            archive_dir = (Path.cwd() / archive_dir).resolve()
    archive_dir = archive_dir.resolve()
    try:
        ensure_archive_dir_safe(state_dir, archive_dir)
    except ValueError as exc:
        raise SystemExit(str(exc)) from None

    # The metadata file can be stale after a crashed process.  The OS owner
    # lock is authoritative; hold it across planning and moves so the
    # all-day factory cannot mutate the same state concurrently.
    lock_path = str(state_dir / "run.lock")
    lock_handle = acquire_os_owner_lock(lock_path)
    if lock_handle is None:
        raise SystemExit("active research owner exists; refusing state archive")

    try:
        summary_moves, checkpoint_moves, protected = plan(
            state_dir, archive_dir, args.keep
        )
        print(
            f"round summaries: {len(summary_moves)} planned, "
            f"checkpoints: {len(checkpoint_moves)} planned, "
            f"protected rounds: {len(protected)}"
        )
        for source, target in summary_moves + checkpoint_moves:
            print(f"  {source} -> {target}")
        if not args.apply:
            print("[dry-run] no files moved")
            return 0
        results = {"moved": 0, "deduplicated": 0}
        for source, target in summary_moves + checkpoint_moves:
            results[_move(source, target)] += 1
        print(f"[applied] {results}")
        return 0
    finally:
        release_os_owner_lock(lock_handle)


if __name__ == "__main__":
    raise SystemExit(main())
