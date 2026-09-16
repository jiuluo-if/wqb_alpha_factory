# Remote-First Architecture

## 原则

```text
AI owns research reasoning.
Python owns platform truth and execution safety.
BRAIN owns Alpha and Simulation evidence.
```

Python 不替 AI 选择经济机制、优化参数或解释研究结论，也不把远端结果复制成第二个 canonical research database。

## 组件关系

```text
research_api
 ├─ platform capability / field discovery
 ├─ SimulationSpec
 ├─ SimulationGateway
 │   ├─ ExecutionGuard
 │   └─ Simulator → WQBClient
 ├─ RemoteAlphaRepository
 │   └─ rebuildable remote metadata cache
 ├─ AlphaFactory → list[SimulationSpec]
 ├─ dedupe / grouping projections
 └─ remote color preview/sync
```

`research_api` 是唯一稳定的 Agent-facing facade。旧 Agent、proposal envelope、round、parent/child、lineage、optimizer workflow 和 factory runner 不属于新 API；新代码不得依赖它们。

## SimulationGateway

Gateway 接受 `SimulationSpec(expression, settings, fields, note, template_id)`。它只做可执行形状校验、实时 operator capability 校验、fingerprint 计算与远端执行编排。

安全顺序固定为：

```text
fingerprint → reject active duplicate → durable SUBMITTING
→ exactly one POST → RUNNING + progress_url → poll same URL
→ get remote Alpha → remove guard only after result is proven
```

guard 仅允许 execution identity/status、progress URL 和时间戳，可选 remote Alpha ID；不得保存 metrics、checks、PnL、expression、hypothesis、lineage、settlement 或 factory state。

## RemoteAlphaRepository

Repository 提供 rolling metadata refresh/list/get/cache status/purge。默认 retention 为 7 天，可配置为 1–90 天。cache 删除后必须能够从 BRAIN 重建，cache 与 live 冲突时 live 优先。

证据读取默认标记来源与时间；研究判断只能使用 live evidence，或明确接受带 freshness 的 cache 视图。

## Dedupe、分组与颜色

执行去重只使用 canonical expression + effective settings fingerprint。结构相似、field 相似、correlation 和 quality 只是 advisory projection，不得替代 exact execution identity。

颜色策略只接收 remote evidence；`dry_run` 不得 PATCH，`overwrite=False` 保留已有颜色，`overwrite=True` 才允许覆盖，每次 PATCH 必须 readback verify。不得创建本地 ownership sidecar。

## 退役边界

旧 `proposals.json`、checkpoint、Trajectory、TrialLedger、settlement、ExperienceMemory、
OptimizerWorkflow 和 factory session 不是 Remote-First 状态模型。它们只能作为待删除的
兼容遗留物存在；新代码不得读取、写入或用其恢复 BRAIN evidence。
