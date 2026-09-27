# 批次设计

默认推理流程：

```text
观察
→ 多个竞争假设
→ 分组批次实验
→ BRAIN 结果
→ 比较假设家族
→ 扩展有前景方向
→ 证伪 / 稳健性
→ 停止或继续
```

每个批次记录：

1. 测试什么机制？
2. 哪些 proposal 是对照、本地兄弟、证伪或新探针？
3. 什么结果会改变下一次决策？

## UNIVERSAL PAPER MAPPING

项目 research/audit 输入提供外部论文与研究材料；不要求本地 Research Agent 联网检索论文。输入资料应覆盖 peer-reviewed papers 与 working papers（包括 SSRN / NBER），涉及 asset pricing、market microstructure、alternative data、ML/statistical learning、transaction costs、anomaly replication/decay 与 multiple-testing/overfitting；经典论文用于机制基础，近 24 个月的研究优先反映当前市场和方法变化，并注明来源与发表/工作论文状态。对输入的每篇论文，都保留准确来源并做一次统一映射，即使当前 BRAIN 没有完整的可观测变量也不丢弃它。不得复制大段论文正文；区分论文的 claim/empirical finding 与作者提出或数据支持的 economic mechanism，不把相关性结果写成已识别的因果关系。

先将论文映射为可质疑的研究主张：

```text
PAPER → claim / empirical finding → economic mechanism
      → predicted variable → observable variables
      → direction → horizon / information timing
      → conditioning / regime → field relationships
      → implementation frictions → transaction-cost implications
      → known decay / redundancy → falsification conditions
```

再写成 BRAIN 可检验的模板草案：

```text
CORE_MECHANISM
FIELD_ROLES
RELATIONSHIP
TEMPORAL_EXTRACTION
OPTIONAL_CONTEXT
SETTINGS_HYPOTHESIS
CONTROL
FALSIFICATION
ROBUSTNESS_AXES
```

每篇输入论文先在 Agent-owned Research mapping 形成 `PAPER_TEMPLATE_CANDIDATE`，至少保留：

```text
SOURCE (citation/DOI, publication date, publication or working-paper status)
CLAIM
ECONOMIC / BEHAVIORAL / MICROSTRUCTURE MECHANISM
PREDICTED VARIABLE
REQUIRED OBSERVABLES
FIELD ROLES / RELATIONSHIP
DIRECTION + REASON
INFORMATION AVAILABILITY
EXPECTED HORIZON / DECAY
CONDITION / REGIME
IMPLEMENTATION / COST FRICTION
FALSIFICATION
NEGATIVE CONTROL
BRAIN MAPPING
PAPER → TEMPLATE ASSUMPTION PROVENANCE
```

`PAPER → TEMPLATE ASSUMPTION PROVENANCE` 逐项说明哪些 mechanism/role/relationship/timing/slot assumptions 来自哪条论文 claim，哪些是 Agent 的新假设。SOURCE、发表日期与这条 provenance 留在 Research mapping，不新增 `AlphaTemplate` 字段、catalog metadata 或 research database。`NEGATIVE CONTROL` 是事前设计的无关/机制破坏对照，写明它为何不应支持目标机制、如何保持可比较，以及若结果与目标版本同样有效会怎样下调机制可信度；它不等同于 `CONTROL_ALPHA` 模板角色。

### Route each paper candidate through the existing template owner

- `map to an existing template`：现有机制、semantic slots、field relationship、direction 和 timing 能忠实表达论文主张时优先复用。
- `extend an existing semantic slot`：新增合理 field carrier/slot hypothesis，但不改变 `CORE_MECHANISM` 与关系时使用；每个 semantic role 指明映射到 AlphaTemplate 的现有 field binding。
- 提出 `NEW_PROBE sibling`：保留可辨认的机制核心，单独检验论文所启发的一个新 carrier、temporal extraction 或 bounded hypothesis。
- 只有当 existing templates cannot faithfully express 论文的核心关系、信息时点或可证伪预测时，才 propose a new template skeleton。新 skeleton 先作为 `PAPER_TEMPLATE_CANDIDATE`，不自动写进 catalog；仍由唯一 `wqb_agent.alpha_templates` owner 定义并通过其 strict validation，晋升需 family-level BRAIN evidence。

