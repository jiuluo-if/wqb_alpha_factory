# 本机 `tmp/` 工作区规范

## 用途与隐私

`tmp/` 是本机忽略目录，不是研究事实源、备份、发布包或跨机器同步区。不得上传其中的研究数据、日志、Alpha、私有字段、完整表达式、凭据或运行状态。通用且适合共享的说明应经审查后放入 `docs/`。

BRAIN 和当前 runtime 是研究事实源；本地快照只作带来源和采集时间的临时证据。`tmp/knowledge/` 只存脱敏、可复用的知识与来源说明，不放实时结果或私有研究内容；修改前先查活动引用。

## 研究运行交接

`tmp/research_handoff.json` 由 Research Agent 独占维护，是本机 `DISCOVERY_HINT`，不是 BRAIN 真相、ExecutionGuard 副本或研究数据库。维护工作只读。新会话完成 readiness handshake 后或 wave/batch 自然结束时，Research Agent 才可更新；当前 `research_status()`、ExecutionGuard 和 BRAIN evidence 优先。

写入方使用以下紧凑结构。未观测值写 `null`、`UNKNOWN` 或省略，不得猜成 0。读取方忽略未知旧字段；只在受控更新时收敛到此结构。

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

`readiness` 仅取 `READY`、`BLOCKED_BY_REMOTE_STATE`、`WAITING_FOR_CAPABILITY`、`UNKNOWN`，且不能授权写入。有未解决 execution 或 `SUBMIT_UNKNOWN` 时必须记为 `BLOCKED_BY_REMOTE_STATE`。计数只描述当前 task/wave 的可靠下界；无法确认时留空或记 `UNKNOWN`。`count_scope` 仅取 `CURRENT_WORKING_SET`、`CURRENT_TASK`、`HANDOFF_CHAIN`、`UNKNOWN`。

交接不得包含 Alpha/field ID、表达式、私有 dataset 配对、paper payload、指标/PnL、完整结果、凭据、progress URL 或自由文本诊断；只保留有限且脱敏的 reason code。维护者不累加旧 handoff，也不覆盖运行中的交接。

## 目录约定

根层保留 `README.md` 和以下一级分类目录。分类目录内不建子目录；task/date 写入文件名。清理前先核对唯一清单 `tmp/maintenance/inventory.csv`。

| 目录 | 内容与约定 |
|---|---|
| `scripts/` | 本机临时或历史 Python 脚本；历史脚本不代表当前流程。|
| `snapshots/` | JSON/JSONL 证据与数据快照；记录来源，不据文件名判断时效。|
| `logs/` | 执行日志；结束后先核对状态、引用和对账，再决定保留或归并。|
| `docs/` | 历史总结、诊断和操作记录；保留独有证据，索引只作导航。|
| `planning/` | 计划、findings、progress、比较矩阵；保护活动 selector 和其引用材料。|
| `field_library/` | 唯一 dataset-keyed 字段查找缓存；刷新规则由根 `AGENTS.md` 负责。|
| `patches/` | 历史补丁；检查用途和引用后再处理。|
| `maintenance/` | 唯一清单及必要的迁移记录；不新建第二套索引。|
| `knowledge/` | 脱敏、可复用的经验与导航；不按文件年龄清理。|

文件名采用简短语义名；日期仅用于定位，不作为删除依据。历史文件保持平铺，不因归档新建目录层级。

## 唯一清单与标签

`tmp/maintenance/inventory.csv` 是唯一文件清单；清单自身不登记。列为 `relative_path`、`category`、`extension`、`bytes`、`last_write`、`owner`、`source`、`captured_or_verified_at`、`scope`、`classification`、`retention_time`、`supersedes`。`last_write` 只描述文件元数据。

`classification` 只取 `KEEP`、`MERGE`、`DELETE`、`REVIEW_REQUIRED`；`retention_time` 只取 `CURRENT`、`FOUNDATIONAL`、`SUPERSEDED`、`TIME_UNKNOWN`。Owner、source 或时效未知时使用 `REVIEW_REQUIRED / TIME_UNKNOWN`。维护清理期间不计算、不读取、不比较哈希；也不得凭日期、文件名或扩展名猜测过期。

每条记录写清 owner、source、scope 和可用的采集/核验时间。`supersedes` 记录替代路径；无法确认时为 `UNKNOWN`。归并产物逐条保留原路径与有效内容；归并后的产物按其自身用途标记 `KEEP`。

只在按需使用（`ON_USE`）、自然 wave/batch 边界（`WAVE_BOUNDARY`）或 owner/schema/contract 变化（`OWNER_CHANGE`）时复核相关条目，不做后台巡检。

## 保留与清理

- `KEEP`：有当前 owner、活动引用、可复用价值或独有证据。
- `MERGE`：语义或内容可归并；先确认有效信息均进入归档，再移除被替代的原件。
- `DELETE`：仅限已确认无当前 owner、无活动引用、无唯一信息，并有可靠替代或重建来源的文件。
- `REVIEW_REQUIRED`：证据不足；保持原状，不推断过期。

缓存、日志、JSON、Markdown、脚本、计划、patch 和快照均须逐项核对内容、owner/source、活动引用、任务/对账状态及替代来源。终态标记本身不证明研究对账完成；未知 owner/source 不得仅凭日期或低使用迹象删除。

可清理 Python 自动生成的 `__pycache__`、`*.pyc`、`*.pyo`，前提是只指定已确认路径。未解决 ExecutionGuard、checkpoint、运行状态、`SUBMIT_UNKNOWN` 证据、唯一研究资料和活动计划及引用材料禁止作为清理对象。不得扫描后顺手修改真实运行目录。

清理时只操作已审查的明确路径，不对 `tmp/` 根目录递归清空。归并/删除后更新本清单，并核对磁盘文件与清单路径一一对应；不新增索引、数据库、watcher、scheduler 或其他维护服务。
