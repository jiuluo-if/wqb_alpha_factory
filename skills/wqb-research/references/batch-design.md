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
- 默认让一个候选表达一个经济机制，并先测一个信号字段，不加辅助腿。若要复合字段，先写明 field roles（各字段的经济角色）、它们为何属于同一机制、组合要解决的具体问题；在相同设置下比较组成信号与复合信号，做 ablation（消融）判断每一项是否提供了所声称的作用。若字段代表不同机制，将组合标成新的 `NEW_PROBE` 假设，不把它包装成去噪腿。
- 辅助、控制或 hedge 腿不是默认去噪器。只有在独立假设说明其风险作用，并通过“原信号单独 / 控制腿单独 / 两者组合”的匹配消融后，才保留该腿；同时检查它是否掩盖原字段信号或主导相关性。
- 需要给既有 agent 工作经验分配注意力时，按证据来源、所属任务、研究契约与时间新鲜度分层：BRAIN 当前 live 证据优先；本轮刚验证的工作集优先于旧 `tmp` 报告和缓存；旧记录先作为假设线索，重新核验后才恢复为当前约束。新证据与旧记录冲突时，保留旧记录的时间/范围并以新 live 结果更新工作集，不把记忆伪装成平台事实。
- 每轮结束时，用最新已验证结果更新简短工作集（强证据、失败归因、未决解释、下一实验）；保留旧记录的来源和日期，避免重复注入所有历史 agent 记忆。时间较近本身不等于更可靠，仍按证据质量和当前任务相关性判断。

## 语义配对、期限与预处理

- 多字段组合前声明 field role、semantic core 与 comparison meaning；优先配对同一指标的 actual/estimate 或不同预测 horizon。共享 semantic core、期限不同的字段可形成 `TERM_STRUCTURE` 假设；不同机制的字段组合要作为 `NEW_PROBE`，不能随机配对或标为 local sibling。
- 数据 cadence、发布日期/可用延迟与经济机制 horizon 是窗口选择的 prior，不是规则。用 anchor 加少量相邻且有经济理由的参数；不得假定低频字段必然需要更长窗口，也不做 Cartesian grid。
- 把 `ts_backfill`、`to_nan`、`winsorize`、`rank`、`zscore` 作为待检验的 preprocessing axis，先保留同字段/同机制/同 settings 的 `RAW CONTROL`，再做只改变预处理的 `PREPROCESSED SIBLING`；比较时记录 coverage/缺失变化。预处理失败或成功都由 Agent 解释，不自动套用。
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

对 `A / B`、`A - B` 或 `A vs B`，事前说明 numerator/denominator role、经济维度、cadence compatibility 与关系为何有意义；两个字段各自有效不等于随机组合有意义。先比较 RAW RATIO/paired expression 与简单 control；只有当前问题需要时，再分别测试 ratio-level 或 operand-level preprocessing，每个 sibling 只变一个轴。看结果时解释 magnitude/distribution、coverage 和 concentration，不能只比 Sharpe。

官方 [BRAIN Alpha 示例](https://worldquantbrain.com/alpha-examples) 展示从 hypothesis、实现到 Simulation，并检查跨年表现与 coverage；[社区改进指南](https://github.com/alexisdpc/WorldQuant-alpha-trading/blob/main/ImprovingAlphas.md) 中的 operator 配方只作情境先验。引用外部资料时标 `EXTERNAL_HYPOTHESIS_SOURCE`：它只能生成 hypothesis/mechanism interpretation，不能证明 live field/operator 能力、平台规则、Simulation setting 或 Alpha performance。

## 风险、换手与中性化实验

`SUBMISSION_CUTOFFS_ARE_PLATFORM_FACTS`。Sharpe、Fitness、Sub-universe、IS Ladder、Correlation 等以当前 BRAIN 返回的 check/cutoff 为准；缺失时记 `UNKNOWN`，不重建公式或猜阈值。研究任务若设目标值，标 `TASK_SCOPED_TARGET`，不把它写成全局 success rule、Skill 常量或 Python threshold。

`METRIC SYMPTOM != MECHANISM DIAGNOSIS`。Low Sharpe、Low Fitness、Low Margin 或 High Turnover 不对应固定 operator。Low Sharpe 的 Agent explanation labels、返回与不稳定的诊断见[结果解释](result-interpretation.md)；先形成竞争原因，再用 matched variants。只用观察足够的指标决定下一问题，不从一次症状开自动处方。

`TURNOVER_REDUCTION != TURNOVER_MINIMIZATION`：目标是减少无意义 position churn，同时保留预测机制；比较 turnover、margin、returns、Sharpe、coverage 与 BRAIN checks。decay/smoothing 可能减 churn，也可能稀释信号，要用 matched variant 同看这些结果。高 turnover 本身不足以优先 `trade_when`。只有能说明 `EVENT CONDITION`、何时 open/update、为何在事件间 hold、可选 exit mechanism 时，才把它列为结构 proposal；普通连续信号中的 `trade_when` 是带独立经济解释的 `NEW_PROBE`。同时观察 event frequency、Alpha coverage、可用时 long/short coverage，以及稀疏触发造成的 performance loss；turnover 降低不单独证明改善。

PnL/表现不稳定时，先选一个可区分原因的轴：missingness transition（NaN↔non-NaN）用 RAW CONTROL vs BACKFILL SIBLING；signal temporal jumpiness 用 RAW vs DECAY/SMOOTHING SIBLING；若 evidence 显示少数股票主导，测试一个 truncation/concentration setting sibling。一次只改一个轴；不要同时叠加 backfill、decay、truncation 与 neutralization。对预处理 sibling 解释 coverage、concentration、PnL shape 和 year stability：RAW 更好可能是 tails/magnitude 含信息，也可能只是集中 outlier luck；rank 后失效可能是幅度信息被移除。

`NEUTRALIZATION IS A RISK-HYPOTHESIS`。改变设置前说明要消除的 exposure、它为何不是 Alpha 核心机制，以及选择 market/sector/industry/subindustry 的经济依据。资料按 dataset category 给出的 neutralization 建议只标 `PLATFORM_RESEARCH_PRIOR`，例如 fundamental/analyst 可从 industry 起步、news/sentiment 可比较 industry/subindustry、options 可比较 market/sector、PV 需警惕细分 neutralization 抹去信号；这些是待验证先验。BRAIN 官方示例自身也因问题不同采用不同设置，不能推广成 mapping。实际选择结合 live BRAIN availability、exposure hypothesis 与 matched Simulation evidence；不得建立 dataset→mandatory-neutralization mapping。若 expression 含 `group_neutralize(...)`，在 proposal mapping 标 `EXPRESSION_NEUTRALIZATION` 并一并检查 settings；双重 neutralization 只能作为独立 `NEW_PROBE`，不得在 Python 自动清空 settings。

`CORRELATION FITTING != RESEARCH DIVERSIFICATION`。高 correlation 时，先找保持原 idea 的 equivalent/semantically close field、same-role operator、reasonable horizon 或有意义 grouping。保持 same mechanism、field role 与 economic direction 的修改可标 `DECORRELATION_SIBLING`；改变机制就是 `NEW_PROBE`，不能为降 correlation 把新 Alpha 伪装成旧机制优化。地区资格、社区阈值与赛季规则均为 `CONTEXTUAL COMMUNITY EXPERIENCE`，不得提升成通用规则；BRAIN 保管 Alpha truth、submission 由人负责，本 Skill 不建 manager/queue/portfolio optimizer。
