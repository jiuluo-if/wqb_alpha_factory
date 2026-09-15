# `docs/` 局部规则

`ARCHITECTURE_AGENT.md` 是面向 Agent 的简要架构地图；其他文档只在当前问题需要时阅读。

默认阅读路径是 `README.md` → `AGENTS.md` → `docs/ARCHITECTURE_AGENT.md` →
`wqb_agent/research_api.py`。历史 phase、迁移报告和已完成 commit 不在当前文档重复保存。

- `BRAIN_PROTOCOL.md` 描述平台事实、能力证据和 Retry-After。
- `reference/OPERATORS_CHEATSHEET.md`、`reference/SIMULATION_SETTINGS.md` 是运行时可能依赖的参考资料。
- `RESEARCH_POLICY.md` 描述研究纪律，不是实时平台状态。
- `STATE_LAYOUT.md` 描述恢复边界；其他路线图和文件组织文档不是默认入口。
- 历史 phase 与已完成的计划不属于当前 policy。

文档不得编造当前实验、指标或平台状态；文档工作不得修改 `.wqb_state`。编辑后检查链接、命令、标题、表格和代码路径。
