# WQB Alpha Factory Agent 指南

## 核心边界

```text
AI 负责研究推理。
Python 负责平台事实与执行安全。
BRAIN 负责 Alpha 模拟证据。
```

唯一公开研究面是 `wqb_agent.research_api`。Python 不维护研究生命周期、结果数据库、父子关系、优化状态或自动研究循环；AI 读取 BRAIN 实时证据后决定下一份 `SimulationSpec`。

## 唯一 Simulation 写链

```text
research_api.simulate / simulate_batch
 → SimulationGateway
 → Simulator
 → WQBClient
 → BRAIN
```

不得新增其他 Simulation POST 路径，不得绕过 Gateway。Simulation 成功不等于经济机制成立；Alpha 提交永远人工完成。

## Gateway 与 ExecutionGuard

Gateway 只负责：规范化有效 settings、基本请求 schema、live 字段/算子能力、执行指纹、精确去重、并发与配额、传输安全、提交、轮询和恢复。不得把研究判断变成执行前硬 gate。

ExecutionGuard 是唯一远端写安全负责方，记录只允许指纹、`SUBMITTING/RUNNING/SUBMIT_UNKNOWN`、progress URL、时间戳、有界 `simulation_count`、`kind`（`SINGLE`/`MULTI_PARENT`/`MULTI_CHILD`）、MULTI_CHILD 的 `parent_fingerprint` 和可选远端 Alpha ID。`simulation_count` 只表示该未解决写入可能代表的 Simulation 数量，不保存 child payload 或结果；POST 前先持久化 `SUBMITTING`；进程异常后视为 `SUBMIT_UNKNOWN`，不得自动重 POST；已知 progress URL 只能轮询同一任务。exact-once 以单个 Simulation 为单位：Multi POST 前 parent 与每个 child 各自登记，重排、拆分、子集重试或 child 改走 Single 都不得再次 POST；恢复时 `MULTI_PARENT` 用 multi 轮询，child 跟随其 parent。

## Remote First

BRAIN 是 Alpha、Simulation、指标、检查项、聚合、PnL 和相关性的事实源。本地缓存只能作为可重建视图并标明新鲜度，不能覆盖实时结果或把 `UNKNOWN` 变成 `PASS`。不得恢复第二份研究结果数据库。

本地允许：ExecutionGuard、远端滚动缓存、外部 credentials 引用和进程锁。不得手改或删除真实运行目录中的未解决 guard；真实环境只做只读诊断和明确授权的恢复。

## 模板与隐私

模板变更前必须阅读本文件、`wqb_agent/AGENTS.md` 和 `wqb_agent/alpha_templates/AGENTS.md`。`alpha_templates` 是唯一模板负责方；公开 catalog 只能使用 synthetic 数据，私有 catalog 必须按显式路径加载且缺失时 fail closed。

tracked 代码、测试、docs 和 fixtures 不得包含真实 Alpha、私有 field、完整研究表达式、credentials 或运行状态。质量检查不得触发线上 Simulation POST。

## Research Skill

项目技能只维护在根目录 `skills/`，且只有一个核心 Skill：`skills/wqb-research/SKILL.md`，最多两个按需 reference。Skill 提供研究方法，不把缓存、Skill 或 memory 经验伪装成 BRAIN 事实，也不新增平台写入入口。工具按 `research_tool_manifest()` 的 CORE profile 默认披露，低频能力需显式请求 full profile。

## 验证与交付

变更后运行与改动直接相关的定向测试、`python -m compileall -q wqb_agent scripts tests`、`python -m ruff check .`、全量 unittest 和 privacy gate；typed frontier 变更时运行对应 mypy。完成声明前必须使用新获取的证据。

用户已持续授权：每次有效修改且验证通过后直接提交并推送。Git 邮箱必须为 `2966684515@qq.com`；提交信息必须使用英文前缀加中文内容（例如 `refactor：收敛执行边界`）。推送前 fetch `origin/main`，确认 exact remote SHA 与 CI；禁止强推、改写历史、跳过 CI 或上传无关改动。
