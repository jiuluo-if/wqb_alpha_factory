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

`proposal_id` 是临时结果的关联键；Agent 在写入前保留它与假设、`note`、`template_id` 的 mapping。Research MCP 对每项只投影 `proposal_id`、状态、reason code、fingerprint、Alpha ID 与字段校验结果；Agent 应从自己的 mapping 还原 note/template，不把这些解释信息写入 `ExecutionGuard` 或本地研究数据库。

为每个字段 ID 保留 dataset provenance 并传入 `SimulationSpec`。具体字段查询路径按当前会话 inventory 选择；Gateway 的 live 校验是安全校验，不是经济适配度评分。

## 新字段、单信号与复合

- 从当前 BRAIN `list_datasets()` 开始，再对问题相关的数据集发现字段：若当前 inventory 暴露 `list_all_datafields()`，用其 bounded pagination；否则用 `list_datafields(dataset_id, limit, offset, field_type)` 显式翻页。开始前设 page budget 或 time budget；预算耗尽但 API 返回的 count/offset 尚未证明覆盖完成时，标记 `FIELD_DISCOVERY_INCOMPLETE`，不得把部分结果说成完整覆盖。`research_api.list_all_datafields` 保留为 public-only 低频能力，不因此加入默认 Core。按语义关键词、字段 ID、`type`、描述、data coverage、dataset、region/universe/delay 与语义核心（semantic core）整理候选；历史未测清单可辅助检索，旧 `field_library`、字段 dump、reservoir 和 cache 只作带 freshness 的索引线索，不能证明当前字段存在、可用或适合 RA。
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
