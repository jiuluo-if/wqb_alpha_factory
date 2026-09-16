# `prompts/` 局部规则

Prompt 是 Agent 的研究规划和证据解释资产，不是执行入口。它只描述如何读取
`research_api` 的平台事实、远端 evidence、去重提示和手工提交边界。

两层角色各自只有一个 prompt，且不互相复制契约：

- `maintenance_agent.md`：外层维护 Agent（architecture、tests、docs、privacy、profiling、dependency、交付）。
- `research_agent.md`：内层研究 Agent（hypothesis、`OptimizationDecision`、结果解释）。

`AGENTS.md` 仍是架构与安全契约的唯一 source-of-truth；两个 prompt 只能引用它，不得复制其正文。

不得把当前平台状态、fields、metrics 或执行轮次事实写成永久事实；运行时必须通过
`research_api` 和 BRAIN 验证。
