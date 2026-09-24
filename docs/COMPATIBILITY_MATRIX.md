# 兼容性与来源对比

本次对比使用当前 v2 树与正式 Agent 运行树 `wqb_alpha_factory`（只读检查，其被忽略的运行时状态/配置/credentials 未读取）。正式树 HEAD `61e553d` 是 v2 合并基点；对比开始时 v2 HEAD 为 `8c3b5ae`（领先 25 个提交、无落后），两棵 tracked 树当时均干净。`new_ai` 样本无 Git 元数据，其 `AIWorker/pyproject.toml` 版本（`1.0.0`）、被引用文件与项目结构是仅有的出处；其 `WebDataScope-1.7.3` 目录是独立扩展样本，不是 BRAIN 平台版本。

## 正式 Agent 运行已带入 v2 的变更

公共基点之后的 v2 提交逐步收紧并扩展了生产设计；证据是提交主题及其测试/源码 diff（非时间戳）：

| 变更 | 证据 | 兼容性决策 |
|---|---|---|
| 统一 Agent-facing `research_api`，移除废弃的 heartbeat/runtime 路径，Simulation 校验集中在 `SimulationGateway`。 | `72d959e`、`3cbf12a`、`wqb_agent/research_api.py`、`wqb_agent/simulation_gateway.py`、架构测试 | 保持单一 facade 与单一校验负责方。 |
| 更严格的不可变 `SimulationSpec`、Single/Multi 路由与有界 Multi 并发；之后在 guard 登记或 POST 前拒绝非 REGULAR 类型。 | `10ac730`、`0f0c086`、`2a8b13b`、`tests/test_simulation_gateway.py` | 在另一完整请求 schema 被独立验证前，`REGULAR` 是 writer 唯一支持的 schema。仅 OPTIONS 广告不构成写入契约。 |
| 类型化远端 Alpha 证据、惰性命名 recordset 读取、配额 header 观察与保守本地估算；移除不受支持的滚动配额假设与缓存行裁剪。 | `731f38f`、`1f24bfc`、`49fea5b`、`a76658d`、`wqb_agent/remote_evidence.py`、`remote_quota.py`、`alpha_feed_cache.py` | 平台配额与本地估算分离。旧滚动配额配置键仅作被忽略输入容忍，不得上报为平台事实。 |
| 缓存读取无副作用；schema 不匹配返回不可用而非删文件。 | `675ffc2`、`wqb_agent/alpha_feed_cache.py`、缓存测试 | 保持宽容、非破坏性读取；显式 refresh/purge 是唯一变更负责方。 |
| 只读远端 Alpha 证据、更丰富指纹与安全恢复投影；收敛到根目录单一 Skills 目录。 | `46f6871`、`5be75a1`、`docs/ARCHITECTURE_AGENT.md`、`skills/` | 保持 live-first 证据与两个项目技能；不得重建状态机或外部技能包。 |
| 可选的有界 stdio MCP 适配器。 | `8c3b5ae`、`wqb_agent/mcp_server.py`、`tests/test_mcp_server.py` | 仅作为 `research_api` 的传输层；不含 Simulation 或 Alpha 提交工具。 |

## 能力与兼容性矩阵

