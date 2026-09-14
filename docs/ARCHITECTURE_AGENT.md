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

架构现代化后的纯领域投影位于 workflow 之前：`research_planning.py` 负责已解析输入的研究空间/假设组装，`evidence_projection.py` 负责严格质量与 correlation gate，`alpha_semantics.py` 负责字段语义 traits，`alpha_relationships.py` 负责字段关系与频率准入，`alpha_feasibility.py` 负责有界可行性诊断，`alpha_assembly.py` 负责字段机制文本组装，`execution_identity.py` 负责 transient durable binding projection，`terminal_evidence.py`/`execution_recovery.py` 负责终态证据和 checkpoint 合并，`proposal_admission.py` 负责提案拒绝统计，`factory_route.py` 负责 legacy route episode 决策。它们均不拥有状态、不调用远端写操作；旧 facade 只委托 canonical 函数。

## 第一阶段架构审计结果（2026-09-14）

| 指标 | before | after |
|---|---:|---:|
| production Python files | 76 | 84 |
| production LOC | 29,951 | 30,217 |
| test Python files | 84 | 91 |
| test LOC | 24,022 | 24,275 |
| `Agent` LOC / methods | 1,727 / 80 | 1,651 / 79 |
| `AlphaFactory` LOC / methods | 2,181 / 35 | 1,822 / 31 |
| `ProposalExecutionWorkflow` LOC / methods | 1,404 / 29 | 1,370 / 29 |
| `AIFactoryRunner` LOC / methods | 2,024 / 57 | 1,948 / 56 |
| import cycles | 0 | 0 |

本轮已将 `CandidateBuilder` 中间层从 production runtime graph 移除；`diagnostics.py`、`smoke.py` 和其他 legacy public surface 仍有当前 consumer，继续保留为 compatibility/operational code。

## 第二阶段当前审计结果（2026-09-15）

| 指标 | 第二阶段起点 | 当前 |
|---|---:|---:|
| production Python files | 86 | 86 |
| production LOC | 30,253 | 30,304 |
| test Python files | 92 | 93 |
| test LOC | 24,303 | 24,365 |
| `Agent` LOC / methods | 1,651 / 79 | 1,650 / 78 |
| `AlphaFactory` LOC / methods | 1,801 / 31 | 1,655 / 30 |
| `ProposalExecutionWorkflow` LOC / methods | 1,365 / 29 | 1,365 / 28 |
| `AIFactoryRunner` LOC / methods | 1,948 / 56 | 1,948 / 53 |
| import cycles | 0 | 0 |

本阶段已完成 AlphaFactory feasibility 的 canonical 迁移，并删除 `wqb_agent/candidate.py`（20 行）及 11 个只导入不使用 `CandidateBuilder` 的测试 import；新增 feasibility 行为测试和删除文件的 targeted 选择规则。由于纯 feasibility 模块保留了完整诊断字段，当前 production LOC 暂时净增 51 行；candidate assembly、ProposalExecution pipeline 和 FactoryRunner control-plane 收敛仍是本阶段剩余工作。

## 已完成的提取

- `alpha_factory.py` → `alpha_semantics.py`：字段语义 profile；`AlphaFactory` 通过 canonical alias 复用。
- `alpha_factory.py` → `alpha_relationships.py`：关系标签、频率 bucket/compatibility、关系类型。
- `alpha_factory.py` → `alpha_feasibility.py`：有界字段/模板可行性诊断与 fingerprint 投影。
- `alpha_factory.py` → `alpha_assembly.py`：字段机制说明与关系证据文本组装。
- `agent.py` → `research_planning.py`：研究空间、best iteration、探索 seed 的纯组装。
- `agent.py` → `evidence_projection.py`：严格 correlation gate 与质量 rating。
- `proposal_execution.py` → `terminal_evidence.py`、`execution_recovery.py`：终态证据判定、失败分类、canonical recovery merge。
- `proposal_execution.py` → `proposal_admission.py`：有界拒绝原因统计。
- `proposal_execution.py` → `execution_identity.py`：按 owner generation 缓存、可重建的精确 binding projection。
- `factory_runner.py` → `factory_route.py`：route episode information-gain decision；runner 仍是兼容 facade。
- `runtime_components.py` → `AlphaFactory`：运行时直接持有 canonical factory，不再经过 `CandidateBuilder`。

