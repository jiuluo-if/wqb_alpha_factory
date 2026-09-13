# Alpha templates

`wqb_agent.alpha_templates` 是模板模型、loader、registry、numeric audit 和
operator coverage 的唯一 owner。tracked `catalog/builtin.toml` 仅含明确的
TOY/SYNTHETIC/NON-RESEARCH 示例，绝不是 production fallback。

模板新增或拓展必须先阅读并遵守模板目录最近的 [`AGENTS.md`](../wqb_agent/alpha_templates/AGENTS.md)，同时确认根 `AGENTS.md` 的模板变更门禁。

## Private catalog

真实模板、私有字段、固定字段配对、表达式、经验和研究证据只允许存在
gitignored local catalog。加载顺序是显式绝对路径、
`WQB_ALPHA_TEMPLATE_CATALOG`、`~/.wqb_alpha_factory/private/alpha_templates.toml`；
缺失返回 `PRIVATE_TEMPLATE_CATALOG_MISSING`，不搜索 cwd/父目录，也不回退公开 catalog。

## Generation contract

`factory_100` 是纯 Probe Round：模板适配器只生成
`factory/exploration/EXPLORE/BASELINE` 的 `signal_discovery` 题案，并由独立的
100-slot Probe 选择契约筛选；它不接受或混入 OptimizationDecision、CHILD 或
ROBUSTNESS。`targeted_optimization` 才是 Optimization Round，必须由正式
`OptimizationDecision` 生成。两者可以共享最终 Simulation 执行链，但不共享研究准入、
选择预算或结果语义。

私有 `PROBE_ALPHA` 必须声明经济机制、字段角色/关系、方向及理由、falsification、
expected horizon、self-correlation impact、novelty family 和 settings arms，并使用
4–6 个算子出现次数、2–4 个经济字段。`CONTROL_ALPHA` 才可使用 1–3 个算子和单字段，
且不因 control 结果直接成为 submission candidate。

研究窗口只使用 `5, 22, 66, 120, 255`。多窗口只选一个有序相邻 profile，不做笛卡尔积；
Universe、Decay、Truncation arm 每次只改变一个主要变量。算子覆盖应在算子已验证且有
明确经济效应时尽可能广泛，但不能为了覆盖率无意义叠加复杂度。

## Schema and validation

loader 使用 `importlib.resources`/`tomllib`，对重复 ID、缺字段、非法 slot/group/direction、
非 lattice horizon、未声明数字、无效 slot 和缺失经济语义 fail-closed。fingerprint 分离
structural、mechanism、instantiation 三类身份；窗口变化不是新经济 Alpha。

新增模板规则详见同目录 [`AGENTS.md`](../wqb_agent/alpha_templates/AGENTS.md)。
