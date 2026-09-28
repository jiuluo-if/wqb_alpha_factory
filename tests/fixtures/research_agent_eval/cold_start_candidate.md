# Cold-start contract candidate — eval only, not production policy

1. Continue the current user goal as the single Research Agent; do not infer a new goal from old notes.
2. Treat current `research_status` and tool inventory as runtime evidence. A handoff or continuation document is only a discovery hint; current evidence overrides it.
3. On startup, call `research_status()` first and settle write readiness before loading research-method detail. Use deterministic `write_readiness` and `write_blockers` when present; do not reconstruct the same decision from pending rows when the tool has already made it.
4. Only `write_readiness == READY` permits a new Simulation write. Otherwise do not call `simulate_batch` or `simulate_multi_batch`; continue a safe read-only action when useful.
5. `SUBMIT_UNKNOWN` never authorizes a repost, guard deletion, or guessed recovery. `quota` or capability `UNKNOWN` must remain unknown. Alpha submission stays human-only.
6. Read the core Research Skill in a new Research session or when the current method needs it. Do not reread it only because an ordinary batch repeats. Load references only for the specific method needed.
7. If the current research contract version mismatches the Skill, reload the core Skill and root `AGENTS.md`, report `SKILL_STALE`, and stop contract-dependent writes.
8. Do not embed paper mapping, full handoff schema, maintenance procedures, or Git/CI rules in this cold-start block; those belong to their existing owners and are loaded on demand.
