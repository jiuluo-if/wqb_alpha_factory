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

- 平台原始事实：`list_datasets()`、`list_datafields()`、`list_all_datafields()`、`get_capabilities()`、`get_operator_reference()`。
- 可选 Probe/template：`list_templates()`、`inspect_template()`、`generate_probes(fields=..., template_ids=..., count=...)`。字段、模板和候选数都由 Agent 显式选择；每个字段必须带上它的 BRAIN dataset provenance 才能执行 live capability 校验；每次最多传入 100 个字段、100 个模板 ID，并请求生成 100 个候选，属于本地资源硬上限。
- Real Simulation：`validate_simulation_spec()`、`simulate()` / `simulate_single()`、`simulate_batch()`、`simulate_multi_batch()`，带实时能力检查、fingerprint、exact-once guard 和已知进度任务恢复。`simulate()` 与 `simulate_single()` 都提交一个请求并委托同一 Gateway；独立 Single 批次用 `simulate_batch()`。
- Simulation 模式：小规模优化可使用 Single 或小批量 Multi；大规模探针使用 Multi，每个 Multi parent 默认/最多 10 个 child（最少 2 个），默认同时 dispatch 2 个 parent，显式 override 的受支持硬上限为 8。`Region-Agnostic Simulation` 保持独立，当前没有经过验证的写入契约，不会被 Multi 窗口隐式替代。
- Remote Alpha：live Alpha/evidence、滚动缓存、去重、比较、分组和颜色预览/同步。
- Agent 默认工具面：`research_tool_manifest()` 返回小型 CORE profile；显式指定 `profile="full"` 才列出模板维护、颜色和其他低频能力。研究方法见唯一核心 Skill [`skills/wqb-research/SKILL.md`](skills/wqb-research/SKILL.md)。

与 Agent 正式运行树及 `new_ai` 样本的功能、契约和取舍见
[`docs/COMPATIBILITY_MATRIX.md`](docs/COMPATIBILITY_MATRIX.md)。

```python
from wqb_agent.research_api import SimulationSpec, simulate, get_alpha_evidence

spec = SimulationSpec(
    expression="rank(synthetic_field)",
    settings={"delay": 1},
    fields=("synthetic_field",),
    field_datasets={"synthetic_field": "synthetic_dataset"},
    proposal_id="proposal-1",
)
result = simulate(spec)
evidence = get_alpha_evidence(result["alpha_id"])
```

推荐闭环：

```text
列出原始 datasets/datafields → Agent 选择字段/假设
→ 按需生成显式选定的 probes → 批量/Multi 模拟 → 读取实时 summary/完整 evidence
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

## 可选 MCP 只读工具

安装 `python -m pip install -e ".[mcp]"` 后，在项目目录启动 `alpha-factory-mcp`，
经 stdio 暴露有界的只读能力、权限、字段、Alpha 证据与本地未决 guard 工具。
工具契约与输出上限见 [`docs/MCP_READ_ONLY.md`](docs/MCP_READ_ONLY.md)。

## 配置与验证

配置使用 `simulation`、`runtime`、`remote_cache`、`quota` 四个顶层 section，示例见 `config.example.json`。`list_all_datafields()` 的 `max_pagination_pages` 可设置为 1–100，默认 20。

```powershell
python main.py diagnostics doctor
python main.py diagnostics audit
python main.py datasets
python scripts/run_targeted_tests.py --files <changed-files>
python -m unittest discover -s tests
python -m ruff check .
python scripts/check_repo_privacy.py
```

tracked tests、docs 和 fixtures 只能使用 synthetic 数据；真实 Alpha、私有 field、研究表达式和 credentials 不得进入仓库。
