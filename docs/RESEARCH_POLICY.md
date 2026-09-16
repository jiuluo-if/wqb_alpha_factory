# Remote-First Research Policy

- AI 负责研究问题、经济机制、字段和表达式选择、实验优先级及结果解释。
- Python 只负责 schema、live platform capability、exact execution safety、远端读取、缓存和隐私。
- BRAIN 是 Simulation、Alpha、metrics、checks、aggregates、PnL 和 correlation 的事实源。

研究闭环：

```text
discover fields/operators → generate/review SimulationSpec → simulate
→ read live Alpha evidence → AI 比较、解释并创建下一份 spec
```

执行成功不等于经济机制得到支持。缺失或未确认的远端证据保持 `UNKNOWN`/`UNAVAILABLE`；cache 必须标明来源和新鲜度，不能覆盖 live response。

相同 expression 与 effective settings 是执行级 exact duplicate；结构相似、字段重合、相关性和质量分组只是 advisory evidence，不自动阻止非 exact candidate。AI 使用历史结果时应意识到 selection bias，不能把参数扫描包装成新机制。

所有 Simulation 经过 `research_api → SimulationGateway → Simulator → WQBClient`。`SUBMIT_UNKNOWN` 不得自动重 POST，已知 progress URL 只能轮询原任务。Alpha submission 始终 `MANUAL_ONLY`，颜色 metadata PATCH 与 submission 分离。

tracked 文档、测试和 fixtures 只能使用 synthetic 数据；真实 Alpha、私有字段、研究表达式、credentials 和本地运行数据不得提交。
