# Alpha 模板（alpha_templates）

`wqb_agent.alpha_templates` 是模板模型、loader、registry、校验和 catalog 的唯一 owner。公开 catalog 只包含 synthetic 示例；生产模板必须通过明确的私有路径加载，缺失时 fail closed。

模板的职责是描述可执行表达式骨架和字段/operator slots。AlphaFactory 只生成 `SimulationSpec`，不提交 Simulation、不选择研究方向、不维护实验生命周期。

核心信息包括 `template_id`、表达式 skeleton、字段 roles、operator requirements、经济机制说明和可选 settings/tags。字段与 operator 的 arity、horizon lattice 和数值 slot 必须经过 schema 校验；模板生成本身不能触发网络写入。

Probe 是可审阅候选，不是自动 submission。AI 选择单个候选时调用 `simulate`，多个独立 Single 候选调用 `simulate_batch`，兼容的大批候选调用 `simulate_multi_batch`，再读取 BRAIN live evidence。
