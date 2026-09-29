# 本机 `tmp/` 工作区治理

## 用途与边界

`tmp/` 是本机 scratch space，整个目录由根 `.gitignore` 忽略。它不作为研究事实源、备份、发布包或跨机器同步机制。不得把这里的文件提交或上传；需要长期共享的通用说明应迁入受审查的 `docs/`，不能带真实 Alpha、私有字段、完整表达式、凭据或运行状态。

本地研究数据和日志可能包含私有信息。不能把它们复制到 tracked 文件、公开 catalog、issue 或远程服务。BRAIN 仍是 Simulation 与 Alpha 结果的事实源；`tmp/` 中的快照只能作为有来源和采集时间的临时证据。

`tmp/knowledge/` 是受限的本机知识层，只放脱敏后的可复用经验、来源索引和维护规则；不放实时 Alpha/Simulation 结果、完整研究表达式、候选 ID 或凭据。它不属于 cache，不按普通临时文件的年龄清理；改写或删除前先检查私有 memory note 与其他文档的引用。

## Research runtime handoff

`tmp/research_handoff.json` 是 Research Agent 的单份、本机、gitignored 的运行交接；它是 `DISCOVERY_HINT`，不是 BRAIN truth、Guard 副本或研究数据库。fresh `research_status()`、当前 ExecutionGuard 结果和 live BRAIN evidence 始终优先。此文件只在新会话完成 readiness handshake 后或自然 wave/batch 边界由 Research Agent 覆盖；`TOOL_OPTIMIZATION` 和 Maintenance procedure 只读，不改写正在运行的 handoff。

字段 owner 在本文档。Writer 使用以下 compact shape；未观测值用 `null`、`UNKNOWN` 或省略，不得把 unknown 记为 0。Reader 忽略未知的旧字段，不据此否决整个 hint；下一次受控 handoff 可覆盖回 compact shape。

```json
{
  "schema_version": 1,
  "updated_at": "ISO-8601 timestamp",
  "research_code_sha": null,
  "research_contract": null,
  "tool_inventory": [],
  "readiness": "UNKNOWN",
  "capability_blockers": [],
  "family_label": "UNKNOWN",
  "related_trial_count_lower_bound": "UNKNOWN",
  "count_scope": "UNKNOWN",
  "batches_observed": 0,
  "candidates_observed": 0,
  "status_counts": {
    "DONE": null,
    "FAILED": null,
    "EXACT_DUPLICATE": null,
    "SUBMIT_UNKNOWN": null,
    "NOT_DISPATCHED": null,
    "UNKNOWN": null
  },
  "pending_execution_count": null,
  "pending_simulation_count": null,
  "pending_multi_child_count": null,
  "tool_friction": {
    "invalid_spec_failures": null,
    "deterministic_failure_codes": null,
    "batch_execution_failures": null,
    "evidence_retrieval_failures": null,
    "evidence_retrieval_pending_count": null,
    "repeated_manual_workaround_count": null
  }
}
```

`readiness` 只能取 `READY`、`BLOCKED_BY_REMOTE_STATE`、`WAITING_FOR_CAPABILITY` 或 `UNKNOWN`，并记录最近一次成功 status observation；它不能授权 write。只要当前观测到 `pending_execution_count > 0` 或 `SUBMIT_UNKNOWN`，handoff readiness 必须为 `BLOCKED_BY_REMOTE_STATE`；fresh `research_status().write_readiness` 仍是当前写入准入 owner。`status_counts` 只表示当前已观察到的 task/wave；值必须是非负整数下界，无法可靠观测时写 `null` 或省略，不能猜 0。`tool_inventory` 和 friction counts 也只限当前 task/wave 的有界观察。`family_label` 只能是短分类；`count_scope` 只能是 `CURRENT_WORKING_SET`、`CURRENT_TASK`、`HANDOFF_CHAIN` 或 `UNKNOWN`。reason code 只记录有限、稳定且脱敏的代码，不记录自由文本诊断。

不得写入 Alpha ID、field ID、expression、私有 dataset 配对、paper payload、metrics/PnL、完整结果、credentials/cookies/tokens、progress URL 或自由文本诊断；不新增 database、telemetry、watcher 或 scheduler。若 Agent 未在当前上下文读取上一份 handoff，不沿 `HANDOFF_CHAIN` 累加旧计数。详细研究 continuation 不属于本 handoff。

## 一级分类与平铺约定

