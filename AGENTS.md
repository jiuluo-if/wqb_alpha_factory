# WQB Alpha Factory Agent Guide

## 核心边界

```text
AI owns research reasoning.
Python owns platform truth and execution safety.
BRAIN owns Alpha simulation evidence.
```

唯一公开研究面是 `wqb_agent.research_api`。Python 不维护研究生命周期、结果数据库、父子关系、优化状态或自动研究循环；AI 读取 BRAIN live evidence 后决定下一份 `SimulationSpec`。

## 唯一 Simulation 写链

```text
research_api.simulate / simulate_batch
 → SimulationGateway
 → Simulator
 → WQBClient
 → BRAIN
```

不得新增其他 Simulation POST 路径，不得绕过 Gateway。Simulation 成功不等于经济机制成立；Alpha submission 永远人工完成。

## Gateway 与 ExecutionGuard

Gateway 只负责：规范化有效 settings、基本请求 schema、live field/operator capability、execution fingerprint、exact duplicate、concurrency/quota、transport safety、提交、轮询和恢复。不得把研究判断变成执行前硬 gate。

ExecutionGuard 是唯一远端写安全 owner，记录只允许 fingerprint、`SUBMITTING/RUNNING/SUBMIT_UNKNOWN`、progress URL、时间戳、bounded `simulation_count` 和可选 remote Alpha ID。`simulation_count` 只表示该 unresolved write 可能代表的 Simulation 数量，不保存 child payload 或结果；POST 前先持久化 `SUBMITTING`；进程异常后视为 `SUBMIT_UNKNOWN`，不得自动重 POST；已知 progress URL 只能轮询同一任务。

## Remote First

BRAIN 是 Alpha、Simulation、metrics、checks、aggregates、PnL 和 correlation 的事实源。本地 cache 只能作为可重建视图并标明 freshness，不能覆盖 live 或把 UNKNOWN 变成 PASS。不得恢复第二份研究结果数据库。

本地允许：ExecutionGuard、远端滚动 cache、外部 credentials 引用和进程锁。不得手改或删除真实运行目录中的未解决 guard；真实环境只做只读诊断和明确授权的恢复。

## 模板与隐私

模板变更前必须阅读本文件、`wqb_agent/AGENTS.md` 和 `wqb_agent/alpha_templates/AGENTS.md`。`alpha_templates` 是唯一模板 owner；公开 catalog 只能使用 synthetic 数据，私有 catalog 必须显式路径加载并 fail closed。

tracked 代码、测试、docs 和 fixtures 不得包含真实 Alpha、私有 field、完整研究表达式、credentials 或运行状态。质量检查不得触发 live Simulation POST。

## Research Skill

项目技能只维护在根目录 `skills/`，且只有一个核心 Skill：`skills/wqb-research/SKILL.md`，最多两个按需 reference。Skill 提供研究方法，不把缓存、Skill 或 memory 经验伪装成 BRAIN fact，也不新增平台写入入口。工具按 `research_tool_manifest()` 的 CORE profile 默认披露，低频能力需显式请求 full profile。

## 验证与交付

变更后运行与改动直接相关的定向测试、`python -m compileall -q wqb_agent scripts tests`、`python -m ruff check .`、全量 unittest 和 privacy gate；typed frontier 变更时运行对应 mypy。完成声明前必须使用 fresh 证据。

用户已持续授权：每次有效修改且验证通过后直接提交并推送。Git 邮箱必须为 `2966684515@qq.com`；提交信息必须使用英文前缀加中文内容（例如 `refactor：收敛执行边界`）。推送前 fetch `origin/main`，确认 exact remote SHA 与 CI；禁止强推、改写历史、跳过 CI 或上传无关改动。
