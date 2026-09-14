# Agent-facing architecture

仓库是研究仪器，不是研究员。Agent 选择 hypothesis、field、expression、解释和下一步；Python 保证平台事实、执行、持久化、校验、安全与恢复。

## 事实层级

```text
BRAIN live truth > append-only evidence > derived view > cache
```

缺失或含糊证据保持 `UNKNOWN` / `UNAVAILABLE`。真实 Alpha、字段、metrics、trajectory 和 ExperienceMemory 只存在本地私有状态。

## 运行时对象图

```text
raw config
  ↓ parse_config / normalize_config
typed AppConfig
  ↓ build_agent_runtime_policy
AgentRuntimePolicy
  ↓ build_runtime_components
RuntimeComponents
  ↓ explicit hooks
Agent → SuggestionWorkflow / ProposalExecutionWorkflow / AlphaFeedWorkflow / OptimizerWorkflow
```

`RuntimeComponents` 只创建一次 memory、trajectory、ledger、discovery、simulator、checkpoint 和 cache；workflow 不反向导入 Agent，不重新创建这些 owner。

## Owner 与写路径

| 能力 | 唯一 owner | 边界 |
|---|---|---|
| BRAIN transport | `client.py` | 认证、Retry-After、分类错误、只读 GET 与 Simulation submit |
| Simulation execution | `ProposalExecutionWorkflow` → `Simulator` | checkpoint、预算、exactly-once、`SUBMIT_UNKNOWN` |
| Research evidence | `Trajectory` / `TrialLedger` | append-only 事实与生命周期审计 |
| Compressed memory | `ExperienceMemory` | lessons、avoid、next、bounded short-term experience |
| Optimization | `OptimizerWorkflow` | 只筛已有 DONE evidence，消费 Agent-authored decision |
| Optimization read surface | `optimization_interfaces.py` | 只读 Alpha detail、aggregates、allow-listed PnL、correlation；不拥有状态 |
| Alpha Feed | `AlphaFeedWorkflow` | 只读远端轻量 metadata，不恢复 metrics |
| Alpha color | `AlphaColorWorkflow` | 显式 metadata PATCH，不能提交 Alpha |
| Template | `wqb_agent.alpha_templates` | schema、loader、registry、validation 和 synthetic/public-private 边界 |

生产 Simulation 唯一链路是 `Agent.run_proposals()` → `ProposalExecutionWorkflow` → `Simulator` → `WQBClient`。Alpha submission 始终由用户手工完成。

## 优化边界

优化 Agent 通过 `research_api` 的 `inspect_optimizer_parents`、`inspect_optimizer_context`、`propose_optimization` 和 `materialize_targeted_batch` 工作。它先读取已有 evidence，再区分 signal、horizon、turnover、portfolio、coverage 和 overfit 问题；Agent 提供经济机制与反证，Python 只做确定性 gate。

`optimization_interfaces.py` 的 provider 复用唯一 `WQBClient`，以 schema 名称解码 PnL；diagnostic 是 hint，不替代研究决策。每个 child 只改变一个主要变量，失败、UNKNOWN、PRUNED 通过既有 `ExperienceMemory` 记账，原始指标仍归 `Trajectory`。

Optimizer 不生成经济机制、不扫描参数、不写 trajectory、不刷新 Alpha Feed、不触发 Simulation，也不创建第二个 inbox、ledger、state 或 memory。

## 提案与恢复

探索批次使用既有 exact-100 contract；Agent-authored targeted batch 使用同一个 `proposals.json`，上限由既有 batch contract 控制。未知写结果只保留 `SUBMIT_UNKNOWN` 并进行只读 reconciliation；已知 progress URL 只能轮询。

Execution identity 只使用 `Agent._proposal_settings()` 合并后的完整有效
Simulation settings；raw proposal settings 和 expression-only shortcut 都不是远端
execution identity。`submission_fingerprint` 已存储时必须与 expression/settings
重新计算的值一致，缺失 fingerprint 仅对 legacy checkpoint 做确定性恢复；不一致、
不可读或 future-schema checkpoint 对 force-new-round 仍是
`UNVERIFIABLE_CHECKPOINT_IDENTITY`，不得产生新的 POST。

checkpoint 是 recovery envelope，不是结果库：它保存可重建同一 Experiment 的
id、datasets、candidate/proposal、created_at、parent provenance、fingerprint、
progress URL 与 template/operator provenance，但不保存 metrics、checks、PnL、
Alpha result 或 validation payload。Optimizer 的 CHILD/ROBUSTNESS 以精确
`parent_id == Experiment.id` 为权威；`parent_expression` 只作 cross-check，legacy
expression-only 只有在 durable canonical history 中唯一时兼容。

TrialLedger 对 proposal lifecycle 的 candidate、execution fingerprint、research role
和 arm 采用最早合法 committed/submitted identity；稀疏 terminal row 只能补充状态，
不能把历史试验迁移到另一个 arm。identity 或 arm drift 由 `state audit` fail closed。

## 变更规则

触碰 owner、proposal/schema、state merge 或安全边界时，必须增加行为/回归测试并更新本文件。公共文档只保留当前 contract；历史由 Git 承担，隐私规则见 [`PRIVACY.md`](PRIVACY.md)，研究方法见 [`RESEARCH_POLICY.md`](RESEARCH_POLICY.md)。
