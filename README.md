# Alpha Factory

Alpha Factory 是一个面向 AI Agents 的 WorldQuant BRAIN Alpha 研究工程：使用真实的 fields、operators 和 Simulation，Agent 负责研究判断，Python 负责执行、证据、恢复与安全。

## 核心研究循环

```text
观察 → 发现 → 假设 → 实验 → 评估 → 记录 → 迭代
```

最短阅读路径：`README.md` → `AGENTS.md` → `wqb_agent/research_api.py`；需要时再读 [`docs/ARCHITECTURE_AGENT.md`](docs/ARCHITECTURE_AGENT.md)。

## 快速开始

安装依赖并复制配置：

```powershell
pip install -r requirements.txt
Copy-Item config.example.json config.json
```

凭据不写入 `config.json`。生产环境/CI 推荐设置完整的
`WQB_USERNAME` 和 `WQB_PASSWORD` 环境变量；本地工作站可在
`~/.brain_credentials.txt` 中按“用户名一行、密码一行”保存。也可以通过
`WQB_CREDENTIALS_ENV_FILE` 显式指定一个绝对路径的 env 文件；程序不会从
当前目录、父目录或 package 目录自动搜索 `.env`，也不会混合不同来源的半套凭据。
显式 env 文件兼容 `WQB_USERNAME/WQB_PASSWORD` 和旧式
`BRAIN_USERNAME/BRAIN_PASSWORD` 键名。

只读地发现平台字段：

```powershell
python main.py suggest
```

Agent 评估并写好 `.wqb_state/proposals.json` 后，使用当前唯一受保护的 Simulation 路径：

```powershell
python main.py run-proposals
```

也可以直接使用 `wqb_agent.research_api` 的 `inspect_state`、`discover_fields`、`run_experiment`、`get_experiment`、`compare_experiments`、`search_history` 和 `reconcile`。特殊诊断命令见 `docs/README.md`，不属于默认研究循环。

## 安全边界

- BRAIN live response 是 datasets、fields、operators、Simulation、metrics 和 checks 的实时事实源；cache、报告和 fixture 不是事实源。
- 字段和算子必须真实验证；设置、schema、去重和硬预算必须在执行前检查。
- 已知 progress URL 只允许只读轮询；远程写结果未知时保持 `SUBMIT_UNKNOWN`，禁止盲目重 POST。
- checkpoint 是恢复边界；缺失或模糊证据保持 `UNKNOWN` / `UNAVAILABLE`，不得伪装成 `PASS`。
- Alpha submission 始终由用户在 BRAIN 中手工完成。

## 项目结构

```text
wqb_agent/research_api.py  # Agent-facing facade
wqb_agent/runtime_policy.py       # AppConfig 到 Agent 运行时投影
wqb_agent/runtime_components.py   # 唯一的基础领域组件
wqb_agent/runtime_composition.py  # 四个 workflow 的显式组合边界
wqb_agent/alpha_feed_workflow.py  # 只读 Alpha 元数据同步
wqb_agent/optimizer_workflow.py    # 证据驱动 CHILD 编排
wqb_agent/                 # BRAIN、执行、状态和评估实现
main.py                    # 兼容 CLI 与安全入口
docs/                      # 当前协议、研究政策与少量参考
tests/                     # 核心安全不变量和可观察行为
```

## Agent 运行时装配

运行时配置和对象装配保持单向、分层：

```text
AppConfig
  ↓ build_agent_runtime_policy()
AgentRuntimePolicy
  ↓ build_runtime_components()
RuntimeComponents
  ↓ Agent 提供显式 hooks
AgentWorkflows
  ├── SuggestionWorkflow（只读建议）
  ├── ProposalExecutionWorkflow（提案执行与恢复）
  ├── AlphaFeedWorkflow（只读 Alpha 元数据同步）
  └── OptimizerWorkflow（证据驱动 CHILD 编排）
```

`AgentRuntimePolicy` 只保存 Agent 实际消费的已解析值，不复制完整
`AppConfig`。`RuntimeComponents` 只拥有 memory、trajectory、ledger、discovery、
simulator、checkpoint 等基础对象，不承担 workflow 编排；四个 workflow 共享既有组件或缓存，
不会自行创建第二个 `Simulator`、`Trajectory` 或 `CheckpointStore`。Agent 仍投影旧的
public attributes，以保持兼容 facade 和现有研究方法不变。`AlphaFeedWorkflow` 只读取
`get_all_user_alphas`，维护 `America/New_York` 七个自然日的轻量元数据，不发送 Simulation
POST、不 PATCH Alpha，也不写入指标或研究证据。