字段语义角色可使用 `direction`、`expectation`、`outcome`、`scale`、`confidence`、`context`、`cohort` 等 Agent vocabulary；多字段关系可解释为 `spread`、`directional ratio`、`surprise`、`confirmation`、`interaction`、`dispersion`、`co-movement` 或 `term structure`。这些是 Research mapping 中的人读经济语义，不自动成为 `semantic_contract` / `relationship_contract` enum。机器 contract 必须忠实匹配当前 AlphaTemplate owner 的 supported set；没有等价 contract 时标 `CAPABILITY_MISSING` 并保留为未 materialize candidate，不能强塞近似枚举。新 contract 需要单独的 owner 变更、验证和测试。

每个 proposed numeric/operator/settings/horizon axis 都在 Research mapping 记录 `default`、`allowed neighborhood`、`economic role` 和 `what changing it tests`。`TemplateNumericSlot`、`TemplateOperatorSlot`、`allowed_settings_arms` 和 `allowed_horizon_profiles` 仍由现有 owner 表达有界可执行范围；Research mapping 说明范围背后的研究问题，不扩大 schema。未知或当前不支持的 observable 标 `UNMAPPED_OBSERVABLE`；只有部分字段/claim 映射时标 `PARTIAL_MAPPING`；当前 operator、setting 或 machine contract 不支持时标 `CAPABILITY_MISSING`，不把三种缺口混为一类。

Candidate mapping 将实现拆为 `CORE_MECHANISM`、`FIELD RELATIONSHIP`、`TEMPORAL_EXTRACTION`、`AUX_PROCESSING`、`RISK / SETTINGS`；前两项主导 template 的经济身份。预处理、neutralization、decay、truncation 通常是实现/风险轴，不能单凭 wrapper 差异伪造新机制；若变化了预测逻辑或 economic relationship，则创建新的 template candidate / `NEW_PROBE` 并说明原因。

模板草案描述经济机制、语义字段角色与可变轴，不是论文公式的照抄、Alpha 表达式、builtin template 或 Python schema。映射时解释方向、信息可用时点和 horizon、conditioning/regime、字段关系、交易成本/实现摩擦、衰减/冗余风险，以及什么结果会推翻机制；由 Agent 根据当前 BRAIN live dataset/datafield 与 capability evidence 选择 observable 和实验，不能让 Python 排名或补齐字段。

方法论文若研究的是数据挖掘偏差、识别、预处理或验证方法，而非可交易预测机制，也使用同一映射框架：把其主张映射到 `CONTROL`、`FALSIFICATION`、`ROBUSTNESS_AXES` 与可观察的数据条件；若没有 Alpha-level observable，标 `PARTIAL_MAPPING` 或 `UNMAPPED_OBSERVABLE`，不强造 Alpha 表达式。

若论文要求的 observable 当前不在 BRAIN 可用数据中，对具体缺项标 `UNMAPPED_OBSERVABLE`；若仅部分主张或变量能落地，标 `PARTIAL_MAPPING` 并列出缺口。不得静默换成名称相似但经济角色不同的 proxy；只有明确论证 proxy 的经济关系、信息时点及可证伪结果后，才可将它作为独立、有限的映射假设。未映射论文继续保留为研究输入，不得宣称已被 BRAIN 验证。

批次多样性按经济机制、field roles、字段关系、operator topology、temporal extraction 与 group/settings 作用判断。不同 `family_label` 不代表不同机制；只换名字、近义 operator 或 wrapper，而核心结构与机制相同的候选仍属于同一结构家族。优先覆盖能区分的机制与竞争解释（`MECHANISM COVERAGE > OPERATOR COVERAGE`），不要为 operator 使用率、entropy 或固定 quota 构造候选。

## Validation Ladder

验证按证据层级解释：

