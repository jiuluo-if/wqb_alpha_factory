# Alpha 模板生成约束

## 强制预读

这是模板目录的最近约束。任何 Agent 在本目录新增、修改、迁移、审查或扩展模板及其 schema/catalog/operator/horizon/settings 规则前，必须先阅读根目录 `AGENTS.md`、[`wqb_agent/AGENTS.md`](../AGENTS.md) 和本文件，并在工作记录中确认。未阅读不得变更；无法阅读时报告 `TEMPLATE_CONSTRAINTS_NOT_READ`。

tracked catalog 只能包含 TOY/SYNTHETIC/NON-RESEARCH 示例；真实模板、字段、表达式、经验和 evidence 只能存在本地私有目录。私有 catalog 仅按显式绝对路径、`WQB_ALPHA_TEMPLATE_CATALOG` 或用户 home 默认路径加载，缺失必须 fail-closed。

## 模板 owner 与契约

本目录拥有模板 schema、fail-closed loader、registry 与数值/算子审计。tracked catalog 只含公开 synthetic 素材；绝不包含生产表达式、私有字段 ID、固定私有配对、研究证据或习得先验。

`family` 与 `template_id` 只是出处/分组元数据，永不做语义或关系准入的分发依据。`semantic_contract` 是一元/主字段适配性的有界机器选择器；`relationship_contract` 是多字段关系的独立有界选择器。`field_relationship` 与机制文本只面向人读。旧模板可以按 `UNDECLARED` 加载，但在用户显式声明受支持的契约前保持仅审阅。

私有模板只从显式绝对构造器路径、`WQB_ALPHA_TEMPLATE_CATALOG` 或 `~/.wqb_alpha_factory/private/alpha_templates.toml` 加载。私有输入缺失报 `PRIVATE_TEMPLATE_CATALOG_MISSING`；绝不搜索 cwd/上级目录，也绝不回退到公开包 catalog。

每个私有模板声明 `role`、字段角色/关系、机制、方向与理由、期望 horizon、证伪、自相关影响、novelty 家族、settings 分支与 horizon profiles。`required_slots` 含全部渲染绑定：`p`/`data_field`、`s`、`t` 是经济字段槽，`g` 是控制绑定；`p` 与 `data_field` 是别名且不可共存。Probe 模板要求 4–6 个算子出现与 2–4 个概念经济字段；对照组是唯一允许 1–3 个算子/单字段的例外。Horizon 取值限定为 `[5, 22, 66, 120, 255]`；多窗口 profile 是有序相邻 lattice 周期，不是笛卡尔积。

算子覆盖是多样性目标，永不是在没有明确经济机制时添加算子的理由。优先在独立机制间广覆盖已验证算子，同时保持 arity、语义关系、novelty 与复杂度 gate。数值字面量按 fail-closed 分类；只有声明的 `RESEARCH_HORIZON` 槽可轮换。

`TemplateNumericSlot` 只支持显式 `RESEARCH_HORIZON` kind；其他/未声明数值 kind 不可物化或轮换。每个 horizon slot 必须是有限数值范围、`allowed_values` 唯一且非空、`default` 在允许值内，且所有 allowed value 属于 `HORIZON_LATTICE`。非 horizon 数字如安全 epsilon、算子参数仍按固定数值审查，不得伪装成可变 slot。

模板默认 `CONCRETE`。只有显式 `PARTIAL_OPERATOR` probe 兄弟模板可声明恰好一个有界 `TemplateOperatorSlot`；该兄弟必须保留其 concrete 父模板的机制、字段关系、方向、数值 profile、settings 分支与家族。物化只用声明算子与当前 `LIVE_VERIFIED` BRAIN 能力的交集。静态语法与 fixture 数据永不是可用性事实；恢复永不重渲染已物化的 proposal。

`AlphaFactory` 消费本 owner。不得在其他地方添加骨架、固定字段组合、参数网格或历史成功理由。

## 论文 candidate 与 Research mapping

`PAPER_TEMPLATE_CANDIDATE` 是 Agent-owned Research mapping，不是 `AlphaTemplate` schema、catalog entry 或持久化 registry。它保留 paper/source、publication date/status、claim、paper mechanism，以及每个拟议 template assumption 的来源；不得复制论文正文。negative control、falsification design、当前 BRAIN mapping 缺口和“改变此轴正在检验什么”也留在 mapping/workset。已有 `TemplateNumericSlot`/`TemplateOperatorSlot` 继续承载受限的 default、allowed values/operators、economic role 与 semantic contract；没有真实 consumer 证明前不加新的 schema 字段。

新 skeleton 只在现有 template 无法忠实表达 paper 的 CORE_MECHANISM 或 FIELD_RELATIONSHIP 时提出。它先是 candidate，不直接成为 reusable/private template。Research mapping 可选择复用现有模板、扩展已有 semantic slot、提出保持机制不变的 `NEW_PROBE` sibling，或提出新 skeleton。后两类仍按现有 AlphaTemplate owner 和 strict validation 走；catalog 晋升还必须有 BRAIN family-level evidence。不能用一个强 Alpha、paper result、fingerprint 或研究先验替代该 evidence。

`structural_fingerprint` 与 `mechanism_fingerprint` 用于 template 结构身份/机制分组和去重，不是性能、质量或可提交性评分。semantic role（如 direction、expectation、outcome、scale、confidence、context、cohort）是概念角色，应映射到现有 `p/data_field`、`s`、`t`、`g` 绑定；不得把真实 field ID 当 slot role。丰富的自然语言关系（如 spread、surprise、interaction、dispersion、co-movement 或 term structure）写入 `field_relationship`。`relationship_contract` 与 `semantic_contract` 必须使用当前 owner 支持且语义忠实的 bounded contract；无等价项时保留为 `CAPABILITY_MISSING` / unmaterialized candidate，不得硬塞相邻枚举。

每个拟议 numeric/operator/settings/horizon 轴都要在 Research mapping 说明 default、允许邻域、economic role 和变化要区分的预测。当前 catalog 的 horizon lattice、settings arms、operator-slot 限制及其他 strict checks 仍由本 owner 执行，不以论文建议或 Skill 文本绕过。
