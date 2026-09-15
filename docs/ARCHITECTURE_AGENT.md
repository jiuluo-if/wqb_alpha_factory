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

架构现代化后的纯领域投影位于 workflow 之前：`research_planning.py` 负责已解析输入的研究空间/假设组装，`evidence_projection.py` 负责严格质量与 correlation gate，`alpha_semantics.py` 负责字段语义 traits，`alpha_relationships.py` 负责字段关系与频率准入，`alpha_feasibility.py` 负责有界可行性诊断，`alpha_assembly.py` 负责候选到完整 proposal 的 canonical 投影，`optimization_screening.py` 负责 DONE evidence 的优化准入与 CHILD projection，`validation_proposals.py` 负责单变量 ROBUSTNESS projection，`execution_identity.py` 负责 transient durable binding projection，`terminal_evidence.py`/`execution_recovery.py` 负责终态证据和 checkpoint 合并，`proposal_admission.py` 负责执行身份准入与提案拒绝统计，`factory_session.py`/`factory_quota.py`/`factory_route.py` 负责工厂控制面投影。它们均不拥有状态、不调用远端写操作；旧 facade 只委托 canonical 函数。

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
| production Python files | 86 | 91 |
| production LOC | 30,253 | 30,340 |
| test Python files | 92 | 96 |
| test LOC | 24,303 | 24,543 |
| `Agent` LOC / methods | 1,651 / 79 | 1,650 / 79 |
| `AlphaFactory` LOC / methods | 1,801 / 31 | 1,141 / 31 |
| `ProposalExecutionWorkflow` LOC / methods | 1,365 / 29 | 1,356 / 29 |
| `AIFactoryRunner` LOC / methods | 1,948 / 56 | 1,754 / 55 |
| import cycles | 0 | 0 |

本阶段已完成 AlphaFactory feasibility、候选到 proposal、optimization screening 与 validation proposal projection 的 canonical 迁移，并删除 `wqb_agent/candidate.py`（20 行）及 11 个只导入不使用 `CandidateBuilder` 的测试 import。`AlphaFactory` 从 1,801 行收敛到 1,141 行；生成/选择仍由 factory 持有，完整审计 proposal record 与 partial-operator realizations 由 `alpha_assembly.assemble_factory_realizations()` 持有，DONE parent screening/CHILD 与 ROBUSTNESS 构造分别由 `optimization_screening.py`、`validation_proposals.py` 持有。ProposalExecution 已将 execution identity admission 委托给纯投影，并用 owner-generation signature 缓存可重建 binding；FactoryRunner 已将 session/quota/route 控制面拆出。

## 已完成的提取

- `alpha_factory.py` → `alpha_semantics.py`：字段语义 profile；`AlphaFactory` 通过 canonical alias 复用。
- `alpha_factory.py` → `alpha_relationships.py`：关系标签、频率 bucket/compatibility、关系类型。
- `alpha_factory.py` → `alpha_feasibility.py`：有界字段/模板可行性诊断与 fingerprint 投影。
- `alpha_factory.py` → `alpha_assembly.py`：字段机制说明、候选到完整 proposal record 的 canonical 组装与 partial-operator realizations。
- `alpha_factory.py` → `optimization_screening.py` / `validation_proposals.py`：DONE evidence screening、agent-authored CHILD 与单变量 ROBUSTNESS proposal projection。
- `agent.py` → `research_planning.py`：研究空间、best iteration、探索 seed 的纯组装。
- `agent.py` → `evidence_projection.py`：严格 correlation gate 与质量 rating。
- `proposal_execution.py` → `terminal_evidence.py`、`execution_recovery.py`：终态证据判定、失败分类、canonical recovery merge。
- `proposal_execution.py` → `proposal_admission.py`：execution identity admission 与有界拒绝原因统计。
- `proposal_execution.py` → `execution_identity.py`：按 owner generation 缓存、可重建的精确 binding projection。
- `factory_runner.py` → `factory_route.py`：route episode information-gain decision；runner 仍是兼容 facade。
- `factory_runner.py` → `factory_session.py` / `factory_quota.py`：session lifecycle、stop/status 与 quota projection；runner 仍是兼容 facade。
- `factory_runner.py` → `factory_blocker.py`：bounded blocker signature、privacy-safe probe 与 recheck projection；runner 保留 hook 调度。
- `runtime_components.py` → `AlphaFactory`：运行时直接持有 canonical factory，不再经过 `CandidateBuilder`。

