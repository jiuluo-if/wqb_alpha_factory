---
name: brain-evidence-review
description: Use when explaining a BRAIN Alpha result, assessing whether its economic mechanism is supported, or choosing a follow-up experiment that separates competing explanations.
---

# BRAIN 证据归因

本技能只提供研究判断方法。先遵守项目根目录 `AGENTS.md`、`prompts/research_agent.md` 与 `docs/RESEARCH_POLICY.md`；平台事实和 Simulation 均通过 `wqb_agent.research_api`，BRAIN live evidence 优先。

## 把三件事分开

| 问题 | 应记录的结论 |
|---|---|
| 执行发生了什么？ | `DONE`、`FAILED_CHECK`、`SUBMIT_UNKNOWN` 等实际返回的状态，附来源与新鲜度。 |
| 预先声明的预测得到什么结果？ | 支持、反驳或证据不足；列出基线和所有已测对照。 |
| 哪个机制产生了结果？ | 对经济来源、替代解释和缺失证据分别判断；好看的指标本身不是机制证明。 |

## 最小分析流程

1. 在看结果前写清论点、经济机制、可证伪预测、主要竞争解释和一个能区分它们的对照。方向必须有事前经济理由，不因负 Sharpe 事后翻转。
2. 复用已有 BRAIN evidence，记录 Alpha ID、有效 settings、来源、新鲜度和未知项。只在研究问题需要时读取更深的 recordsets；缺失证据保持 `UNKNOWN`。
3. 按原始字段、经济组合、信号提取、门控或包装、settings 五层检查收益来源。若结果只在 `trade_when`、阈值或通用 wrapper 后出现，检查未门控基线、可比替代和合理负对照，避免把选择效应解释为字段机制。
4. 同时检查年度或市场阶段、子股票池、风险中性与可投资性、PnL 集中度、换手和相关性。使用 BRAIN 返回的实际可用数据；不把参考资料中的固定阈值写成项目硬 gate。
5. 输出完整比较：基线、所有已测变体、负对照、支持与反驳证据、竞争解释、尚缺的证据，以及下一项最能区分解释的实验。没有足够证据时结论为「未确认」。

## 下一项实验

优先用已有 siblings 和 recordsets 回答问题。确需新增 Simulation 时，由 AI 说明新增数量、预算、单一变化维度与停止条件，再构造并校验 `SimulationSpec`；小规模用明确 Single，合适的大规模使用明确 Multi，始终经过 `research_api → SimulationGateway → Simulator → WQBClient`。不要用固定四个 child、参数网格或自动循环代替研究理由。

本技能不建立研究结果数据库、自动评分晋升、Alpha 自动提交或新的 BRAIN POST 路径。`SUBMIT_UNKNOWN` 只对账，不重发。
