# WQB Alpha Factory Agent 指南

## 核心边界

```text
AI 负责研究推理。
Python 负责平台事实与执行安全。
BRAIN 负责 Alpha 模拟证据。
```

唯一公开研究面是 `wqb_agent.research_api`。Python 不维护研究生命周期、结果数据库、父子关系、优化状态或自动研究循环；AI 读取 BRAIN 实时证据后决定下一份 `SimulationSpec`。

Agent 架构地图见 [`docs/ARCHITECTURE_AGENT.md`](docs/ARCHITECTURE_AGENT.md)；公开接口从 [`wqb_agent/research_api.py`](wqb_agent/research_api.py) 开始。

## 研究目标与平台机会

目标是长期可持续 Alpha 产出，同时兼顾高质量、低冗余以及当前 BRAIN submission / checks / correlation / cost 约束和 Genius / Theme / competition / consultant 等实际平台机会。平台规则、资格、活动和报酬属于易变 live facts；Research Agent 每个 research wave 应按当前 BRAIN/account evidence 与实际可用工具刷新，不能从旧 tmp 快照或固定 Skill/Python 阈值推断。没有当前证据时标 `UNKNOWN`。活动机会不能替代机制证据，也不能覆盖 BRAIN hard checks、安全约束或长期稳健性；不得承诺收益。

本项目的定向研究目标是大量产生高质量、低自相关、彼此正交的候选：在不同算子组合、live 数据集/字段、经济上可解释的自定义分组和模板机制之间开展批量 Multi GLB 实验；根据 BRAIN live evidence 筛选 Regular parent 晋升至 Region-Agnostic（RA）验证。候选目标为 Sharpe >3、Fitness >2、Margin >10、return 尽可能高；RA parent 在以上目标基础上至少两个地区 pass。低自相关可支持放宽次要研究阈值，但不得改写平台 hard checks，也须记录取舍证据。阈值是研究目标，不是平台能力真值。

## Research Agent 与工具优化阶段

```text
Research Agent:
RESEARCH → TOOL_OPTIMIZATION → RESEARCH
```

同一个 Research Agent 可根据当前 BRAIN evidence 与本地匿名 `tmp/research_handoff.json` 决定继续实验，或在自然 research-wave 边界进入 `TOOL_OPTIMIZATION`。只有真实运行证据表明工具限制造成确定性错误、浪费 Simulation、阻断 evidence 获取或反复人工 workaround 时，才优化工具；单纯的新经济想法直接留在 RESEARCH 阶段。

`TOOL_OPTIMIZATION` 可修改现有 Skill/prompt/reference、research-facing Python/MCP、small helper 和对应 tests，但不得创建研究生命周期、研究结果库、自动 optimizer、scheduler 或第二套 state。Python 只修确定性工具摩擦，不替 Agent 选择经济机制、候选或下一实验。

阶段必须互斥：活跃 live research wave 中不得并行修改会影响该 wave 的执行代码；进入 `TOOL_OPTIMIZATION` 后停止所有 live Simulation POST，只运行离线/fake-client tests 和静态质量门。工具修改经测试、commit/PR/CI 验证并部署后，Agent 重新执行 `research_status()`，确认新代码 contract/readiness，再回到 RESEARCH。Simulation 唯一写链、ExecutionGuard/exact-once、隐私边界和人工 Alpha submission 不变。

`tmp/research_handoff.json` 仍是本地匿名运行交接，不是 BRAIN truth 或研究数据库；可以承载少量当前 task/wave 的 readiness 摘要、工具清单、聚合工程摩擦计数和 safe reason codes，以便 Research Agent 判断是否需要 TOOL_OPTIMIZATION。不得写入私有研究内容，也不得由 Tool Optimization 阶段伪造或覆盖正在运行的 agent handoff。字段 owner、compact schema 和隐私细节见 [`docs/TMP_WORKSPACE_POLICY.md`](docs/TMP_WORKSPACE_POLICY.md)。

## 目标与证据

维护任务按此顺序判断：用户目标 > 可观察程序行为 > 与目标直接相关的验证 > 代码语义 > 文件元数据 > hash。
hash、SHA、checksum、cache key 与 mtime 只说明身份、完整性、来源、新鲜度或建议分组，不能证明任务完成、语义正确、bug 已修复或研究成功。
`submission_fingerprint` 是 ExecutionGuard 使用的安全关键 machine identity，必须保留；它不是代理指标问题。
`variant_family_fingerprint` 只用于建议分组；Git commit SHA 只用于代码来源、证据版本比较与 stale 判断；reference checksum 只用于 artifact 完整性/来源。
Git SHA 可确认 runtime evidence 对应版本、main 是否包含某 fix、handoff 是否 stale；是否修好仍须检查行为、测试或 artifact/runtime evidence。
mtime 只帮助判断是否值得重新读取；mtime 未变不能推出运行正常、batch 完成或没有新错误。
same hash != 功能正确；different hash != 修改正确；CI PASS != 用户目标自动满足；main SHA matched != runtime research 正常。
每次完成声明必须回答：目标是什么？哪个直接证据证明它？