`tmp/` 的目标布局是在根层保留 `README.md` 和按性质分类的一级目录。分类目录内不创建 task 子目录或其他嵌套目录；任务日期和 task-id 写入文件名。每次清理前通过本地 inventory 确认实际文件位置。当前分类如下：

| 一级目录 | 内容 | 命名/结项 |
| --- | --- | --- |
| `scripts/` | 本机临时或历史 Python 脚本 | 使用语义化 `lower_snake_case`；历史研究脚本不代表当前流程 |
| `snapshots/` | JSON/JSONL 证据和研究数据快照 | 使用 `YYYY-MM-DD_<semantic_name>.<ext>`，并记录来源 |
| `logs/` | 执行日志和文本输出 | 结项并对账后评估清理 |
| `docs/` | 历史总结、诊断和 playbook | 保留历史正文，当前索引负责导航 |
| `planning/` | task plan、findings、progress、比较矩阵 | 扁平文件名 `YYYY-MM-DD_<task-id>_<semantic_name>.md`，不造无意义轮数 |
| `field_library/` | dataset-keyed 稳定 lookup 文件及索引 | 保留原 dataset key；更新 freshness/index |
| `patches/` | 历史补丁 | 只读保留，先核实是否已合入再评估清理 |
| `maintenance/` | inventory、候选表、迁移 manifest 和 cleanup 记录 | 文件直接放在本目录，维护当前清单 |
| `knowledge/` | 脱敏后的可复用经验和索引 | 稳定语义名；不按普通缓存年龄清理 |

## Research data lifecycle

Treat each local artifact as either `SHORT_TERM` working material or `LONG_TERM_LOCAL` reusable knowledge. BRAIN, the configured ExecutionGuard, and current runtime capability remain `LIVE_TRUTH`; a tmp copy never becomes a second source of truth.

### Existing inventory owner

`tmp/maintenance/inventory.csv` is the single local file inventory. Extend this CSV when new retention metadata is needed; do not create another index, database, sync process, watcher, or scheduler. Alongside its path/category/size/identity columns, each row records:

| Column | Meaning |
|---|---|
| `owner` | Task/wave for short-term artifacts, or the maintained library owner for long-term material |
| `source` | Evidence source/provenance class; derived artifacts name their source inventory path |
| `captured_or_verified_at` | ISO-8601 content capture or evidence verification time; `UNKNOWN` when unavailable |
| `scope` | Task/wave or bounded subject scope; keep private identifiers only in this ignored local inventory |
| `freshness` | `CURRENT`, `STALE`, or `UNKNOWN`, based on the owning source/contract rather than file metadata |
| `retention_status` | `SHORT_TERM`, `LONG_TERM_LOCAL`, `DELETE_CANDIDATE`, `HISTORICAL`, or `REVIEW_REQUIRED` |
| `supersedes` | Replacing inventory path or `UNKNOWN`; the previous artifact then becomes historical or a deletion candidate |

Existing rows with no provenance review stay `UNKNOWN` with `REVIEW_REQUIRED`; do not infer owner, source, freshness, or permission to delete from `last_write`, mtime, hash, filename, or extension. Inventory identity columns such as SHA remain integrity/grouping evidence only.

### Short-term and long-term handling

- `SHORT_TERM` covers the current task/wave, recovery context, temporary evidence, diagnostics, plans, and logs. Record owner, source, `captured_or_verified_at`, scope, and whether it is current or superseded. At wave/task end, finish required reconciliation and choose `DELETE` or explicit promotion.
- `LONG_TERM_LOCAL` is limited to reusable, privacy-safe `knowledge/`, `field_library/`, or unique historical evidence that has provenance, scope, and a current freshness state. Do not promote one-time winners, live BRAIN snapshots, reconstructible caches, full expressions, or sensitive run state.
- Promote only when the material is reusable across tasks, non-sensitive, not cheaply re-readable/rebuildable, has an identified source, and will prevent real repeated work. Promotion is explicit, never automatic.
- On use, re-check long-term freshness against its owner. At a natural research-wave boundary, review short-term artifacts. On an owner/schema/contract change, review only inventory entries that name that owner/source. This is `ON_USE + WAVE_BOUNDARY + OWNER_CHANGE`, not a background process.
- If newer live/repository evidence replaces a local artifact, mark the old row `STALE` and connect it through `supersedes`; delete it when it has no historical value, or retain it as `HISTORICAL` and exclude it from default Agent context. Keep at most one current pointer per artifact class; use inventory/navigation for prior versions.

