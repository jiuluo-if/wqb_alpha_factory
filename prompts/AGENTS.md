# `prompts/` 局部规则

Prompt 是 Agent 的研究规划和证据解释资产，不是执行入口。它只描述如何读取
`research_api` 的平台事实、远端 evidence、去重提示和手工提交边界。

只设一个 Research Agent owner；它在 RESEARCH 与 TOOL_OPTIMIZATION 阶段间切换，不把任务交给独立 Maintenance Agent。两个文件分工如下，且不互相复制安全契约：

- `research_agent.md`：唯一 Research Agent prompt，负责 hypothesis、实验选择、结果解释，并按真实运行证据决定是否进入 TOOL_OPTIMIZATION。
- `maintenance_agent.md`：同一个 Research Agent 在 TOOL_OPTIMIZATION 阶段采用的 procedure reference，不是独立角色或独立授权入口。

`AGENTS.md` 仍是架构与安全契约的唯一 source-of-truth；两个 prompt 只能引用它，不得复制其正文。

不得把当前平台状态、fields、metrics 或执行轮次事实写成永久事实；运行时必须通过
`research_api` 和 BRAIN 验证。
