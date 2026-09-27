# 结果解释

## 证据深度

宽面筛选用 Simulation 返回的 live Alpha detail，或调用 Core 工具 `get_alpha_evidence()` 读取默认 lightweight summary。只有问题需要 aggregates、PnL、self-correlation 或指定 recordsets 时，才为小型终选集合请求 full evidence。仅当当前 Research tool inventory 暴露 `get_alpha_prod_correlation()` 时，才为终选候选按需读取 PROD correlation；若未暴露，应报告能力缺失，不用 full evidence 代替，也不为普通探针等待它。

批次比较优先使用 `get_alpha_evidence()` 的默认 summary：一次轻量 summary 读取就是一个 Alpha detail 请求，不得为每个候选扇出为 PnL、年度聚合与相关性调用。

## 选择偏差与多重检验

Research Agent 在当前工作集定义 Agent-owned 的 `family_label`（假设/模板/局部变体/语义家族；只是组织标签，不证明信号等价）、`related_trial_count_lower_bound`（实际观察到的相关试验下界）和 `count_scope`（`CURRENT_WORKING_SET`、`CURRENT_TASK` 或 `HANDOFF_CHAIN`）。这些不是 BRAIN/MCP 字段，也不代表完整账户或历史试验数；不知道时写 `UNKNOWN`，不能自动评分或过滤候选。

## TEMPLATE EVIDENCE AND PROMOTION

`PAPER SUPPORT != BRAIN SUPPORT`。论文证据只说明某个机制值得测试；BRAIN Simulation 才能支持该机制在当前 observable、实现和平台环境中的实现。论文模板只有经过 BRAIN family-level validation 才能成为可靠模板。`family_label` 用于组织证据，不是 family membership 的证明；须按共享 economic mechanism、field roles/relationships、信息时点和结构解释实际 sibling。论文主张与 template 草案是待验证假设，不是 BRAIN evidence。单一 Alpha、单一强字段、一个高分 winner 或某项活动达标都不能晋升。

晋升证据按研究问题逐步累积，不是固定 pipeline 或固定样本量：

```text
paper mechanism
→ BRAIN observables
→ SIMPLE IMPLEMENTATION
→ CONTROL + FALSIFICATION + NEGATIVE CONTROL
→ LARGE-SCALE FAMILY ADMISSION
→ supporting / falsification siblings
→ ATTRIBUTION
→ LOCAL / STRUCTURAL STABILITY
→ structural / settings robustness
→ COST + CORRELATION + TIME ROBUSTNESS
→ current platform checks
→ REUSABLE TEMPLATE
```

Simulation budget 随信息增益逐步增加：`SEMANTIC / TIMING / COVERAGE SCREEN → LARGE-SCALE FAMILY ADMISSION → FAMILY TRIAGE → SUPPORT + FALSIFICATION + NEGATIVE CONTROL → LOCAL / STRUCTURAL STABILITY → FINALIST DEEP EVIDENCE`。每次新增 Simulation 前说明它区分哪两个解释、什么结果会改变决策，以及现有 evidence 是否已回答；若不能改变研究判断，先复用已有 BRAIN evidence。早期扩展机制/template/data coverage，中期集中到重复支持的 family，后期才深查 PnL/year evidence/correlation/qualification。批次大小依信息增益和 live 预算决定，不设固定 batch quota。

可晋升时，证据应表明同一经济机制能在合理的 semantic field、horizon/time、数据/地区或结构/settings 变化下重复得到支持；至少要能解释哪些变体支持机制、哪些结果会证伪它，以及提升不是由单一字段或 `AUX_PROCESSING` 主导。变化轴由竞争解释与现有 BRAIN evidence 决定，不设固定字段数、批次数或通过率。使用 `related_trial_count_lower_bound` 与 `count_scope` 说明搜索和选择压力，不能把反复筛选后的极值当作独立复现。

晋升审查还要检查当前 BRAIN hard checks、submission 状态、return 与 stability/performance、turnover/margin、coverage、persistence/time decay、sibling consistency、selection pressure、transaction cost、self/PROD correlation（仅在可用且对问题必要时）、falsification 和 redundancy。交易成本和 implementability 从 admission 阶段就是经济证据，不是最后才加的筛选项。若 negative control 与目标版本同样有效，降低机制可信度并重审竞争解释；不能把二者并列 PASS 后照常晋升。通过某项活动或资格门槛只是当前机会条件，不证明机制成立；当前规则或资格无 live 证据时保持 `UNKNOWN`。平台约束、历史阈值与活动收入不得固化成通用常数；不汇总成单一综合值（No single Quality Score）。