## 唯一 Simulation 写链

```text
research_api.simulate / simulate_batch / simulate_multi_batch
 → SimulationGateway
 → Simulator
 → WQBClient
 → BRAIN
```

不得新增其他 Simulation POST 路径，不得绕过 Gateway。Simulation 成功不等于经济机制成立；Alpha 提交永远人工完成。

## Gateway 与 ExecutionGuard

Gateway 只负责：规范化有效 settings、基本请求 schema、live 字段/算子能力、执行指纹、精确去重、并发与配额、传输安全、提交、轮询和恢复。不得把研究判断变成执行前硬 gate。

`SimulationGateway` 是唯一 Simulation 写入准入 owner。`research_status()` 必须返回只表达“是否允许新的 Simulation write”的 `write_readiness`、有界 `write_blockers`、pending 计数和 contract version；Agent 只消费该结果，不自行放宽或重算。状态只取 `READY`、`BLOCKED_BY_REMOTE_STATE`、`WAITING_FOR_CAPABILITY`、`UNKNOWN`；只有适用的 Gateway admission scope 返回 `READY` 才允许新 POST：全局 `research_status()` 适用于无未决 guard 的场景，候选级 `research_batch_status(specs)` 仅允许该批中精确不冲突且其余条件通过的 proposals。任一 candidate 的 unresolved guard、官方 quota **已知耗尽**、write capability 不可用都 fail closed。官方 quota 只在成功 Simulation response 的 header 中出现，因此尚未观察到该事实时它保持 `UNKNOWN`，且不阻塞新的 Simulation write（否则任何新进程都无法发出第一个 POST）；`APPROXIMATE` estimate 永远不能把官方 quota `UNKNOWN` 提升为已知值或 `READY`。Startup status 只返回 pending summary；逐行诊断仅按需通过现有 READ_ONLY `research_api.get_pending_executions()` 获取。

Regular field capability 按候选自身的 live field scope 核验。`REGION_AGNOSTIC` 的表达式在 USA/EUR/ASI/GLB 子地区执行，而 field catalog 不接受聚合的 `region=ALL` scope；Gateway 分别查询四个子地区，并要求表达式声明的全部字段在至少两个相同子地区可用且 VECTOR 使用合法。少于两个完整地区时只阻断该候选并给出 capability reason；Agent 不得省略字段 provenance、改 region scope 或绕过检查。

`research_status()` 的 `BLOCKED_BY_REMOTE_STATE` 表示至少一个已有写入未解决，不得重发或清除它；它不自动证明所有不同候选都不可运行。候选批次先用 READ_ONLY `research_batch_status(specs)` 让 Gateway 按真实执行指纹逐项检查：命中 guard 的 proposal 标为 `SUBMIT_UNKNOWN`/重复并排除，互不冲突且能力、权限、quota 和字段/算子检查通过的 proposal 可获 `READY`。Gateway 写入时再次执行全部准入检查与远端去重。Research Agent 不得自行重算或覆盖该结论；若一部分候选被阻断，应保留其隔离并继续独立可执行的候选，只有批次级准入也阻断时才暂停写入，并沿现有 reconcile/能力恢复路径处理具体原因。

Multi 默认最多 8 个并发 parent；每个未解决的 `MULTI_PARENT`（包括 `SUBMIT_UNKNOWN`）保留一个平台并发槽，因此剩余容量为 `max(0, 8 - unresolved_multi_parents)`。容量不足时 Gateway 排队或返回明确 blocker，禁止超过平台总上限。Research API 与 MCP 默认拒绝少于 80 proposals 的 write wave；Gateway 在本地 guard 与远端去重后再次检查至少有 80 个新 child 可派发，低于下限时整批不 POST，已存在的 exact-once guard 保持原样。常规大规模研究每批至少提交 80 个经 candidate admission 确认的全新 child（8 个 parent ×10）；guard/重复/无效 proposals 不计入，须补充新候选。只有 fresh BRAIN evidence 可确认累计完成至少 4000 次后，Research Agent 才可显式请求较小的 `minimum_eligible_children` 并传入已完成计数；本地缓存/handoff 不能单独作为达标证明。计数未知或小于 4000 时保持 80。此批量要求不覆盖平台 quota、write readiness、exact-once 或远端 capability 硬限制。

