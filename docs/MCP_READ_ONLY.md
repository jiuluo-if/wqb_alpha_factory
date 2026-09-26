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

`alpha-factory-mcp` 继续只暴露上表 5 个 `READ_ONLY` 工具，不包含 Simulation、模板写、颜色同步或 Alpha 提交。

## 显式 Research Mode

需要真实研究写入的 Agent 可单独连接 `alpha-factory-research-mcp`。启动该 entrypoint 前，host 必须显式设置 `ALPHA_FACTORY_ENABLE_SIMULATION_WRITES=1`；未设置时 server 以 `RESEARCH_WRITE_MODE_NOT_ENABLED` 退出，不提供降级后的 Research 工具面。这个 server 只复用 `wqb_agent.research_api`，Simulation 写入仍沿 `research_api → SimulationGateway → Simulator → WQBClient → BRAIN`。

Research Mode 暴露 10 个工具：`research_status`、`list_datasets`、`list_datafields`、`get_operator_reference`、`validate_simulation_spec`、`simulate_batch`、`simulate_multi_batch`、`get_alpha_evidence`、`get_alpha_prod_correlation`、`reconcile_execution`。它与 `research_tool_manifest(profile="core")` 共享同一组 canonical names；`get_alpha_evidence(alpha_id)` 默认返回轻量 summary，显式选择 recordsets 时读取对应深度，均不隐式请求 PROD correlation。单独的 `get_alpha_prod_correlation(alpha_id)` 标记为 `FINALIST_ONLY`，只在 Agent 显式检查终选候选时调用。`simulate_batch` 接受 1–50 个 `SimulationSpec`；`simulate_multi_batch` 接受 2–100 个候选。每个 write spec 必须带 1–48 字符、batch 内唯一的 `proposal_id`；不满足时 transport 返回 `INVALID_ARGUMENT` 且不调用 facade。Simulation 输入仅包含 spec 字段，state directory、config 和 credentials 固定在 server process。每项结果仅投影 `proposal_id`、`status`、`reason_code`、`fingerprint`、`alpha_id` 和 `field_validation`；owner 合法范围内的提案、execution 与 Alpha identity 完整返回，batch-level `remote_duplicate_scan` 只返回一次。`note`、`template_id` 已由 Agent 持有的 proposal mapping 覆盖；`batch_fingerprint` 不回显，Multi child 的完整 `fingerprint` 可通过 `reconcile_execution(child_fingerprint)` 沿 `MULTI_CHILD.parent_fingerprint` 恢复 parent。100 条结果的 owner-max synthetic projection 为 40,139 / 49,152 bytes。缺失或异常结果会保留候选槽位并设置 `truncated=true`。两个 Simulation 工具标记为 remote write；Alpha submission、HTTP、WQBClient、模板写和维护工具不暴露。

Research Agent 必须先检查 tool inventory 并调用 `research_status`。缺少该工具时仅报告一次 `LIVE_RESEARCH_CAPABILITY_MISSING`，将 goal 设为 `WAITING_FOR_CAPABILITY`；inventory 未变化（包括 host 重启）不得自动重试。工具存在但远端权限、quota 或未解决 guard 阻塞时标记 `BLOCKED_BY_REMOTE_STATE`；只有 live readiness 成功才是 `READY`。

代码支持不代表当前 Agent 会话已连接新 server。实际 host 仍需安装/启动 `alpha-factory-research-mcp` 并将其注册到 Research Agent，之后才能在该 Agent 的 inventory 中看到上述工具。

`alpha-factory-mcp` 对本地 guard 只读有界投影，不轮询或恢复；Research Mode 的 `reconcile_execution` 可直接接收 child fingerprint，通过同一 facade 跟随 `MULTI_CHILD → parent_fingerprint` 做恢复，永不重发 Simulation POST。当前运行期存在性永不记入 tracked 文档。
