# `wqb_agent` 局部约束

本目录实现远端优先（Remote-First）公开工具，不实现研究员状态机。Research Agent 可在真实运行证据触发的 `TOOL_OPTIMIZATION` 阶段修改确定性 research-facing 工具，但只能在 live research wave 已结束、且工具改动/测试期间不执行 live Simulation POST 时进行。

- `research_api.py` 是唯一 Agent-facing facade。
- Simulation 只能经 `SimulationGateway → Simulator → WQBClient` 写入 BRAIN。
- `ExecutionGuard` 只保存未解决远端写安全记录；`SUBMIT_UNKNOWN` 永不自动重 POST，已知 progress URL 只能原地轮询。exact-once 以单个 Simulation 为单位：Multi 的 parent 与每个 child 各自登记 `kind`/`parent_fingerprint`，重排、拆分、子集重试或 child 改走 Single 都不得再次 POST；恢复时按 `kind` 选择 Single 或 Multi 轮询。
- 全局 `research_status()` 的 pending blocker 保持原样。对新候选先调用 Gateway-owned READ_ONLY `research_batch_status(specs)`；它只隔离精确冲突的 proposals，为每个未解决 Multi parent 预留一个 8 槽上限中的位置，并返回其余候选的 live admission。写入前 Gateway 重复完整 preflight；不得清 guard、重发 unknown 或在 Agent/Python 里实现第二套准入判断。部分 blocked 不应让无关候选停摆。
- Region-Agnostic field preflight 由 Gateway 对 USA/EUR/ASI/GLB 子地区做 live catalog 查询；只有全部表达式字段在至少两个相同地区可用且类型检查通过时才派发。`region=ALL` 是 RA Simulation scope，不是 field-catalog scope；Agent 不得通过省略 fields 或改 scope 绕过准入。
- Gateway 是当前 Multi child/parent、并发、quota 与 rate-limit capacity 的唯一 owner。`research_batch_status()` 返回逐 proposal admission 和按未决 parent 保留槽位后的当前容量；写入前 Gateway 重查。Research API/MCP 不设研究批次最小值或累计 Simulation 解锁数。Research Agent 按研究理由与当前可用 capacity 提交任意有意义且 READY 的候选；小批次正常提交，单候选用 Single，不足容量时不填充候选。
- `RemoteAlphaRepository` 负责远端 Alpha 读取和可重建滚动缓存；live 响应优先。
- `AlphaFactory` 与 `alpha_templates/` 只负责纯候选生成和 schema/能力校验，不提交、不写研究结果、不输出下一步研究决策。
- 去重、相似性、分组和颜色必须与执行安全分离；除精确执行重复外，不得替 AI 强制阻止实验。
- credentials 只能由 resolver 解析，不写入 config、state、log、cache 或测试。
- 新代码不得引入研究结果数据库、研究生命周期、父子 lineage、自动 optimizer 或第二套 config/state 抽象。
- 工具优化只修可复现的 schema/validation、mapping、dispatch、reconcile、duplicate-scan 或 evidence-read 摩擦；不得把经济机制选择、字段排名、实验推荐或研究阶段状态机移入 Python。工具修改须使用 offline/fake-client tests 验证，之后经仓库交付门并以新 `research_status()` 握手恢复 live Research。

模板变更还必须遵守 `alpha_templates/AGENTS.md`。变更后运行直接相关测试、编译、Ruff 和 privacy gate；真实平台写操作不属于质量测试。
