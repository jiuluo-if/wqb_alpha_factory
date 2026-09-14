# Alpha Factory Architecture Modernization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task with review checkpoints.

**Goal:** 在保持现有研究语义与安全 owner 不变的前提下，完成 Alpha Factory 的模块化重构、legacy 清理、测试治理和文档升级。

**Architecture:** 按 `domain → state → mechanisms → workflows → runtime composition → facade/CLI` 单向收敛。先建立可执行架构契约，再以 strangler 方式从 `Agent`、`AlphaFactory`、`ProposalExecutionWorkflow` 和 `AIFactoryRunner` 逐步抽出 canonical 组件，旧 public surface 仅保留窄委托。

**Tech Stack:** Python 3.11、stdlib `ast`/`unittest`、现有 `ruff`/`mypy`、JSONL/atomic persistence、现有 targeted test runner。

**Spec:** `docs/superpowers/specs/2026-09-14-architecture-modernization-design.md`

## Global Constraints

- 唯一 Simulation 写链必须保持 `Agent.run_proposals()` → `ProposalExecutionWorkflow` → `Simulator` → `WQBClient`。
- `SUBMIT_UNKNOWN` 不重发；known progress URL 只读；same-origin、checkpoint exactly-once、Trajectory/TrialLedger 唯一 owner 不得弱化。
- UNKNOWN/UNAVAILABLE 不得升 PASS；Alpha submission 保持手工完成；真实研究状态、credentials、private catalog 不得提交或打印。
- 不新增第二套 state、proposal contract、evaluation、facade、manager/orchestrator 或 Simulation 路径。
- 只运行受影响的定向测试；禁止 `unittest discover`、裸 `pytest` 和全量 coverage。
- 每个有效阶段完成并验证后使用 `2966684515@qq.com` 提交中文 commit，并推送 GitHub。

---

### Task 1: 建立 architecture contract 与 baseline tooling

**Files:**
- Create: `tests/test_architecture_contracts.py`
- Modify: `scripts/run_targeted_tests.py`
- Modify: `docs/TESTING.md`

**Interfaces:**
- `tests/test_architecture_contracts.py` 提供 `test_workflows_do_not_import_agent`, `test_domain_modules_do_not_import_transport`, `test_state_owners_do_not_import_orchestration`。
- `scripts/run_targeted_tests.py` 显式映射新测试和后续新模块。

- [ ] 写 AST contract tests，按相对 import 和绝对 import 解析仓内模块；违反规则时输出 source/target。
- [ ] 先运行 `python -m unittest tests.test_architecture_contracts`，确认基线约束在当前代码上通过。
- [ ] 把新测试加入 targeted mapping，并补充测试文档的 architecture lane。
- [ ] 运行 `python scripts/run_targeted_tests.py --files tests/test_architecture_contracts.py scripts/run_targeted_tests.py docs/TESTING.md`。
- [ ] 提交 `test：建立架构依赖契约` 并推送。

### Task 2: 提取 Agent 的纯规划与 projection 领域能力

**Files:**
- Create: `wqb_agent/research_planning.py`
- Create: `wqb_agent/evidence_projection.py`
- Modify: `wqb_agent/agent.py`
- Test: `tests/test_agent_flow.py`, `tests/test_agent_evaluation.py`, `tests/test_architecture_contracts.py`

**Interfaces:**
- `research_planning.form_research_space(...)` 与 `research_planning.form_hypothesis(...)` 只接收显式 config、round、field/profile 和 existing evidence。
- `evidence_projection.terminal_identity_map(...)`、`evidence_projection.settlement_summary(...)` 只返回 derived projection，不写 state、不调用 client。
- `Agent` 保留 `run_suggestion_round`, `run_proposals` 和 CLI 使用的 public methods，内部改为窄委托。

- [ ] 为每个提取函数先写针对当前行为的 characterization tests，覆盖空字段、缺失证据、UNKNOWN 和 parent identity。
- [ ] 运行对应测试，确认新接口尚未实现时失败。
- [ ] 从 `Agent` 原样迁移实现，保留原错误码、排序、边界和输出字段；删除重复 private forwarding。
- [ ] 将 Agent 调用改为显式依赖，不让新模块 import `Agent`、`Client` 或状态 owner。
- [ ] 运行 `python scripts/run_targeted_tests.py --files wqb_agent/agent.py wqb_agent/research_planning.py wqb_agent/evidence_projection.py`、`py_compile` 和 Ruff。
- [ ] 提交 `refactor：抽离Agent领域规划与证据投影` 并推送。

### Task 3: 模块化 AlphaFactory

**Files:**
- Create: `wqb_agent/alpha_semantics.py`
- Create: `wqb_agent/alpha_relationships.py`
- Create: `wqb_agent/alpha_assembly.py`
- Modify: `wqb_agent/alpha_factory.py`
- Modify: `scripts/run_targeted_tests.py`
- Test: `tests/test_factory_semantic_traits.py`, `tests/test_factory_relationship_gate.py`, `tests/test_factory_batch_contract.py`, `tests/test_factory_mechanism_selection.py`

**Interfaces:**
- `alpha_semantics.derive_field_semantic_traits(profile)` 返回现有 traits 结构。
- `alpha_relationships.relationship_gate(profiles, template)` 返回现有 admission result，不触发网络或 state mutation。
- `alpha_assembly.assemble_candidate(...)` 只构造 proposal/domain data；`AlphaFactory` 组合这些能力并保持原 public API。