## Remaining architecture debt

- `discovery.py`、`memory.py` 和 `factory_runner.py` 仍较大：它们同时承载现有持久化/兼容 consumer，下一刀需要先建立更细 owner contract，不能只按行数拆。
- `alpha_factory.py` 仍包含 template compatibility、candidate generation 与 optimization/validation proposal construction；后续只在 owner contract 明确时继续拆分，不复制 registry 或 prepared-facts owner。
- `proposal_execution.py` 仍包含 admission sequencing 和 manual recovery mutation；durable identity binding 已由 transient `ExecutionBindingIndex` 承担 projection，exactly-once fence 仍由既有 durable owners 与 workflow 共同维护。
- `discovery.py`、`memory.py`、`state.py`、`client.py` 与 `trial_ledger.py` 仍是有真实 consumer 的大模块；本阶段未凭行数删除它们。脚本审计未发现可安全删除的 ACTIVE/兼容入口。

## 第二阶段删除证据

| 对象 | 分类 | 证据与替代 |
|---|---|---|
| `wqb_agent/candidate.py` / `CandidateBuilder` | DEAD | 仓内 production、CLI、文档和动态导入均无 consumer；运行时改为 `RuntimeComponents.alpha_factory` |
| 11 个测试中的 `CandidateBuilder` import 与旧 `builder` shape assertion | DEAD | 仅旧架构形状测试使用，删除后 runtime composition contract 直接验证 AlphaFactory identity |
| `HighSignalValidator` 包级动态导出 | DEAD | 仓内无 import/文档/CLI consumer；验证器仍由 `validation.py` 内部使用 |
| `tests/test_security_hardening_batch.py` | ACTIVE | 原子写、Trajectory integrity、same-origin、PBO 与 packaging 各有独立安全覆盖，当前文件仍保留未重复的行为测试 |
| `scripts/reconcile_pending.py`、`scripts/refresh_self_correlation.py`、`scripts/benchmark_local_io.py` | ACTIVE | 分别是只读恢复、只读 SELF_CORRELATION 回填和离线性能入口，均在 README/CLI/测试中有 consumer |

`RuntimeComponents` 只创建一次 memory、trajectory、ledger、discovery、simulator、checkpoint 和 cache；workflow 不反向导入 Agent，不重新创建这些 owner。

## Owner 与写路径

| 能力 | 唯一 owner | 边界 |
|---|---|---|
| BRAIN transport | `client.py` | 认证、Retry-After、分类错误、只读 GET 与 Simulation submit |
| Simulation execution | `ProposalExecutionWorkflow` → `Simulator` | checkpoint、预算、exactly-once、`SUBMIT_UNKNOWN` |
| Research evidence | `Trajectory` / `TrialLedger` | append-only 事实与生命周期审计 |
| TrialLedger append index | `TrialLedger` transient SQLite | owner-local exact event-id/completeness membership，可从 `trial_ledger.jsonl` 重建；SQLite 文件临时 disposable，JSONL 仍是唯一 durable truth |
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

## Phase III Research Kernel Decomposition（2026-09-15）

本阶段把纯策略与 owner 分开，但不新增 state、proposal 或 workflow 抽象。
`FieldDiscovery` 仍独占 client 读取、catalog/cache 生命周期和持久化；纯字段证据与
评分分别位于 `field_metadata.py`、`discovery_selection.py`。`ExperienceMemory` 仍
独占 experience/garbage 文件、tier mutation、compress 和 save；纯 durable-view
编解码位于 `memory_codec.py`。`Reflector` 仍独占 round effects 与单次 save；质量门和
诊断位于 `reflection_evaluation.py`。`OptimizerWorkflow` 仍独占 trajectory/cloud
metadata acquisition 与编排；parent evidence gate 位于 `optimizer_selection.py`。
`WQBClient` 仍独占 Session、认证、限流和 endpoint；同源 progress URL 与退避计算位于
`client_transport.py`。`Trajectory`、checkpoint exactly-once、SUBMIT_UNKNOWN 和
known progress URL GET-only contract 未改变。

