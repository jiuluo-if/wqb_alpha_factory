# `wqb_agent/` 局部规则

## Alpha template change gate

任何 Agent 在更新或拓展 `alpha_templates` 及其调用方前，必须先阅读根目录 `AGENTS.md`、本文件和 [`alpha_templates/AGENTS.md`](alpha_templates/AGENTS.md)，并在变更记录中确认。未完成阅读不得进行模板变更；无法阅读时报告 `TEMPLATE_CONSTRAINTS_NOT_READ`。

先读根 `AGENTS.md`、`docs/ARCHITECTURE_AGENT.md` 和 `research_api.py`。其他模块是 facade 所组合的现有运行时能力。

## Optimization Agent interface gate

优化专项必须先阅读根 `AGENTS.md`、`docs/RESEARCH_POLICY.md` 和 `research_api.py`，
并阅读 `optimization_interfaces.py` 的专项接口。只允许使用既有 `OptimizerWorkflow`、
`OptimizationDecision`、`ClientOptimizationEvidenceProvider` 与 `ExperienceMemory` owner；
不得创建第二套请求、状态或经验路径。无法完成预读时报告
`OPTIMIZATION_CONSTRAINTS_NOT_READ`。

## 稳定架构边界

当前 package 边界已冻结：`runtime_components.py` 只拥有基础对象，`runtime_composition.py` 只负责 wiring，四个 Agent workflow 不反向导入 Agent；`AlphaColorWorkflow` 是独立的显式远端颜色写 owner。生产 Simulation 只能沿 `Agent.run_proposals()` → `ProposalExecutionWorkflow` → `Simulator` → `WQBClient`，旧 `WQBClient.run_simulation()` 无生产调用。Optimizer 只消费 local `Trajectory` evidence，cloud cache 只能提供 metadata priority；审计 JSONL 无 owner lock 时明确为 best-effort。后续只因具体 feature/bug 或新的证据触碰边界，并同步测试、架构文档和完整质量门；不要新增 workflow/state/config abstraction 或继续拆 Agent 私有层。

## 职责

- `client.py`、`discovery.py`、`simulator.py`、`state.py`、`proposal_contract.py` 是核心机制边界。
- `metrics.py`、`expression.py`、`artifacts.py`、`locking.py` 和 failure helpers 是共享安全/序列化原语。
- `search*`、`validation*`、`robustness.py`、`incremental_value.py`、`memory.py`、`submission.py` 是内部评估或 workspace 视图。
- `factory_runner.py` 是兼容/legacy control plane，不是默认 Agent mental model。

`SuggestionWorkflow` 是 suggestion round 的唯一编排 owner：它负责 discovery fallback、suggestion bundle 组装、`suggestions.json` emission 和既有控制台输出；`Agent` 只保留高层研究规划 hooks 与兼容 facade。Workflow 通过显式依赖和窄 operation-shaped hooks 工作，不反向导入 `Agent`，不直接依赖 `Client`/`Simulator`/checkpoint，也不复制 `_last_round_skipped` 或 `memory.best_exhausted`。

运行时装配的 owner 关系是：`AppConfig` → `AgentRuntimePolicy` → `RuntimeComponents` → `AgentWorkflows`。`build_agent_runtime_policy()` 集中解析 Agent 实际消费的配置投影；`RuntimeComponents` 只拥有基础领域对象；`runtime_composition.py` 只用既有组件和显式 hooks 创建 `SuggestionWorkflow`、`ProposalExecutionWorkflow`、`AlphaFeedWorkflow` 与 `OptimizerWorkflow`，不得反向导入 `Agent` 或重复构造组件。Agent 的旧 public attributes 可以继续保留，但必须集中投影自这套唯一对象图。

`AlphaFeedWorkflow` 是 `AgentWorkflows` 的第三个成员，负责 BRAIN 用户 Alpha 的只读分页、`America/New_York` 七个自然日窗口、去重、bucket 以及 `DailyResearchCache`/`WeeklyAlphaFeedCache` 更新。它只接收 `get_all_user_alphas` 操作，不接收整个 Client，不依赖 Agent、Simulator、proposal/suggestion/optimizer workflow，不产生 POST、PATCH、submission 或 checkpoint 写入。`OptimizerWorkflow` 是第四个成员，拥有 `_cloud_alpha_ids()` 消费逻辑、已有证据筛选、Agent hypothesis gate 和 AlphaFactory CHILD 编排；cloud metadata 只作排序提示。

`alpha_colors.py` 是纯 derived color domain owner：保留 `classify_alpha_color()`、`has_research_signal()`、轻量 evidence summary 与 `load_color_candidates()`，不执行远端写入。`AlphaColorWorkflow` 是独立的 CLI control/write workflow，不属于四个 `AgentWorkflows`；它通过窄的 `get_alpha` / `set_alpha_color` hooks 编排远端读取、ownership fail-closed、dry-run、verified PATCH 和结果摘要。`main.py` 继续拥有锁、lazy Client、CLI JSON 与 exit code；只有显式 `alpha sync-colors` 才允许颜色 metadata 写入。

`credentials.py` 是 local-only credential resolver，不依赖 Client、requests、Agent、Simulator、proposal execution 或 state。它只接受完整来源：显式 Client pair、WQB 环境变量、显式绝对路径 `WQB_CREDENTIALS_ENV_FILE`、home credentials file；partial、malformed、unreadable source 必须 fail-closed，不得从 cwd/父目录/package 自动发现 `.env` 或跨源拼接。`client.py` 继续拥有认证协议和 retry/Session 机制，只消费 resolver 结果。

