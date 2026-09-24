# Remote-First Architecture

## 核心原则

```text
AI 负责研究推理。
Python 负责平台事实与执行安全。
BRAIN 负责 Alpha 模拟证据。
```

## 公开工具面

`wqb_agent.research_api` 是唯一 Agent-facing facade，提供原始 dataset/datafield 列表、operator capability、可选 template/probe、Simulation、remote Alpha evidence、dedupe、group 和 color 工具。字段/机制筛选由 Agent 负责；执行前只有确定性的 live schema 与 field/operator capability 校验。

`research_status()` 是 Agent 的唯一起步调用：它只读地聚合 live capability、simulation modes、带 freshness 的 quota、pending executions、cache freshness 和 `research_contract_version`，不给出任何研究建议。`research_tool_manifest()` 默认只披露 CORE profile；模板维护、颜色同步、similarity 等低频能力需显式请求 `profile="full"`。

`wqb_agent.mcp_server` 是对该 facade 一小部分只读工具可选的 stdio 传输，不暴露任何写操作，也不为研究语义或 BRAIN 访问增设第二负责方；见 [`MCP_READ_ONLY.md`](MCP_READ_ONLY.md)。

## 唯一 Simulation 写链

```text
research_api.simulate / simulate_single / simulate_batch
research_api.simulate_multi_batch
 → SimulationGateway
 → Simulator
 → WQBClient
 → BRAIN
```

Simulation 模式边界是显式的：生产 writer 当前支持已验证的 `REGULAR` 与 `REGION_AGNOSTIC` 请求 schema；`REGION_AGNOSTIC` 由客户端 scope 提供 `region="ALL"` 等约束，一次写入返回一个 Region-Agnostic parent 和它的多个地区 child。Single Simulation 小规模优化默认 10 个 worker 窗口；Multi-Simulation 每个 parent 2–10 个 REGULAR child，默认并发 dispatch 2 个 parent，显式硬上限 8 个 parent 供大规模探针使用。SUPER 可以是平台广告的能力，但不是 writer 支持的模式，在其独立 `combo`/`selection` 写入契约实现并验证之前，永不作为回退或 Multi 窗口的一部分。

任何其他模块不得直接提交 Simulation。

`SimulationGateway` 只负责请求规范化、基本 schema、live field/operator capability、execution fingerprint、exact dedupe、quota/concurrency、ExecutionGuard、提交、轮询和恢复。研究假设、机制判断、参数选择和结果解释属于 AI。

## ExecutionGuard

唯一持久安全记录是 `.wqb_state/execution_guard.json`。每条记录最多包含 fingerprint、`SUBMITTING/RUNNING/SUBMIT_UNKNOWN`、progress URL、时间戳、bounded `simulation_count`、`kind`（`SINGLE`/`MULTI_PARENT`/`MULTI_CHILD`）、MULTI_CHILD 的 `parent_fingerprint` 和可选 remote Alpha ID。`simulation_count` 只表达该 unresolved write 对应的 Simulation 数量；不保存 child payload、expression、settings 或结果。完成结果被 BRAIN 确认前不得删除；不确定 POST 永不自动重试。

exact-once 以每个 Simulation 为单位，而不是每个 parent payload：Multi POST 前 parent 与每个 child 都会各自登记 fingerprint，因此 unknown 之后的重排、拆分、子集重试或把 child 改走 Single，都无法对同一个 unresolved child 再次 POST。`kind` 同时决定恢复路径：`MULTI_PARENT` 用 `poll_multi_progress`，否则用 `poll_progress`；child 的恢复跟随其 parent。

## RemoteAlphaRepository

Repository 读取 BRAIN Alpha 与 evidence，并维护可删除、可重建的滚动 metadata cache。默认保留 7 天，可配置 1–90 天；live response 优先于 cache，cache 不能把 UNKNOWN 变成 PASS。

## Factory、去重与颜色

AlphaFactory 是纯候选生成器，输出 `SimulationSpec`，不提交、不写研究数据库、不运行长时控制循环。Probe 固定预算在多个模板间采用 deterministic、lazy、bounded 的 template-level coverage-first traversal；这只改变合法候选的遍历顺序，不表示模板具有相同经济权重，也不是 winner ranking。精确去重使用 canonical expression 加完整 effective settings；严格 structural 与 variant family 仅作为 AI 的 advisory evidence。variant family 只对已知 time-series horizon lattice 的最终窗口参数做保守 abstraction，不表示 semantic equivalence；安全 epsilon、operator-required constant、threshold 和未知 numeric literal 保持 identity。颜色先复用同一份 remote evidence snapshot 的 `variant_family_key`，再由 AI 显式给出最多 5 个 family 到既有 palette 颜色的 assignment；preview plan 必须同时保留 strict structural key、`family_member_count` 和 `observed_execution_count`，后者只是当前 snapshot/window 的独立 execution 下界，经 review 后 sync 只消费该 exact plan，并在 PATCH 前重新读取颜色，stale 时 fail closed。质量状态只作为文本证据，颜色不表示质量或 winner；`overwrite` 和 readback verify 明确控制 metadata PATCH。

Alpha submission 永远是人工操作，credentials、真实 Alpha 和私有字段不得进入 tracked 文件。
