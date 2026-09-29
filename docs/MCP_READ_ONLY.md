# MCP transports

`wqb_agent.mcp_server` adapts the `wqb_agent.research_api` facade to MCP stdio. It owns transport, bounded result projection, tool descriptions, and error presentation; it does not own research policy, BRAIN HTTP paths, credentials, caches, or execution state.

## Read-only transport

Install the optional MCP SDK with `python -m pip install -e ".[mcp]"`, then run `alpha-factory-mcp`. This transport contains read-only discovery, capability, Alpha evidence, and pending-guard reads. It has no Simulation write, template catalog write, metadata write, or Alpha submission path.

Every returned result carries an access mode, owner, source, status, evidence status, freshness, and truncation state. Live evidence is read at call time; local guard evidence is read only from the configured directory. Unavailable data and unknown permissions remain visible. Errors are classified without returning raw HTTP messages, URLs, local paths, or credentials. Alpha expressions are returned only by the explicitly authorized Alpha evidence route.

## Explicit Research Mode

For a Research Agent host, configure the separate `alpha-factory-research-mcp` entrypoint and explicitly set `ALPHA_FACTORY_ENABLE_SIMULATION_WRITES=1`. Without that opt-in, server startup fails with `RESEARCH_WRITE_MODE_NOT_ENABLED`; it does not silently degrade to another tool profile.

`research_tool_manifest(profile="core")` is the machine-readable owner of canonical Research tool names, modes, and owners. MCP registration supplies descriptions and input/output schemas. Contract tests compare the tools actually returned by MCP `tools/list` with the CORE manifest; a connected host must still inspect its current `tools/list` inventory because source code does not prove that a host registered or started the server.

The Research path includes live readiness and discovery, a paged private template inventory, explicit-input `generate_probes`, candidate validation/admission, Simulation through `SimulationGateway`, evidence reads, and read-only reconciliation. `list_templates` uses the same configured private catalog as generation, omits raw template expressions and fixed field bindings, and fails closed when the catalog is absent. `generate_probes` uses only Agent-selected template IDs, fields, and count; it does not rank fields or templates, admit candidates, or submit Simulations. Generated specs retain their field/dataset provenance; the Agent attaches unique proposal IDs, validates candidates, and checks Gateway admission before selecting a write tool.

`research_status` reports bounded readiness/blockers and a pending summary. Call `get_pending_executions` only when a concrete diagnosis needs guard-row detail; it is a local `READ_ONLY` view and never reconciles or changes state. `get_alpha_evidence(alpha_id)` defaults to a light summary; explicitly requested recordsets do not implicitly request PROD correlation. `get_alpha_prod_correlation(alpha_id)` is marked `FINALIST_ONLY` and only used for finalist review.

Simulation inputs contain only spec fields; config, state directory, and credentials stay in the server process. Results are bounded to 48 KiB and may be marked truncated. Batch limits and required proposal identity are expressed in the live tool schemas/descriptions and enforced by transport plus Gateway. Simulation writes have non-idempotent remote-write annotations and remain on the unique `research_api → SimulationGateway → Simulator → WQBClient → BRAIN` path. Template CRUD, direct HTTP, arbitrary Python, and Alpha submission are not exposed.

The server inventory proves only what this process registered. It does not prove that an external Research Agent session is connected to it. When a required tool or the private template catalog is unavailable, report the capability limit; do not substitute shell scripts, internal imports, or stale local guides.
