# 结果解释

## 证据深度

宽面筛选用 Simulation 返回的 live Alpha detail，或调用 Core 工具 `get_alpha_evidence()` 读取默认 lightweight summary。只有问题需要 aggregates、PnL、self-correlation 或指定 recordsets 时，才为小型终选集合请求 full evidence。仅当当前 Research tool inventory 暴露 `get_alpha_prod_correlation()` 时，才为终选候选按需读取 PROD correlation；若未暴露，应报告能力缺失，不用 full evidence 代替，也不为普通探针等待它。

批次比较优先使用 `get_alpha_evidence()` 的默认 summary：一次轻量 summary 读取就是一个 Alpha detail 请求，不得为每个候选扇出为 PnL、年度聚合与相关性调用。

## 选择偏差与多重检验

Research Agent 在当前工作集定义 Agent-owned 的 `family_label`（假设/模板/局部变体/语义家族；只是组织标签，不证明信号等价）、`related_trial_count_lower_bound`（实际观察到的相关试验下界）和 `count_scope`（`CURRENT_WORKING_SET`、`CURRENT_TASK` 或 `HANDOFF_CHAIN`）。这些不是 BRAIN/MCP 字段，也不代表完整账户或历史试验数；不知道时写 `UNKNOWN`，不能自动评分或过滤候选。

重复使用同一历史选择模型或搜索配置，会提高偶然胜出的候选被选中的风险；White 的 data-snooping 检验、Bailey 等人的 backtest-overfitting 框架及资产定价 multiple-testing 研究均讨论了这一问题，但其统计阈值不应直接硬编码到 BRAIN 研究流程中。[White (2000)](https://doi.org/10.1111/1468-0262.00152), [Bailey et al. (2017)](https://doi.org/10.21314/jcf.2016.322), [Harvey, Liu & Zhu (2016)](https://doi.org/10.1093/rfs/hhv059)。

证据复核按问题选择，不要求每个候选全部执行：local sibling/相邻参数 → 同机制跨年度 → 小 settings 邻域 → 语义匹配的替代字段 → 假设支持时的替代 dataset/region。孤立峰值弱于邻域一致性，邻域一致性又弱于多种独立且机制一致的证据；结合观察到的试验下界解释，不自动选 winner，并保留竞争或证伪解释。

## 缺失值与预处理

缺失并非必然随机。资产定价面板研究发现，complete-case 与无条件均值填补在其设定下可能低效或带来有偏推断；生存截断与极端值删除/ winsorization 也曾在特定长周期研究设计中扭曲关系。[Freyberger et al. (2025)](https://doi.org/10.1093/rfs/hhae003), [Kothari, Sabino & Zach (2005)](https://doi.org/10.1016/j.jacceco.2004.02.003)。这些结果说明预处理不是免费的清洗步骤，不构成对所有 BRAIN 字段或处理方式的普遍结论。

结合缺失覆盖、数据 cadence 与经济 horizon 解释 `ts_backfill`、`to_nan`、`winsorize`、`rank`、`zscore` 等处理。RAW 有信号而预处理 sibling 失效，可能说明极值、稀疏事件或幅度包含信息；RAW 不稳而处理 sibling 稳定，也只是相应的待检验解释。由 Agent 判断，不自动选择处理方式。

## 失败归因

记录最可能的类别及其证据：假设、字段、算子、horizon、实现、相关性或稳健性。平台/实现失败要和经济上的负结果分开。模糊写入只通过对账其既有 progress URL 重试；无 URL 的 `SUBMIT_UNKNOWN` 永不触发替代 POST。