### Deletion decision

`DELETE_CANDIDATE` is not an instruction to delete. Before removal, identify the exact row/path, owner, source, consumer references, task/reconciliation status, and why it is reconstructible or fully superseded. Preserve unknown and unique evidence as `REVIEW_REQUIRED`. Never delete unresolved guard/checkpoint state, `SUBMIT_UNKNOWN` evidence, or material referenced by an unfinished task. After an approved cleanup, refresh only the affected inventory rows and record the action; never clear the tmp root or apply a single age-based cutoff.

- 新时间快照日期优先取文件内 capture/create/save 日期，其次可信内容日期；没有时才以所属日期语义或 mtime 回退，并在 manifest 标注来源。
- 保留真实 round/实验标识；不新造 `round1`、`batch2` 等无意义编号。
- 历史材料在 2026-09-26 按性质平铺归类；历史正文和脚本内的路径文字保留原样。只更新当前 README、目录索引和 selector，使人能导航到新位置。
- 迁移前先生成 `old_path → new_path` manifest，检查目标冲突、owner 和 SHA-256。目录只允许一级分类，不允许 category/subcategory 嵌套。
- 归档文件不是缓存；不得只因日期旧或文件名含 `old`、`archive`、`summary` 就删除。

## 清理规则

### 可直接清理

- `__pycache__/**`、`*.pyc`、`*.pyo`：Python 可从源文件重建。
- 结项任务内明确标记为 cache 的文件：核实来源仍可用、重建不触发线上 Simulation POST 后清理。
- 已结项且完成相关研究/提交状态对账的临时日志。

### 必须先核对 owner 和来源

- `*.json`、`*.jsonl`、`*.md`、`*.txt`、`*.patch`、研究脚本和实验报告。
- 各种 rounds、candidate/shortlist、field、correlation、submission、checkpoint 和 guard 相关文件。
- `docs/`、`planning/`、`snapshots/`、work playbook 与本地 memory。

仅当文件可从权威来源精确重建，或已核实被受控版本完整替代且无未结任务引用时，才列入删除清单。mtime 和扩展名都不是充分删除条件。

### 禁止作为 tmp 清理对象

- 真实运行目录中的任何未解决 ExecutionGuard、checkpoint 或运行状态；不得因整理 tmp 去扫描后顺手改动/删除这些状态。
- 有 `SUBMIT_UNKNOWN` 或仍需对账任务关联的状态和证据。
- 唯一研究证据、尚未结项任务材料或无法确认可重建来源的文件。

## 执行清理的最低步骤

1. 先生成候选清单，包含完整相对路径、大小、修改时间、类型和删除理由；明确区分 cache、日志、研究证据、脚本和归档文档。
2. 搜索实际消费者，确认缓存源可用、历史材料非唯一副本，并核对相关任务已结项。
3. 删除时只指定已审查的文件/缓存子目录；不得对 `tmp/` 根目录执行递归清空，也不得使用单一年龄阈值批量删除。
4. 删除后重新枚举目标并记录数量/空间变化；保留的研究证据和归档不因本轮清理而重排。

## 2026-09-26 基线

本次只读盘点看到 1,304 个文件（约 16.8 MB）：532 JSON、451 Python、201 log、99 Markdown、11 pyc，其余为少量文本、JSONL、patch 和 plan selector。文件覆盖 2026-09-17 至 2026-09-26，说明这里同时承担了临时脚本、研究证据和历史归档，不应整目录删除。确认并清理的缓存限于 11 个 Python bytecode 文件；`v2-archive/` 中的历史计划与根部 playbook 作为不可重建资料保留。

清理后剩余 1,293 个文件（16,652,896 bytes）：根目录 1,003 个、`field_library/` 116 个、`glb_research/` 66 个、`v2-archive/` 108 个。该数字和目录描述为性质分类前的历史基线；当前布局以本节一级分类表及 `tmp/maintenance/inventory.csv` 为准。

## 2026-09-26 平铺整理基线

当日的迁移验收记录为：材料按性质平铺到九个一级目录，分类目录无子目录；README、知识索引和 planning selector 指向迁移位置。历史正文/脚本中的旧路径文字保留。此基线不证明当前 tmp 文件布局或研究运行状态；本机路径映射与 SHA-256 由 `tmp/README.md` 导航到维护清单。
