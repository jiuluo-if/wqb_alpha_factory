---
name: wqb-research
description: "用于 WorldQuant BRAIN Alpha 研究：实验批次设计、Simulation 证据、结果解释与下一次实验选择。"
metadata:
  wqb_alpha_factory_research_contract: "2026-09-29"
---

# WQB 研究

## 执行契约

- **INPUT**：当前用户研究问题或简短工作集、bootstrap 交给 Skill 的当前 MCP inventory、对问题有用的 BRAIN dataset/datafield/evidence，以及可选的外部 Research Packet。
- **OUTPUT**：有当前证据支持的下一项研究行动和简短映射；行动可以是经唯一研究 API 提交的 `SimulationSpec`/batch，也可以是明确的 no-write、`UNKNOWN` 或能力限制结论。
- **PRECONDITIONS**：bootstrap 已确认当前 Skill contract version 与本 frontmatter 一致；Skill/reference 仅提供方法，不替代 runtime readiness、权限和写入契约。
- **SUCCESS**：行动能指出它要区分的解释、会改变决策的观测、证据范围与反证；Simulation 成功只证明执行结果可用，不自动证明经济机制。
- **FAILURE**：研究问题缺失且不能从上下文恢复时，指出最小缺项并停止构造候选；方法所需证据缺失时保留 `UNAVAILABLE`/`UNKNOWN`；所需研究工具缺失时报告能力限制并跳过受影响分支。

平台事实、readiness、ExecutionGuard、唯一写链、隐私和 Alpha submission 的 owner 均见根目录 [`AGENTS.md`](../../AGENTS.md)。本 Skill 只提供研究方法，不复刻 runtime/write safety contract。

研究结果只支持或反对被检验的机制，不自动证明机制成立。缺失证据保留 `UNKNOWN`/`UNAVAILABLE`。如果研究目标是 Region-Agnostic（RA）parent，优先筛选 Sharpe >3、Fitness >2、Margin >10、return 高且自相关低的 Regular parent，并在 RA 至少两个地区通过；这些是研究晋升目标，不覆盖 BRAIN 当前 hard checks。低自相关候选可有依据地放宽次要阈值，记录放宽项和理由，不伪称达标。Alpha submission 永远由人完成。

## INCOME / PLATFORM ALIGNMENT

研究同时服务长期、可持续的高质量低冗余 Alpha 产出和当前真实平台机会。每个 research wave 刷新可用的 BRAIN submission/status/checks、correlation、turnover/margin/cost，以及当前 account 的 Genius / Theme / competition / consultant 规则与资格；以 live 工具或本轮项目 research/audit 输入为来源，并在临时工作集中注明来源与观察时间。当前 inventory 或输入未提供某项事实时标 `UNKNOWN`，不得引用旧 tmp 快照、猜阈值、臆测资格或保证收入。活动机会不能替代机制证据，也不能凌驾于 BRAIN hard checks、科学稳健性或低冗余要求之上。