## 稳定架构边界

当前 owner、状态和远端写路径已经冻结：配置经 `parse_config/normalize_config` 进入 typed `AppConfig`，再经过 `AgentRuntimePolicy`、`RuntimeComponents` 和 `Agent` 组合四个 workflow；远端颜色写入由显式 `alpha sync-colors` 独占。生产研究闭环仍只有：

```text
python main.py suggest
        ↓
审阅 .wqb_state/proposals.json
        ↓
python main.py run-proposals
```

后续默认进入 feature、bug fixing 和 evidence-driven local maintenance，不再开展连续架构重构。完整对象图、owner/state/write matrix、冻结不变量和变更规则见 [`docs/ARCHITECTURE_AGENT.md`](docs/ARCHITECTURE_AGENT.md)。

## 文档入口

- [`AGENTS.md`](AGENTS.md)：coding/research agent 的最高优先级说明
- [`docs/ARCHITECTURE_AGENT.md`](docs/ARCHITECTURE_AGENT.md)：概念、事实层级和调用边界
- [`docs/BRAIN_PROTOCOL.md`](docs/BRAIN_PROTOCOL.md)：BRAIN 协议、Retry-After 和 capability
- [`docs/RESEARCH_POLICY.md`](docs/RESEARCH_POLICY.md)：假设、反证、稳健性和统计纪律
- [`docs/PRIVACY.md`](docs/PRIVACY.md)：公共仓库与本地研究数据边界

算子与设置参考文档若被运行时需要，会在 `docs/README.md` 中标为 `REFERENCE`，不作为默认 mental model。

## 测试

```powershell
python -m compileall -q wqb_agent scripts tests
python -m mypy wqb_agent/config.py wqb_agent/runtime_policy.py wqb_agent/runtime_components.py wqb_agent/runtime_composition.py wqb_agent/credentials.py wqb_agent/suggestion_workflow.py wqb_agent/alpha_feed_workflow.py wqb_agent/optimizer_workflow.py wqb_agent/alpha_color_workflow.py
python scripts/run_targeted_tests.py --files <changed-files>
python -m ruff check .
python -m py_compile <changed-python-files>
```

本地验证与 CI 使用同一增量质量门：测试只由 `scripts/run_targeted_tests.py` 按变更文件显式映射选择，禁止 `unittest discover`、无选择器 pytest 和 coverage 驱动全量测试；mypy 只检查九个 typed frontier 模块，Ruff 启用现有规则、import sorting、选定安全 UP 规则及 `B007/B904`。质量检查保持离线，不执行 live BRAIN、Simulation 或 Alpha submission。

## 颜色、Agent 优化与阶段配额

- 颜色在每个 Simulation settle 后立即刷新当前进程的 `DailyResearchCache`，整批结束时再按完整证据重算；因此混合批次或中断不会把颜色更新延迟到整批成功。
- `suggestions.json` 的 `optimizer_context` 是优化门禁的可审计摘要。`OptimizerWorkflow` 只做 DONE/证据筛选、AlphaFactory 代码初筛和 Agent 已明确提供的 `child_economic_hypothesis` 语义/反过拟合验证；只有 gate 通过才允许生成 `CHILD`/`agent_optimizer` 候选。Python 不生成经济机制，参数、窗口、符号扫描不构成自主优化。
- 当前阶段工厂本地配额为每周 `11200` 次（`7*1600`），每日 `1600` 次，按 `America/New_York` 本地日刷新。`factory_session.json` 只保存配额控制元数据；未完成 checkpoint 的恢复预留优先，不能通过新轮绕过。
- `python main.py alpha sync-feed` 每次只读分页拉取当前工作日前推 7 个自然日的用户 Alpha：提交 Alpha 与模拟 Alpha 在同一刷新批次按纽约本地日分桶，写入 `.alpha_feed_cache/weekly.json`，保留 `updated_at`/`expires_at`，并按 `11200（7*1600）` 模拟元数据上限清理窗口外数据。

旧式 boolean flag 命令在有限兼容窗口内仍可使用，但会输出弃用提示；新命令使用结构化子命令。

自主 factory round 分成两个研究层：优化层优先使用云端轻量 Alpha 元数据命中的本地完成证据，再使用本轮完成证据；代码先做证据和反过拟合初筛，Agent 再确认新的经济机制。探索层由工厂使用稳定轮次种子进行大批量随机字段/模板组合，目标是定位信号而非扫描参数。两层共享 100 题案原子 gate、预算和 `Agent.run_proposals()`，`factory_batch_stats` 会记录层级与来源。
