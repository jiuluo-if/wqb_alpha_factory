# Alpha Factory 架构现代化设计

## 目标

在不改变研究语义和安全不变量的前提下，降低 `Agent`、`AlphaFactory`、`ProposalExecutionWorkflow` 与 legacy factory control plane 的耦合；让领域逻辑、状态 owner、机制服务、workflow、运行时装配和 CLI/facade 形成可读的单向依赖。

## 现状证据

- HEAD 为 `80922cdde2959ae92c71e77b4189f553e1ee3aef`，工作树干净。
- 生产代码 76 个 Python 文件、29,951 行；测试 84 个 Python 文件、24,022 行。
- 最大模块依次为 `alpha_factory.py` 2181 行、`factory_runner.py` 2024 行、`agent.py` 1727 行、`proposal_execution.py` 1404 行。
- AST 依赖图有 222 条仓内模块边，当前未发现循环依赖；`Agent` 直接依赖约 25 个内部模块。

## 设计原则

1. 只有一个 canonical implementation 和一个 owner；compatibility facade 只能委托，不复制实现。
2. 生产 Simulation 写入继续唯一经过 `Agent.run_proposals()` → `ProposalExecutionWorkflow` → `Simulator` → `WQBClient`。
3. `CheckpointStore`、`Trajectory`、`TrialLedger`、cache、factory session 各自保持唯一 owner；不新增第二套 state、ledger、inbox 或结果缓存。
4. `SUBMIT_UNKNOWN`、已知 progress URL 只读轮询、same-origin 校验、checkpoint exactly-once、UNKNOWN/UNAVAILABLE fail-closed、手工 Alpha submission、凭据 source isolation 和隐私边界必须有行为回归证明。
5. 重构只迁移现有行为；发现 correctness bug 时单独记录并增加回归测试，不混入研究策略、阈值或经济含义改变。
6. 新模块按真实职责命名，优先使用纯函数、dataclass 和窄 Protocol，不引入 DI/IoC、service locator、event bus 或泛化 manager。

## 目标架构

```text
domain / pure logic
  ├─ field semantics, relationship, feasibility, candidate assembly
  ├─ evaluation / settlement projections
  └─ dependency policy predicates
          ↓
state / persistence owners
  ├─ Trajectory, TrialLedger, CheckpointStore
  └─ artifact/cache/session persistence
          ↓
mechanisms
  ├─ Simulator, WQBClient, discovery, evidence readers
  └─ quota / legacy control-plane projections
          ↓
workflows
  ├─ SuggestionWorkflow
  ├─ ProposalExecutionWorkflow
  ├─ AlphaFeedWorkflow / AlphaColorWorkflow
  └─ OptimizerWorkflow
          ↓
runtime composition
          ↓
Agent compatibility facade / CLI / research_api
```

## 分阶段方案

### 阶段 1：架构契约与测量

新增基于 stdlib `ast` 的 dependency contract tests，验证 workflow 不反向依赖 `Agent`、纯领域模块不依赖 transport/client、state owner 不依赖 orchestration，并记录 before/after 指标。补齐 targeted-test mapping，确保每个新模块都能进入定向测试。

### 阶段 2：Agent 瘦身

从 `agent.py` 抽出纯的研究空间/假设规划、终态身份与 evidence projection、proposal settings policy 和 settlement/summary projection。新模块只接收明确输入和既有 owner 查询结果；`Agent` 保留 runtime composition、public CLI 入口和兼容 facade。任何仍需兼容的 public 方法必须是单行委托并有行为测试。

### 阶段 3：AlphaFactory 与 ProposalExecution 模块化

从 `alpha_factory.py` 提取 semantic profiling、relationship/admission、feasibility 和 candidate assembly；`AlphaFactory` 变成组合 facade。从 `proposal_execution.py` 提取 admission/preflight、identity/recovery、terminal evidence 与 settlement projection；`ProposalExecutionWorkflow` 仍是唯一编排 owner，提取组件不能直接提交 Simulation 或拥有状态。

### 阶段 4：legacy factory 隔离

把 `factory_runner.py` 拆为 session/control-state projection、quota projection、route episode policy 和 legacy facade。`factory_session.json` 仍由同一 control-plane owner 写入；历史分支只有在 git grep、CLI、测试、文档和动态引用均无 consumer 时才删除。

### 阶段 5：清理与文档

按证据删除 dead/internal compatibility code、重复 architecture-shape tests 和无 consumer 的一次性脚本；保留并迁移全部 frozen invariant tests。更新根/局部 AGENTS、architecture/state/testing/scripts 文档，生成 before/after metrics 和 remaining debt。

## 错误处理与安全

- 提取组件遇到缺失/含糊证据必须返回既有 `UNKNOWN`/`UNAVAILABLE`，不得默认成功。
- 恢复路径按 Experiment identity 和完整 execution fingerprint 合并；不能按 expression 猜远端身份。
- 未知 POST 结果继续保持 `SUBMIT_UNKNOWN`；没有 progress URL 时禁止再次 POST。
- 任何新依赖边界由 AST contract test fail-closed；测试映射缺失也 fail-closed。
- 不读取、打印、提交真实 credentials、private catalog 或 `.wqb_state` research payload；不执行 live BRAIN 写操作。

## 验证与交付

每个阶段先执行相关行为测试和最近邻 regression，再执行 changed Python 的 `py_compile`、Ruff 及必要的 typed-frontier mypy。最终执行 `scripts/run_targeted_tests.py --files <changed-files>`、离线 doctor/audit/privacy 检查和 architecture metrics。提交信息使用中文内容，Git 邮箱固定为 `2966684515@qq.com`；验证通过后推送 GitHub，不强推、不改写历史。
