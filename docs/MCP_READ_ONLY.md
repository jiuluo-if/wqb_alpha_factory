# MCP 传输

`wqb_agent.mcp_server` 将 `wqb_agent.research_api` facade 接入 MCP stdio。它负责传输、有界结果投影、工具说明和错误呈现；不负责研究策略、BRAIN HTTP 路径、凭据、缓存或执行状态。

## 只读传输

使用 `python -m pip install -e ".[mcp]"` 安装可选 MCP SDK，然后运行 `alpha-factory-mcp`。此传输只提供发现、能力、Alpha 证据和待处理 guard 的读取接口，不提供 Simulation 写入、模板目录写入、元数据写入或 Alpha 提交路径。

每个返回结果都带有访问模式、负责方、来源、状态、证据状态、新鲜度和截断状态。实时证据在调用时读取；本地 guard 证据只从配置目录读取。不可用数据和未知权限会明确呈现。错误会分类处理，不返回原始 HTTP 消息、URL、本地路径或凭据。只有显式授权的 Alpha 证据接口会返回 Alpha 表达式。

## 显式研究模式

Research Agent 宿主需配置独立入口 `alpha-factory-research-mcp`，并显式设置 `ALPHA_FACTORY_ENABLE_SIMULATION_WRITES=1`。未选择此模式时，服务器启动会以 `RESEARCH_WRITE_MODE_NOT_ENABLED` 失败，不会静默切换到其他工具配置。

`research_tool_manifest(profile="core")` 是 canonical Research 工具名称、模式和负责方的机器可读 owner。MCP 注册层提供工具说明及输入/输出 schema。契约测试会将 MCP `tools/list` 实际返回的工具与 CORE manifest 对照；已连接的宿主仍须检查当前 `tools/list` 清单，因为源码无法证明宿主已注册或启动服务器。

Research 路径包括实时就绪状态与发现、分页私有模板清单、显式输入的 `generate_probes`、候选校验与准入、经 `SimulationGateway` 执行 Simulation、证据读取和只读对账。`list_templates` 与生成流程使用同一配置的私有目录，不返回原始模板表达式和固定字段绑定；目录缺失时 fail closed。`generate_probes` 只使用 Agent 选定的模板 ID、字段和数量，不会给字段或模板排序、准入候选或提交 Simulation。生成的 spec 保留字段/数据集来源；Agent 负责附加唯一 proposal ID、校验候选，并在选择写入工具前检查 Gateway 准入。

`research_status` 返回有界的就绪状态、阻断原因和待处理摘要。`research_batch_status` 对 REGULAR Multi 候选返回逐 proposal 准入及 Gateway 管理的 `available_multi_parent_slots`、`children_per_multi_parent` 和 `upper_bound_children_in_active_window`；ExecutionGuard 状态未知时，这些容量值保持 `UNKNOWN`。同一只读准入也接受单个或一批同为 `REGION_AGNOSTIC` 的单腿候选，逐 proposal 检查精确 guard fingerprint、四地区字段能力、账号权限及 write capability，不占用 Multi parent 槽；混合模拟类型仍拒绝。全局 `BLOCKED_BY_REMOTE_STATE` 不自动否决与未决 guard 不冲突且准入明确 `READY` 的候选。候选不足时，Agent 可以少派发候选。只有具体诊断需要查看 guard 行详情时才调用 `get_pending_executions`；它是本地 `READ_ONLY` 视图，不执行对账或状态修改。`get_alpha_evidence(alpha_id)` 默认返回轻量摘要；决赛候选需要 PnL 和 self-correlation 证据时，再显式请求 `depth="full"`。该接口读取既有证据 owner，不增加 self-correlation 工具。`get_alpha_prod_correlation(alpha_id)` 标记为 `FINALIST_ONLY`，只用于决赛候选的 PROD 复核。

MCP 不暴露候选间本地 PnL/Pearson 相关性工具。Research Agent 按 live 字段描述和 AlphaTemplate 的机制、slot 角色与关系契约作定性冗余分组；该近似不产生数值相关性，也不能替代 BRAIN 的 self/PROD 官方 checks。Self/PROD check 为 `PENDING` 时继续保持未决，直到平台产生新的官方 evidence。

Simulation 输入只包含 spec 字段；配置、状态目录和凭据留在服务器进程中。结果限制在 48 KiB 内，超限时会标记为截断。传输上限、实时容量和 proposal 身份要求以当前工具 schema/结果为准，并由传输层和 Gateway 执行；这些上限不构成全项目统一的研究 wave 大小。Simulation 写入带有非幂等远端写入标记，且必须走唯一链路 `research_api → SimulationGateway → Simulator → WQBClient → BRAIN`。不暴露模板增删改查、直接 HTTP、任意 Python 或 Alpha 提交接口。

服务器清单只证明此进程注册了哪些工具，不能证明外部 Research Agent 会话已连接。所需工具或私有模板目录不可用时，应报告能力限制；不要改用 shell 脚本、内部导入或过期资料。
