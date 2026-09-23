# Remote-First Architecture

## 核心原则

```text
AI owns research reasoning.
Python owns platform truth and execution safety.
BRAIN owns Alpha simulation evidence.
```

## 公开工具面

`wqb_agent.research_api` 是唯一 Agent-facing facade，提供 discovery、operator capability、template/probe、Simulation、remote Alpha evidence、dedupe、group 和 color 工具。

## 唯一 Simulation 写链

```text
research_api.simulate_single / simulate_single_batch
research_api.simulate_multi_batch
 → SimulationGateway
 → Simulator
 → WQBClient
 → BRAIN
```

Simulation mode boundaries are explicit: the production writer currently
supports only the verified `REGULAR` request schema. Single Simulation uses a
default ten-worker window for small optimizations; Multi-Simulation groups two
to ten REGULAR children per parent, dispatches two parents concurrently by
default, and supports an explicit hard maximum of eight parents for large
probes. Region-Agnostic and SUPER may be platform-
advertised capabilities, but are not writer-supported modes and are never a
fallback or part of the Multi window until their independent write contracts
are implemented and verified.

任何其他模块不得直接提交 Simulation。

`SimulationGateway` 只负责请求规范化、基本 schema、live field/operator capability、execution fingerprint、exact dedupe、quota/concurrency、ExecutionGuard、提交、轮询和恢复。研究假设、机制判断、参数选择和结果解释属于 AI。

## ExecutionGuard

唯一持久安全记录是 `.wqb_state/execution_guard.json`。每条记录最多包含 fingerprint、`SUBMITTING/RUNNING/SUBMIT_UNKNOWN`、progress URL、时间戳、bounded `simulation_count` 和可选 remote Alpha ID。`simulation_count` 只表达该 unresolved write 对应的 Simulation 数量；不保存 child payload、expression、settings 或结果。完成结果被 BRAIN 确认前不得删除；不确定 POST 永不自动重试。

## RemoteAlphaRepository

Repository 读取 BRAIN Alpha 与 evidence，并维护可删除、可重建的滚动 metadata cache。默认保留 7 天，可配置 1–90 天；live response 优先于 cache，cache 不能把 UNKNOWN 变成 PASS。

## Factory、去重与颜色

AlphaFactory 是纯候选生成器，输出 `SimulationSpec`，不提交、不写研究数据库、不运行长时控制循环。Probe 固定预算在多个模板间采用 deterministic、lazy、bounded 的 template-level coverage-first traversal；这只改变合法候选的遍历顺序，不表示模板具有相同经济权重，也不是 winner ranking。精确去重使用 canonical expression 加完整 effective settings；严格 structural 与 variant family 仅作为 AI 的 advisory evidence。variant family 只对已知 time-series horizon lattice 的最终窗口参数做保守 abstraction，不表示 semantic equivalence；安全 epsilon、operator-required constant、threshold 和未知 numeric literal 保持 identity。颜色先复用同一份 remote evidence snapshot 的 `variant_family_key`，再由 AI 显式给出最多 5 个 family 到既有 palette 颜色的 assignment；preview plan 必须同时保留 strict structural key、`family_member_count` 和 `observed_execution_count`，后者只是当前 snapshot/window 的独立 execution 下界，经 review 后 sync 只消费该 exact plan，并在 PATCH 前重新读取颜色，stale 时 fail closed。质量状态只作为文本证据，颜色不表示质量或 winner；`overwrite` 和 readback verify 明确控制 metadata PATCH。

Alpha submission 永远是人工操作，credentials、真实 Alpha 和私有字段不得进入 tracked 文件。
