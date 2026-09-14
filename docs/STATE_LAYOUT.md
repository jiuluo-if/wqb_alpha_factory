# `.wqb_state` 状态与记忆布局

> `.wqb_state` 是程序维护的运行状态目录。文件名是接口的一部分；本文件负责分类和阅读顺序，不授权手工移动、改名或重写状态文件。

## 分类总览

| 类别 | 固定路径/模式 | 内容与权威性 | 处理规则 |
|---|---|---|---|
| 运行记忆 | `context.md`、`experience.json`、`garbage.json` | 可选的 Agent 进程内决策视图；默认运行不落盘 | 新进程不从本地结果恢复 |
| 原始证据 | `trajectory.jsonl`（Trajectory owner 的 append-only canonical 证据）；`trial_ledger.jsonl` 是 durable lifecycle/accounting owner | 已确认 Experiment 的研究证据：metrics、checks、field audit、hypothesis、economic mechanism；同一 `id` 可有 canonical 首行 + `RESEARCH_SETTLED` 结算 revision | `trajectory.jsonl` 由既有 owner 追加 revision 并在新进程只读 rehydrate（latest valid revision wins）；ledger selection/settlement 由同一 state owner scope 保护；远程未完成状态只由 checkpoint 恢复 |
| 执行恢复 | `round_*.checkpoint.json`、`run.lock`、POSIX `run.lock.guard` | 提交状态、progress URL、锁和崩溃恢复依据 | `run.lock`/OS mutex 是整个 `state_dir` 的唯一 root owner；POSIX `.guard` 是 `flock` 同步原语，不是 research/result sidecar。Simulation worker 只能使用当前 root owner 显式创建的、绑定 `state_dir` 与 owner 生命周期的内存 delegation；owner 释放后 delegation 立即失效。OS owner 存活或存在未完成 checkpoint 时禁止新轮和移动 |
| 当前工作项 | `suggestions.json`、`proposals.json` | 当前 discovery 证据包与待执行提案；长时工厂复用同一 inbox | 只由规定流程生成/审阅；逻辑内容不变不重写；targeted inbox 的 read/check/write 在同一 owner transaction 内完成 |
| 工厂控制面 | `factory_session.json` | Factory session 的 deadline、最近动作、`stop_requested`、本地配额、bounded retry/route counters、blocker 和 opaque route aggregate | 固定单文件且是 Factory quota 的唯一 owner；quota lifecycle 按纽约本地日/ISO week 连续，session renewal 不等于 quota renewal；只保存 bounded control-plane projection、counts 和 session-bound set digest，不保存 Alpha/field/expression/template/dataset/research/metrics payload；blocker 仅为去私有化 control-plane projection，不创建第二 state store；阶段配额为每周 11200、每日 1600（纽约本地日刷新）；`factory status` 只读，`factory stop` 原子请求安全停止；不按轮次复制 session/log |
| TrialLedger | `.wqb_state/trial_ledger.jsonl` | 唯一 durable append-only lifecycle、optimization selection 与 settlement accounting owner | 本地 private research state；所有 durable append 由 root owner thread 或显式 delegated Simulation worker 执行；首次真实 accounting mutation 时才记录 `COMPLETE_FROM_START` 或 `INCOMPLETE_LEGACY` 边界；构造和只读 inspection 不写盘，不从 Trajectory 伪造遗漏 selection |
| Trajectory / ExperienceMemory | `trajectory.jsonl` / existing memory owner | Trajectory 只拥有 executed Experiment evidence；ExperienceMemory 只保留派生 decision/learning view；同一结算回放通过稳定 `source_key` 幂等 | settlement revision 通过同一 Experiment identity；selection fact 不再写入 ExperienceMemory；source 冲突由 `MEMORY_SETTLEMENT_CONFLICT` 审计，不新增 state owner |
| 当日结果视图 | 进程内 `DailyResearchCache` | 当前进程内的模拟结果、Alpha 和颜色视图 | 跨纽约本地日自动清空；不写研究状态 |
| 滚动 7 日 Alpha 元数据缓存 | `.alpha_feed_cache/weekly.json` | 远端提交/模拟 Alpha 的轻量 ID、状态、时间戳，按纽约本地日分桶 | 保留当前工作日前推 7 个自然日；`updated_at`/`expires_at`；模拟元数据上限 `11200（7*1600）`；每次同步清理窗口外和过期资源 |
| 结果/提交侧车 | 不再生成 `sims_results.json`、`submission_pool.json`、`evidence_cache.json`、颜色 evidence | 模拟结果、提交证据和颜色判定的临时视图 | 只在当日进程内存中存在；远端 Alpha 轻量元数据另按滚动 7 日缓存保存 |
| 发现缓存 | `fields_cache.json`、`platform_field_catalog_YYYYMMDD/` | 按纽约本地日固化的多数据集字段目录与字段快照；生产发现会只读刷新平台 `alphaCount` 做字段查重 | 只保存字段元数据、查询范围、哈希和平台使用量状态，不保存模拟/Alpha 结果；缺失平台计数在严格模式下不准入 |
| 平台审计快照 | `active_alphas_YYYYMMDD.json`、`new_active_details_YYYYMMDD.json` | ACTIVE Alpha 辅助 provenance | 只作审计背景；平台当前响应优先 |
| 隔离区 | `quarantine/` | 明确隔离的异常、备份或不可直接使用材料；例如 `quarantine/submission_pool_history/`、`quarantine/duplicate_round_summaries/` | 不得自动回流生产链 |
| 当前 checkpoint | `round_*.checkpoint.json` | 未完成远程任务的 progress URL、身份和最小恢复元数据 | 这是唯一结果恢复边界；不得手工覆盖 |