ExecutionGuard 是唯一远端写安全负责方，记录只允许指纹、`SUBMITTING/RUNNING/SUBMIT_UNKNOWN`、progress URL、时间戳、有界 `simulation_count`、`kind`（`SINGLE`/`MULTI_PARENT`/`MULTI_CHILD`）、MULTI_CHILD 的 `parent_fingerprint` 和可选远端 Alpha ID。`simulation_count` 只表示该未解决写入可能代表的 Simulation 数量，不保存 child payload 或结果；POST 前先持久化 `SUBMITTING`；进程异常后视为 `SUBMIT_UNKNOWN`，不得自动重 POST；已知 progress URL 只能轮询同一任务。exact-once 以单个 Simulation 为单位：Multi POST 前 parent 与每个 child 各自登记，重排、拆分、子集重试或 child 改走 Single 都不得再次 POST；恢复时 `MULTI_PARENT` 用 multi 轮询，child 跟随其 parent。

## Remote First

BRAIN 是 Alpha、Simulation、指标、检查项、聚合、PnL 和相关性的事实源。本地缓存只能作为可重建视图并标明新鲜度，不能覆盖实时结果或把 `UNKNOWN` 变成 `PASS`。不得恢复第二份研究结果数据库。

本地允许：ExecutionGuard、远端滚动缓存、外部 credentials 引用和进程锁。不得手改或删除真实运行目录中的未解决 guard；真实环境只做只读诊断和明确授权的恢复。

## 本地 tmp 工作区

`tmp/` 是被 Git 忽略的本机临时工作区，不是研究事实源、备份或交付目录。tmp/ 历史材料已按性质平铺到一级 `scripts/`、`snapshots/`、`logs/`、`docs/`、`planning/`、`field_library/`、`patches/`、`maintenance/`、`knowledge/`，分类目录内不得建立子目录。新任务按性质直接写入这些目录，日期/task-id 编入文件名。历史文件正文和脚本路径文字不改写，只更新当前导航和 selector。缓存须可从明确来源重建并记录 owner/source；结项时清理缓存，失去 owner 的缓存在 30 天后进入清理候选。清理规则、分类和保护范围见 [`docs/TMP_WORKSPACE_POLICY.md`](docs/TMP_WORKSPACE_POLICY.md)。

## 模板与隐私

模板变更前必须阅读本文件、`wqb_agent/AGENTS.md` 和 `wqb_agent/alpha_templates/AGENTS.md`。`alpha_templates` 是唯一模板负责方；公开 catalog 只能使用 synthetic 数据，私有 catalog 必须按显式路径加载且缺失时 fail closed。

论文与模板来源、发表日期及哪些模板假设来自论文，保留在 Agent 的 `PAPER_TEMPLATE_CANDIDATE` Research mapping 中，不借此扩展第二套模板 schema 或研究数据库。若现有 AlphaTemplate 无法忠实表达新机制/字段关系，Agent 可以提出新 skeleton candidate；只有经过当前 owner validation 和 BRAIN family-level evidence 后，才考虑把真实模板写入显式 private catalog。结构/机制 fingerprint 只服务身份与去重，不能证明模板质量。

每个 paper/template 的 negative control、capability/mapping gaps 和 axis-specific testing question 也属于 Research mapping；candidate mapping 不能绕过 `alpha_templates` 的 strict validation、private catalog 边界、唯一 Simulation 写链或 human submission。

tracked 代码、测试、docs 和 fixtures 不得包含真实 Alpha、私有 field、完整研究表达式、credentials 或运行状态。质量检查不得触发线上 Simulation POST。

## Research Skill

项目技能只维护在根目录 `skills/`。研究运行时只有一个核心 Skill：`skills/wqb-research/SKILL.md`，最多两个按需 reference；另允许独立的 `skills/skill-authoring/SKILL.md` 维护 Skill 编写规范，不参与研究运行、不增加研究工具或状态。研究 Skill 提供研究方法，不把缓存、Skill 或 memory 经验伪装成 BRAIN 事实，也不新增平台写入入口。工具按 `research_tool_manifest()` 的 CORE profile 默认披露，低频能力需显式请求 full profile。

## 验证与交付

变更后运行与改动直接相关的定向测试、`python -m compileall -q wqb_agent scripts tests`、`python -m ruff check .`、全量 unittest 和 privacy gate；typed frontier 变更时运行对应 mypy。完成声明前必须使用新获取的证据。

用户已持续授权：每次有效修改且验证通过后直接提交并推送。Git 邮箱必须为 `2966684515@qq.com`；提交信息必须使用英文前缀加中文内容（例如 `refactor：收敛执行边界`）。推送前 fetch `origin/main`，确认 exact remote SHA 与 CI；禁止强推、改写历史、跳过 CI 或上传无关改动。
