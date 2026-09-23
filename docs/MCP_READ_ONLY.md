# Read-only MCP transport

`wqb_agent.mcp_server` adapts selected `wqb_agent.research_api` functions to
MCP stdio. It owns no research logic, BRAIN HTTP path, cache, credentials, or
execution state. Install the optional SDK with `python -m pip install -e
".[mcp]"`, then run `alpha-factory-mcp` from the project directory.

All five exposed tools are marked `READ_ONLY` in their description, result,
and MCP `readOnlyHint` annotation:

| Tool | Facade owner | Reads | Bounds |
|---|---|---|---|
| `get_capabilities` | `research_api.get_capabilities` | Live operator capability | Common 48 KiB result cap |
| `get_simulation_modes` | `research_api.get_simulation_modes` | Authentication and advertised permissions | Unknown permission remains unknown |
| `list_datafields` | `research_api.list_datafields` | One live field page | Request limit 1–50; output at most 20 rows |
| `get_alpha_evidence` | `research_api.get_alpha_evidence` | One live Alpha and explicitly named recordsets | At most two recordsets; common result cap |
| `get_pending_executions` | `research_api.get_pending_executions` | Configured local ExecutionGuard | Common result cap; no reconciliation or state change |

Every result includes `access_mode`, `owner`, `source`, `status`,
`evidence_status`, `freshness`, optional `fetched_at`/`age_sec`, and a
`truncated` flag. Sources are passed through from the facade. Live data is
marked `READ_AT_CALL`; local guard data is marked `LOCAL_STATE_AT_CALL`.
Unavailable data and unknown permissions stay visible. Authentication,
permission, missing-evidence, rate-limit, and other read failures return a
short classified error; raw HTTP messages are not returned. Credential-like
keys are redacted. Expression text may appear only in the explicitly
authorized Alpha evidence result.

The MCP server exposes no Simulation, cache-write, template-write, color-sync,
or Alpha-submission tool. Existing research execution continues to use
`research_api → SimulationGateway → Simulator → WQBClient`; a host wanting
Simulation must use the existing Agent facade outside this read-only server and
preserve its guard and reconciliation contract. Alpha submission remains
manual.

The current v2 worktree contains unresolved local guard entries. The MCP server
only reads them; it does not poll known progress URLs, resume a task, rewrite a
guard, or retry an ambiguous POST.