基线（Phase II 收口后 `f7735a9`）为 92 个 production Python 文件 / 30340 行、96 个
测试文件 / 24543 行；当前收口为 101 个 production Python 文件 / 30395 行、96 个
测试文件 / 24644 行。重点 owner 当前指标为：`discovery.py` 1159 行（31 函数，1 类），
`memory.py` 1101 行（48 函数，2 类），`reflection.py` 844 行（26 函数，1 类），
`optimizer_workflow.py` 1034 行（33 函数，3 类），`state.py` 977 行、`client.py` 945
行。新增纯模块合计 551 行、34 函数、0 类；相对基线删除旧实现 809 行、增加显式
边界与契约代码 747 行，净变化包含本轮 architecture tests 与文档/映射开销。

本阶段未对 `state.py` 做风险性 wholesale 拆分，也未宣称未建立的性能收益；仅完成离线
AST/LOC 结构计量，并为 Discovery 的 request-local coverage 复用增加了调用计数回归断言。Phase III 影响范围的定向映射测试共 344 个通过；最终 Ruff、九个
typed frontier 的 mypy、`state doctor`、`state audit` 和 repository privacy check
均通过。doctor 对 fixture 的 `LEDGER_MISSING` 与 capability unavailable 只报告既有
WARN，未触碰真实研究状态。所有阶段提交均使用 `2966684515@qq.com` 并推送到
`origin/main`。

## Architecture Modernization Phase IV（2026-09-15）

本阶段从 baseline `2b3e0fb` 继续，完成不改变研究语义的局部收缩：`research_catalog.py` 成为
fallback hypothesis 的纯目录 owner；`proposal_inbox.py` 与 `execution_plan.py` 分离提案解析和
checkpoint disposition；`factory_control.py` 成为工厂控制词汇 owner；`proposal_schema.py` 成为
schema vocabulary owner，而 `proposal_contract.py` 保留兼容 facade re-export。

`ProposalExecutionWorkflow` 保持唯一 Simulation 编排路径，恢复、终态结算、`SUBMIT_UNKNOWN`
和 known `progress_url` GET-only 语义不变。`TrialLedger` 删除 Python `self._event_ids` 长历史集合，
改为 owner-local exact SQLite membership projection：JSONL 先 append+fsync，索引失败只使 projection
invalid 并触发后续 rebuild，不重写第二条 durable event；启动、文件签名变化和外部 owner append 都会重建。

当前静态计量为 `Agent` 1,577 行/78 defs、`ProposalExecutionWorkflow` 1,330 行/24 defs、
`ProposalExecutionHooks` 19 callbacks、`AIFactoryRunner` 1,547 行/48 defs、
`proposal_contract.py` 45 行/0 defs、`proposal_batch.py` 236 行/5 defs、
`proposal_validation.py` 344 行/10 defs。当前 production 为 109 个 Python 文件/30,565 行，
tests 为 99 个 Python 文件/24,810 行。相对本阶段早期 snapshot，已删除 3 个执行 hooks、
3 个 checkpoint forwarding wrappers、3 个 FactoryRunner probe wrappers，并移除 Agent 的
candidate-rejection forwarding method。离线基准命令为
`python scripts/benchmark_local_io.py --rows 10000,100000,500000 --workloads trial_ledger_startup,trial_ledger_append --repeat 1`：

| rows | startup rebuild ms | append ms | startup/append Python peak KB |
|---:|---:|---:|---:|
| 10,000 | 111.803 | 7.783 | 57.8 / 20.3 |
| 100,000 | 1,166.758 | 7.809 | 61.6 / 20.7 |
| 500,000 | 5,588.456 | 9.959 | 60.3 / 19.7 |

兼容面按当前 consumer 保留并分类如下：

| surface | classification | 处理 |
|---|---|---|
| `Agent.run_suggestion_round()` / `run_proposals()` | `PUBLIC_RETIRED_GUARD` | 保留 facade，真实编排下沉到 workflow |
| `AIFactoryRunner` 公共入口 | `PUBLIC_REQUIRED` | 保留外部 factory 控制面入口，纯控制/route/probe 投影下沉 |
| `proposal_contract` 导出名称 | `PUBLIC_REQUIRED` | 保留薄 facade，canonical 实现位于 schema/batch/validation |
| 已删除的 private probe/checkpoint forwarding wrappers | `DEAD` | 无 runtime consumer，测试已迁移至 canonical owner |
| Agent → workflow operation hooks | `INTERNAL_COMPAT` | 仅保留 Agent-owned state/evidence/output 边界，禁止任意 Agent 逃生通道 |

