# Research API migration

`wqb_agent.research_api` is the only Agent-facing facade. The canonical exact
duplicate lookup is `find_duplicate_alphas`, matching the current formal
Agent prompt and migration guidance. The redundant spelling
`find_alpha_duplicates` has been retired.

Template writes use `create_template`, `update_template`, and
`delete_template` with an explicit absolute private catalog path. The public
synthetic catalog is read-only.

`simulate` and `simulate_single` both submit one request through the same
`SimulationGateway`; `simulate_batch` executes independent Single requests,
and `simulate_multi_batch` handles bounded Multi parents. The redundant
`simulate_single_batch` wrapper remains retired. None of these APIs submits an
Alpha; all Simulation writes flow through `SimulationGateway`.
