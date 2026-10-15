# `prompts/` 目录规则

Prompt 是 Agent 的研究规划与证据解释资料，不是执行入口；只说明如何读取 `research_api` 的平台事实、远端证据、去重提示和人工提交边界。

Research Agent 是唯一 owner，在 `RESEARCH` 与 `TOOL_OPTIMIZATION` 阶段间切换；不设独立 Maintenance Agent。两个文件分工如下，安全契约不互相复制：

- `research_agent.md`：唯一 Research Agent prompt，负责 hypothesis、实验选择和结果解释，并依据真实运行证据判断是否进入 `TOOL_OPTIMIZATION`。
- `maintenance_agent.md`：同一 Agent 在 `TOOL_OPTIMIZATION` 阶段使用的流程参考，不是独立角色或授权入口。

根 `AGENTS.md` 是架构与安全契约的唯一来源。两个 prompt 只能引用，不复制其正文。不得把当前平台状态、字段、指标或执行轮次写成永久事实；运行时通过 `research_api` 和 BRAIN 核验。