1. `validate_simulation_spec()` 检查 deterministic request shape。返回 `valid=true` 且 `evidence_status=INCONCLUSIVE` 只表示请求形状通过；`VALID` 不等于表达式已可执行，也不验证 operator signature、keyword 参数、`MATRIX/VECTOR/GROUP` 完整类型链或远端 parser 接受。
2. live operator、settings 与 field capability 由 Gateway/BRAIN 验证；BRAIN 是能力事实源。
3. 表达式能否执行由实际 Simulation/BRAIN 结果证明。

不要在 production 复制完整 tokenizer/parser/semantic analyzer 或 operator catalog。`FULL_LOCAL_EXPRESSION_COMPILER = DEFER`；只有 handoff 证明同类 deterministic syntax failure 反复浪费 Multi batch 时，才评估小型验证。未来规则必须可 100% 确定识别、稳定、不与 live BRAIN capability 冲突且实现很小。

`proposal_id` 是临时结果的关联键；Agent 在写入前保留它与假设、`note`、`template_id` 的 mapping。Research MCP 对每项只投影 `proposal_id`、状态、reason code、fingerprint、Alpha ID 与字段校验结果；Agent 应从自己的 mapping 还原 note/template，不把这些解释信息写入 `ExecutionGuard` 或本地研究数据库。

为每个字段 ID 保留 dataset provenance 并传入 `SimulationSpec`。具体字段查询路径按当前会话 inventory 选择；Gateway 的 live 校验是安全校验，不是经济适配度评分。

## 新字段、单信号与复合

- 从当前 BRAIN `list_datasets()` 开始，再对问题相关的数据集发现字段：若当前 inventory 暴露 `list_all_datafields()`，用其 bounded pagination；否则用 `list_datafields(dataset_id, limit, offset, field_type)` 显式翻页。开始前设 page budget 或 time budget；预算耗尽但 API 返回的 count/offset 尚未证明覆盖完成时，标记 `FIELD_DISCOVERY_INCOMPLETE`，不得把部分结果说成完整覆盖。`research_api.list_all_datafields` 保留为 public-only 低频能力，不因此加入默认 Core。按语义关键词、字段 ID、`type`、描述、data coverage、dataset、region/universe/delay 与语义核心（semantic core）整理候选；历史未测清单可辅助检索，旧 `field_library`、字段 dump、reservoir 和 cache 只作带 freshness 的索引线索，不能证明当前字段存在、可用或适合 RA。
- 发现顺序为：dataset metadata → semantic themes → bounded field pages → small field shortlist → operator-role selection → probes。由 Agent 根据 dataset 和 page/time budget 决定数量；shortlist 应小到能认真分析，并说明 discovery 是否 incomplete。不要硬编码主题数、字段数或算子数，也不要建 vector DB、embedding service 或 semantic index。
- 对每个新 dataset/语义方向，先建立单字段基线（single-field baseline），确认它对应单一机制，再判断字段语义、类型变换与 RA 地区资格。确认后才扩大到同义字段或复合；保留字段到 dataset 的 live provenance，并传入 `field_datasets`。
- 选择字段时，除 field role 外，可由 Agent 记录 `LIKELY_FALSE_PROXY` 判断：字段是否更像 liquidity、size、coverage、reporting frequency、post-event reaction，或 `none/unknown`。优先依据当前 BRAIN description、type、coverage/context 与经济解释；字段名只是线索，不用 Python 的名字规则推断代理属性。
- 对 News 类字段，分清 `SOURCE_INFORMATION`、`CONTEXT_OR_GATE` 与 `POST_EVENT_REACTION`。sentiment、event direction、estimate/relevance 可能承载 source information；新闻后的价格、成交量或 reaction statistics 通常先视为 context/confirmation/gate。若将 reaction 当主信号，先写明独立机制（如 delayed absorption、overreaction 或 reaction speed）；否则不要把它当 standalone source。
- 默认让一个候选表达一个经济机制，并先测一个信号字段，不加辅助腿。若要复合字段，先写明 field roles（各字段的经济角色）、它们为何属于同一机制、组合要解决的具体问题；在相同设置下比较组成信号与复合信号，做 ablation（消融）判断每一项是否提供了所声称的作用。若字段代表不同机制，将组合标成新的 `NEW_PROBE` 假设，不把它包装成去噪腿。
- Agent 可按当前字段语义提出 `SPREAD`、`SURPRISE`、`INTENSITY`、`INTERACTION`、`DISPERSION` 或 `CONFIDENCE_WEIGHT` 等关系；它们只是 construction vocabulary，不是 builtin/template factory。每次仍需说明 field roles、经济关系、预期方向与可证伪观察。
- 辅助、控制或 hedge 腿不是默认去噪器。只有在独立假设说明其风险作用，并通过“原信号单独 / 控制腿单独 / 两者组合”的匹配消融后，才保留该腿；同时检查它是否掩盖原字段信号或主导相关性。
- 需要给既有 agent 工作经验分配注意力时，按证据来源、所属任务、研究契约与时间新鲜度分层：BRAIN 当前 live 证据优先；本轮刚验证的工作集优先于旧 `tmp` 报告和缓存；旧记录先作为假设线索，重新核验后才恢复为当前约束。新证据与旧记录冲突时，保留旧记录的时间/范围并以新 live 结果更新工作集，不把记忆伪装成平台事实。
- 每轮结束时，用最新已验证结果更新简短工作集（强证据、失败归因、未决解释、下一实验）；保留旧记录的来源和日期，避免重复注入所有历史 agent 记忆。时间较近本身不等于更可靠，仍按证据质量和当前任务相关性判断。

