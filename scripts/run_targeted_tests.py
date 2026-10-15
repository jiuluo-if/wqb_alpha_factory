"""Run only tests related to the files changed in the current CI revision.

This module deliberately has no unittest discovery fallback.  A new production
module must be added to ``DIRECT_TESTS`` before it can pass CI.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

COMMON_CONTRACT_TESTS = (
    "tests/test_simulation_gateway.py",
    "tests/test_architecture_contracts.py",
    "tests/test_targeted_ci.py",
)

DIRECT_TESTS = {
    "AGENTS.md": COMMON_CONTRACT_TESTS,
    "docs/TESTING.md": (
        "tests/test_architecture_contracts.py",
        "tests/test_targeted_ci.py",
    ),
    ".github/workflows/ci.yml": (
        "tests/test_architecture_contracts.py",
        "tests/test_targeted_ci.py",
    ),
    "constraints/ci-py311.txt": ("tests/test_targeted_ci.py",),
    "main.py": ("tests/test_cli.py",),
    "scripts/run_targeted_tests.py": ("tests/test_targeted_ci.py",),
    "scripts/check_repo_privacy.py": ("tests/test_repo_privacy.py",),
    "scripts/run_research_agent_eval.py": ("tests/test_research_agent_eval.py",),
    "scripts/run_cold_start_eval.py": ("tests/test_research_agent_eval.py",),
    "wqb_agent/client.py": (
        "tests/test_client_refactor.py",
        "tests/test_protocol_truth.py",
    ),
    "wqb_agent/client_transport.py": (
        "tests/test_client_refactor.py",
        "tests/test_protocol_truth.py",
    ),
    "wqb_agent/protocol.py": ("tests/test_protocol_truth.py",),
    "wqb_agent/query_errors.py": ("tests/test_client_refactor.py",),
    "wqb_agent/alpha_semantics.py": (
        "tests/test_alpha_semantics.py",
        "tests/test_live_field_semantic_precedence.py",
        "tests/test_alpha_operating_income_relationship.py",
        "tests/test_event_lookback_frequency.py",
    ),
    "wqb_agent/AGENTS.md": COMMON_CONTRACT_TESTS,
    "wqb_agent/alpha_relationships.py": (
        "tests/test_alpha_semantics.py",
        "tests/test_event_lookback_frequency.py",
        "tests/test_alpha_operating_income_relationship.py",
    ),
    "wqb_agent/alpha_quality.py": ("tests/test_aqaf.py",),
    "wqb_agent/osmosis_allocation.py": ("tests/test_aqaf.py",),
    "tests/test_research_agent_eval.py": ("tests/test_research_agent_eval.py",),
    "tests/fixtures/research_agent_eval/cases.json": (
        "tests/test_research_agent_eval.py",
    ),
    "tests/fixtures/research_agent_eval/cold_start.json": (
        "tests/test_research_agent_eval.py",
    ),
    "tests/fixtures/research_agent_eval/cold_start_candidate.md": (
        "tests/test_research_agent_eval.py",
    ),
    "wqb_agent/field_metadata.py": ("tests/test_research_api.py",),
    "wqb_agent/__init__.py": (
        "tests/test_research_api.py",
    ),
    "wqb_agent/reference/__init__.py": (
        "tests/test_research_api.py",
    ),
    "wqb_agent/audit.py": (
        "tests/test_remote_diagnostics.py",
        "tests/test_cli.py",
    ),
    "wqb_agent/artifacts.py": (
        "tests/test_artifacts.py",
    ),
    "wqb_agent/simulator.py": ("tests/test_simulation_gateway.py",),
    "wqb_agent/config.py": ("tests/test_remote_diagnostics.py",),
    "wqb_agent/doctor.py": (
        "tests/test_remote_diagnostics.py",
    ),
    "wqb_agent/operator_reference.py": ("tests/test_protocol_truth.py",),
    "wqb_agent/alpha_factory.py": (
        "tests/test_research_api.py",
        "tests/test_alpha_semantics.py",
        "tests/test_alpha_multi_field_confirmation.py",
        "tests/test_alpha_operating_income_relationship.py",
    ),
    "wqb_agent/alpha_feed_cache.py": ("tests/test_remote_alpha.py",),
    "wqb_agent/alpha_templates/model.py": ("tests/test_alpha_template_catalog.py",),
    "wqb_agent/alpha_templates/loader.py": (
        "tests/test_alpha_template_catalog.py",
    ),
    "wqb_agent/alpha_templates/validation.py": (
        "tests/test_alpha_template_catalog.py",
    ),
    "wqb_agent/alpha_templates/registry.py": (
        "tests/test_alpha_template_catalog.py",
    ),
    "wqb_agent/alpha_templates/__init__.py": ("tests/test_alpha_template_catalog.py",),
    "wqb_agent/alpha_templates/catalog/builtin.toml": (
        "tests/test_alpha_template_catalog.py",
    ),
    "wqb_agent/alpha_templates/AGENTS.md": ("tests/test_alpha_template_catalog.py",),
    "wqb_agent/locking.py": (
        "tests/test_locking.py",
    ),
    "wqb_agent/research_api.py": (
        "tests/test_research_api.py",
        "tests/test_alpha_grouping.py",
        "tests/test_remote_alpha.py",
        "tests/test_research_correlation_contract.py",
        "tests/test_aqaf.py",
        "tests/test_mcp_poll_budget.py",
    ),
    "wqb_agent/mcp_server.py": (
        "tests/test_mcp_server.py",
        "tests/test_mcp_poll_budget.py",
        "tests/test_mcp_research_inventory_projection.py",
    ),
    "wqb_agent/remote_quota.py": ("tests/test_remote_quota.py",),
    "wqb_agent/credentials.py": (
        "tests/test_credentials.py",
        "tests/test_remote_diagnostics.py",
    ),
    "wqb_agent/cli.py": ("tests/test_cli.py",),
    "wqb_agent/diagnostics.py": ("tests/test_remote_diagnostics.py",),
    "wqb_agent/evidence_status.py": ("tests/test_protocol_truth.py",),
    "wqb_agent/failures.py": ("tests/test_client_refactor.py",),
    "wqb_agent/expression.py": ("tests/test_expression.py",),
    "wqb_agent/remote_evidence.py": (
        "tests/test_remote_evidence.py",
        "tests/test_aqaf.py",
        "tests/test_research_correlation_contract.py",
    ),
    "wqb_agent/simulation_gateway.py": (
        "tests/test_simulation_gateway.py",
        "tests/test_multi_parent_capacity.py",
    ),
    "wqb_agent/remote_alpha_repository.py": ("tests/test_remote_alpha.py",),
    "wqb_agent/remote_colors.py": ("tests/test_remote_alpha.py",),
    "wqb_agent/alpha_grouping.py": ("tests/test_alpha_grouping.py",),
    "wqb_agent/smoke.py": ("tests/test_cli.py",),
}

FRONTEND_CONFIG_FILES = {
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
}

FRONTEND_CONFIG_TESTS = (
    "tests/test_remote_diagnostics.py",
    "tests/test_targeted_ci.py",
)

NON_CODE_PREFIXES = (
    ".planning/",
    ".github/",
    "docs/",
    "prompts/",
)


class TargetSelectionError(RuntimeError):
    """Raised when a changed code file has no explicit test route."""


def _active_production_files() -> set[str]:
    """Return source boundaries that must participate in explicit routing."""
    candidates = [ROOT / "main.py"]
    candidates.extend(sorted((ROOT / "scripts").rglob("*.py")))
    candidates.extend(sorted((ROOT / "wqb_agent").rglob("*.py")))
    return {
        path.relative_to(ROOT).as_posix()
        for path in candidates
        if path.is_file()
    }


def _unittest_test_files(tests_root: Path) -> set[str]:
    """Return test modules unittest discovery can import from this root."""
    tests_root = Path(tests_root)
    discovered = set()
    for path in tests_root.rglob("test_*.py"):
        parent = path.parent
        importable = True
        while parent != tests_root:
            if not (parent / "__init__.py").is_file():
                importable = False
                break
            parent = parent.parent
        if importable:
            discovered.add(path.relative_to(tests_root).as_posix())
    return discovered


def mapping_inventory() -> dict[str, object]:
    """Build a small, deterministic graph audit for the routing contract."""
    tests_root = ROOT / "tests"
    all_test_files = {
        path.relative_to(ROOT).as_posix()
        for path in tests_root.rglob("test_*.py")
        if path.is_file()
    }
    actual_tests = {
        "tests/" + relative_path
        for relative_path in _unittest_test_files(tests_root)
    }
    mapped_tests = {
        test for tests in DIRECT_TESTS.values() for test in tests
    } | set(FRONTEND_CONFIG_TESTS) | set(COMMON_CONTRACT_TESTS)
    stale_mapping_keys = {
        key for key in DIRECT_TESTS
        if not (ROOT / key).is_file()
    }
    duplicate_keys = len(DIRECT_TESTS) != len(set(DIRECT_TESTS))
    production = _active_production_files()
    mapped_production = production & DIRECT_TESTS.keys()
    return {
        "production_files": production,
        "mapped_production_files": mapped_production,
        "unmapped_production_files": production - mapped_production,
        "mapped_missing_tests": mapped_tests - actual_tests,
        "unreachable_tests": actual_tests - mapped_tests,
        "undiscoverable_tests": all_test_files - actual_tests,
        "duplicate_keys": duplicate_keys,
        "stale_mapping_keys": stale_mapping_keys,
        "mapping_entries": len(DIRECT_TESTS),
    }


def validate_mapping_integrity() -> dict[str, object]:
    """Fail closed on stale routes or active code without a route."""
    inventory = mapping_inventory()
    undiscoverable = sorted(inventory["undiscoverable_tests"])
    if undiscoverable:
        raise TargetSelectionError(
            "Test file(s) cannot be imported by unittest discover: "
            + ", ".join(undiscoverable)
        )
    if inventory["duplicate_keys"]:
        raise TargetSelectionError("DIRECT_TESTS contains duplicate normalized keys")
    stale = sorted(inventory["stale_mapping_keys"])
    if stale:
        raise TargetSelectionError(
            "DIRECT_TESTS contains missing production path(s): " + ", ".join(stale)
        )
    missing = sorted(inventory["mapped_missing_tests"])
    if missing:
        raise TargetSelectionError("Mapped targeted test file(s) do not exist: " + ", ".join(missing))
    unmapped = sorted(inventory["unmapped_production_files"])
    if unmapped:
        raise TargetSelectionError(
            "Active production file(s) have no explicit targeted-test route: "
            + ", ".join(unmapped)
        )
    unreachable = sorted(inventory["unreachable_tests"])
    if unreachable:
        raise TargetSelectionError(
            "Active test file(s) have no explicit production or common-contract route: "
            + ", ".join(unreachable)
        )
    return inventory


def _normalize(path: str) -> str:
    normalized = path.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized.lstrip("/")


def _existing(paths: set[str]) -> tuple[str, ...]:
    missing = sorted(path for path in paths if not (ROOT / path).is_file())
    if missing:
        raise TargetSelectionError(
            "Mapped targeted test file(s) do not exist: " + ", ".join(missing)
        )
    return tuple(sorted(paths))


def select_tests(changed_files: list[str]) -> tuple[str, ...]:
    """Return explicit test files for changed files, or fail closed."""
    validate_mapping_integrity()
    selected: set[str] = set()
    unmapped: list[str] = []
    production_change = False
    production_files = _active_production_files()

    for raw_path in changed_files:
        path = _normalize(raw_path)
        if not path:
            continue
        if path.startswith("tests/") and path.endswith(".py"):
            # A deleted test is itself the intentional cleanup; do not try
            # to import a path that no longer exists in the checkout.
            if (ROOT / path).is_file():
                selected.add(path)
            continue
        if path in DIRECT_TESTS:
            selected.update(DIRECT_TESTS[path])
            production_change = production_change or path in production_files
            continue
        if path in FRONTEND_CONFIG_FILES:
            selected.update(FRONTEND_CONFIG_TESTS)
            continue
        if path == "AGENTS.md" or path.endswith("/AGENTS.md"):
            continue
        if path.startswith(NON_CODE_PREFIXES):
            continue
        if path.endswith(".md") or path.endswith(".txt"):
            continue
        if not (ROOT / path).is_file():
            deleted = subprocess.run(
                ["git", "cat-file", "-e", f"HEAD:{path}"],
                cwd=ROOT,
                capture_output=True,
            ).returncode == 0
            if deleted:
                continue
        if path.startswith("wqb_agent/") or path.endswith(".py"):
            unmapped.append(path)

    if unmapped:
        raise TargetSelectionError(
            "No explicit targeted-test route for changed code file(s): "
            + ", ".join(sorted(unmapped))
            + ". Add the file to DIRECT_TESTS; full-suite fallback is forbidden."
        )
    if production_change:
        selected.update(COMMON_CONTRACT_TESTS)
    return _existing(selected)


def changed_files(base_sha: str | None) -> list[str]:
    if base_sha:
        command = ["git", "diff", "--name-status", f"{base_sha}...HEAD"]
    else:
        command = ["git", "diff", "--name-status", "HEAD^", "HEAD"]
    result = subprocess.run(
        command,
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    changed = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        fields = line.split("\t")
        status = fields[0]
        if status.startswith("D"):
            continue
        # Rename/copy records contain old and new paths; route the new path.
        changed.append(fields[-1])
    return changed


def _test_modules(test_files: tuple[str, ...]) -> list[str]:
    return [path[:-3].replace("/", ".") for path in test_files]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run only explicitly mapped tests for changed files."
    )
    parser.add_argument("--base-sha", help="Git revision to diff against")
    parser.add_argument(
        "--files",
        nargs="*",
        help="Changed files for local verification; bypasses Git diff discovery",
    )
    parser.add_argument("--list-only", action="store_true")
    args = parser.parse_args(argv)

    files = args.files if args.files is not None else changed_files(args.base_sha)
    tests = select_tests(files)
    if not tests:
        print("No code/test files changed; targeted unit tests skipped.")
        return 0
    print("Targeted tests (explicit mapping only):")
    for test in tests:
        print(f"  {test}")
    if args.list_only:
        return 0
    return subprocess.run(
        [sys.executable, "-m", "unittest", *_test_modules(tests)],
        cwd=ROOT,
        check=False,
    ).returncode


if __name__ == "__main__":
    raise SystemExit(main())
