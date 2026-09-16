# Alpha Factory Agent 指南

本仓库是面向 AI Agent 的 WorldQuant BRAIN 研究工具。研究判断属于 AI；
Python 只负责确定性的平台事实、写入安全、schema、去重和可恢复执行。

## Remote-First 总览

```text
AI
  -> wqb_agent.research_api
  -> SimulationGateway / ExecutionGuard
  -> Simulator -> WQBClient -> BRAIN
  -> RemoteAlphaRepository -> AI
```

新的唯一公共研究面是 `wqb_agent.research_api`。新的 Simulation 必须使用
`SimulationSpec`，经过 `SimulationGateway`，由 BRAIN 返回 Alpha、metrics、
checks、aggregates、PnL、correlation 和其他 evidence；本地不能复制这些事实。

核心 API 包括 `discover_fields`、`generate_probes`、`simulate`、
`simulate_batch`、`get_alpha_evidence`、`list_remote_alphas`、分组和颜色投影。
优化由 AI 基于远端 evidence 直接决定下一次 `SimulationSpec`，不得新增本地
Trajectory、TrialLedger、checkpoint 或 optimizer control plane。

## Simulation 安全契约

- POST 前必须持久化 `SUBMITTING`；中断后的状态只能是 `SUBMIT_UNKNOWN`，禁止自动重 POST。
- 已知 `progress_url` 只能轮询原任务；`SUBMITTING`、`RUNNING`、`SUBMIT_UNKNOWN` 是唯一 guard 状态。
- 只有实时确认的 operator capability 才能进入 POST；缺失、过期或未知能力必须 fail-closed。
- ExecutionGuard 只保存 fingerprint、状态、远端 URL/ID 和时间等执行恢复信息，不能保存 metrics、checks、PnL、研究结论或 lineage。
- Alpha submission 始终由用户手工完成；颜色 PATCH 只能通过显式授权的远端证据路径执行。

## 本地与隐私边界

本地仅允许 credentials 引用、进程锁、`execution_guard.json` 和可重建的短期
远端 metadata cache。cache 不保存 metrics、expression、evidence 或研究结论。
旧 `.wqb_state` 中的真实 checkpoint、trajectory、proposals、experience 和
lock 不得手改、删除、归档或上传；真实工作区 preflight 为 `BLOCKED` 时只做
只读对账/恢复，不启动新 Simulation。

tracked 文件、fixture、报告和文档不得包含真实 Alpha、私有 field、表达式、
凭据或研究结果。模板变更前必须阅读根指南、`wqb_agent/AGENTS.md` 与
`wqb_agent/alpha_templates/AGENTS.md`；公共模板只能使用 synthetic 数据。

## 迁移期兼容层

旧的 `run-proposals`、`Agent` facade、Trajectory、TrialLedger、checkpoint、
factory session 和旧研究 projection 仅为迁移兼容，不是新的事实来源。删除或
收紧它们前，必须先迁移全部消费者，补充行为/回归测试，更新
`docs/ARCHITECTURE_AGENT.md`，并通过完整质量门；不得建立第二套 state、proposal
contract、evaluation 或 workflow。

## 工作与交付

- 先读本文件、`wqb_agent/research_api.py`、目标模块、直接依赖和相关测试；不要递归扫描仓库。
- 代码行为改变先补/改定向测试；运行 `python scripts/run_targeted_tests.py --files <changed-files>`、语法检查和 Ruff。
- 完成前必须使用 fresh CI/验证证据；质量门包括 mypy typed frontier、Ruff、targeted mapping、全量 unittest、offline doctor/audit 和 privacy。
- Git 提交邮箱固定为 `2966684515@qq.com`；提交信息使用英文前缀加中文内容，例如 `refactor：迁移远端证据读取`。
- 每次有效修改验证通过后立即 commit 并 push 到 GitHub；禁止强推、改写历史、跳过 CI 或上传无关改动。

## 推荐闭环

```text
discover -> hypothesize -> build SimulationSpec -> simulate
-> read BRAIN evidence -> AI evaluates -> next SimulationSpec or stop
```

所有事实以 BRAIN live response 为准；缺失 evidence 保持 `UNKNOWN`/
`UNAVAILABLE`，不得伪装成 `PASS`。
