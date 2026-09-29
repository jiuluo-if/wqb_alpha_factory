# 远端优先架构

## 核心原则

```text
AI 负责研究推理。
Python 负责平台事实与执行安全。
BRAIN 负责 Alpha 模拟证据。
```

## 公开工具面

`wqb_agent.research_api` 是唯一 Agent-facing facade，提供原始 dataset/datafield 列表、operator capability、template/probe、Simulation、remote Alpha evidence、pairwise daily-PnL comparison、dedupe、group 和 color 工具。字段/机制筛选由 Agent 负责；执行前只有确定性的 live schema 与 field/operator capability 校验。`compare_alphas(..., pairwise_pnl=True)` 通过现有 evidence owner 读取 live BRAIN PnL 并返回有界相关性 evidence；它不判断候选资格、不设置相关阈值，也不创建 portfolio state。

`research_status()` 是 Agent 的唯一起步调用：它返回 bounded `write_readiness` / `write_blockers`、capability/authentication summary、quota 与 cache freshness、pending 计数和 `research_contract_version`，不给出研究建议，也不携带 pending rows。准入状态及其判定规则以根目录 [`AGENTS.md`](../AGENTS.md) 为唯一 owner；逐行诊断可按需调用现有 READ_ONLY `get_pending_executions()`。`research_tool_manifest()` 默认披露 canonical Agent Core；direct facade 的低频能力需显式请求 `profile="full"`。

MCP stdio 入口有两个模式：`alpha-factory-mcp` 仅提供 READ_ONLY tools；显式 opt-in 的 `alpha-factory-research-mcp` 提供 canonical Agent Core 与按需 pending-detail READ_ONLY tool，Simulation writes 仍只能经 `research_api → SimulationGateway → Simulator → WQBClient → BRAIN`。它们是同一 facade 的 transport，不创建第二研究语义或 BRAIN 访问负责方；详见 [`MCP_READ_ONLY.md`](MCP_READ_ONLY.md)。

## 唯一 Simulation 写链

```text
research_api.simulate_batch
research_api.simulate_multi_batch
 → SimulationGateway
 → Simulator
 → WQBClient
 → BRAIN
```

`simulate` 与 `simulate_single` 继续作为兼容 Python API，但不进入默认 Agent CORE。

Writer 支持的 Simulation 类型、batch 范围与派发并发见 [`reference/SIMULATION_SETTINGS.md`](reference/SIMULATION_SETTINGS.md)；平台 schema 与 live capability 的证据边界见 [`BRAIN_PROTOCOL.md`](BRAIN_PROTOCOL.md)。

[`SimulationGateway`](../wqb_agent/simulation_gateway.py) 负责确定性的请求校验与 dispatch；研究假设、机制判断、参数选择和结果解释属于 AI。Simulation 写入与 exactly-once 约束以根目录 [`AGENTS.md`](../AGENTS.md) 为准。

Candidate-scoped admission 同时报告 READY candidates 与 Gateway 基于未决 parent guards 得出的当前 Multi transport window；research sizing/budget 属于 Agent 的临时工作计划。运行时保留必要的 per-request/parent/concurrency transport limits，但不为 Research 设置最小 wave size 或累计 Simulation 解锁门槛。

## ExecutionGuard

ExecutionGuard 是唯一远端写安全负责方；持久字段、恢复路径与 exactly-once 不变量统一见根目录 [`AGENTS.md`](../AGENTS.md)。

## RemoteAlphaRepository

[`RemoteAlphaRepository`](../wqb_agent/remote_alpha_repository.py) 从 BRAIN 读取 Alpha/evidence，并维护可重建的滚动 metadata cache；live response 与 cache 的优先级见根目录 [`AGENTS.md`](../AGENTS.md)。

## Factory、去重与颜色

AlphaFactory 是纯候选生成器，输出 `SimulationSpec`，不提交、不写研究数据库、不运行长时控制循环。Probe generator 可用的确定性、lazy、bounded coverage traversal 只控制单次调用如何遍历已选模板，不设置全局研究预算，也不表示模板具有相同经济权重或进行 winner ranking。精确去重使用 canonical expression 加完整 effective settings；严格 structural 与 variant family 仅作为 AI 的 advisory evidence。variant family 只对已知 time-series horizon lattice 的最终窗口参数做保守 abstraction，不表示 semantic equivalence；安全 epsilon、operator-required constant、threshold 和未知 numeric literal 保持 identity。颜色先复用同一份 remote evidence snapshot 的 `variant_family_key`，再由 AI 显式给出最多 5 个 family 到既有 palette 颜色的 assignment；preview plan 必须同时保留 strict structural key、`family_member_count` 和 `observed_execution_count`，后者只是当前 snapshot/window 的独立 execution 下界，经 review 后 sync 只消费该 exact plan，并在 PATCH 前重新读取颜色，stale 时 fail closed。质量状态只作为文本证据，颜色不表示质量或 winner；`overwrite` 和 readback verify 明确控制 metadata PATCH。

Alpha submission 与隐私边界见根目录 [`AGENTS.md`](../AGENTS.md)。
