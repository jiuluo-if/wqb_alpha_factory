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

### 低表现诊断

以下是 Agent-owned 的解释标签，不是 API status，也不自动改变平台或 Alpha 状态：

- `RETURN_WEAK`：从经济 idea、field 是否表达该 idea、horizon、事前方向及变换是否损伤信息解释低 return；再设计 matched variants，不映射成 rank、scale 或加字段的固定处方。
- `INSTABILITY_HIGH`：把 Sharpe/IR 的不稳定性视为独立解释，先查 coverage/missingness 跳变、signal 自身过快变化、少数股票集中或 group/market exposure；每次只测试一个有证据支持的原因。
- `MIXED`：return 弱与 instability 同时有证据。
- `INCONCLUSIVE`：无法从当前范围的证据区分 return 与 instability。

这些 Sharpe 标签与下面的 field/extraction 诊断是不同解释轴，可并列记录；不能替代平台 check 或 BRAIN evidence。Sharpe 可因平均 return 提升或 return volatility/instability 降低而改善，不能只凭 Sharpe 一个汇总值归因。

- `FIELD_SIGNAL_WEAK`：只有同一字段上多个经济含义不同但合理的 extraction（例如不同 operator role 或 horizon）都没有支持信号时，才提高此解释权重；仍不能据此宣布 dataset 已失效。
- `EXTRACTION_WEAK`：同一字段的某个结构 sibling 有明显证据、其它结构失败时，优先考虑表达式/算子设计，而非断言字段完全无信号；围绕有证据的机制继续。
- `INCONCLUSIVE`：只试少量候选、高度相似 variants 或单一 operator family 时，证据不足以判断字段/方向失败。

负 Sharpe 本身不授权事后翻转方向；方向与理由必须在结果前声明。只有反向机制原本就是 competing hypothesis 时才能测试反向版本，否则创建带新经济解释的 `NEW_PROBE`。

## Robustness Ladder

只对有真实前景的候选按研究问题选择验证项；`DO_NOT_FIT_THE_TEST`：这不是每个候选都要执行的固定 pipeline，也不是继续把 validation 拟合到通过：

1. local parameter neighborhood：看相邻有经济依据的参数是否方向一致；孤立最高点弱于稳定邻域，不自动选择 best/second-best。
2. rank transform：检验去掉 magnitude 后 relative ordering 是否仍有预测信息；raw 有效而 rank 失效提示 magnitude 可能承载机制信息，不等于 overfit。
3. sign/binary transform：检验只保留方向是否仍有 signal；失效只说明 magnitude 可能重要，不直接证明过拟合。
4. sub/super-universe 或有经济意义的 universe sibling：事前写明要检验的流动性/集中性问题，不遍历 universe 挑最高 Sharpe。
5. time-period / yearly evidence：检查跨期表现、coverage 与机制一致性。
6. 若 live BRAIN settings 支持，finalist 可做 train/test-period validation；不能反复根据 test 结果调参。若据 test 结果修改 Alpha，原 test evidence 降为 `DEVELOPMENT_EVIDENCE`，之后需新的未使用验证维度。
7. semantically equivalent field：检查核心机制是否跨近义字段保持，而非扩大成随机 field search。

robustness check 失败先按具体证据归到 `ROBUSTNESS`、`CONCENTRATION`、`CORRELATION`、`TIME_STABILITY` 或 `PERFORMANCE`，不要统称 “Alpha bad”。不得针对失败 check 连续加 wrapper 直到 PASS；那会把 validation 变成 optimization objective，须显式标为新的开发问题。

批次足够大且分布信息有助于当前判断时，可同时描述 median、spread、tails 与 failure distribution，不只报 batch mean 或 top-1。mean 上升但 median 不动可能由少数极值驱动；mean 和 median 同向则与整体分布移动一致，但都不是自动评分。单 Alpha Sharpe/Fitness/Return 较高也不证明 alpha pool 或 combined portfolio 贡献更好；本项目不据此创建 portfolio optimizer。