## 语义配对、期限与预处理

- 多字段组合前声明 field role、semantic core 与 comparison meaning；优先配对同一指标的 actual/estimate 或不同预测 horizon。共享 semantic core、期限不同的字段可形成 `TERM_STRUCTURE` 假设；不同机制的字段组合要作为 `NEW_PROBE`，不能随机配对或标为 local sibling。
- 数据 cadence、发布日期/可用延迟与经济机制 horizon 是窗口选择的 prior，不是规则。用 anchor 加少量相邻且有经济理由的参数；不得假定低频字段必然需要更长窗口，也不做 Cartesian grid。
- 把 `ts_backfill`、`to_nan`、`winsorize`、`rank`、`zscore` 作为待检验的 preprocessing axis，先保留同字段/同机制/同 settings 的 `RAW CONTROL`，再做只改变预处理的 `PREPROCESSED SIBLING`；比较时记录 coverage/缺失变化。预处理失败或成功都由 Agent 解释，不自动套用。
- 若 `trade_when`、hard threshold、event gate、rank/winsorize/decay 等强 wrapper 带来明显提升，先审计 source 是否已存在，还是 wrapper 选择了有利样本或重塑了暴露。能区分时优先比较 ungated/raw control 或已有邻近 sibling；若表现只在 wrapper 后出现，可记为 `WRAPPER_DEPENDENT_EVIDENCE` 并降低机制信心，不自动判 FAIL。Negative control 有明确解释时可预先设计，并在看结果前写明判读；不得看到结果后临时翻转 sign。
- operator substitution 若保留相同经济角色且在现有 template operator slot 明确允许，可作为 local sibling；改变经济关系（如 ratio→difference 或 ranking→residualization）则是 `NEW_PROBE`。理论型 proposal（CAPM、GGM/DDM、DuPont、PEG 等）用已有 `semantic_contract`、`relationship_contract`、`field_relationship`、`expected_horizon` 说明适用对象、可能失效假设与可证伪观察；不加 schema 字段、不把社区公式直接晋升为 builtin。

## 自定义分组

- 分组参数必须使用从当前 BRAIN datafields 读到、`type=GROUP` 的真实 GROUP type 字段，并保留其 dataset provenance。不能把 `bucket(rank(...))` 等数值表达式当作 GROUP 字段；历史实测中这类表达式未产生预期分组。
- 自定义 GROUP 字段先做 live 能力与字段覆盖核验，再作为单一实验轴与已验证分组比较。分组会改变组内比较对象，不能假设它只是无害中性化；逐地区比较结果与 checks。

## 算子、模板与字段搜索

