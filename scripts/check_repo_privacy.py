"""Research-data privacy guard for this public repository.

``git ls-files`` is the source of truth, so a local-only directory such as
``research_data/`` never blocks the check even though it holds raw exports. The
guard targets research-data privacy (machine-specific paths and private research
artifacts); it complements, and does not replace, credential hygiene.

Tracked text is scanned by streaming it line by line, so a large tracked file
cannot bypass the scan.  Binaries are detected from their leading bytes and
reported as ``skipped_binary``; a tracked file that cannot be read becomes a
finding so the check fails closed instead of exiting clean.

Usage:
    python scripts/check_repo_privacy.py
    python scripts/check_repo_privacy.py --json
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

BINARY_PROBE_BYTES = 4096

PATH_RULES: tuple[tuple[str, re.Pattern[str], str], ...] = (
    ("RAW_AUDIT_ARTIFACT", re.compile(r"(^|/)audit\.json$"), "raw audit JSON must stay local"),
    (
        "RAW_AUDIT_ARTIFACT",
        re.compile(r"(^|/)research_quality_audit[^/]*/"),
        "raw research audit export",
    ),
    ("RAW_RESEARCH_EXPORT", re.compile(r"(^|/)trajectory\.jsonl$"), "raw trajectory export"),
    ("RAW_RESEARCH_EXPORT", re.compile(r"(^|/)\.wqb_state/"), "local research state tree"),
    ("RAW_RESEARCH_EXPORT", re.compile(r"(^|/)\.alpha_feed_cache/"), "local alpha feed cache"),
)

CONTENT_RULES: tuple[tuple[str, re.Pattern[str], str], ...] = (
    (
        "ABSOLUTE_LOCAL_PATH",
        re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/]"),
        "machine-specific drive path",
    ),
    (
        "ABSOLUTE_HOME_PATH",
        re.compile(r"(?<![A-Za-z0-9._-])/(?:home|Users)/[A-Za-z0-9._-]+"),
        "home directory path",
    ),
    (
        "PRIVATE_ATTACHMENT_PATH",
        re.compile(r"attachments[\\/][0-9a-fA-F]{8}-"),
        "private attachment path",
    ),
    (
        "ALPHA_IDENTIFIER",
        re.compile(r"\balpha-\d{6,}\b"),
        "platform Alpha identifier",
    ),
    (
        "SUBMISSION_IDENTIFIER",
        re.compile(r"\bp-[0-9a-f]{8,}\b"),
        "proposal or submission identifier",
    ),
    (
        "SIMULATION_PROGRESS_URL",
        re.compile(r"api\.worldquantbrain\.com/simulations/[0-9a-f]{8,}"),
        "remote simulation progress url",
    ),
    (
        "HARDCODED_CREDENTIAL",
        re.compile(
            r"(?i)\b(?:password|passwd|secret|api[_-]?key|access[_-]?token|client[_-]?secret)"
            r"\b\s*[:=]\s*\"([^\"\n]{8,})\""
        ),
        "credential-like literal assignment",
    ),
)

OPERATOR_REFERENCE_HISTORY_RULES: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)\b(?:trajectory|campaign|round[_ -]?\d+|historical|usage\s+count|real\s+simulation)\b"),
    re.compile(r"(?i)\b(?:sharpe|fitness|turnover)\s*[:=]\s*\d"),
    re.compile(r"(?:甜点|充分探索|历史失败样本|使用状态|可探索)"),
)

_PLACEHOLDER_MARKERS = (
    "os.environ",
    "getenv",
    "environ[",
    "redacted",
    "example",
    "changeme",
    "placeholder",
    "dummy",
    "xxxx",
    "<",
    "$",
    "{",
    "...",
)


@dataclass(frozen=True)
class Finding:
    code: str
    path: str
    line: int | None
    detail: str
    excerpt: str

    def as_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "path": self.path,
            "line": self.line,
            "detail": self.detail,
            "excerpt": self.excerpt,
        }


def repo_root(start: Path | None = None) -> Path:
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=str(start) if start else None,
        capture_output=True,
        text=True,
        check=True,
    )
    return Path(result.stdout.strip())


def tracked_files(root: Path) -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=str(root),
        capture_output=True,
        text=True,
        check=True,
    )
    return [name for name in result.stdout.split("\x00") if name]


def _scan_line(relative: str, number: int, line: str) -> list[Finding]:
    findings: list[Finding] = []
    for code, pattern, detail in CONTENT_RULES:
        match = pattern.search(line)
        if match is None:
            continue
        if code == "HARDCODED_CREDENTIAL":
            value = match.group(1)
            lowered = value.lower()
            if any(marker in lowered for marker in _PLACEHOLDER_MARKERS):
                continue
            if len(value) < 12:
                continue
            if not (re.search(r"[0-9]", value) and re.search(r"[A-Za-z]", value)):
                continue
        findings.append(
            Finding(
                code=code,
                path=relative,
                line=number,
                detail=detail,
                excerpt=match.group(0)[:120],
            )
        )
    if relative.replace("\\", "/") == "docs/reference/OPERATORS_CHEATSHEET.md":
        for pattern in OPERATOR_REFERENCE_HISTORY_RULES:
            if pattern.search(line):
                findings.append(
                    Finding(
                        code="OPERATOR_REFERENCE_RESEARCH_HISTORY",
                        path=relative,
                        line=number,
                        detail="tracked operator reference must contain syntax only",
                        excerpt=line[:120],
                    )
                )
                break
    return findings


def _scan_text(relative: str, text: str) -> list[Finding]:
    findings: list[Finding] = []
    for number, line in enumerate(text.splitlines(), 1):
        findings.extend(_scan_line(relative, number, line))
    return findings


def _scan_stream(relative: str, handle) -> list[Finding]:
    """Scan one tracked text file line by line without loading it whole."""
    findings: list[Finding] = []
    for number, raw in enumerate(handle, 1):
        line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
        findings.extend(_scan_line(relative, number, line))
    return findings


def scan(root: Path, files: Iterable[str]) -> tuple[list[Finding], list[str]]:
    findings: list[Finding] = []
    skipped_binary: list[str] = []
    for relative in files:
        for code, pattern, detail in PATH_RULES:
            if pattern.search(relative):
                findings.append(
                    Finding(code=code, path=relative, line=None, detail=detail, excerpt=relative)
                )
        target = root / relative
        if not target.is_file():
            continue
        try:
            with target.open("rb") as handle:
                if b"\x00" in handle.read(BINARY_PROBE_BYTES):
                    skipped_binary.append(relative)
                    continue
                handle.seek(0)
                findings.extend(_scan_stream(relative, handle))
        except OSError as error:
            findings.append(
                Finding(
                    code="UNREADABLE_TRACKED_FILE",
                    path=relative,
                    line=None,
                    detail="tracked file could not be read; the privacy scan fails closed",
                    excerpt=str(error)[:120],
                )
            )
    return findings, skipped_binary


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit machine-readable findings")
    parser.add_argument("--root", default=None, help="repository root (defaults to git toplevel)")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve() if args.root else repo_root()
    files = tracked_files(root)
    findings, skipped_binary = scan(root, files)

    if args.json:
        print(
            json.dumps(
                {
                    "tracked_files": len(files),
                    "findings": [item.as_dict() for item in findings],
                    "skipped_binary": skipped_binary,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        for item in findings:
            location = f"{item.path}:{item.line}" if item.line else item.path
            print(f"{item.code}  {location}  {item.detail}  [{item.excerpt}]")
        print(
            f"tracked_files={len(files)} findings={len(findings)} "
            f"skipped_binary={len(skipped_binary)}"
        )
        for name in skipped_binary:
            print(f"skipped_binary={name}")

    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