外部论文由项目研究/audit 输入提供；不要求本地 Research Agent 联网检索论文。输入的每篇论文都进入[统一论文映射](references/batch-design.md#universal-paper-mapping)，即使 BRAIN 当前没有相应 observable 也保留映射缺口。论文提供待检验机制，不提供已验证 Alpha；可靠模板必须经过[BRAIN family-level validation](references/result-interpretation.md#template-evidence-and-promotion)。

每篇有研究价值的论文形成 `PAPER_TEMPLATE_CANDIDATE`：在 research mapping 中保留来源/发表日期/状态、claim、机制和每项 template assumption 的来源链，以及 negative control 与当前 `BRAIN MAPPING`。候选可映射已有模板、扩展语义 slot、提出同机制 `NEW_PROBE` sibling；只有现有模板无法忠实表达核心机制/关系时才提出新 skeleton candidate。Candidate 是研究笔记，不是私有 catalog 条目；写入 catalog 仍受 `wqb_agent/alpha_templates/AGENTS.md` 和 BRAIN family evidence 晋升约束。

要新增或修改 private template/catalog 时，先阅读 `wqb_agent/alpha_templates/AGENTS.md` 并只通过唯一 `AlphaTemplate` owner 与现有 strict validation；若当前能力不足，candidate 留在 Research mapping 并标记 capability gap，不绕过 owner。

## Skill 版本

frontmatter 中 `metadata.wqb_alpha_factory_research_contract` 是本文件编写时所针对的 Research contract version。bootstrap 负责比较 runtime 版本；不匹配即本 Skill 为 `SKILL_STALE`，不得让本文件覆盖 runtime/tool owner。

本 Skill/reference 提到的具体研究工具必须对照当前会话实际 inventory。Core 未暴露的低频能力若无可用路径，标记 `LOCAL_RESEARCH_CAPABILITY_LIMIT` 并跳过受影响分支；不得臆造工具。

## Canonical Agent path

按当前会话实际 `tools/list` 和返回 schema 工作：先读 readiness，再发现 live dataset、field 和 operator evidence；需要模板候选时读 `list_templates` 的私有 catalog inventory，并由 Agent 选择模板、字段及数量后调用 `generate_probes`。Inventory 提供当前经济机制、方向理由、角色、字段槽、关系契约、horizon/settings arms 和已声明 numeric/operator slots；Generator 只物化 Agent 指定的输入，不为 Agent 排名或挑选模板、字段、候选。

保留 generator 返回的 `SimulationSpec` 字段与 dataset provenance，由 Agent 加上唯一 `proposal_id` 和本地 hypothesis/note/template mapping，再调用 spec validation 与 `research_batch_status`。按 admission 与当前用户目标选择后续 write tool；Gateway 再做完整写入准入。模板 inventory 和 generation 使用同一配置下的显式 private catalog；缺失时 fail closed，不回退到 public synthetic catalog。若工具、schema 或私有 catalog 不可用，报告能力限制；不猜 template ID、不直接 import 内部模块、不用 shell 或辅助脚本补缺。

## 每个研究批次

### PROBE 与 OPTIMIZE 两条 Agent lane

- `PROBE` 是持续主线：寻找新的 mechanism、dataset、field、template family、relationship 与有经济理由的 operator realization。开始优化不能关闭 Probe。
- `OPTIMIZE` 只作用于有真实支持证据的 Alpha/family；可围绕一个已声明的 horizon、template slot、setting、neutralization/decay/truncation 或一个有理由的结构 sibling 优化，一次只改变一个有意义的轴，不做 Cartesian search。
- `low self-correlation` 是重要研究优先级；同时检查性能、BRAIN hard checks 与当前 qualified pool 的 pairwise 冗余，再在约束满足时继续提高 Return。
- 两条 lane 可在同一 research wave 和同一 Simulation batch 并行。复用现有 `proposal_id`、`note`、`template_id` 与 Agent-owned mapping 标记每个 proposal 的 lane、hypothesis、field provenance 和 changed axis；不添加 schema 或 Python 研究状态。
- 每个方向的 `soft probe budget` 是该 wave 的 Agent work plan，由当前 live quota/capacity、数据/字段/模板宽度、研究不确定性与已有 BRAIN evidence 决定。新证据出现时可调整或开启 `EARLY OPTIMIZATION`；没有跨项目固定的批次或累计 Simulation 门槛。
- 先准备所有当前有研究理由且 admission 为 READY 的候选，再按 Gateway/工具给出的当前容量提交。批次可以较小；单候选使用合法 Single path；Multi 由 Gateway 按兼容设置分组、限制并发并 refill capacity。不得为了填满 transport capacity 制造字段、operator、近义 template 或无意义 wrapper。
- 若一个机制有支持证据、live self-correlation 较低且没有明显失败，Agent 可提前给它分配 OPTIMIZE lane，同时继续新的 PROBE；它仍可为 `TARGET_NOT_MET` 或 `BRAIN_SUBMITTABLE=false`。按[结果解释](references/result-interpretation.md#target-and-submission-are-separate)分别记录目标和 BRAIN 资格，Optimization 不放宽 hard checks。
- 对终选集合使用 full Alpha evidence 取得 self-correlation；用当前 `compare_alphas()` inventory 对 qualified candidates 读取 pairwise daily-PnL correlation。按[结果解释](references/result-interpretation.md#qualified-pool-and-pairwise-pnl-correlation)判断冗余与 UNKNOWN；self / PROD / pairwise correlation 不互相替代。

- 从多个竞争假设起步。使用 BRAIN 原始 dataset/datafield 列表，并解释每个 Agent 选定字段。
- 探索新字段、自定义分组、字段复合、新算子或新模板时，先读[批次设计](references/batch-design.md)中的 live discovery 与结构判据。
- 每个 proposal 都要有 batch 内唯一的 `proposal_id`，并在调用写工具前保留 `proposal_id → hypothesis/note/template` mapping。适用时提供有用的 `note` 和模板/家族标签。批次按对照组、本地兄弟组、证伪组与新探针分组。
- 每份 `SimulationSpec.settings` 都要从当前 `get_simulation_config()` 的完整设置开始，再显式覆盖本实验要改的 scope/settings；Gateway 与 BRAIN 使用 spec 实际传入的 settings，不会替 Agent 默默合并 config defaults。
- `note` 可使用 `H2:EXPLORE`、`H3:CONTROL`、`H2:FALSIFY`、`H2:LOCAL` 等假设标签，由 Agent 在 mapping 中解释；Simulation payload/result 细节遵循当前 MCP tool schema。
- 说明每组测试什么机制、什么结果会改变下一次决策、使用多少 Simulation。大量有目的的 Simulation 受欢迎；不可追溯的随机表达式不受欢迎。
- 保留探索。高结果只是比较候选，不是大举开发该方向的许可。
- 本地变体保持字段、算子与表达式拓扑不变；拓扑变化是带独立假设的 `NEW_PROBE`。
- 失败归因到假设、字段、算子、horizon、实现、相关性或稳健性。没有新证据不得重跑已明确失败的形态。
- 候选应覆盖能区分机制解释的数据集/字段、关系与有经济理由的算子 realization，并控制 selection pressure、冗余与多重检验；wave budget 由当前有价值的候选、研究不确定性与 live admission 决定，不为数量凑样本。
- Cold start 先读 `research_status()`。若全局被已有 guard 标为 `BLOCKED_BY_REMOTE_STATE`，绝不重发、删除或绕过原请求；取得具体候选后调用 READ_ONLY `research_batch_status(specs)`。按 proposal admission 排除精确冲突的候选，继续本批可执行候选；不把一个 unknown 扩大成全体候选停摆。若批次状态也不是 `READY`，处理返回的能力/quota/capacity 原因或只跳过受影响分支，再继续不受影响的数据发现、表达式设计与证据整理。
- 多候选 batch 使用当前 `research_batch_status` 的 eligible/capacity evidence；Gateway 在写前重查全部事实，隔离精确冲突项并继续其余 READY 候选。传输容量与 unresolved guard 处理由 Gateway 管理，不把安全 blocker 扩大成研究数量门槛；未决写入绝不清除或重发。
- Regular parent 晋升到 Region-Agnostic 时，使用 `REGION_AGNOSTIC` 与 `region=ALL` 的 live Simulation scope，并保留真实字段→dataset provenance。Gateway 会分别按 USA/EUR/ASI/GLB 查询 field catalog；表达式的所有字段须在至少两个相同子地区可用且 VECTOR 类型检查通过。`validate_simulation_spec()` 只证明请求形状，不能替代 Gateway 的 live field/write preflight。
- Skill、记忆教训或历史运行不是 BRAIN 事实。记忆只保留少量可迁移研究教训；永不存完整转录。

## 方向与局部优化

- Probe 开始前声明 `direction`、`direction_reason` 和 `direction_transform`，理由必须先于结果；不得因 Sharpe 为负而事后反转方向。
- 复杂度按最终表达式的 effective operator occurrence 计算，`direction_transform` 等机械 wrapper 也计数。CONTROL 为 1–3，PROBE 为 4–6；上限是硬约束，不是目标，不加无经济理由的算子凑数。
- 局部优化先声明 immutable anchor。每个 variant 只改一个已声明的 numeric slot 或一个 settings key；字段、template、mechanism、算子顺序/拓扑和 operator count 保持不变。需要改拓扑时另立 `NEW_PROBE` 假设。
- Template-slot optimization 只使用当前 `list_templates` inventory 明确返回的 `default`、`allowed_values` 与 `economic_role`。需要的信息未暴露时报告 `TEMPLATE_SLOT_METADATA_MISSING`，归类为 `LOCAL_RESEARCH_CAPABILITY_LIMIT`；不得猜 allowed values、从旧缓存恢复当前 template contract，或提交依赖该槽位的 Simulation，但可继续普通 `NEW_PROBE`、字段研究和非 template-specific settings research。
- baseline 已有证据时不重复提交它。解释时比较 baseline 与所有 variants，不自动选 winner；若邻近变化没有一致、可解释的支持证据，停止该优化维度并保留其他解释。

## 研究工作集

为当前问题保持一个简短工作集，每轮覆盖：

```text
问题
活跃假设
强证据
被否决的解释
开放不确定性
有前景的家族
paper/template candidate（来源、发表日期、template assumption provenance）
当前平台机会（BRAIN/account 来源与观察时间，或 `UNKNOWN`）
试验上下文（`family_label` / `related_trial_count_lower_bound` / `count_scope`）
下一实验
```

它是工作记忆而非档案：永不替代 BRAIN 证据；当前活动资格与规则须每个 wave 重新核验，只有跨任务仍成立的经验才配进 reference。

Alpha/PA 或其他 execution mode 只有在 fresh `research_status()` 与当前 tool inventory 明确暴露时，才可作为同一 template mechanism 的不同实验环境比较；若缺失或 validation/write contract 不可用，标 `CAPABILITY_MISSING`，不假设存在或绕过 facade。

## Wave-end research and tool-efficiency review

每个自然 research wave 结束时，复盘研究信息增益、family yield 和无效 Simulation 来源，并区分 `RESEARCH_UNKNOWN`（研究未知）与 `TOOL_FRICTION`（可复现的确定性工程问题）。研究未知量进入下一轮 paper/template/data/Simulation 研究；只有可复现的工具摩擦才按根目录 [`AGENTS.md`](../../AGENTS.md) 的 owner 流程进入 `TOOL_OPTIMIZATION`。研究方法以提高 `INFORMATION GAIN / SIMULATION`、缩短 `paper → template → evidence` 路径为导向。

规划或标注大批次时读[批次设计](references/batch-design.md)；比较优胜者、检查稳健性或决定是否继续时读[结果解释](references/result-interpretation.md)。
