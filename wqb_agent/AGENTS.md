# `wqb_agent` 局部约束

本目录实现远端优先（Remote-First）公开工具，不实现研究员状态机。Research Agent 可在真实运行证据触发的 `TOOL_OPTIMIZATION` 阶段修改确定性 research-facing 工具，但只能在 live research wave 已结束、且工具改动/测试期间不执行 live Simulation POST 时进行。

- `research_api.py` 是唯一 Agent-facing facade。
- Simulation 只能经 `SimulationGateway → Simulator → WQBClient` 写入 BRAIN。
- `ExecutionGuard` 只保存未解决远端写安全记录；`SUBMIT_UNKNOWN` 永不自动重 POST，已知 progress URL 只能原地轮询。exact-once 以单个 Simulation 为单位：Multi 的 parent 与每个 child 各自登记 `kind`/`parent_fingerprint`，重排、拆分、子集重试或 child 改走 Single 都不得再次 POST；恢复时按 `kind` 选择 Single 或 Multi 轮询。
- `RemoteAlphaRepository` 负责远端 Alpha 读取和可重建滚动缓存；live 响应优先。
- `AlphaFactory` 与 `alpha_templates/` 只负责纯候选生成和 schema/能力校验，不提交、不写研究结果、不输出下一步研究决策。
- 去重、相似性、分组和颜色必须与执行安全分离；除精确执行重复外，不得替 AI 强制阻止实验。
- credentials 只能由 resolver 解析，不写入 config、state、log、cache 或测试。
- 新代码不得引入研究结果数据库、研究生命周期、父子 lineage、自动 optimizer 或第二套 config/state 抽象。
- 工具优化只修可复现的 schema/validation、mapping、dispatch、reconcile、duplicate-scan 或 evidence-read 摩擦；不得把经济机制选择、字段排名、实验推荐或研究阶段状态机移入 Python。工具修改须使用 offline/fake-client tests 验证，之后经仓库交付门并以新 `research_status()` 握手恢复 live Research。

模板变更还必须遵守 `alpha_templates/AGENTS.md`。变更后运行直接相关测试、编译、Ruff 和 privacy gate；真实平台写操作不属于质量测试。