- 新算子先查当前 BRAIN operator catalog 与 live operator capability，并确认其输入类型与候选字段兼容；再说明算子如何承载当前机制。不得为提高算子使用数或 coverage quota 而硬塞未用算子，也不得用静态 operator list 替代 live capability。
- 新模板可先提出 proposal 并记录 contract 草案；只有当前 inventory 或明确进入 full profile 的 direct facade 暴露模板维护能力时，才 create/update template。不得因为 Skill 提到 template 就假设当前 Agent 能修改 catalog。新模板应封装可复用的经济机制与字段角色，注明方向、field type/dataset 约束、operator 数、numeric slots 的默认值/允许值/经济作用及可证伪结果。模板数量、候选表达式数量和字段数量都不是研究质量；没有机制或 live 能力证据时不新增模板。

只构造区分解释所需的最少候选。大批次有用当且仅当其分组有不同解释；不得生成笛卡尔积或 Python 规划的搜索循环。

## 研究循环、机制与模板归因

Agent 可按三个研究视角拆解问题；它们不是 Python lifecycle/state machine。`CORE_MECHANISM`、`AUX_PROCESSING`、`DECORRELATION_SIBLING`、`FIELD_DOMINATED_EVIDENCE` 等都是 proposal mapping 中的 Agent 概念，不新增 schema 字段：

```text
IDEA → IMPLEMENTATION → OPTIMIZATION / ROBUSTNESS
```

1. `IDEA`：预测什么经济机制、为什么关联未来收益、预期方向和可证伪证据是什么？
2. `IMPLEMENTATION`：用 BRAIN live fields、operators、horizon、field relationships 忠实表达 idea。
3. `OPTIMIZATION / ROBUSTNESS`：仅在已有可解释 signal 后，研究 settings、neutralization、preprocessing、局部参数邻域及稳健性。

proposal mapping 可标出 `CORE_MECHANISM` 与 `AUX_PROCESSING`。前者写核心经济关系（如 revision surprise、ratio/spread、residual relationship、event response、term structure）；后者写 backfill、winsorize、rank/zscore、grouping、neutralization 或 smoothing。这样才能判断变化改了机制还是实现稳健性；这些标签不进入 schema。

`TEMPLATE_EFFECT != FIELD_EFFECT`。单一强字段不能证明 template 可复用；若只有该字段支持，标为 `FIELD_DOMINATED_EVIDENCE`，不晋升 builtin。先用至少一种对照区分 template mechanism 与字段效应：同字段更简单 control、同 template 的语义近邻字段/兼容 dataset，或 template ablation。晋升还要能解释 `CORE_MECHANISM`、拆分 `AUX_PROCESSING`、说明稳定 operator roles 与至少一个 falsification test；不要求固定字段数或 dataset 数，不改 template schema。

