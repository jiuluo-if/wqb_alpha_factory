# Research API migration

`wqb_agent.research_api` is the only Agent-facing facade. New integrations
should use `find_duplicate_alphas`; `find_alpha_duplicates` remains a tested
compatibility alias and will not introduce another repository or execution
path.

Template writes use `create_template`, `update_template`, and
`delete_template` with an explicit absolute private catalog path. The public
synthetic catalog is read-only.

Simulation calls remain explicit: use `simulate_single` for one request,
`simulate_single_batch` for independent Single requests, and
`simulate_multi_batch` for bounded Multi parents. None of these APIs submits an
Alpha; all Simulation writes flow through `SimulationGateway`.
