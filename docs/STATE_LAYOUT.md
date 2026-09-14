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
| Trajectory / ExperienceMemory | `trajectory.jsonl` / existing memory owner | Trajectory 只拥有 executed Experiment evidence；ExperienceMemory 只保留派生 decision/learning view | settlement revision 通过同一 Experiment identity；selection fact 不再写入 ExperienceMemory |
| 当日结果视图 | 进程内 `DailyResearchCache` | 当前进程内的模拟结果、Alpha 和颜色视图 | 跨纽约本地日自动清空；不写研究状态 |
| 滚动 7 日 Alpha 元数据缓存 | `.alpha_feed_cache/weekly.json` | 远端提交/模拟 Alpha 的轻量 ID、状态、时间戳，按纽约本地日分桶 | 保留当前工作日前推 7 个自然日；`updated_at`/`expires_at`；模拟元数据上限 `11200（7*1600）`；每次同步清理窗口外和过期资源 |
| 结果/提交侧车 | 不再生成 `sims_results.json`、`submission_pool.json`、`evidence_cache.json`、颜色 evidence | 模拟结果、提交证据和颜色判定的临时视图 | 只在当日进程内存中存在；远端 Alpha 轻量元数据另按滚动 7 日缓存保存 |
| 发现缓存 | `fields_cache.json`、`platform_field_catalog_YYYYMMDD/` | 按纽约本地日固化的多数据集字段目录与字段快照；生产发现会只读刷新平台 `alphaCount` 做字段查重 | 只保存字段元数据、查询范围、哈希和平台使用量状态，不保存模拟/Alpha 结果；缺失平台计数在严格模式下不准入 |
| 平台审计快照 | `active_alphas_YYYYMMDD.json`、`new_active_details_YYYYMMDD.json` | ACTIVE Alpha 辅助 provenance | 只作审计背景；平台当前响应优先 |
| 隔离区 | `quarantine/` | 明确隔离的异常、备份或不可直接使用材料；例如 `quarantine/submission_pool_history/`、`quarantine/duplicate_round_summaries/` | 不得自动回流生产链 |
| 当前 checkpoint | `round_*.checkpoint.json` | 未完成远程任务的 progress URL、身份和最小恢复元数据 | 这是唯一结果恢复边界；不得手工覆盖 |

自主 factory 的优化层/探索层只写入当前 `proposals.json` 的审计字段和 `factory_batch_stats`，不新增状态文件；云端优先级仍来自 `.alpha_feed_cache/weekly.json` 的轻量 ID/时间戳，优化证据仍必须来自当前进程的完成记录和后续 live API。

`factory_session.json` 的 route probe 只保留每个比较维度的 bounded count 与
带 `session_id + dimension` domain separation 的 SHA-256 set digest；相同 session
内的集合比较仍保留 route decision 语义，session renewal 不产生稳定的跨 session
研究 fingerprint。rich feasibility、budget audit、proposal、checkpoint、Trajectory
和 TrialLedger payload 继续归其既有 owner，不通过 session projection 搬迁或复制。

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