可复用 template 的说明应保留机制不变量、FIELD_ROLES 与 semantic slots、已由 live contract 验证的允许替换范围、合理 horizon、可变 extraction/settings axes、已知失败条件、成本特征、适用 region/data 条件和验证证据范围。只记录足以支持复用的匿名机制与证据摘要；具体候选字段、表达式、Alpha 或完整研究内容仍遵循隐私约束。没有足够家族证据时停留在 candidate/provisional 工作集，不创建 template registry 或自动晋升器。

## Validation Budget

任何 evidence 一旦被用来调整 `field`、`expression`、`parameter`、`operator` 或 `settings`，都转为 `DEVELOPMENT_EVIDENCE`，不再算作独立 finalist validation。finalist 应尽量保留未参与开发的验证轴；同一个 validation 反复被拿来调 Alpha 时标 `VALIDATION_EXHAUSTED`，停止将该轴声称为验证证据并寻找尚未消耗的 axis。不得在同一 validation 上继续拟合到 PASS。

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

### Attribution before repair

修复前先定位问题层。Agent 可用轻量解释标签 `RAW_FIELD`、`FIELD_RELATIONSHIP`、`TEMPORAL_EXTRACTION`、`GROUP_OR_NEUTRALIZATION`、`SETTINGS`、`COVERAGE_OR_CONCENTRATION`、`REGIME_OR_TIME_STABILITY` 或 `UNKNOWN`；这些标签不属于 API status，也不新增 schema/class/database。用已有 baseline、PnL、checks、year evidence 与已模拟 siblings 优先区分解释；只有现有证据不能分辨重要竞争原因时，才考虑最少的额外 ablation。修复应针对归因层：字段弱时换 wrapper 不解决字段问题；关系弱时重审关系；extraction 弱时检验有理由的 temporal sibling；settings 暴露时比较 matched settings；coverage/concentration 问题则针对相应暴露。不要修与失败来源无关的层。

结构相似度也参与归因：不同 family label、近义 operator 或 wrapper 若保留同一机制和核心拓扑，不算独立机制证据。反之，字段语义不同也不能只凭字段名断言机制不同；结合 live description/type/coverage 与字段在表达式中的角色判断。

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
8. structural perturbation（finalist only）：在保留经济机制的前提下，改变一个非平凡结构选择，例如合理的 temporal extraction，或移除非核心 preprocessing wrapper。若变化已改变经济机制，应作为 `NEW_PROBE`，不算 robustness sibling。
9. settings perturbation（finalist only, when relevant）：对照 anchor 与一个经济含义明确的 alternate setting，例如 neutralization、truncation 或 decay；它是解释风险的证据，不要求固定数量或通过比例。

检查前先复用已有的 parameter、structural、settings 或 semantic siblings；新 Simulation 只用于补足会改变结论的证据。若只有精确 anchor 表现强，附近合理 sibling 都崩溃，可标 `BRITTLE_EXACT_POINT` 并降低 finalist 信心，不自动 FAIL。

robustness check 失败先按具体证据归到 `ROBUSTNESS`、`CONCENTRATION`、`CORRELATION`、`TIME_STABILITY` 或 `PERFORMANCE`，不要统称 “Alpha bad”。不得针对失败 check 连续加 wrapper 直到 PASS；那会把 validation 变成 optimization objective，须显式标为新的开发问题。

一个孤立的负年份不是自动否决。结合跨年方向、近期表现、coverage、PnL concentration、机制解释与平台 checks 判断；若数据范围或 siblings 不足，保留 `INCONCLUSIVE`，不要套用固定 CV、负年份计数或近期年阈值。

长期 reference 的 lesson 晋升门槛应高于当前 working set：至少需要重复且机制一致的证据、支持 attribution 的证据、一个 falsification/semantic sibling test，以及清楚的适用范围。未满足时把经验留在当前工作集；不为此建立 research memory database、自动 writer 或晋升队列。

批次足够大且分布信息有助于当前判断时，可同时描述 median、spread、tails 与 failure distribution，不只报 batch mean 或 top-1。mean 上升但 median 不动可能由少数极值驱动；mean 和 median 同向则与整体分布移动一致，但都不是自动评分。单 Alpha Sharpe/Fitness/Return 较高也不证明 alpha pool 或 combined portfolio 贡献更好；本项目不据此创建 portfolio optimizer。
