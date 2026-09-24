---
name: wqb-research
description: "用于 WorldQuant BRAIN Alpha 研究：实验批次设计、Simulation 证据、结果解释与下一次实验选择。"
compatible_research_contract: "2026-09-24"
---

# WQB 研究

## 负责方

- BRAIN 是能力、字段、Simulation 与 Alpha 证据的首要事实源。
- Agent 拥有假设、机制、字段选择、实验分组与解释权。Python 校验确定性契约并安全执行，不做经济研究选择。
- Alpha 提交永远人工完成。所有 Simulation 写入走 `research_api → SimulationGateway → Simulator → WQBClient`。
- Simulation 结果不等于其主张机制得到支持。缺失证据保持 `UNKNOWN`/`UNAVAILABLE`。

## 契约握手

frontmatter 中 `compatible_research_contract` 是本文件编写时所针对的契约。先调用 `research_status()` 并比对其 `research_contract_version`；不匹配即本 Skill 为 `SKILL_STALE`：运行任何批次前重新阅读仓库 Skill 与 `AGENTS.md`。不得凭信任复用过期 Skill。

## 每个研究批次

- 从多个竞争假设起步。使用 BRAIN 原始 dataset/datafield 列表，并解释每个 Agent 选定字段。
- 每个 proposal 给 `proposal_id`、有用的 `note`，适用时加模板/家族标签。批次按对照组、本地兄弟组、证伪组与新探针分组。
- `note` 是每个结果回显的假设链接，如 `H2:EXPLORE`、`H3:CONTROL`、`H2:FALSIFY`、`H2:LOCAL`。Python 原样返回该标签，永不解释它。
- 说明每组测试什么机制、什么结果会改变下一次决策、使用多少 Simulation。大量有目的的 Simulation 受欢迎；不可追溯的随机表达式不受欢迎。
- 保留探索。高结果只是比较候选，不是大举开发该方向的许可。
- 本地变体保持字段、算子与表达式拓扑不变；拓扑变化是带独立假设的 `NEW_PROBE`。
- 失败归因到假设、字段、算子、horizon、实现、相关性或稳健性。没有新证据不得重跑已明确失败的形态。
- Skill、记忆教训或历史运行不是 BRAIN 事实。记忆只保留少量可迁移研究教训；永不存完整转录。

## 研究工作集

为当前问题保持一个简短工作集，每轮覆盖：

```text
问题
活跃假设
强证据
被否决的解释
开放不确定性
有前景的家族
下一实验
```

它是工作记忆而非档案：永不替代 BRAIN 证据；只有跨任务仍成立的经验才配进 reference。

## 权限

- `RESEARCH_MODE`（本 Skill）：运行 live 研究与 Simulation；永不改核心代码、`AGENTS.md` 或本 Skill。
- `MAINTENANCE_MODE`：改代码、测试与 Skill；永不执行 live Simulation POST。
- Skill 变更永不由单次 Simulation 生效：执行证据 → 提出 Skill diff → 独立测试/评审 → 接受或拒绝 → 之后研究在已接受版本下继续。

规划或标注大批次时读[批次设计](references/batch-design.md)；比较优胜者、检查稳健性或决定是否继续时读[结果解释](references/result-interpretation.md)。
