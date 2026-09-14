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
    "tests/test_runtime_safety.py",
    "tests/test_agent_flow.py",
    "tests/test_security_hardening_batch.py",
)

DIRECT_TESTS = {
    "main.py": ("tests/test_cli.py",),
    "scripts/run_targeted_tests.py": ("tests/test_targeted_ci.py",),
    "scripts/check_repo_privacy.py": ("tests/test_repo_privacy.py",),
    "scripts/archive_completed_rounds.py": (
        "tests/test_architecture.py",
        "tests/test_canonical_ops.py",
    ),
    "scripts/reconcile_pending.py": (
        "tests/test_recovery.py",
        "tests/test_proposal_safety.py",
        "tests/test_canonical_ops.py",
    ),
    "wqb_agent/client.py": (
        "tests/test_client_refactor.py",
        "tests/test_protocol_truth.py",
        "tests/test_security_hardening_batch.py",
    ),
    "wqb_agent/protocol.py": ("tests/test_protocol_truth.py",),
    "wqb_agent/query_errors.py": ("tests/test_alpha_feed_workflow.py", "tests/test_client_refactor.py"),
    "wqb_agent/alpha_feed_workflow.py": (
        "tests/test_alpha_feed_workflow.py",
        "tests/test_runtime_composition.py",
        "tests/test_runtime_safety.py",
    ),
    "wqb_agent/research_planning.py": (
        "tests/test_research_planning.py",
        "tests/test_agent_flow.py",
    ),
    "wqb_agent/evidence_projection.py": (
        "tests/test_evidence_projection.py",
        "tests/test_agent_evaluation.py",
        "tests/test_runtime_settlement.py",
    ),
    "wqb_agent/alpha_semantics.py": (
        "tests/test_alpha_semantics.py",
        "tests/test_factory_semantic_traits.py",
        "tests/test_factory_relationship_gate.py",
    ),
    "wqb_agent/alpha_relationships.py": (
        "tests/test_alpha_relationships.py",
        "tests/test_factory_relationship_gate.py",
        "tests/test_factory_mechanism_selection.py",
    ),
    "wqb_agent/alpha_assembly.py": (
        "tests/test_alpha_assembly.py",
        "tests/test_factory_mechanism_selection.py",
    ),
    "wqb_agent/alpha_feasibility.py": (
        "tests/test_alpha_feasibility.py",
        "tests/test_factory_feasibility.py",
    ),
    "wqb_agent/runtime_composition.py": (
        "tests/test_runtime_composition.py",
        "tests/test_architecture_contracts.py",
    ),
    "wqb_agent/proposal_admission.py": (
        "tests/test_execution_identity.py",
        "tests/test_proposal_execution.py",
        "tests/test_proposal_safety.py",
    ),
    "wqb_agent/execution_identity.py": (
        "tests/test_execution_identity.py",
        "tests/test_proposal_execution.py",
        "tests/test_proposal_safety.py",
    ),
    "wqb_agent/factory_quota.py": (
        "tests/test_factory_quota.py",
        "tests/test_factory_boundaries.py",
        "tests/test_research_loop.py",
    ),
    "wqb_agent/optimization_screening.py": (
        "tests/test_agent_decision_to_proposal.py",
        "tests/test_factory_provenance_persistence.py",
    ),
    "wqb_agent/validation_proposals.py": (
        "tests/test_agent_decision_to_proposal.py",
        "tests/test_optimization_decision_contract.py",
    ),
    "wqb_agent/factory_session.py": (
        "tests/test_factory_session.py",
        "tests/test_factory_session_privacy.py",
        "tests/test_factory_boundaries.py",
    ),
    "wqb_agent/discovery.py": (
        "tests/test_discovery.py",
        "tests/test_discovery_selection.py",
        "tests/test_discovery_semantics.py",
    ),
    "wqb_agent/__init__.py": (
        "tests/test_runtime_composition.py",
        "tests/test_research_api.py",
    ),
    "wqb_agent/reference/__init__.py": (
        "tests/test_security_hardening_batch.py",
    ),
    "wqb_agent/agent.py": (
        "tests/test_agent_flow.py",
        "tests/test_proposal_execution.py",
        "tests/test_runtime_composition.py",
        "tests/test_runtime_safety.py",
        "tests/test_security_hardening_batch.py",
    ),
    "wqb_agent/checkpoints.py": (
        "tests/test_checkpoint_store.py",
        "tests/test_recovery.py",
        "tests/test_canonical_ops.py",
    ),
    "wqb_agent/audit.py": (
        "tests/test_runtime_safety.py",
        "tests/test_workspace_snapshot.py",
        "tests/test_research_constraints.py",
        "tests/test_cli.py",
        "tests/test_canonical_ops.py",
    ),
    "wqb_agent/workspace_snapshot.py": (
        "tests/test_runtime_safety.py",
        "tests/test_workspace_snapshot.py",
        "tests/test_research_constraints.py",
        "tests/test_canonical_ops.py",
    ),
    "wqb_agent/state.py": (
        "tests/test_state.py",
        "tests/test_recovery.py",
        "tests/test_settled_evidence_durability.py",
        "tests/test_trajectory_batch_reads.py",
        "tests/test_factory_provenance_persistence.py",
        "tests/test_security_hardening_batch.py",
    ),
    "wqb_agent/artifacts.py": (
        "tests/test_artifacts.py",
        "tests/test_security_hardening_batch.py",
    ),
    "wqb_agent/validation_report.py": (
        "tests/test_validation_report.py",
        "tests/test_evaluation.py",
        "tests/test_security_hardening_batch.py",
    ),
    "wqb_agent/memory.py": (
        "tests/test_memory_tiers.py",
        "tests/test_agent_evaluation.py",
        "tests/test_replay_idempotency.py",
    ),
    "wqb_agent/reflection.py": (
        "tests/test_memory_tiers.py",
        "tests/test_agent_evaluation.py",
        "tests/test_replay_idempotency.py",
    ),
    "wqb_agent/proposal_execution.py": (
        "tests/test_proposal_execution.py",
        "tests/test_proposal_safety.py",
        "tests/test_recovery.py",
        "tests/test_settled_evidence_durability.py",
        "tests/test_simulator.py",
        "tests/test_runtime_safety.py",
        "tests/test_search_policy.py",
    ),
    "wqb_agent/terminal_evidence.py": (
        "tests/test_execution_projections.py",
        "tests/test_proposal_execution.py",
        "tests/test_recovery.py",
    ),
    "wqb_agent/execution_recovery.py": (
        "tests/test_execution_projections.py",
        "tests/test_recovery.py",
        "tests/test_proposal_execution.py",
    ),
    "wqb_agent/simulator.py": (
        "tests/test_simulator.py",
        "tests/test_proposal_execution.py",
        "tests/test_recovery.py",
        "tests/test_runtime_safety.py",
    ),
    "wqb_agent/factory_runner.py": (
        "tests/test_factory_boundaries.py",
        "tests/test_factory_blocker_control.py",
        "tests/test_factory_feasibility.py",
        "tests/test_proposal_execution.py",
        "tests/test_research_loop.py",
    ),
    "wqb_agent/factory_route.py": (
        "tests/test_factory_route.py",
        "tests/test_factory_boundaries.py",
        "tests/test_research_loop.py",
    ),
    "wqb_agent/factory_blocker.py": (
        "tests/test_factory_blocker_control.py",
        "tests/test_factory_session_privacy.py",
    ),
    "wqb_agent/weekly_quota.py": (
        "tests/test_factory_boundaries.py",
    ),
    "wqb_agent/config.py": (
        "tests/test_runtime_config_boundary.py",
        "tests/test_runtime_safety.py",
    ),
    "wqb_agent/doctor.py": (
        "tests/test_runtime_safety.py",
        "tests/test_runtime_config_boundary.py",
    ),
    "wqb_agent/runtime_policy.py": (
        "tests/test_runtime_config_boundary.py",
        "tests/test_runtime_composition.py",
        "tests/test_runtime_safety.py",
    ),
    "wqb_agent/proposal_contract.py": (
        "tests/test_factory_batch_contract.py",
        "tests/test_factory_provenance_persistence.py",
        "tests/test_targeted_batch_contract.py",
        "tests/test_security_hardening_batch.py",
    ),
    "wqb_agent/optimizer_workflow.py": (
        "tests/test_optimizer_workflow.py",
        "tests/test_agent_decision_to_proposal.py",
        "tests/test_proposal_safety.py",
    ),
    "wqb_agent/alpha_factory.py": (
        "tests/test_factory_batch_contract.py",
        "tests/test_factory_mechanism_selection.py",
        "tests/test_factory_relationship_gate.py",
        "tests/test_factory_semantic_traits.py",
        "tests/test_factory_provenance_persistence.py",
        "tests/test_factory_feasibility.py",
        "tests/test_research_loop.py",
    ),
    "wqb_agent/diversity.py": (
        "tests/test_factory_mechanism_selection.py",
        "tests/test_research_loop.py",
    ),
    "wqb_agent/search_policy.py": (
        "tests/test_search_policy.py",
        "tests/test_search_calibration.py",
        "tests/test_recovery.py",
    ),
    "wqb_agent/alpha_templates/model.py": ("tests/test_alpha_template_catalog.py",),
    "wqb_agent/alpha_templates/loader.py": (
        "tests/test_alpha_template_catalog.py",
        "tests/test_private_template_contract.py",
    ),
    "wqb_agent/alpha_templates/validation.py": (
        "tests/test_alpha_template_catalog.py",
        "tests/test_private_template_contract.py",
    ),
    "wqb_agent/alpha_templates/registry.py": (
        "tests/test_alpha_template_catalog.py",
        "tests/test_private_template_contract.py",
    ),
    "wqb_agent/alpha_templates/__init__.py": ("tests/test_alpha_template_catalog.py",),
    "wqb_agent/alpha_templates/catalog/builtin.toml": (
        "tests/test_alpha_template_catalog.py",
        "tests/test_private_template_contract.py",
    ),
    "wqb_agent/alpha_templates/AGENTS.md": ("tests/test_alpha_template_catalog.py",),
    "wqb_agent/locking.py": (
        "tests/test_locking.py",
        "tests/test_proposal_execution.py",
    ),
    "wqb_agent/preflight.py": (
        "tests/test_research_constraints.py",
        "tests/test_runtime_safety.py",
        "tests/test_agent_context.py",
    ),
    "wqb_agent/research_api.py": (
        "tests/test_research_api.py",
        "tests/test_proposal_safety.py",
        "tests/test_targeted_batch_contract.py",
    ),
    "wqb_agent/runtime_components.py": (
        "tests/test_runtime_composition.py",
        "tests/test_runtime_safety.py",
    ),
    "wqb_agent/trial_ledger.py": (
        "tests/test_incremental_value.py",
        "tests/test_optimization_selection_accounting.py",
        "tests/test_protocol_truth.py",
        "tests/test_proposal_safety.py",
        "tests/test_recovery.py",
        "tests/test_runtime_safety.py",
        "tests/test_search_calibration.py",
        "tests/test_trial_ledger_io.py",
    ),
    "scripts/benchmark_local_io.py": ("tests/test_benchmark_harness.py",),
}

FRONTEND_CONFIG_FILES = {
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
}

NON_CODE_PREFIXES = (
    ".planning/",
    ".github/",
    "docs/",
    "prompts/",
)


class TargetSelectionError(RuntimeError):
    """Raised when a changed code file has no explicit test route."""


def _normalize(path: str) -> str:
    return path.replace("\\", "/").lstrip("./")


def _existing(paths: set[str]) -> tuple[str, ...]:
    missing = sorted(path for path in paths if not (ROOT / path).is_file())
    if missing:
        raise TargetSelectionError(
            "Mapped targeted test file(s) do not exist: " + ", ".join(missing)
        )
    return tuple(sorted(paths))


def select_tests(changed_files: list[str]) -> tuple[str, ...]:
    """Return explicit test files for changed files, or fail closed."""
    selected: set[str] = set()
    unmapped: list[str] = []

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
            continue
        if path in FRONTEND_CONFIG_FILES:
            selected.update(COMMON_CONTRACT_TESTS)
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
        status, _, path = line.partition("\t")
        if status.startswith("D"):
            continue
        changed.append(path)
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