自主 factory 的优化层/探索层只写入当前 `proposals.json` 的审计字段和 `factory_batch_stats`，不新增状态文件；云端优先级仍来自 `.alpha_feed_cache/weekly.json` 的轻量 ID/时间戳，优化证据仍必须来自当前进程的完成记录和后续 live API。

## 四种不可混用的身份

| 身份 | 权威 owner | 用途 |
|---|---|---|
| candidate identity | proposal / TrialLedger candidate event | 表示一次候选生成或拒绝，不代表已提交 Simulation |
| proposal identity | `proposal_id` 与现有 TrialLedger lifecycle | 表示提案生命周期与 arm accounting |
| Simulation execution identity | `submission_fingerprint(expression, settings)` | 表示一次远程 Simulation 写入；未完成 checkpoint 跨轮次、跨进程禁止再次 POST |
| Experiment identity | `Experiment.id` 与 `trajectory.jsonl` | 表示同一次执行的 durable evidence 及其 `RESEARCH_SETTLED` revision |

`force-new-round` 只解除旧轮次的编排阻塞，不能解除未决 Simulation execution identity；
同一表达式但不同 settings 的 fingerprint 不同，不能把它们按表达式折叠。`state audit`
以持久化 Trajectory、TrialLedger 和 checkpoint 做只读 parity 检查，不把任一身份推断成另一身份。

checkpoint 的 recovery envelope 与 `state.IDENTITY_FIELDS` 机械共用字段声明，另保留
必要的 template/operator provenance 与 `progress_url`。legacy row 缺少新增 provenance
时只允许 tolerant read 和已知 URL 的只读 reconcile；不能由 expression 猜 candidate、
dataset 或 parent。存在的 `submission_fingerprint` 若与完整 payload 不一致属于
`CHECKPOINT_SUBMISSION_IDENTITY_MISMATCH`，不能静默重算；malformed、unreadable 或
future-schema 的 foreign checkpoint 即使 `force-new-round` 也不允许新写入。

Checkpoint `complete` 必须是 JSON bool；合法 Experiment status 复用
`UNRESOLVED_STATUSES ∪ TERMINAL_STATUSES`。每个 row 的 round 必须等于文件
`round_no`，有 top-level hypothesis id 时必须一致。一个 checkpoint 内的 Experiment
id、重算 submission fingerprint、非空 proposal id 不能重复；progress URL 不能指向
多个 execution identity。`complete=true` 携带 unresolved status 会被标为
`CHECKPOINT_COMPLETE_WITH_UNRESOLVED_EXECUTION`，不会释放 execution fence。

Checkpoint 写入和读取共用 `CheckpointStore._validate_with_code()`；写入端拒绝
`"false"`、`"true"`、`0/1`、`None` 等非 JSON bool，并在 atomic write 前验证最终
recovery-only projection。raw JSON 的 `complete=true` 不是归档授权；archive 只消费
`CheckpointStore.scan()` 的 `malformed == False` 且 `complete is True` 结果。不可验证、
未完成或 future-schema checkpoint 永远留在 live state，`--apply` 遇到不可验证对象时
不移动任何 checkpoint。

`reconcile_pending.py` 是 `READ_ONLY=YES` 的远端观测工具：target 只来自 canonical
Trajectory rows 与 validated incomplete checkpoint rows，身份使用 Experiment id、
proposal id、submission fingerprint 和已知 progress URL，不以 expression 猜配对。
它可以写派生 reconcile report/history，但不写 Trajectory、Checkpoint、TrialLedger 或
ExperienceMemory；`--commit` 已退役，canonical recovery 仍由 `run-proposals` owner 完成。

