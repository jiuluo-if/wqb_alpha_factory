# Research policy

本文只描述当前研究方法与安全边界，不代替 BRAIN live response，也不包含真实字段、Alpha、指标、研究结果或 campaign history。

## 事实与执行边界

- BRAIN live response 是 datasets、fields、operators、Simulation、metrics、checks、aggregates 和 correlation 的事实源。
- `trajectory.jsonl` 是 append-only 实验证据，checkpoint 是 exactly-once 恢复边界；二者不得手工改写。
- Simulation 写入只能沿 `Agent.run_proposals()` → `ProposalExecutionWorkflow` → `Simulator` → `WQBClient`；未知结果保持 `SUBMIT_UNKNOWN`，不得重 POST。
- 缺失或含糊证据保持 `UNKNOWN` / `UNAVAILABLE`；Alpha submission 始终由用户手工完成。

## 假设、反证与 trial accounting

每个实验只回答一个可证伪问题，并记录 hypothesis、expression、settings、预期失败模式和结果。先区分平台事实、回测观察、经济解释和未验证假设。

- `BASELINE` 检验最小机制；`CHILD` / `ROBUSTNESS` 每次只改变一个主要变量。
- 多字段必须有语义关系、比率、差分或状态—信号配对；禁止无机制堆叠。
- 命中 falsification 时停止该假设，不用窗口、权重、符号或方向扫描掩盖证伪。
- 同一 hypothesis 的所有参数、窗口、字段替换和结构变体属于同一 experiment family；成功、失败、UNKNOWN 和 PRUNED 都必须计入 trial。
- 高 Sharpe 不等于机制成立；未经独立证据确认的解释保持 unresolved，不晋升为 supported/contradicted。

## 指标驱动的优化顺序

先根据 `Sharpe`、`Fitness`、`Returns`、`Turnover`、`Drawdown`、`Margin`、checks、health、yearly evidence 和 correlation 诊断问题，再选择研究动作：

- 低 Sharpe：优先审查信号质量、经济机制、年度稳定性、coverage、子 Universe 和语义字段替换，不先调 decay。
- Sharpe 尚可但换手高：先确认机制速度；只有 Turnover 高于 Fitness 的 0.125 floor 时才考虑 horizon 或适度 smoothing，并检查 Sharpe/Returns 保留。
- Sharpe 尚可且换手已低：审查 Returns、信息密度和独立互补信息，不继续无意义压换手。
- 高集中度：先查 coverage、missingness、异常值和多空宽度，再决定 truncation；设置不能隐藏数据缺陷。
- 单一窗口、年份、Universe 或字段异常突出：视为脆弱性线索，不视为发现。
- `SELF_CORRELATION` 必须使用平台真实结算；结构相似度和缓存只能做 pre-screen。

## 优化 Agent 接口

优化 Agent 只使用 `research_api` 的 bounded 优化入口：`inspect_optimizer_parents`、`inspect_optimizer_context`、`propose_optimization` 和 `materialize_targeted_batch`。真实只读证据可由 `wqb_agent.optimization_interfaces.ClientOptimizationEvidenceProvider` 聚合 Alpha detail、aggregates、allow-listed PnL recordset 与 self-correlation；PnL 按服务端 schema 名称解析，不假设列位置。该接口不创建第二套 HTTP、state、ledger、trajectory 或 memory owner。

优化只读证据槽位彼此独立，可分别为 `AVAILABLE`、`UNKNOWN` 或 `UNAVAILABLE`；Alpha detail 是确认 Alpha 身份的必要 anchor。可选 endpoint 的明确能力缺失可以降级为 `UNAVAILABLE`，但 AUTH、RATE_LIMIT、transport 和 parent-not-found 等基础设施或身份错误必须保留为异常；correlation 已可达但尚未结算时保持 `UNKNOWN`，不能伪造 PASS/FAIL。

优化 Agent 必须输出经济机制、方向理由、falsification、竞争解释和 information gain。每个 child 最多改变一个主要变量；`VALIDATE` 只用于有界的单变量 robustness，不能冒充新机制。失败和剪枝结果通过既有 `ExperienceMemory` 记账，原始指标仍由 trajectory 保存。

优化选择的事实计数由既有 `TrialLedger` 唯一拥有：每个 finalized `OptimizationDecision`（包括 STOP、REROUTE、被拒绝或剪枝的 CHILD/VALIDATE）最多记一次，重复语义按 parent identity 与决策内容幂等；该非 Simulation 选择事件不改变既有 Simulation lifecycle 或历史 candidate/trial 计数。`ExperienceMemory` 仅是可失败的压缩投影，不能覆盖或删除账本事实。

优化决策使用唯一稳定 semantic identity 贯穿 workflow、proposal provenance、账本和 targeted inbox；同一 active targeted batch 重放为 no-op，不同 active batch 不得静默覆盖，过期 batch 才能在同一 canonical inbox 中替换。真实 proposal 的归属必须由 decision identity 证明，不能从 parent、列表顺序或 proposal 数量猜测。

## 统计、稳健性与停止

- DONE 结果必须结合 headline、checks、health、yearly、PnL（能力已验证时）和 correlation 解释。
- PSR/DSR/PBO/CSCV 只能使用其所需的真实、时间对齐数据；缺失数据返回 `UNAVAILABLE`，不得用 proxy 冒充。
- `PROMOTE` 只进入人工审核池；`CONTINUE` 必须能改变下一步判断；机制被证伪、无信息增益或过拟合风险过高时 `STOP`；未知远端状态走 `RECONCILE`。

## Public/private boundary

真实 Alpha、字段 ID/配对、metrics、PnL、trajectory、ExperienceMemory 和研究报告只能本地保存。公共代码、文档、prompt 和 fixture 只允许 synthetic 示例与 evergreen contract；详见 [`PRIVACY.md`](PRIVACY.md)。
