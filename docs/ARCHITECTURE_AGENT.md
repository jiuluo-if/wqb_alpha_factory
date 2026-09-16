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
research_api.simulate / simulate_batch
 → SimulationGateway
 → Simulator
 → WQBClient
 → BRAIN
```

任何其他模块不得直接提交 Simulation。

`SimulationGateway` 只负责请求规范化、基本 schema、live field/operator capability、execution fingerprint、exact dedupe、quota/concurrency、ExecutionGuard、提交、轮询和恢复。研究假设、机制判断、参数选择和结果解释属于 AI。

## ExecutionGuard

唯一持久安全记录是 `.wqb_state/execution_guard.json`。每条记录最多包含 fingerprint、`SUBMITTING/RUNNING/SUBMIT_UNKNOWN`、progress URL、时间戳和可选 remote Alpha ID。完成结果被 BRAIN 确认前不得删除；不确定 POST 永不自动重试。

## RemoteAlphaRepository

Repository 读取 BRAIN Alpha 与 evidence，并维护可删除、可重建的滚动 metadata cache。默认保留 7 天，可配置 1–90 天；live response 优先于 cache，cache 不能把 UNKNOWN 变成 PASS。

## Factory、去重与颜色

AlphaFactory 是纯候选生成器，输出 `SimulationSpec`，不提交、不写研究数据库、不运行长时控制循环。精确去重使用 canonical expression 加完整 effective settings；结构相似和相关性仅作为 AI 的 advisory evidence。颜色只消费 remote evidence，`dry_run`、`overwrite` 和 readback verify 明确控制 metadata PATCH。

Alpha submission 永远是人工操作，credentials、真实 Alpha 和私有字段不得进入 tracked 文件。
