# `wqb_agent/` 局部规则

本目录的正式 AI-facing surface 是 `research_api.py`。默认路径为：

```text
public research_api -> SimulationGateway -> Simulator / WQBClient
                   -> BRAIN canonical result -> RemoteAlphaRepository
```

## 读取与修改边界

- 先读根 [`AGENTS.md`](../AGENTS.md)、[`docs/ARCHITECTURE_AGENT.md`](../docs/ARCHITECTURE_AGENT.md) 和 [`research_api.py`](research_api.py)。
- 修改模板前必须再读 [`alpha_templates/AGENTS.md`](alpha_templates/AGENTS.md)。
- 研究判断由 AI 负责；Python 只负责平台事实、schema、exact-once、预算和安全边界。
- 真实 Simulation 只能经 `research_api.simulate()` / `simulate_batch()`；不得新增 POST 路径。
- `SUBMIT_UNKNOWN` 只能保留并对账已知 progress URL，禁止重发；缺失证据保持 `UNKNOWN/UNAVAILABLE`。
- Alpha submission 始终由用户手工完成。

## Owner 关系

- `simulation_gateway.py`：`SimulationSpec`、`ExecutionGuard` 和唯一 Simulation 写入口；不拥有指标、checks、PnL、trajectory 或 research state。
- `remote_alpha_repository.py`：BRAIN Alpha 的只读 evidence/metadata 读取与可重建本地 cache；cache 不能恢复指标或证据。
- `remote_quota.py`：唯一 `SimulationQuota`，只由远端 Alpha metadata 加 active guard 投影额度。
- `alpha_factory.py`：只生成可审阅 `SimulationSpec` probe，不执行、不写 inbox、不维护 round/session。
- `alpha_colors.py`、`alpha_grouping.py`：纯远端 Alpha 派生视图；颜色写入只能经显式 `sync_alpha_colors()` 并验证回读。
- `credentials.py`、`locking.py`：local-only 凭据与并发安全原语。

## 已退役的旧路径

`Agent`、`runtime_components.py`、`runtime_composition.py`、旧 workflows、
Trajectory/TrialLedger/Checkpoint、Factory session/control-plane 和
`proposals.json` 不属于新研究模型。兼容文件不得成为新代码 consumer；不得恢复
local result persistence、round/parent/lineage state 或第二套 workflow/config abstraction。

## 隐私

tracked 文件只能包含 synthetic/TOY 示例。真实字段、表达式、模板、Alpha evidence、
ExperienceMemory、trajectory、credentials 和 `.wqb_state/` 不得写入代码、文档、测试、
commit 或报告。提交前运行 `python scripts/check_repo_privacy.py`。

## 验证与交付

先跑受影响行为、changed Python 的 `py_compile` 和 Ruff；跨安全 owner 时扩大到对应 contract
suite。提交信息使用英文前缀加中文内容，邮箱必须为 `2966684515@qq.com`；验证通过后按用户授权推送，确认远端 SHA 与本地 HEAD 一致。