- [ ] 为 traits、relationship gate 和 assembly 各补一个正常、缺失证据、拒绝路径测试。
- [ ] 迁移实现并保持 template/private-catalog gate、operator/horizon/settings 和经济语义完全不变。
- [ ] 让 `AlphaFactory` 只保留组合与 public compatibility；禁止新模块反向 import facade。
- [ ] 运行四个 factory subsystem 测试文件和 architecture contract。
- [ ] 运行 changed-file `py_compile`、Ruff；提交 `refactor：模块化AlphaFactory领域能力` 并推送。

### Task 4: 分解 ProposalExecutionWorkflow 内部阶段

**Files:**
- Create: `wqb_agent/proposal_admission.py`
- Create: `wqb_agent/execution_recovery.py`
- Create: `wqb_agent/terminal_evidence.py`
- Modify: `wqb_agent/proposal_execution.py`
- Modify: `scripts/run_targeted_tests.py`
- Test: `tests/test_proposal_execution.py`, `tests/test_proposal_safety.py`, `tests/test_recovery.py`, `tests/test_simulator.py`, `tests/test_settled_evidence_durability.py`

**Interfaces:**
- `proposal_admission.admit_proposals(...)` 只做 schema/preflight/admission projection。
- `execution_recovery.reconcile_checkpoint(...)` 只按 exact Experiment identity/fingerprint 读取和合并，不 POST。
- `terminal_evidence.require_durable_terminal_evidence(...)` 与 `terminal_evidence.finalize_round_projection(...)` 只使用现有 owners 的窄操作。
- `ProposalExecutionWorkflow.run/resume_checkpoint` 继续是唯一编排入口。

- [ ] 先增加 SUBMIT_UNKNOWN 不重发、known URL GET-only、identity collision 和 callback failure 的行为测试。
- [ ] 运行 proposal/recovery subsystem 测试作为迁移前基线。
- [ ] 抽出纯校验和 projection；owner mutation 留在 workflow/context 现有边界内。
- [ ] 检查新模块没有 `Agent`、直接 client POST、第二 checkpoint/trajectory writer。
- [ ] 运行 targeted runner、`py_compile`、Ruff 和受影响 frontier 检查；提交 `refactor：分解提案执行内部阶段` 并推送。

### Task 5: 隔离 FactoryRunner legacy control plane

**Files:**
- Create: `wqb_agent/factory_session.py`
- Create: `wqb_agent/factory_quota.py`
- Create: `wqb_agent/factory_route.py`
- Modify: `wqb_agent/factory_runner.py`
- Modify: `scripts/run_targeted_tests.py`
- Test: `tests/test_factory_boundaries.py`, `tests/test_factory_blocker_control.py`, `tests/test_factory_feasibility.py`, `tests/test_factory_session_privacy.py`, `tests/test_research_loop.py`

**Interfaces:**
- `factory_session.read_session/request_stop/status_view` 继续是 `factory_session.json` 的唯一 control-plane projection。
- `factory_quota.prepare/reserve/release` 只处理 bounded quota counts。
- `factory_route.route_decision` 只处理 route episode 与 bounded digest，不生成 research evidence。
- `AIFactoryRunner` 只编排 legacy facade，不创建第二 state owner 或 Simulation path。

- [ ] 先通过 git grep、CLI、tests、docs 和动态调用搜索确认每个候选分支的 consumer。
- [ ] 为 session stop merge、quota boundary、route privacy 和 blocker recovery 写行为测试。
- [ ] 原样抽出组件并让 runner 委托；保留旧 public class 作为 compatibility facade。
- [ ] 仅删除有零 consumer 证据的历史分支，并在 commit body/文档记录 replacement。
- [ ] 运行 factory subsystem targeted lane、语法检查和 Ruff；提交 `refactor：隔离legacy工厂控制面` 并推送。

### Task 6: 清理 public surface、重复测试、脚本并升级文档

**Files:**
- Modify: `wqb_agent/__init__.py`
- Modify: `tests/test_architecture.py` and affected subsystem tests
- Modify: `AGENTS.md`, `wqb_agent/AGENTS.md`, `docs/ARCHITECTURE_AGENT.md`, `docs/STATE_LAYOUT.md`, `docs/TESTING.md`, `scripts/README.md`
- Delete only after evidence: unused production/test/script files identified in Tasks 2–5

**Interfaces:**
- `wqb_agent.research_api`、CLI 和明确列出的 Agent/WQBClient compatibility surface remain public。
- Removed symbols must have zero imports, CLI/docs/tests/`__all__`/dynamic references and a replacement or explicit retired classification.

- [ ] 生成删除候选证据表，逐项记录 ACTIVE/PUBLIC_COMPAT/INTERNAL_COMPAT/DEAD/RETIRED。
- [ ] 删除仅保护旧架构形状的测试；将 frozen invariant tests 迁移到 owner subsystem，不减少安全覆盖。
- [ ] 收缩 package root exports，保留必要兼容适配器并增加 deprecation contract tests。
- [ ] 更新文档中的目标模块图、owner 表、测试路由和 remaining debt。
- [ ] 生成 after metrics：LOC、文件数、最大模块、Agent/AlphaFactory/ProposalExecution/FactoryRunner 方法数、wrapper/dead file/duplicate test、依赖环。
- [ ] 运行最终 targeted runner、changed Python 的 `py_compile`/Ruff、typed-frontier mypy、fixture doctor/audit/privacy 和 `git diff --check`；提交 `docs：完成架构治理与契约清理` 并推送。

## Final Verification

- `python scripts/run_targeted_tests.py --files <all-changed-files>` exits 0。
- 每个 changed Python file passes `python -m py_compile` and `python -m ruff check`。
- Required typed frontier passes the existing mypy command。
- Offline fixture doctor/audit and privacy checks pass。
- Frozen invariants have direct behavior-test evidence; no live BRAIN write occurred。
- GitHub CI for the pushed commits is checked and reported with the final SHA.
