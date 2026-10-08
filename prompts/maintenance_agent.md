# Research Agent — TOOL_OPTIMIZATION procedure

这是唯一 Research Agent 在 `TOOL_OPTIMIZATION` 阶段使用的 procedure reference，不是独立 Maintenance Agent。此阶段只响应 handoff/runtime evidence 中已观察到的确定性工程摩擦，不代替 Agent 生成经济机制或实验参数。开始时 fetch `origin/main` 并检查根 [`AGENTS.md`](../AGENTS.md) 与目标目录约束。本文件只规定本阶段的 evidence-review procedure；架构、安全、handoff、隐私、验证与 Git 交付以根指南和 [`docs/TMP_WORKSPACE_POLICY.md`](../docs/TMP_WORKSPACE_POLICY.md) 为准。

每个维护任务在当前 planning 中保持这个短 Task Contract：

```text
TASK_TARGET:
DIRECT_VERIFICATION:
CONSTRAINTS:
CURRENT_BLOCKER:
NEXT_USEFUL_ACTION:
```

运行每条新命令前先问：它会直接减少 `TASK_TARGET` 的不确定性吗？若不会，停止该动作。避免重复计算 hash、确认同一 SHA、检查同一 mtime 或证明文件未变，除非结果会改变当前决策。

## Runtime evidence fast path

按 [`docs/TMP_WORKSPACE_POLICY.md`](../docs/TMP_WORKSPACE_POLICY.md) 读取 Research Agent 独占维护的本地 handoff：

```text
tmp/research_handoff.json
```

读取最少的 artifact/runtime evidence；handoff 字段语义和读取边界以其 canonical owner 为准。将 handoff SHA/contract 与刚 fetch 的代码版本核对；旧 SHA 的问题若已在 main 修复，标记 `STALE_RUN_EVIDENCE`，不重复实现。将最后处理的 `updated_at` 和相关 tmp 检查点记在当前 task 的 planning progress 中。

- handoff 合法且比 task checkpoint 新：以它作为本轮首要运行线索；只有数据异常、与代码契约冲突，或需要定位已确认摩擦时才查看对应的少量原始 artifact。
- handoff 合法但未更新：若相关 tmp 检查点也未变化且用户未要求新的 runtime audit，记录 `WAITING_FOR_RUNTIME_EVIDENCE` 并停止 runtime-friction loop；仍可完成用户明确要求、且有独立静态契约依据的维护，不重扫已审窗口。
- handoff 缺失或 malformed：仅在首次建立本任务窗口、出现新的相关 tmp 证据，或用户明确要求重审新窗口时，做一次 metadata-first、范围有界的 fallback scan；记录检查点后不要重复读取同一批历史文件。
- 活动日志只能使用完整落盘记录；没有 terminal status 的输入/尾部保持 `PARTIAL_OBSERVATION`。

blocked 状态必须指向真实 blocker，例如 `WAITING_FOR_RUNTIME_EVIDENCE`（缺少新 handoff、terminal batch 或 runner）。hash 未确认、SHA 未重查或 metadata 未重算不能单独构成 blocker。blocker 明确后停止无关状态确认。

对继续执行的维护任务做一次 `DELETE / KEEP / DEFER` simplification audit。工程候选最多 3 个，production FIX 最多 1 个；运行摩擦类问题必须有真实运行证据，静态确定的契约错误则用对应测试验证。无足够证据时保持代码不变；进入 `WAITING_FOR_RUNTIME_EVIDENCE` 后不重复同一轮 static/runtime audit 或汇报 UNKNOWN。

handoff 更新由 Research Agent 在自然研究边界负责。阶段转换与返回 Research 的条件以根 `AGENTS.md` 为准；本 procedure 只规定运行证据的读取节奏。

报告固定包含 `RUNTIME_EVIDENCE`、`CHANGE`、`DIRECT_VERIFICATION`、`RESEARCH_IMPACT`、`REMAINING_BLOCKER`。可以另列 `CODE_SHA` 作 provenance。报告只记录匿名工程事实；有有效修改时按根 `AGENTS.md` 执行验证和交付，没有有效修改时不创建空提交。离线验证通过并完成部署后，重新获取 fresh `research_status`，再交还 `RESEARCH` 阶段。
