# WQB Alpha Factory

面向 AI Research Agent 的 WorldQuant BRAIN 工具。AI 负责研究推理与下一次实验选择；Python 负责平台访问、远端写入安全和可重建缓存；BRAIN 是 Simulation 与 Alpha evidence 的事实源。

## 架构

```text
AI
 ↓
wqb_agent.research_api
 ↓
SimulationGateway → Simulator → WQBClient
 ↓
BRAIN
 ↓
RemoteAlphaRepository → AI 分析 evidence
```

## 能力

- 平台事实：`get_capabilities()`、`discover_fields()`、`get_operator_reference()`。
- Probe/template：`list_templates()`、`inspect_template()`、`generate_probes()`。
- Real Simulation：`validate_simulation_spec()`、`simulate_single()` / `simulate_single_batch()`、`simulate_multi_batch()`，带实时能力检查、fingerprint、exact-once guard 和已知进度任务恢复。
- Simulation 模式：小规模优化可使用 Single 或小批量 Multi；大规模探针使用 Multi，每个 Multi parent 默认/最多 10 个 child（最少 2 个），默认同时 dispatch 2 个 parent，显式 override 的受支持硬上限为 8。`Region-Agnostic Simulation` 保持独立，当前没有经过验证的写入契约，不会被 Multi 窗口隐式替代。
- Remote Alpha：live Alpha/evidence、滚动缓存、去重、比较、分组和颜色预览/同步。

```python
from wqb_agent.research_api import SimulationSpec, simulate, get_alpha_evidence

result = simulate(SimulationSpec(
    expression="rank(close)",
    settings={"delay": 1},
    fields=("close",),
))
evidence = get_alpha_evidence(result["alpha_id"])
```

推荐闭环：

```text
discover → generate/review → simulate → read live evidence
→ AI 分析 → AI 创建下一份 SimulationSpec
```

## 本地边界

本地只保存未解决的 ExecutionGuard、可重建的远端缓存、外部 credentials 引用和进程锁。指标、checks、PnL、aggregates、correlation 与研究结论均不由本地数据库维护。

ExecutionGuard 的安全不变量：

- POST 前持久化 `SUBMITTING`。
- 进程中断后转为 `SUBMIT_UNKNOWN`，不得自动重 POST。
- 已知 `progress_url` 只能轮询同一任务。
- 相同 expression 与 effective settings 的 active execution 只允许一次 POST。
- Alpha submission 始终人工完成；颜色 PATCH 是独立的显式远端 metadata 操作。

## Optional MCP read tools

Install `python -m pip install -e ".[mcp]"` and start `alpha-factory-mcp` in
the project directory to expose bounded read-only capability, permission,
field, Alpha evidence, and local pending-guard tools over stdio. The exact tool
contract and output bounds are in [`docs/MCP_READ_ONLY.md`](docs/MCP_READ_ONLY.md).

## 配置与验证

配置使用 `simulation`、`runtime`、`remote_cache`、`quota`、`factory` 五个顶层 section，示例见 `config.example.json`。

```powershell
python main.py diagnostics doctor
python main.py diagnostics audit
python scripts/run_targeted_tests.py --files <changed-files>
python -m unittest discover -s tests
python -m ruff check .
python scripts/check_repo_privacy.py
```

tracked tests、docs 和 fixtures 只能使用 synthetic 数据；真实 Alpha、私有 field、研究表达式和 credentials 不得进入仓库。
