---
name: spc-prompt-writer
description: Use when drafting or reviewing a WorldQuant BRAIN SPC investment thesis prompt or its sample JSON output.
---

# SPC Prompt 撰写与检查

SPC Prompt 是投资论点文本，不是本项目的 Alpha expression、`SimulationSpec` 或 Simulation 写链。需要撰写、修改或检查 SPC Prompt 时使用本技能；研究 Alpha expression 时使用项目的 `prompts/research_agent.md`。

## 先确认当前契约

本技能从本地参考资料提炼写作方法。SPC 的表单字段、字符限制、模型选项、更新频率、权重范围、输出格式和提交规则可能变化。实际使用前，以当前 BRAIN SPC 页面或用户提供的现行规则核对；无法核实时，将规则标为「待平台确认」，不要声称可提交或已通过验证。

## 撰写步骤

1. 明确经济论点、预测方向、观察期、股票范围、可查证的信息来源和一种可能推翻论点的情形。不要用空泛的“基本面好”等词代替可观察条件。
2. 将 Prompt 写成角色、任务、股票范围、输出和约束五个短段。要求模型使用当前可核实资料，并明确资料不足时如何减少或放弃持仓，避免编造标识符或证据。
3. 对照当前平台表单填写名称、Prompt 正文、模型、可选版本、运行频率、权重和样本输出等项目。只有在实时表单确认后才采用具体枚举值或限额。
4. 若当前平台仍要求 `ISIN|MIC → confidence`，样本输出必须是单一 JSON object；键是经过核实的证券标识符与交易所代码，值是有限数值，并满足平台当前允许范围。不要附加 Markdown、解释或非 JSON 文本。下面仅示意结构，不能直接提交：

   ```json
   {"<verified_ISIN>|<verified_MIC>": 0.25}
   ```

5. 用 JSON 解析器验证样本，逐项核对键、值、标识符来源和与 Prompt 指令的一致性；再在独立的新会话试跑，记录输出是否稳定。没有真实试跑时写「未实测」。

## 交付边界

- 交付 Prompt、表单填写建议、样本输出检查结果，以及仍需在平台确认的项目。
- SPC 与本项目 Alpha Simulation 分开；不得用 `research_api.simulate*` 测试 SPC Prompt，不得为 SPC 增加第二条 BRAIN 写入路径。
- 不自动提交 SPC 或 Alpha；真实股票标识符、私有研究表达式和账号信息不写入仓库。
