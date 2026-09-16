# Remote-First Research Policy

## 责任边界

- AI 负责 hypothesis、经济机制、表达式、实验选择、结果解释和是否继续。
- Python 负责 schema、实时平台能力、fingerprint、exactly-once、Retry/timeout、远端读取和隐私边界。
- BRAIN 负责 Alpha/Simulation 的 metrics、checks、aggregates、PnL、correlation 和提交状态。

Python 不得用本地研究状态机替 AI 生成经济结论或下一组参数。

## 研究闭环

```text
discover fields/operators → generate/review SimulationSpec → simulate
→ get_alpha_evidence(live=True) → AI 比较与解释 → AI 创建下一份 SimulationSpec
```

优化不是 `parent → CHILD` 的 Python 生命周期。若 AI 根据 Alpha A 创建 B，可在 `SimulationSpec.note` 中留下人类可读说明，但 note 不参与 execution identity。

## Evidence truth

- 默认使用 live BRAIN response，并返回 `source=LIVE`、`fetched_at` 和 freshness 信息。
- cache 只是可重建的 metadata convenience；stale 或缺失 cache 不能升级 UNKNOWN、制造 PASS 或覆盖 live response。
- self-correlation、checks、aggregates 等未确认时必须保留 `UNKNOWN`/`UNAVAILABLE`。
- execution success 不等于经济机制得到支持；平台结果、研究假设和机制解释必须分开记录在 AI 的上下文中，而非写入 Python 生命周期数据库。

## Simulation 安全

所有真实 Simulation 必须经过 `SimulationGateway`。同一 expression + effective settings 的 active execution 只能有一次 POST；`SUBMIT_UNKNOWN` 永不自动重发；已知 progress URL 只能轮询原任务。任何 operator capability 缺失或无法实时确认时，必须在 POST 前拒绝。

Alpha submission 是 `MANUAL_ONLY`。颜色 metadata 修改必须是显式、独立、可审计的远端 PATCH，不能隐式提交 Alpha。

## Similarity 与选择偏差

exact duplicate 是执行安全结论；structural similarity、field overlap、correlation 和 quality group 只是给 AI 的 advisory evidence，不自动阻止非 exact candidate。AI 使用历史结果优化时，应明确承认 selection bias，不能把参数扫描包装成新的机制发现。

## 隐私与状态

tracked 文档、测试和 fixture 只能使用 synthetic data。真实研究结果、Alpha ID、私有 field、表达式、credentials 和本地研究 state 不得提交。新架构不得新增 round、parent/lineage、settlement、memory、optimizer 或 factory session state；迁移期兼容代码也不得被新 API 扩展为第二套 canonical。
