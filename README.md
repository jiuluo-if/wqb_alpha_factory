# WQB Alpha Factory

这是一个面向 AI Agent 的 WorldQuant BRAIN 研究工具。Agent 负责研究判断，Python 负责平台事实与远端写入安全，BRAIN 负责 Alpha/Simulation 结果的唯一事实来源。

## 当前目标架构

```text
AI
 ↓
wqb_agent.research_api
 ↓
SimulationGateway + ExecutionGuard
 ↓
BRAIN Simulation
 ↓
RemoteAlphaRepository
 ↓
AI 读取真实 evidence 后决定下一次 Simulation
```

主要能力：

- Real Simulation：`SimulationSpec`、实时算子能力校验、fingerprint 去重、exactly-once guard、已知 progress URL 恢复。
- Remote Alpha Data：live Alpha、metrics、checks、aggregates、PnL、self-correlation，以及可重建的短期 metadata cache。
- Probe Factory：从已验证 field/template 生成可审阅的 `SimulationSpec`，不提交 Simulation、不创建研究状态机。
- Dedupe / Group / Color：执行去重与研究相似性分离；分组和颜色只消费 remote evidence。

## Agent-facing API

```python
from wqb_agent.research_api import (
    SimulationSpec, discover_fields, generate_probes, simulate,
    simulate_batch, get_alpha_evidence, compare_alphas, group_alphas,
    preview_alpha_colors,
)

spec = SimulationSpec(
    expression="rank(close)", settings={"delay": 1}, fields=("close",)
)
result = simulate(spec)
evidence = get_alpha_evidence(result["alpha_id"])
```

推荐闭环是：

```text
discover_fields → generate_probes → AI 审阅 → simulate/simulate_batch
→ get_alpha_evidence → AI 决定下一次 SimulationSpec
```

`run_experiment(ExperimentSpec)`、`run-proposals`、旧 round/parent/lineage、Trajectory/TrialLedger 和 Factory control-plane 目前只作为迁移期兼容层，不是新的研究事实来源；新代码不得依赖它们建立第二套流程。

## 本地状态

长期本地状态只允许包括 `.wqb_state/execution_guard.json`、可重建的 `.wqb_state/.alpha_feed_cache/`、外部 credentials 与进程锁。metrics、checks、PnL、aggregates、correlation、validation、reward 和研究结论必须从 BRAIN live API 获取。

## 安全不变量

- POST 前先持久化 `SUBMITTING`；中断后重启只能变成 `SUBMIT_UNKNOWN`，禁止自动重 POST。
- 已知 `progress_url` 只能轮询同一远端任务。
- 缺失或过期 evidence 保持 `UNKNOWN`/`UNAVAILABLE`，不能伪装成 PASS。
- Alpha submission 始终人工完成；颜色 metadata PATCH 是独立且显式授权的操作。
- tracked tests/docs/fixtures 不得包含真实 Alpha、私有 field、表达式、凭据或研究结果。

## 验证

```powershell
python scripts/run_targeted_tests.py --files <changed-files>
python -m ruff check .
python scripts/check_repo_privacy.py
```

完整质量门由 CI 执行。真实工作区若 preflight 为 `BLOCKED`，只能进行只读对账/恢复，不得启动新的 Simulation。
