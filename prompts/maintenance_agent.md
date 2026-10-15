# Research Agent 工具优化流程

本文件仅供唯一 Research Agent 在 `TOOL_OPTIMIZATION` 阶段使用，不是独立维护角色。此阶段只处理 handoff/runtime evidence 已证实的确定性工程摩擦，不替 Agent 选择经济机制或实验参数。开始前检查当前本地代码、根 [`AGENTS.md`](../AGENTS.md) 和目标目录规则。只有用户明确授权时才核验远端状态。架构、安全、隐私、handoff 与 Git 规则以根指南和 [`docs/TMP_WORKSPACE_POLICY.md`](../docs/TMP_WORKSPACE_POLICY.md) 为准。

每项任务在当前计划记录中保留这份短契约：

```text
TASK_TARGET:
DIRECT_VERIFICATION:
CONSTRAINTS:
CURRENT_BLOCKER:
NEXT_USEFUL_ACTION:
```

运行新命令前先判断它是否直接减少 `TASK_TARGET` 的不确定性；无关检查停止。不要重复检查同一文件、状态或时间戳，除非结果会改变决定。

## 运行证据读取

按 [`docs/TMP_WORKSPACE_POLICY.md`](../docs/TMP_WORKSPACE_POLICY.md) 只读 Research Agent 独占维护的 `tmp/research_handoff.json`。只读取必要的原始资料与运行证据；字段语义以其 owner 为准。修复是否已包含，只依据当前代码、调用契约和直接运行证据。证据不足时保持 `UNKNOWN`，不重复实现。记录最后处理的 `updated_at` 和相关 tmp 检查点。

- handoff 合法且新于任务检查点：作为首要线索；只有内容异常、与代码契约冲突或需定位已证实摩擦时，才查看少量对应原始资料。
- handoff 未更新且检查点也未变化：若用户未要求新审查，记录 `WAITING_FOR_RUNTIME_EVIDENCE` 并停止重复扫描；仍可完成有独立静态依据的明确维护。
- handoff 缺失或格式错误：仅在首次建立本任务窗口、出现新相关资料或用户要求重审时，先审元数据、做一次有界扫描并记录检查点。
- 活动日志只按完整落盘记录处理；没有终态标记的输入或尾部保持 `PARTIAL_OBSERVATION`。

阻塞必须指向真实缺口，例如缺少新 handoff、终态批次或运行进程。重复状态确认和无关元数据检查不能单独构成阻塞；明确阻塞后停止无关检查。

资料只用 `KEEP / MERGE / DELETE / REVIEW_REQUIRED` 分类，时间只用 `CURRENT / FOUNDATIONAL / SUPERSEDED / TIME_UNKNOWN`。不设数量配额、不凑项；只处理证据支持且属于当前任务的必要内容。运行摩擦须有直接运行证据；静态契约问题按对应验证方式核验。证据不足时不改代码。进入 `WAITING_FOR_RUNTIME_EVIDENCE` 后不重复同一轮审查或反复报告 `UNKNOWN`。

handoff 由 Research Agent 在自然研究边界更新。阶段转换和返回 `RESEARCH` 的条件以根 `AGENTS.md` 为准。

报告固定包含 `RUNTIME_EVIDENCE`、`CHANGE`、`DIRECT_VERIFICATION`、`RESEARCH_IMPACT`、`REMAINING_BLOCKER`。只写匿名工程事实。有修改时按根指南验证和交付，并遵循用户对远端操作的授权；未授权时只在本地处理，不推送。完成获授权部署后，重新获取最新的 `research_status` 响应，再返回 `RESEARCH`。
