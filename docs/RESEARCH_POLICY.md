# Remote-First Research Policy

- AI 负责研究问题、经济机制、字段和表达式选择、实验优先级及结果解释。
- Python 只负责 schema、live platform capability、exact execution safety、远端读取、缓存和隐私。
- BRAIN 是 Simulation、Alpha、metrics、checks、aggregates、PnL 和 correlation 的事实源。

研究闭环：

```text
list raw datasets/datafields and operators → Agent selects evidence and fields
→ optionally generate/review an explicitly bounded SimulationSpec → simulate
→ read live Alpha evidence → AI 比较、解释并创建下一份 spec
```

执行成功不等于经济机制得到支持。缺失或未确认的远端证据保持 `UNKNOWN`/`UNAVAILABLE`；cache 必须标明来源和新鲜度，不能覆盖 live response。

相同 expression 与 effective settings 是执行级 exact duplicate；strict structural 与 variant family、字段重合、相关性和质量分组只是 advisory evidence，不自动阻止非 exact candidate。Probe 固定预算在模板维度采用 deterministic、lazy、bounded 的 coverage-first traversal，只改变合法候选的遍历顺序，不表示模板具有相同经济权重或构成 winner ranking。variant family 只自动抽象仓库明确认可的 time-series research horizon；safety epsilon、threshold、operator-required constant 和未知 numeric literal 不因“都是数字”而合并。AI 使用历史结果时应意识到 selection bias，不能把 `observed_execution_count` 当完整 trial count 或把参数扫描包装成新机制。

所有 Simulation 经过 `research_api → SimulationGateway → Simulator → WQBClient`。`SUBMIT_UNKNOWN` 不得自动重 POST，已知 progress URL 只能轮询原任务。Alpha submission 始终 `MANUAL_ONLY`，颜色 metadata PATCH 与 submission 分离；颜色只能由同一远端 evidence snapshot 的 variant family 经 AI 显式 assignment 产生，同时保留 strict structural key，且同步必须消费 review 后的 exact plan 并通过 stale/readback 检查。

Simulation 完成后的 deep recordsets 由 AI 按研究问题选择读取，不作为 broad Probe 默认全量请求；Python 只提供 live recordset discovery、bounded table decoding 和原始数值，不自动解释稳健性、winner 或下一轮研究方向。

tracked 文档、测试和 fixtures 只能使用 synthetic 数据；真实 Alpha、私有字段、研究表达式、credentials 和本地运行数据不得提交。

Template/Probe 的复杂度预算按最终 effective expression 的 operator occurrence 计算，direction transform 等机械 wrapper 计入预算；局部优化必须保持 immutable anchor 的 operator topology，不得通过叠加 operator 扩大表达式。
