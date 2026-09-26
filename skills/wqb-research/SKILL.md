---
name: wqb-research
description: "用于 WorldQuant BRAIN Alpha 研究：实验批次设计、Simulation 证据、结果解释与下一次实验选择。"
compatible_research_contract: "2026-09-26"
---

# WQB 研究

## 负责方

- BRAIN 是能力、字段、Simulation 与 Alpha 证据的首要事实源。
- Agent 拥有假设、机制、字段选择、实验分组与解释权。Python 校验确定性契约并安全执行，不做经济研究选择。
- Alpha 提交永远人工完成。所有 Simulation 写入走 `research_api → SimulationGateway → Simulator → WQBClient`。
- Simulation 结果不等于其主张机制得到支持。缺失证据保持 `UNKNOWN`/`UNAVAILABLE`。

## 契约握手

frontmatter 中 `compatible_research_contract` 是本文件编写时所针对的契约。先调用 `research_status()` 并比对其 `research_contract_version`；不匹配即本 Skill 为 `SKILL_STALE`：运行任何批次前重新阅读仓库 Skill 与 `AGENTS.md`。不得凭信任复用过期 Skill。

Skill/reference 提到的具体工具必须先对照当前会话的实际 inventory。Core 未暴露的低频能力若有等价 Core 路径则使用该路径；否则标记 `LOCAL_RESEARCH_CAPABILITY_LIMIT`，跳过依赖该能力的分支并继续其它可执行假设。不得臆造工具或绕过 facade。

## 每个研究批次

- 从多个竞争假设起步。使用 BRAIN 原始 dataset/datafield 列表，并解释每个 Agent 选定字段。
- 探索新字段、自定义分组、字段复合、新算子或新模板时，先读[批次设计](references/batch-design.md)中的 live discovery 与结构判据。
- 每个 proposal 都要有 1–48 字符、batch 内唯一的 `proposal_id`，并在调用写工具前保留 `proposal_id → hypothesis/note/template` mapping。适用时提供有用的 `note` 和模板/家族标签。批次按对照组、本地兄弟组、证伪组与新探针分组。
- `note` 可使用 `H2:EXPLORE`、`H3:CONTROL`、`H2:FALSIFY`、`H2:LOCAL` 等假设标签，由 Agent 在 mapping 中解释。Research MCP 结果仅投影 `proposal_id`、`status`、`reason_code`、`fingerprint`、`alpha_id` 和 `field_validation`；`note`、`template_id` 保留在 Agent mapping 中，`batch_fingerprint` 不回显。Multi child 可用 child fingerprint 通过 `reconcile_execution` 恢复 parent；direct Python facade 的既有返回兼容行为保持不变。
- 说明每组测试什么机制、什么结果会改变下一次决策、使用多少 Simulation。大量有目的的 Simulation 受欢迎；不可追溯的随机表达式不受欢迎。
- 保留探索。高结果只是比较候选，不是大举开发该方向的许可。
- 本地变体保持字段、算子与表达式拓扑不变；拓扑变化是带独立假设的 `NEW_PROBE`。
- 失败归因到假设、字段、算子、horizon、实现、相关性或稳健性。没有新证据不得重跑已明确失败的形态。
- Skill、记忆教训或历史运行不是 BRAIN 事实。记忆只保留少量可迁移研究教训；永不存完整转录。

## 方向与局部优化

- Probe 开始前声明 `direction`、`direction_reason` 和 `direction_transform`，理由必须先于结果；不得因 Sharpe 为负而事后反转方向。
- 复杂度按最终表达式的 effective operator occurrence 计算，`direction_transform` 等机械 wrapper 也计数。CONTROL 为 1–3，PROBE 为 4–6；上限是硬约束，不是目标，不加无经济理由的算子凑数。
- 局部优化先声明 immutable anchor。每个 variant 只改一个已声明的 numeric slot 或一个 settings key；字段、template、mechanism、算子顺序/拓扑和 operator count 保持不变。需要改拓扑时另立 `NEW_PROBE` 假设。
- 仅当当前 inventory 或明确进入 full profile 的 direct facade 暴露 `inspect_template()` 时，才依据其 `default`、`allowed_values` 与 `economic_role` 做 template-slot optimization。否则报告 `TEMPLATE_INSPECTION_CAPABILITY_MISSING`，归类为 `LOCAL_RESEARCH_CAPABILITY_LIMIT`；不得猜 allowed values、从旧缓存恢复当前 template contract、优化或提交依赖该槽位的 Simulation，但可继续普通 `NEW_PROBE`、字段研究和非 template-specific settings research。
- baseline 已有证据时不重复提交它。解释时比较 baseline 与所有 variants，不自动选 winner；若邻近变化没有一致、可解释的支持证据，停止该优化维度并保留其他解释。

## 研究工作集

为当前问题保持一个简短工作集，每轮覆盖：

```text
问题
活跃假设
强证据
被否决的解释
开放不确定性
有前景的家族
试验上下文（`family_label` / `related_trial_count_lower_bound` / `count_scope`）
下一实验
```

它是工作记忆而非档案：永不替代 BRAIN 证据；只有跨任务仍成立的经验才配进 reference。

## 权限

- `RESEARCH_MODE`（本 Skill）：运行 live 研究与 Simulation；永不改核心代码、`AGENTS.md` 或本 Skill。
- `MAINTENANCE_MODE`：改代码、测试与 Skill；永不执行 live Simulation POST。
- Skill 变更永不由单次 Simulation 生效：执行证据 → 提出 Skill diff → 独立测试/评审 → 接受或拒绝 → 之后研究在已接受版本下继续。

规划或标注大批次时读[批次设计](references/batch-design.md)；比较优胜者、检查稳健性或决定是否继续时读[结果解释](references/result-interpretation.md)。
