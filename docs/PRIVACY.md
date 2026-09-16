# Repository privacy

## Public repository

Tracked files may contain stable code, synthetic fixtures, evergreen contracts,
sanitized platform references and minimal operational documentation. Public
examples must use placeholders such as `field_a`, `field_b`, `signal_x`, `X`,
`Y` and `GROUP`.

## Local private research

Real Alpha expressions, field IDs and pairings, metrics, Simulation evidence,
trajectory, ExperienceMemory, PnL, reports, plans, findings and generated
exports are local-only. Keep them under ignored `.wqb_state/`, `research_data/`,
`reports/` or `.planning/` paths owned by the existing runtime.

The retired `.wqb_state/factory_session.json`, proposals inbox and research
history files are not part of the new local model and must not be created by new
code. Only `execution_guard.json`, rebuildable remote metadata cache, external
credentials references and process locks are allowed for the Remote-First path.

## Documentation and commit hygiene

Do not put research history, dated round reports, private paths, credentials or
platform identifiers in tracked docs, prompts, tests, logs or commit messages.
Prefer editing the canonical owner and deleting obsolete historical artifacts;
do not create versioned copies or tracked archives.

## Automated enforcement

Run `python scripts/check_repo_privacy.py` before delivery. It scans
`git ls-files`, so ignored local research remains available without becoming a
public artifact. Run `git diff --check` and inspect staged changes separately.

## Git history limitation

Current-tree cleanup does not purge previously published Git history. This
repository uses `HISTORY_REWRITE = NO`; past history remains an archive and
must not be represented as current public research evidence.