Trajectory canonical projection 还为 `state audit` 提供历史 remote-execution 检测：
多个 Experiment IDs 共享非空 fingerprint 且共享 progress URL，或共享 fingerprint
且共享 alpha ID，报告 `DUPLICATE_REMOTE_EXECUTION_PROJECTION`。这是只读人工审核信号，
不会删除、合并或重写历史状态。

远端 execution dedupe 使用当前 batch 内的 `batch_execution_fingerprints`，输入是
`Agent._proposal_settings()` 产出的完整 effective settings。research expression 去重、
SearchPolicy arm 和 execution dedupe 是三种不同判断；`DUPLICATE_EFFECTIVE_EXECUTION`
在 SearchPolicy 关闭时仍然生效。

写入 Simulation 前，ProposalExecution 建立 transient proposal binding；同 batch
的同 proposal_id 多 fingerprint 使用 `PROPOSAL_ID_EXECUTION_COLLISION`，durable
历史已证明的不同 fingerprint 使用 `PROPOSAL_ID_REBIND`。该检查发生在
`SearchPolicy.accept()` 之前；BudgetAllocator 对 terminal proposal key 的不同 arm
也拒绝 reserve，不把旧 lifecycle 静默迁移到新 arm。

Optimizer child 的 `parent_id` 是 Experiment identity，`parent_expression` 只用于
一致性核验；同表达式多个 DONE parent 时，legacy expression-only 引用保持
`PARENT_REFERENCE_AMBIGUOUS`。TrialLedger 的 lifecycle arm 取最早合法 committed/submitted
identity，terminal sparse metadata 不会把试验移到 unknown arm。

`factory_session.json` 的 route probe 只保留每个比较维度的 bounded count 与
带 `session_id + dimension` domain separation 的 SHA-256 set digest；相同 session
内的集合比较仍保留 route decision 语义，session renewal 不产生稳定的跨 session
研究 fingerprint。rich feasibility、budget audit、proposal、checkpoint、Trajectory
和 TrialLedger payload 继续归其既有 owner，不通过 session projection 搬迁或复制。

durable projection 是 live orchestrator session 的 bounded copy：`_save_session()`
只校验 internal control vocabulary、合并并发 stop bit、投影并原子写盘，不清空或
改写 caller 持有的 runtime object；`factory run` 返回值在边界处单独投影。session
status/action 使用 closed set，新增 runtime transition 未登记时 fail closed，旧会话
的未知值仅允许在 read/status inspection 中降级为 `UNKNOWN`。

## 记忆阅读顺序

1. 当前进程内 Agent 记忆和日缓存：只用于本轮决策，不作为事实源。
2. checkpoint：恢复任务、核对远程状态和 exactly-once 边界。
3. BRAIN live response：模拟与 Alpha 的真实当前证据。

## 冲突仲裁与整理边界

- 未完成传输状态以 checkpoint 为准；已确认实验事实以 BRAIN live response 为准；canonical 完成证据由 `Trajectory` 追加到 `trajectory.jsonl` 并在新进程只读 rehydrate，派生的结果/提交/颜色侧车仍只存在进程内视图；当周 Alpha 元数据仅按本表缓存规则落盘。
- `context.md` 与 `experience.json` 是压缩决策视图，不得反向覆盖原始证据。
- Experiment 的 `yearly_evidence` 是由已知 Alpha 的 aggregates 派生的年度稳定性证据；缺失或 `UNKNOWN` 不得解释为稳定通过。
- Experiment 的 `validation_plan`/`validation_report` 记录预注册 robustness 变量与聚合判定；只有 report `PASS` 的 parent 才能为 `STABLE`、进入 `current_best` 或提交池。
- validation report 只在当前进程中参与判断，不作为本地历史结果留存。
- `SELF_CORRELATION` 缺失、`PENDING` 或未完成时只能标记 `RECONCILE`，不得升级为 `PROMOTE`。
- 任何历史状态快照、`quarantine/` 内容和本地字段目录都不能冒充当前 BRAIN API 响应。
- 字段选择必须使用可复现种子做多数据集分层抽样；`dataset_selection` 中的池、顺序、选中数量和拒绝原因是审计证据，不能用一个数据集的字段数量冒充多数据集覆盖。
- 允许新增审计说明或外部报告；禁止手工移动/重命名 canonical 文件、删除运行锁、覆盖状态文件，或把备份直接放回生产路径。
- 需要归档的历史材料移至 `docs/archive/`，按对象类别分类；不要在 `.wqb_state` 内复制一份“整理版”状态。
- 非 canonical 的状态备份可移入已有 `quarantine/<category>/` 子目录；保留原文件名和内容，并在本文件或审计记录中说明来源。
- 轮次归档使用 `python scripts/archive_completed_rounds.py` 先 dry-run，再经确认加 `--apply`；默认保留最近 10 个摘要。
# Feasibility probe scope

