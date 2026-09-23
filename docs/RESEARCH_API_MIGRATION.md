# Research API migration

`wqb_agent.research_api` is the only Agent-facing facade. The canonical exact
duplicate lookup is `find_duplicate_alphas`, matching the current formal
Agent prompt and migration guidance. The redundant spelling
`find_alpha_duplicates` has been retired.

Template writes use `create_template`, `update_template`, and
`delete_template` with an explicit absolute private catalog path. The public
synthetic catalog is read-only.

Simulation calls use the formal Agent-run names: `simulate` for one request,
`simulate_batch` for independent Single requests, and `simulate_multi_batch`
for bounded Multi parents. The interim v2-only wrappers `simulate_single` and
`simulate_single_batch` have been retired. None of these APIs submits an Alpha;
all Simulation writes flow through `SimulationGateway`.