当前验证证据：本次跨 owner targeted lane `Ran 304 tests ... OK`，Ruff 与 py_compile 均通过；此前
proposal/factory/architecture lane 262 tests 也通过。TrialLedger 基准为单次离线样本，不能替代
多轮稳定性比较。

## Architecture Modernization Phase V（2026-09-15）

本阶段停止按 LOC 继续拆分大模块，改为收紧统计、搜索、安装态、安全和可复现性边界：

| owner | baseline LOC | current LOC | result |
|---|---:|---:|---|
| `validation_report.py` | 628 | 429 | 纯 PSR/DSR/PBO 实现迁至 `validation_statistics.py`（171 LOC）；旧导入路径保留 |
| `search_policy.py` | 571 | 381 | `BudgetAllocator`/lifecycle 留在原 owner；证据迁至 `search_evidence.py`（186 LOC） |
| `search_snapshot.py` | 139 | 139 | 不再依赖 mutable `BudgetAllocator`，直接使用 `search_evidence` |
| `research_api.py` | 639 | 639 | 默认 operator reference 改为 package resource；显式 path 兼容保留 |
| `credentials.py` | 147 | 167 | POSIX 同 descriptor 验证 regular/private/non-symlink；Windows 跳过 chmod 语义并依赖 ACL |
| `workspace_snapshot.py` | 686 | 695 | 保持单一 read-only owner，加入 request-local `read_passes` instrumentation |

`research_api.inspect_state()` 由 `Trajectory.load()` + 第二次完整扫描改为一次 bounded streaming pass，
保留原始 row count 与 bounded canonical recent records。clean-wheel smoke 在脱离仓库 cwd 的临时
解压环境读取 `wqb_agent.reference/OPERATORS_CHEATSHEET.md` 成功，无 credential/BRAIN 依赖。
read-pass instrumentation 的当前证据为：trajectory `2 → 1`，trial ledger `1 → 1`，validation
reports `1 → 1`，checkpoint scan `1 → 1`；snapshot 仍不建立 durable cache，且没有把完整历史
加载进 Python object graph。

CI reproducibility 使用 `constraints/ci-py311.txt` 约束当前 runtime/dev/perf direct dependencies；
dependency drift test 会对 `pyproject.toml` 的 direct entries fail closed。Actions 从 2 个 floating
major refs 收紧为 0 个：checkout `v4.2.2` 使用已验证 SHA
`11bd71901bbe5b1630ceea73d27597364c9af683`，setup-python `v5.6.0` 使用已验证 SHA
`a26af69be951a213d495a4c3e4e4022e16d87065`。CI 不新增 coverage gate，默认仍为 explicit targeted
Fast Lane + syntax/Ruff/typed frontier/offline doctor/audit/privacy。

Phase V architecture lane 增加统计内核、搜索证据、snapshot 反向依赖、installed facade 与 dependency
constraint contracts；本阶段未改变 PSR/DSR/PBO、UCB/novelty、credential source priority、workspace
state ownership 或任一冻结 Simulation/recovery contract。当前真实剩余债务仅包括：CI 新约束与 Action
pin 已由当前 SHA run 193 验证成功；多轮性能 benchmark 不因本轮未触碰的路径重复运行。

精准 cleanup：删除 `validation_report.py` 中 7 个统计实现、`search_policy.py` 中 10 个纯证据实现
及无 consumer 的 `subtree_fingerprints()`；生产 imports 改指向 canonical owners，未新增 state、
workflow、manager 或 dependency framework。Phase V architecture contracts 报告 0 个 import cycle
和 0 个 forbidden dependency violation。

## 变更规则

触碰 owner、proposal/schema、state merge 或安全边界时，必须增加行为/回归测试并更新本文件。公共文档只保留当前 contract；历史由 Git 承担，隐私规则见 [`PRIVACY.md`](PRIVACY.md)，研究方法见 [`RESEARCH_POLICY.md`](RESEARCH_POLICY.md)。
