# Current Alpha Factory architecture

本文只描述当前系统，不记录历史 phase、迁移过程、LOC 或 commit 报告。

## 1. Truth hierarchy

1. BRAIN live response 是平台 datasets、fields、operators、Simulation、metrics、checks 与 Alpha metadata 的事实源。
2. `Trajectory` 是 executed Experiment evidence 的 canonical owner。
3. `TrialLedger` 是 lifecycle、selection 和 settlement accounting 的 canonical owner。
4. `CheckpointStore` 是未完成远程任务的 recovery boundary。
5. `ExperienceMemory`、daily/weekly cache、audit 和 quality/context 都是 bounded projections，不能覆盖 canonical facts。

缺失或未知证据保持 `UNKNOWN`/`UNAVAILABLE`；`SUBMIT_UNKNOWN` 不重 POST；Alpha submission 始终手工完成。

## 2. Two-loop architecture

```text
Outer Control / Maintenance Agent
  ├─ inspect runtime and bounded context
  ├─ validate cursor and materialize canonical proposals
  └─ execute/recover Agent.run_proposals when authorized
       ↓
Inner Research Agent
  └─ author hypothesis, ExperimentSpec, OptimizationDecision and falsification
       ↓
execution round (round_no)
  → ProposalExecutionWorkflow → Simulator → WQBClient
  → Trajectory + TrialLedger + validation + settlement
  → ResearchQualityAssessment → new research_cursor
```

`research_cycle_id` 表示一次 Inner decision，`round_no` 表示一次 Outer execution。一个 cycle 可以映射多个 rounds；recovery 仍复用同一个 round。两者不共用 counter。

## 3. Owner graph

```text
AppConfig → AgentRuntimePolicy → RuntimeComponents → AgentWorkflows
                                                   ├─ SuggestionWorkflow
                                                   ├─ ProposalExecutionWorkflow
                                                   ├─ AlphaFeedWorkflow
                                                   └─ OptimizerWorkflow
```

`research_cursor.py`、`research_quality.py`、`research_context.py` 是无状态纯 projection；`research_api.py` 是唯一 agent-facing facade。它们不创建 Client、Simulator、Trajectory、TrialLedger、Checkpoint 或第二 state model。

## 4. Write-path matrix

| Operation | Owner | Remote write |
|---|---|---:|
| discovery/context/quality/cursor | existing readers + pure projections | no |
| proposal validation/materialization | OptimizerWorkflow + existing proposals inbox | no |
| Simulation submission/recovery | Agent.run_proposals → ProposalExecutionWorkflow → Simulator → WQBClient | yes |
| lifecycle/settlement accounting | TrialLedger | no |
| executed evidence revision | Trajectory | no |
| factory stop/status | factory-session owner | no |
| Alpha submission | user/BRAIN | manual |

## 5. Identity model

- candidate identity：候选生成/拒绝。
- proposal identity：`proposal_id` lifecycle。
- execution identity：`submission_fingerprint(canonical expression, complete effective settings)`。
- Experiment identity：`Experiment.id` 与 Trajectory revision。
- research cycle identity：`source_research_cursor + sorted decision semantic IDs` 的 opaque digest。

Cycle provenance 可进入 proposal、Experiment、Trajectory、TrialLedger、settlement 与 quality projection，但不能进入 Simulation fingerprint。相同 execution fingerprint 被不同 cycle 引用时不得产生第二次 POST。

## 6. Cursor and cycle

`build_research_cursor()` 从已 settled Experiment ID/settlement ID、TrialLedger history-completeness/effective denominator、未完成 checkpoint identity、active targeted batch digest 和 runtime state 派生 opaque SHA-256。它不包含 expression、Alpha ID、metrics、凭据或时间戳。

相同 canonical evidence 必须得到相同 cursor；新增 final settlement 改变 cursor；cache/timestamp refresh 不改变 cursor。`INCOMPLETE_LEGACY` 的 denominator 保持 `UNAVAILABLE`。没有 cycle provenance 的 legacy Experiment 可读，但 cycle projection 标记 `LEGACY_RESEARCH_CYCLE_UNVERIFIABLE`。

## 7. Quality projection

`ResearchQualityAssessment` 只描述 evidence，包含 execution status、finality（`UNRESOLVED`/`PROVISIONAL`/`FINAL`）、completeness、各 evidence dimension、classification、eligibility、TrialLedger denominator、missing evidence 和 blockers。它调用已有 report/metrics/research reducers，不重定义 Sharpe、Fitness、checks 或 validation threshold，也不提出研究方向。

`UNKNOWN`、`SUBMIT_UNKNOWN`、运行中状态和 infrastructure failure 不能被质量 projection 改写为研究 `FAIL`。未完成 validation 不产生 final settlement side effect。

## 8. Facade boundary

只读入口包括 `inspect_runtime_context`、`inspect_research_context`、`inspect_execution_round`、`inspect_research_cycle`、`inspect_pending_work`、`inspect_trial_accounting`、`inspect_factory_status`、三类 `assess_*`、discovery、operator reference 和 `reconcile`。

受控入口包括 `materialize_targeted_batch`、`execute_pending_round`、`resume_pending_round` 和 `request_factory_stop`。materialization 校验 `expected_research_cursor`，过期上下文返回 `RESEARCH_CONTEXT_STALE` 且不覆盖 inbox；执行入口只能调用 `Agent.run_proposals`。`research_tool_manifest` 声明模式、owner、remote write 和 readiness 要求。

## 9. Recovery and safety invariants

POST 前先固化 identity/checkpoint；未知 POST 结果保持 `SUBMIT_UNKNOWN`；已知 progress URL 只 GET/poll。checkpoint、Trajectory、TrialLedger、ExperienceMemory 和 Alpha feed 各自只有一个 owner。factory session 只保存 bounded control metadata；research cycle、cursor、quality、context 都是 derived projection，不生成 durable sidecar。

Outer 默认 `MAINTENANCE`，`REAL_SIMULATION_RUN=NO`；只有用户明确授权才进入 `RESEARCH_ORCHESTRATION`。Inner 不能执行 Simulation、修改 raw state 或 factory session；Outer 不能创造经济 hypothesis、自动调参或自动 Alpha submission。

## 10. Reading and verification

阅读顺序：根 [`AGENTS.md`](../AGENTS.md) → 本文 → [`research_api.py`](../wqb_agent/research_api.py) → 当前 owner 与直接测试。Local 先执行 explicit targeted mapping；CI 依次执行 compileall、typed frontier、Ruff、Targeted Fast Lane、whole-repository final gate、offline doctor/audit/privacy。
