# Alpha Factory

Alpha Factory 是面向 AI Agents 的 WorldQuant BRAIN 研究仪器。Inner Agent 负责经济假设、ExperimentSpec、OptimizationDecision 与结果解释；Outer Agent 负责状态检查、准入、物化、执行、恢复和交付。Python 负责事实、证据、恢复与安全边界。

## 两层研究循环

```text
Inner Research Cycle (research_cursor)
        ↕ bounded ResearchContext
Outer Execution Round (round_no)
        ↓
proposals.json → Agent.run_proposals → ProposalExecutionWorkflow
        ↓
Simulator → WQBClient → Trajectory + TrialLedger + Validation + Settlement
        ↓
ResearchQualityAssessment → new research_cursor → next cycle
```

一个 research cycle 可以对应多个 execution round；`round_no` 仍由现有 `Agent.next_round_no()` 和 canonical owners 决定。`research_cycle_id` 与 `source_research_cursor` 只是 provenance，绝不进入 Simulation fingerprint。

## 开始

```powershell
pip install -r requirements.txt
Copy-Item config.example.json config.json
python main.py suggest
# 审阅并写入 .wqb_state/proposals.json
python main.py run-proposals
```

凭据不写入配置。使用完整 `WQB_USERNAME/WQB_PASSWORD`，或显式绝对路径的 `WQB_CREDENTIALS_ENV_FILE`，或 `~/.brain_credentials.txt`。系统不会搜索 cwd/父目录 `.env`，也不会混合半套凭据。

## Public research_api

| 工具 | 模式 | 作用 |
|---|---|---|
| `inspect_runtime_context` | READ_ONLY | runtime、cursor、checkpoint、quota、允许动作 |
| `inspect_research_context` | READ_ONLY | bounded Inner Agent context |
| `inspect_execution_round` | READ_ONLY | round lifecycle 与质量摘要 |
| `inspect_research_cycle` | READ_ONLY | cycle 到 execution 的映射 |
| `inspect_pending_work` | READ_ONLY | inbox、恢复阻塞、SUBMIT_UNKNOWN、下一安全动作 |
| `inspect_trial_accounting` | READ_ONLY | TrialLedger bounded accounting |
| `inspect_factory_status` | READ_ONLY | 现有 factory-session projection |
| `assess_experiment` / `assess_execution_round` / `assess_research_cycle` | READ_ONLY | canonical quality projection |
| `discover_fields` / `get_operator_reference` | READ_ONLY | BRAIN discovery/capability |
| `propose_optimization` | LOCAL_MUTATION | 验证 Agent decision，不执行 Simulation |
| `materialize_targeted_batch` | LOCAL_MUTATION | 校验 cursor 后写入唯一 proposals inbox |
| `execute_pending_round` / `resume_pending_round` | SIMULATION_WRITE | 只调用 `Agent.run_proposals()` |
| `reconcile` | READ_ONLY | 只轮询已知 progress URL |
| Alpha submission | MANUAL_ONLY | 仅用户在 BRAIN 中完成 |

`research_tool_manifest()` 返回上述授权等级与 owner。Inner Agent 不执行 Simulation、不能读 raw state、不能修改 factory session；Outer Agent 也不创造经济 hypothesis 或自动提交 Alpha。

## 质量与安全

`ResearchQualityAssessment` 是无状态 projection，复用既有 metrics/check/validation/statistical/incremental/yearly/correlation/settlement 结果，不重定义阈值，也不提出研究方向。Trial denominator 只来自 TrialLedger；`INCOMPLETE_LEGACY` 保持不可验证。`UNKNOWN`、`SUBMIT_UNKNOWN` 和基础设施失败不会被解释成研究 FAIL。

唯一 Simulation 写链是：

```text
Agent.run_proposals → ProposalExecutionWorkflow → Simulator → WQBClient
```

`SUBMIT_UNKNOWN` 永不盲重 POST；已知 progress URL 只读轮询；checkpoint、Trajectory、TrialLedger、ExperienceMemory 各只有一个 owner；不创建 research-cycle sidecar。

## 文档与验证

- [`AGENTS.md`](AGENTS.md)：架构、安全和 owner 契约
- [`docs/ARCHITECTURE_AGENT.md`](docs/ARCHITECTURE_AGENT.md)：当前对象图与写路径
- [`docs/RESEARCH_POLICY.md`](docs/RESEARCH_POLICY.md)：研究 cycle 与证据纪律
- [`docs/STATE_LAYOUT.md`](docs/STATE_LAYOUT.md)：canonical 状态布局
- [`docs/TESTING.md`](docs/TESTING.md)：本地增量与 CI 质量门

本地先跑 changed-file 映射的 targeted tests；CI 运行 compileall、typed frontier mypy、Ruff、Targeted Fast Lane、whole-repository final gate、offline doctor/audit 和 privacy。所有质量检查必须离线，不触发 live Simulation 或 Alpha submission。
