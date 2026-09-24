# Research API 迁移说明

`wqb_agent.research_api` 是唯一 Agent-facing facade。规范化的精确重复查询是 `find_duplicate_alphas`，与当前正式 Agent prompt 及迁移指引一致；冗余名 `find_alpha_duplicates` 已退役。

模板写入使用 `create_template`、`update_template`、`delete_template`，需显式绝对私有 catalog 路径；公开 synthetic catalog 只读。

`simulate` 与 `simulate_single` 都通过同一 `SimulationGateway` 提交单个请求；`simulate_batch` 执行独立 Single 请求；`simulate_multi_batch` 处理有界 Multi parent。冗余项 `simulate_single_batch` 包装器保持退役。这些 API 都不提交 Alpha；所有 Simulation 写入都经 `SimulationGateway`。
