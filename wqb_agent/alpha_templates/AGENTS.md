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

模板默认 `CONCRETE`。只有显式 `PARTIAL_OPERATOR` probe 兄弟模板可声明恰好一个有界 `TemplateOperatorSlot`；该兄弟必须保留其 concrete 父模板的机制、字段关系、方向、数值 profile、settings 分支与家族。物化只用声明算子与当前 `LIVE_VERIFIED` BRAIN 能力的交集。静态语法与 fixture 数据永不是可用性事实；恢复永不重渲染已物化的 proposal。

`AlphaFactory` 消费本 owner。不得在其他地方添加骨架、固定字段组合、参数网格或历史成功理由。