## Remaining architecture debt

- `discovery.py`、`memory.py` 和 `factory_runner.py` 仍较大：它们同时承载现有持久化/兼容 consumer，下一刀需要先建立更细 owner contract，不能只按行数拆。
- `alpha_factory.py` 仍包含 candidate assembly 和 optimization screening；candidate construction 仍共享 template registry、prepared-facts memo 与 proposal provenance，下一步应先以纯输入/输出 contract 测试隔离，再移动实现。
- `proposal_execution.py` 仍包含 admission 和 manual recovery mutation；durable identity binding 已由 transient `ExecutionBindingIndex` 承担 projection，exactly-once fence 仍由既有 durable owners 与 workflow 共同维护。
- 本阶段尚未删除脚本或 root public symbols；它们仍需满足零 imports/CLI/docs/tests/`__all__`/dynamic references 后才能删除。

`RuntimeComponents` 只创建一次 memory、trajectory、ledger、discovery、simulator、checkpoint 和 cache；workflow 不反向导入 Agent，不重新创建这些 owner。

## Owner 与写路径

| 能力 | 唯一 owner | 边界 |
|---|---|---|
| BRAIN transport | `client.py` | 认证、Retry-After、分类错误、只读 GET 与 Simulation submit |
| Simulation execution | `ProposalExecutionWorkflow` → `Simulator` | checkpoint、预算、exactly-once、`SUBMIT_UNKNOWN` |
| Research evidence | `Trajectory` / `TrialLedger` | append-only 事实与生命周期审计 |
| TrialLedger append index | `TrialLedger` 内存 | owner-local event-id/completeness membership，可从 `trial_ledger.jsonl` 重建；不成为 durable owner |
| Persistence durability | `artifacts.py` / existing append owners | POSIX atomic replace 后同步 parent directory；首次 append 创建文件时同步目录，普通 append 保持 file fsync |
| Runtime operator reference | `wqb_agent.reference` package resource | installed package 是运行时 syntax reference owner；`docs/reference` 仅作同步的人类镜像 |
| Checkpoint recovery boundary | `CheckpointStore` | canonical semantic validation、unfinished scan 与 atomic recovery envelope |
| Remote reconciliation observation | `scripts/reconcile_pending.py` | 只读 GET/poll/get-alpha；不写 canonical research state |
| Completed-state archive | `scripts/archive_completed_rounds.py` | 只移动 `CheckpointStore.scan()` 已验证的 complete checkpoint |
| Historical projection audit | `workspace_snapshot.py` → `audit.py` | 只报告 duplicate remote execution projection，不自动修复 |
| Compressed memory | `ExperienceMemory` | lessons、avoid、next、bounded short-term experience；派生 settlement 使用可选稳定 `source_key` 幂等投影 |
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

Checkpoint validator 还严格要求 `complete` 为 bool、Experiment status 属于
`UNRESOLVED_STATUSES ∪ TERMINAL_STATUSES`，并校验 round、top-level hypothesis id
及 execution set 内的 Experiment id、重算 fingerprint、proposal id 和 progress URL
唯一性。`complete=true` 不能包含 unresolved execution；这些错误在恢复授权前
fail closed，并以 bounded validation code 进入 audit。

`CheckpointStore.write()` 先构造 recovery-only persisted projection，再复用同一
`_validate_with_code()`；禁止 `complete` coercion，validator 失败时抛出带 code 的
`CheckpointWriteError`，不会创建或覆盖 checkpoint bytes。raw JSON 的 `complete=true`
不等于 canonical completed checkpoint，只有 `CheckpointStore.scan()` 验证通过且
`complete is True` 才可由 `archive_completed_rounds.py` 归档；任何 malformed、
future-schema、identity mismatch 或 unresolved checkpoint 都留在 live state，
`--apply` 发现不可验证 checkpoint 时整体 fail closed。

