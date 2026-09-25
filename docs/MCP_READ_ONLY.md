# 只读 MCP 传输

`wqb_agent.mcp_server` 把选定的 `wqb_agent.research_api` 函数适配到 MCP stdio。它不持有研究逻辑、BRAIN HTTP 路径、缓存、credentials 或执行状态。安装可选 SDK：`python -m pip install -e ".[mcp]"`，然后在项目目录运行 `alpha-factory-mcp`。

全部 5 个工具均在描述、结果和 MCP `readOnlyHint` 注解中标记 `READ_ONLY`：

| 工具 | facade 负责方 | 读取内容 | 边界 |
|---|---|---|---|
| `get_capabilities` | `research_api.get_capabilities` | 实时算子能力 | 通用 48 KiB 结果上限 |
| `get_simulation_modes` | `research_api.get_simulation_modes` | 认证与广告权限 | 未知权限保持未知 |
| `list_datafields` | `research_api.list_datafields` | 一页实时字段 | 请求上限 1–50；输出最多 20 行 |
| `get_alpha_evidence` | `research_api.get_alpha_evidence` | 一个实时 Alpha 与显式命名的 recordsets | 最多两个 recordset；通用结果上限 |
| `get_pending_executions` | `research_api.get_pending_executions` | 本地已配置 ExecutionGuard | 通用结果上限；不做对账或状态变更 |

每个结果都包含 `access_mode`、`owner`、`source`、`status`、`evidence_status`、`freshness`、可选 `fetched_at`/`age_sec` 与 `truncated` 标志；source 由 facade 透传。实时数据标记 `READ_AT_CALL`，本地 guard 数据标记 `LOCAL_STATE_AT_CALL`。不可用数据与未知权限保持可见。认证、权限、证据缺失、限流及其他读失败只返回简短分类错误，不回传原始 HTTP 消息；凭证类键脱敏；表达式文本只允许出现在显式授权的 Alpha 证据结果中。

MCP 服务器不暴露 Simulation、缓存写、模板写、颜色同步或 Alpha 提交工具。研究执行仍走 `research_api → SimulationGateway → Simulator → WQBClient`；需要 Simulation 的主机必须在本只读服务器之外使用既有 Agent facade，并保留其 guard 与对账契约。Alpha 提交保持人工。

存在本地 guard 条目时，MCP 服务器只读其有界投影：不轮询已知 progress URL、不恢复任务、不改写 guard、不重试模糊 POST。当前运行期存在性永不记入 tracked 文档。
