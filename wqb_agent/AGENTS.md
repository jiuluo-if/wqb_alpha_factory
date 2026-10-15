# `wqb_agent/` 局部职责

本目录实现 Remote-First research tools，不维护研究生命周期、研究结果库或自动研究循环。

- `research_api.py` 是唯一 Agent-facing facade。
- `RemoteAlphaRepository` 负责读取远端 Alpha 与维护可重建的滚动缓存；实时响应优先。
- `AlphaFactory` 与 `alpha_templates/` 只负责候选生成、物化和 schema/能力校验。模板局部规则见 [`alpha_templates/AGENTS.md`](alpha_templates/AGENTS.md)。
- Simulation 写链、Gateway 准入与容量、ExecutionGuard/exact-once、地区字段能力、隐私和 Research/TOOL_OPTIMIZATION 边界统一以根 [`AGENTS.md`](../AGENTS.md) 为准；本文件不重复这些跨组件契约。
- 工具变更使用 offline/fake-client 验证，不触发真实 Simulation POST。