`alpha_templates/` 是 Alpha template model、私有 catalog loader、registry 与 numeric/operator audit 的唯一 owner；其局部生成规则见 `wqb_agent/alpha_templates/AGENTS.md`。tracked `builtin.toml` 只能是 TOY/SYNTHETIC/NON-RESEARCH 示例，真实模板、私有字段、固定配对、表达式和经验只能在 gitignored private catalog/ExperienceMemory 中。私有 catalog 只能由显式绝对路径、`WQB_ALPHA_TEMPLATE_CATALOG` 或用户 home private 默认路径加载，缺失必须 `PRIVATE_TEMPLATE_CATALOG_MISSING`，严禁回退 public catalog。

探针模板必须有明确经济机制、字段关系、方向理由、horizon、falsification、self-correlation impact 与 novelty；默认 4–6 个算子出现次数、2–4 个经济字段，只有 control 可用 1–3 个算子/单字段。horizon 仅使用 5/22/66/120/255；多窗口只能使用相邻有序 profile，禁止笛卡尔积。算子覆盖应在经济效应明确且 operator 已验证时尽可能广泛，但不得为覆盖率无意义叠加算子。配置 arm 每次只改一个主要变量。

领域模块不得导入 `Agent` 或创建另一条执行/状态路径；BRAIN 事实留在 client/discovery 边界，纯评估函数不得产生网络写入。

## 不变量

- `ProposalExecutionWorkflow` 是 proposal 预检、checkpoint 恢复、Simulation 调度前后编排和终态结算的唯一工作流；`Agent.run_proposals()` 仅保留兼容 facade。工作流不得导入 `Agent` 或直接调用 `client.submit_simulation`，真实提交仍由 `Simulator` 持有，checkpoint 持久化仍由 `CheckpointStore` 持有。
- POST 前持久化 identity/checkpoint；写结果含糊时只轮询或对账同一 job，绝不重复 POST。Simulation 结果与 Alpha 证据不落盘；远端 Alpha 仅以轻量 ID/状态/时间戳按滚动 7 日写入 `.alpha_feed_cache/weekly.json`。
- 保留 Retry-After、锁、预算、schema、expression dedup 和 `UNKNOWN` / `UNAVAILABLE`。
- 远端 Alpha 轻量元数据写入 `.alpha_feed_cache/weekly.json`：每 3 小时与提交 Alpha 同批刷新，按纽约本地日分桶，仅保留当前工作日前推 7 个自然日和 `11200（7*1600）` 条模拟元数据上限；每次刷新清理滚动窗口外和超时临时资源，并保留 `updated_at`/`expires_at`。不得写入指标、表达式、trajectory 或证据。
- Alpha submission 始终手工完成。

## 自主模拟双层边界

- `OptimizerWorkflow` 先调用 `AlphaFactory.screen_optimization_parents()` 做代码初筛，再验证 Agent 已提供的经济机制/反过拟合 gate；`Agent.optimizable_signal_records()`、`optimizer_gate_report()` 和 `generate_optimized_proposals()` 仅为兼容 facade。云端 Alpha 轻量缓存只提升已有本地证据的优先级，不作为独立性能证据。
- 优化题案标记 `research_layer=optimization`、`research_role=EXPLOIT`；来源按 `cloud`、`current_run` 审计。探索题案由 `AlphaFactory.generate_factory_batch()` 以稳定种子随机化已核验字段和经济模板，标记 `research_layer=exploration`、`research_role=EXPLORE`、`experiment_stage=BASELINE`、`exploration_objective=signal_discovery`。
- 双层不增加执行入口：完整批次仍须通过 100 题案 gate、正常 Agent preflight 和 `Agent.run_proposals()`；代码/Agent 任一层不足或失败都不能用重复题案填充。

## 配置边界

- `config.py` 是唯一允许解释外部 raw `agent` / `simulation` key 的 package 模块。
- `normalize_config()` 之后的 `AppConfig` 只包含 typed sections；其他 package 模块不得读取 `config.agent` 或 `config.simulation`。
- 用户可见的 parser 错误路径（例如 `config.agent.*` 和 `config.simulation.*`）必须保持不变。

研究空间轮换、搜索分配和候选优先级属于 `RESEARCH_POLICY`，不是 BRAIN 机制。

## 验证

本地默认采用增量验证：

```powershell
python -m unittest tests.test_<affected_module>.<TestClass>.<test_method>
python -m unittest tests.test_<nearest_contract>
python -m py_compile <changed-python-files>
python -m ruff check <changed-python-files>
```

先跑直接受影响行为和一个最近邻 regression；失败时再扩大到相关 subsystem contract suite。改动跨多个 owner、shared helper、proposal/schema、state merge semantics 或 safety contract 时，使用 Local Expanded Lane。普通本地修改默认不跑 whole repository regression。

仅当 typed frontier 被改动时，运行对应的 mypy。typed frontier 只包含上述九个边界清晰模块；全局 mypy 保持非 strict，不为类型检查重写 `agent.py`、`client.py`、`simulator.py` 或 `proposal_execution.py`。

CI 只执行与变更相关的 targeted tests；不得执行 whole-repository test suite、`unittest discover` 或 coverage 驱动的全量测试。测试必须由 `scripts/run_targeted_tests.py` 的显式变更文件映射选择，映射缺失时 fail-closed；静态检查和 offline doctor/audit/privacy 仍按 workflow 需要执行。

```powershell
python scripts/run_targeted_tests.py --base-sha <CI base SHA>
```

Coverage 不得作为恢复全量测试的旁路；如单独使用，只能覆盖明确选定的相关测试模块。
