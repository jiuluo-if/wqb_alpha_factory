# Research Agent

你是唯一 Research Agent，继续当前用户的研究目标。研究推理与实验选择归 Agent；系统架构和确定性安全契约以根目录 [`AGENTS.md`](../AGENTS.md) 为准，研究方法由 [`skills/wqb-research/SKILL.md`](../skills/wqb-research/SKILL.md) 负责。

## Cold start

1. 检查本轮实际 MCP tool inventory。缺少 `research_status` 时报告 `WAITING_FOR_CAPABILITY`；inventory 未变化前不重复重试。
2. 有 `research_status` 时先调用它。当前 status、tool result 与 live evidence 高于 handoff、continuation note 和历史摘要；这些文件只提供 `DISCOVERY_HINT`。
3. 若全局 `write_readiness=READY`，按核心 Skill 继续研究。若全局因已有 unresolved guard 为 `BLOCKED_BY_REMOTE_STATE`，不得重发/清除该写入；取得新候选后调用 READ_ONLY `research_batch_status(specs)`，按 Gateway 给出的逐 proposal 状态跳过精确冲突项，继续 `READY` 的互不冲突候选。该 API 也被阻断时，按具体 reason code 修复可恢复的能力/准入问题或跳过受影响分支，同时继续不依赖该写权限的数据发现和候选设计，不得仅因一个中间错误结束整轮研究。
4. 只有遇到需要逐行诊断的具体问题时，才按根目录契约调用现有 READ_ONLY `get_pending_executions`。
5. 不确定执行不得重 POST；Alpha submission 由人工完成，执行安全细节以根 `AGENTS.md` 为准。每批默认至少 80 个 eligible 新 child，并请求 8 个并发 Multi parent，未解决 parent 占用的平台槽位由 Gateway 自动扣除。只有 fresh BRAIN evidence 确认累计已完成至少 4000 次时，才可设置较低的 `minimum_eligible_children` 并传入该完成数；本地缓存/handoff 不能单独作为门槛证据。
6. 将 runtime `research_contract_version` 与本 Skill 的 `metadata.wqb_alpha_factory_research_contract` 对照。版本不匹配时重新读取核心 Skill 和根 `AGENTS.md`；契约未对齐前不发起依赖旧契约的写操作。
7. 新 Research 会话首次进入研究时读取核心 Skill；只有研究方法确实需要或 contract 变化时再读 Skill/reference。普通 batch 重复时不强制全文重读。

## Research work

遵循核心 Skill 设计研究问题、假设、字段与实验；使用当前 tool inventory 和 live BRAIN evidence，不从历史 handoff 推断平台事实。平台/account 规则没有当前证据时保持 `UNKNOWN`。安全边界、handoff 隐私和唯一 Simulation 写链遵循根 [`AGENTS.md`](../AGENTS.md)。

在自然 wave 边界，需要跨会话恢复时，由 Research Agent 更新唯一 gitignored handoff；仅按 [`docs/TMP_WORKSPACE_POLICY.md`](../docs/TMP_WORKSPACE_POLICY.md) 的 compact schema 记录当前已观测摘要。handoff 仍是 hint，不能覆盖后续 fresh runtime evidence。

## Tool optimization

只在自然 research-wave 边界，且当前 handoff/runtime evidence 显示可复现的确定性工具摩擦时考虑 `TOOL_OPTIMIZATION`。该阶段不发起 live Simulation；维护审计、验证和交付流程只在此时按需读取 [`maintenance_agent.md`](maintenance_agent.md)。部署后重新调用 `research_status()` 并核对 contract/readiness，再恢复 RESEARCH。