`reconcile_pending.py` 只读取 `Trajectory.iter_canonical_rows()` 与
`CheckpointStore.scan()` 的 validated incomplete projection。它只观察已知
`progress_url`，不使用 expression 猜远端身份，不写 Trajectory、Checkpoint、TrialLedger
或 ExperienceMemory；旧 `--commit` 仅返回 `RECONCILE_COMMIT_RETIRED`，已知 URL 的
settlement 必须回到 `ProposalExecutionWorkflow.resume_checkpoint()`。

`state audit` 还对 canonical Trajectory projection 检查同一非空
`submission_fingerprint + progress_url` 或 `submission_fingerprint + alpha_id` 被多个
Experiment IDs 表示的历史 remote execution projection，报告 bounded
`DUPLICATE_REMOTE_EXECUTION_PROJECTION`，不自动合并、删除或修复历史。

Trajectory 的兼容报告读取可以 tolerant 地跳过历史坏行；执行、恢复、parent
binding 和 durable mutation 使用 strict reader。strict reader 对中间 malformed
JSON、非 object、非法 UTF-8 和同一 Experiment ID 的 immutable identity collision
fail closed；只有文件末尾未完整写入的 torn tail 可记录为 bounded diagnostic 并忽略，
不自动截断或修复原文件。`Experiment.id` 新写入使用 128-bit hex，旧短 ID 仍可读但不
改变其身份。

远端 `Location`/persisted `progress_url` 在任何 GET 前都必须相对当前 BRAIN base URL
解析并通过同源 scheme/host/effective-port 校验；拒绝 userinfo、fragment、跨 host、
跨 port 和 scheme downgrade。提交后收到非法 Location 仍保持 `SUBMIT_UNKNOWN`，绝不
因不明写结果重发 POST。

ProposalExecution 在 SearchPolicy admission 之前建立 transient
`proposal_id -> effective_submission_fingerprint` binding；同 batch 冲突使用
`PROPOSAL_ID_EXECUTION_COLLISION`，既有 Trajectory、checkpoint 或
Experiment-backed TrialLedger 证明的重绑定使用 `PROPOSAL_ID_REBIND`。SearchPolicy
只负责 allocation，不拥有 execution identity；BudgetAllocator 对 terminal proposal
key 的跨 arm reserve 也 fail closed。

Simulation 的远端终态与本地研究终态是两个有序边界：`Simulator` 先收到
`DONE/FAILED`，再等待 `on_complete` 将 terminal evidence 追加到 canonical
Trajectory，成功确认后才发送终态 checkpoint update。`on_complete` 失败会向上抛出，
保留已知远端身份但不写 terminal checkpoint；`UNKNOWN/SUBMIT_UNKNOWN` 仍按原有只读
对账规则处理。恢复时只按 `Experiment.id + same_execution_identity` 批量合并，不能
按 expression 猜测；Trajectory 的完整终态优先于稀疏 checkpoint，无法恢复且没有
`progress_url` 时返回 `TERMINAL_EVIDENCE_UNRECOVERABLE`，不得反思、奖励或提交。

自动 recovery 与手工 `finalize_recorded_round()` 共用同一 terminal projection：只有
execution set 与 canonical Trajectory 一致、每个终态 evidence durable、无 unresolved
且无 identity drift 时才能写 `complete=true`。`state audit` 对 cross-store 漂移只输出
round/count/opaque IDs，具体包括 `TERMINAL_CHECKPOINT_MISSING_CANONICAL_EVIDENCE`、
`CHECKPOINT_TRAJECTORY_IDENTITY_MISMATCH`、`DONE_CANONICAL_RESULT_EVIDENCE_INCOMPLETE`
和 `CHECKPOINT_STATUS_BEHIND_CANONICAL_TERMINAL`。

TrialLedger 对 proposal lifecycle 的 candidate、execution fingerprint、research role
和 arm 采用最早合法 committed/submitted identity；稀疏 terminal row 只能补充状态，
不能把历史试验迁移到另一个 arm。identity 或 arm drift 由 `state audit` fail closed。

## 变更规则

触碰 owner、proposal/schema、state merge 或安全边界时，必须增加行为/回归测试并更新本文件。公共文档只保留当前 contract；历史由 Git 承担，隐私规则见 [`PRIVACY.md`](PRIVACY.md)，研究方法见 [`RESEARCH_POLICY.md`](RESEARCH_POLICY.md)。