以上是字段与机制的局部辨析，不单独构成可靠模板晋升。将论文映射变成候选 template 后，还须按[family-level BRAIN evidence](result-interpretation.md#template-evidence-and-promotion)区分可复用机制与偶然的单字段/单 Alpha winner；不得把 paper mapping 草案或一次支持性 Simulation 称为 validated template。

对 `A / B`、`A - B` 或 `A vs B`，事前说明 numerator/denominator role、经济维度、cadence compatibility 与关系为何有意义；两个字段各自有效不等于随机组合有意义。先比较 RAW RATIO/paired expression 与简单 control；只有当前问题需要时，再分别测试 ratio-level 或 operand-level preprocessing，每个 sibling 只变一个轴。看结果时解释 magnitude/distribution、coverage 和 concentration，不能只比 Sharpe。

官方 [BRAIN Alpha 示例](https://worldquantbrain.com/alpha-examples) 展示从 hypothesis、实现到 Simulation，并检查跨年表现与 coverage；[社区改进指南](https://github.com/alexisdpc/WorldQuant-alpha-trading/blob/main/ImprovingAlphas.md) 中的 operator 配方只作情境先验。引用外部资料时标 `EXTERNAL_HYPOTHESIS_SOURCE`：它只能生成 hypothesis/mechanism interpretation，不能证明 live field/operator 能力、平台规则、Simulation setting 或 Alpha performance。

## 风险、换手与中性化实验

`SUBMISSION_CUTOFFS_ARE_PLATFORM_FACTS`。Sharpe、Fitness、Sub-universe、IS Ladder、Correlation 等以当前 BRAIN 返回的 check/cutoff 为准；缺失时记 `UNKNOWN`，不重建公式或猜阈值。研究任务若设目标值，标 `TASK_SCOPED_TARGET`，不把它写成全局 success rule、Skill 常量或 Python threshold。

`METRIC SYMPTOM != MECHANISM DIAGNOSIS`。Low Sharpe、Low Fitness、Low Margin 或 High Turnover 不对应固定 operator。Low Sharpe 的 Agent explanation labels、返回与不稳定的诊断见[结果解释](result-interpretation.md)；先形成竞争原因，再用 matched variants。只用观察足够的指标决定下一问题，不从一次症状开自动处方。

`TURNOVER_REDUCTION != TURNOVER_MINIMIZATION`：目标是减少无意义 position churn，同时保留预测机制；比较 turnover、margin、returns、Sharpe、coverage 与 BRAIN checks。decay/smoothing 可能减 churn，也可能稀释信号，要用 matched variant 同看这些结果。高 turnover 本身不足以优先 `trade_when`。只有能说明 `EVENT CONDITION`、何时 open/update、为何在事件间 hold、可选 exit mechanism 时，才把它列为结构 proposal；普通连续信号中的 `trade_when` 是带独立经济解释的 `NEW_PROBE`。同时观察 event frequency、Alpha coverage、可用时 long/short coverage，以及稀疏触发造成的 performance loss；turnover 降低不单独证明改善。

PnL/表现不稳定时，先选一个可区分原因的轴：missingness transition（NaN↔non-NaN）用 RAW CONTROL vs BACKFILL SIBLING；signal temporal jumpiness 用 RAW vs DECAY/SMOOTHING SIBLING；若 evidence 显示少数股票主导，测试一个 truncation/concentration setting sibling。一次只改一个轴；不要同时叠加 backfill、decay、truncation 与 neutralization。对预处理 sibling 解释 coverage、concentration、PnL shape 和 year stability：RAW 更好可能是 tails/magnitude 含信息，也可能只是集中 outlier luck；rank 后失效可能是幅度信息被移除。

`NEUTRALIZATION IS A RISK-HYPOTHESIS`。改变设置前说明要消除的 exposure、它为何不是 Alpha 核心机制，以及选择 market/sector/industry/subindustry 的经济依据。资料按 dataset category 给出的 neutralization 建议只标 `PLATFORM_RESEARCH_PRIOR`，例如 fundamental/analyst 可从 industry 起步、news/sentiment 可比较 industry/subindustry、options 可比较 market/sector、PV 需警惕细分 neutralization 抹去信号；这些是待验证先验。BRAIN 官方示例自身也因问题不同采用不同设置，不能推广成 mapping。实际选择结合 live BRAIN availability、exposure hypothesis 与 matched Simulation evidence；不得建立 dataset→mandatory-neutralization mapping。若 expression 含 `group_neutralize(...)`，在 proposal mapping 标 `EXPRESSION_NEUTRALIZATION` 并一并检查 settings；双重 neutralization 只能作为独立 `NEW_PROBE`，不得在 Python 自动清空 settings。

`CORRELATION FITTING != RESEARCH DIVERSIFICATION`。高 correlation 时，先找保持原 idea 的 equivalent/semantically close field、same-role operator、reasonable horizon 或有意义 grouping。保持 same mechanism、field role 与 economic direction 的修改可标 `DECORRELATION_SIBLING`；改变机制就是 `NEW_PROBE`，不能为降 correlation 把新 Alpha 伪装成旧机制优化。地区资格、社区阈值与赛季规则均为 `CONTEXTUAL COMMUNITY EXPERIENCE`，不得提升成通用规则；BRAIN 保管 Alpha truth、submission 由人负责，本 Skill 不建 manager/queue/portfolio optimizer。