| 领域与来源证据 | 正式树 / 当前 v2 消费方 | 差距、价值与风险 | 决策 |
|---|---|---|---|
| **SPC prompt 规则** — `new_ai/AIWorker/spc-prompt-writer/SKILL.md`（本树已退役；单一 `skills/wqb-research/SKILL.md` 只覆盖研究方法） | v2 已无 SPC prompt/样例 JSON 消费方 | 样例技能断言精确表单字段、字符上限、模型/频率枚举与 `ISIN\|MIC` 权重范围；本次评审无官方表单契约可验证这些可变声明，照搬可能产生无效提交或虚假保证 | 条件指令保留在条件 prompt：先核对当前表单或用户提供的规则，否则把每条契约项标记为未验证。不得自动化 SPC 提交，不得用 Simulation 测试 SPC。 |
| **BRAIN MCP 面** — `new_ai/AIWorker/platform_functions.py`；v2 `wqb_agent/mcp_server.py`、`docs/MCP_READ_ONLY.md` | new_ai MCP 混合字段/能力、Alpha、年度/PnL/相关性/检查、论坛、竞赛、支付、选择、配置、Simulation 与提交调用；v2 实际 MCP 消费方是宿主 Agent，只暴露能力、模式、一页字段、Alpha 证据与本地未决 guard | 宽工具依赖 `cnhkmcp`、`fastmcp`、浏览器/LLM/数据包与直接平台实现；直接写与本项目的 Gateway guard 不兼容。v2 适配器响应有界、带来源/新鲜度标签与分类错误，有意省略非读操作 | 保持 5 工具可选只读适配器覆于 `research_api`。只有出现当前项目内消费方与已测试的 facade 契约才扩展。禁止直接 HTTP、`submit_alpha`、第二执行状态或从扩展便捷导入。 |
| **公开 API 名** — 正式 `wqb_agent/research_api.py`、prompt 与迁移说明；v2 facade、包导出与 manifest | 正式 Agent prompt 调 `simulate`、`simulate_batch`、`find_duplicate_alphas`；当前研究规格另显式要求 `simulate_single` | 单请求命名必须保持相同 Gateway 行为；冗余项无新操作 | 保留 `simulate` 与 `simulate_single`（同一单请求 Gateway 操作的两个名字），外加 `simulate_batch`、`simulate_multi_batch`、`find_duplicate_alphas`；`simulate_single_batch` 与 `find_alpha_duplicates` 保持退役。所有 Simulation 写入仍走同一 Gateway。 |
| **Single/Multi 派发与恢复** — `simulation_gateway.py`、`simulator.py`、`client.py`、guard 测试 | 两树均用 Gateway → Simulator → WQBClient；v2 增加 Multi parent 边界、并发/限流、child 计数与状态投影 | 超时或中断的 POST 可能模糊；重 POST 有重复烧预算风险；已知 progress URL 必须绑定同一远端任务；大 Multi 批次需按 parent/child 分别计数 | 保持 POST 前持久 guard、指纹、`SUBMIT_UNKNOWN` 不重 POST、已知 URL 只轮询、Multi 2–10 child。Single/Multi 测试只用 fake。 |
| **配额与配置兼容** — `config.py`、`remote_quota.py`、`protocol.py`、`BRAIN_PROTOCOL.md`、`remote_diagnostics.py` | `research_api.simulation_quota` 读取最近一次成功响应中有界配额 header，并单独从远端 Alpha 元数据加未决 guard 估算用量；配置支持有界的每日本地策略 | 正式配置曾有 daily 与 rolling 字段；v2 移除 rolling 字段，因为 Alpha 行不是精确 Simulation 台账且无已验证的滚动平台上限支撑；旧字段被忽略而非用于发明限额 | 保持 v2 的来源区分（`BRAIN_SIMULATION_HEADERS` 对近似本地估算）。无证据不推断 reset 单位或滚动平台配额。 |
| **远端 Alpha、recordset、相关性与检查** — `remote_evidence.py`、`remote_alpha_repository.py`、`protocol.py`、`BRAIN_PROTOCOL.md`、`skills/wqb-research/references/result-interpretation.md` | 当前 Agent 工具调 `get_alpha`、命名 metrics/aggregates/PnL/self-correlation 与 `get_alpha_evidence`；recordset 读取需已发现名称。证据 live-first，缺失数据保持 `UNKNOWN` | new_ai 的辅助 MCP 摊平大量记录并用扩展专属规则推导提交锁；本项目声明的当前官方边界支持 Alpha detail、recordset 发现/读取、activity diversity 与已文档化的相关性读取，部分检查仍为仅观察或 UNKNOWN。重实现扩展派生的 pass/fail 语义会把建议性证据变成硬 gate | 保持 BRAIN 为证据 owner，只读请求的有界证据，复用已返回证据，保留 checks/status 出处。不移植自动 pass/fail 规则，不自创本地检查。 |
| **S0–S9 研究技能** — `new_ai/AIWorker/ai-worker-skill/SKILL.md` 与 `wqb-*/SKILL.md`；v2 `prompts/research_agent.md`、`skills/wqb-research/SKILL.md` | new_ai 把研究组织为分阶段技能并引用 workflow/reference/data/memory 产物；多阶段假设固定四 child 调用、持久登记、打分/晋升或提交门。v2 消费方是保留推理与实验选择权的 Research Agent | 有价值的方法是事后可编辑/伪预测、竞争解释、阴性对照、基线/兄弟复用、wrapper 层归因、年度/子池/风险/可投资检查与稳健性复核。其硬编码序列、阈值、精确 child 数与状态依赖与本项目架构和已验证写入上限冲突 | 有价值方法保留为现有 prompt/证据技能中的 AI 指导。不建 Python 研究状态机、结果数据库、自动晋升分、固定四任务批次或强制研究门。 |
| **WebDataScope** — `new_ai/WebDataScope-1.7.3/`；v2 `wqb_agent/protocol.py`、`BRAIN_PROTOCOL.md` | 扩展观察/增强浏览器页面，含社区、备忘录、PnL 共享与遥测组件；当前无 v2 模块或 Agent 技能消费其 API | 扩展派生的提交检查分类不等于已验证的 `research_api` 契约；部分组件持久化或外传数据；引入会制造第二客户端、依赖与隐私审查面且无当前消费方 | 不集成。BRAIN 返回 checks 时带来源/新鲜度暴露，由 Agent 解释；未知检查保持未知。 |
| **安装与依赖** — new_ai `AIWorker/pyproject.toml` 与安装脚本；v2 `pyproject.toml`、`requirements.txt`、`credentials.py` | new_ai 要求 Python 3.12 与大批 MCP/浏览器/LLM/数据依赖，安装脚本配置平台与模型 credentials；v2 支持 Python 3.11+，核心只要求 `requests`，MCP 可选 | 照搬安装脚本会扩大攻击面并把本包耦合到无关系统；平台密码、API key 与私有数据不得回显、track 或经无关工具传递 | 核心安装保持小体积加可选 MCP extra。credentials 走既有外部 resolver；禁止明文展示、tracked 密钥或隐式账户配置。 |
| **日志与输出尺寸** — v2 `mcp_server.py`、`MCP_READ_ONLY.md`、`tests/test_mcp_server.py`、隐私规则 | MCP 是唯一传输层专属消费方：结果/行数/字符串有上限，除授权证据外脱敏 secrets 与表达式，错误省略原始响应文本 | 宽原始表格或传输消息可能泄露私有表达式或压垮 Agent 上下文；证据读取可经授权研究 facade 合法返回请求的表达式 | 保持有界投影、显式来源/新鲜度与逐工具 READ_ONLY 模式；表达式只允许出现在授权证据读取中。 |
| **测试与证据质量** — new_ai 扩展测试；v2 `tests/`、`scripts/run_targeted_tests.py`、隐私 gate | v2 有 fake-client 测试覆盖 API 契约、auth/权限/429、模糊 POST、进程恢复、缓存兼容、MCP 边界与输出隐私；CI 把变更代码映射到显式测试套件。WebDataScope 自带 JS 测试但不测本 Python facade | 无源产物或运行时移植行为会留下不可测假设；质量测试不得消耗线上 Simulation 配额或检查真实结果 payload | 保持 synthetic fixtures/fakes 与现有测试路由器。运行定向套件、全量请求 gate 与隐私检查；质量测试不调 BRAIN。 |

## 技能与引用完整性

v2 项目在根 `skills/` 下只保留一个核心技能目录 `wqb-research`，含两个按需 reference（`batch-design.md`、`result-interpretation.md`）：有 YAML frontmatter、作用域描述，无仓内第二技能根；早前的 `spc-prompt-writer` 与 `brain-evidence-review` 技能已退役，其指令现由单一研究技能覆盖。new_ai 样本含 18 个 worker 技能目录加一个 umbrella 技能与 SPC 技能；其内部 Markdown 链接在给定树内可解析，但技能引用的 `workflow/`、`reference/`、`data/`、`memory/` 产物在样本中技能旁并不存在，因此其 schema 与平台断言不能当作自包含的当前契约。

## 未验证的平台事实

本次评审没有可用的当前已认证 BRAIN SPC 表单。SPC 字段、字符上限、模型名、频率取值、权重边界与 `ISIN|MIC` 样例契约均为**未验证**。官方公开 [BRAIN 竞赛页](https://platform.worldquantbrain.com/competition/) 只是通用登录/竞赛落地页，不暴露这些表单细节。同样，平台广告的 Simulation 类型不足以证明本客户端完整写入 payload 受支持；非 REGULAR 类型保持被拒，直到完整请求 schema 被独立验证并测试。