Feasibility diagnostics are control-plane audit metadata only. They contain counts, evidence provenance, and failure taxonomy; they do not expand checkpoint, trajectory, ledger, Alpha Feed, or metrics state.
# Route and handoff metadata

`factory_session.json` may contain bounded route decision metadata and the latest feasibility probe summary. These fields are control-plane observations; they do not contain metrics, checks, Alpha payloads or checkpoint recovery data. Optimizer handoff counters are likewise diagnostic and do not establish a second research-state store.

Discovery profiles carry the bounded `frequency_evidence` that was established
by the current discovery round. Feasibility and proposal preflight consume that
nested evidence; the profile's top-level `frequency` is not permission to
relabel an inferred value as `EXPLICIT_PLATFORM`. Dataset-description fallback
is derived only from the single live dataset-listing snapshot for that round;
an unavailable listing remains `UNKNOWN` and never reuses a prior round.

# Feed freshness and heartbeat

The existing `.alpha_feed_cache/weekly.json` remains the only Feed cache. Its `updated_at`/`expires_at` support a read-only freshness view; refresh attempts and failures are transient runtime metadata and do not replace the last successful timestamp. Heartbeat events are process-local and transient: no `heartbeat.jsonl`, metrics sidecar, checkpoint field, trajectory row, quota record, or separate scheduler is created.


# ResearchYield projection

ResearchYield adds no new file and no new state owner. It is a derived
in-memory / report projection over existing evidence (checkpoint experiments,
SearchOutcome, optimizer handoff diagnostics, incremental verdicts). If route
policy ever needs cross-restart control metadata, only a minimal per-family
row may be stored in the existing `factory_session.json` envelope:
`mechanism_family`, `evaluated_count`, `outcome_state`, `stop_reason`.
SearchOutcome lists, metrics, checks bodies, Simulation payloads and reward
history replicas are forbidden in any ResearchYield persistence. Lightweight
cloud metadata (e.g. Alpha Feed rows without expression/template/fields) is
filtered out at funnel construction and can never become yield evidence.

# Settled evidence revisions

`trajectory.jsonl` stays the only canonical completed-Experiment evidence owner.
One Experiment may now occupy more than one append-only row: the first canonical
append plus later `trajectory_revision="RESEARCH_SETTLED"` settlement revisions
written by `Trajectory.settle()` / `settle_many()`. Reads merge them
latest-valid-revision-wins; identity mismatches and corrupt rows are skipped.

- `revision != second execution`, `revision != second store`. No
  `final_evidence.json`, `settled_experiments.json`, `optimizer_history.json`,
  `research_result_store.json` or `final_outcome_cache` is created, and the
  checkpoint is still not a metrics store.
- Execution identity (id / expression / settings / fields / proposal /
  submission fingerprint / lineage) is immutable across revisions; refusal is
  fail-closed and audited as `SETTLEMENT_REVISION_REJECTED`.
- Settlement revisions never touch the Alpha Feed cache, color metadata, quotas
  or remote state; they add no scheduler and no second research-state owner.

# Terminal evidence ordering and recovery

Remote `DONE/FAILED` is transport evidence, not yet a locally settled result.
The ordering is `remote terminal -> on_complete/canonical Trajectory append ->
acknowledgement -> terminal checkpoint update -> round finalization`. A failed
canonical append leaves the remote job known and the checkpoint unresolved; it
never rewrites the remote result as `UNKNOWN` and never closes the checkpoint.

Recovery joins checkpoint rows to canonical rows by exact `Experiment.id` and
`same_execution_identity` in one batch lookup. A full canonical terminal row
replaces a sparse checkpoint row without GET/POST. A terminal row with no
canonical evidence may only poll its existing `progress_url`; without that URL
recovery is `TERMINAL_EVIDENCE_UNRECOVERABLE`. Sparse terminal rows cannot enter
reflection, SearchPolicy reward accounting, SubmissionPool, robustness or
promotion. Workspace audit emits only bounded opaque IDs for cross-store drift.
