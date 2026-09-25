# 结果解释

## 证据深度

宽面筛选用 Simulation 返回的 live Alpha detail，或调用 Core 工具 `get_alpha_evidence()` 读取默认 lightweight summary。只有问题需要 aggregates、PnL、self-correlation 或指定 recordsets 时，才为小型终选集合请求 full evidence。仅当当前 Research tool inventory 暴露 `get_alpha_prod_correlation()` 时，才为终选候选按需读取 PROD correlation；若未暴露，应报告能力缺失，不用 full evidence 代替，也不为普通探针等待它。

批次比较优先使用 `get_alpha_evidence()` 的默认 summary：一次轻量 summary 读取就是一个 Alpha detail 请求，不得为每个候选扇出为 PnL、年度聚合与相关性调用。

## 选择偏差与多重检验

- 结果要结合 `variant_family`、`observed_execution_count`、结构相似度、年度证据、相关性、参数邻域与被主张的机制一起解释。
- `observed_execution_count` 是观察到的下界，不是完整 trial 历史；它警示被选结果之前有多少相关尝试，不是自动打分或过滤器。
- 大量相关 trial 之后的高 Sharpe 暴露于多重检验与选择偏差；要看跨年度、兄弟组、settings 与合理参数邻域的证据，不得把一个孤立峰值升格。
- 有前景方向的同时保留探索。只有比较证据支持特定机制时才扩展，并至少保留一个竞争或证伪解释。

## 失败归因

记录最可能的类别及其证据：假设、字段、算子、horizon、实现、相关性或稳健性。平台/实现失败要和经济上的负结果分开。模糊写入只通过对账其既有 progress URL 重试；无 URL 的 `SUBMIT_UNKNOWN` 永不触发替代 POST。
