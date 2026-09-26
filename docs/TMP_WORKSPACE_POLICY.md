# 本机 `tmp/` 工作区治理

## 用途与边界

`tmp/` 是本机 scratch space，整个目录由根 `.gitignore` 忽略。它不作为研究事实源、备份、发布包或跨机器同步机制。不得把这里的文件提交或上传；需要长期共享的通用说明应迁入受审查的 `docs/`，不能带真实 Alpha、私有字段、完整表达式、凭据或运行状态。

本地研究数据和日志可能包含私有信息。不能把它们复制到 tracked 文件、公开 catalog、issue 或远程服务。BRAIN 仍是 Simulation 与 Alpha 结果的事实源；`tmp/` 中的快照只能作为有来源和采集时间的临时证据。

`tmp/knowledge/` 是受限的本机知识层，只放脱敏后的可复用经验、来源索引和维护规则；不放实时 Alpha/Simulation 结果、完整研究表达式、候选 ID 或凭据。它不属于 cache，不按普通临时文件的年龄清理；改写或删除前先检查私有 memory note 与其他文档的引用。

## 分类与目录约定

新任务使用 `tmp/<YYYY-MM-DD>-<task-id>/`，按需建立：

| 子目录 | 内容 | 结项处理 |
| --- | --- | --- |
| `scripts/` | 本次任务的一次性分析/检查脚本 | 判断是否值得迁入正式 `scripts/`；其余随任务关闭清理 |
| `evidence/` | 输入快照、中间 JSON、报告和结果 | 仅保留尚未可重建、且对结论有用的本地证据；标来源/时间 |
| `logs/` | 命令输出与诊断日志 | 结项并完成状态对账后删除；开放任务的日志保留 |
| `cache/` | 可由明确来源再生成的数据 | 结项时清理；无 owner 的缓存超过 30 天列为清理候选 |
| `archive/` | 有历史价值且不能重建的任务总结 | 只读保留；确认已迁入受控文档且内容重复后才删除旧副本 |

## 命名约定

- 任务目录统一使用 `YYYY-MM-DD-<task-id>`；日期与 task-id 使用小写 ASCII 和连字符。
- 源脚本和稳定 lookup key 保留有意义的 `lower_snake_case` 名称，不为了日期或计数修改代码标识。
- 时间快照文件统一使用 `YYYY-MM-DD_<semantic_name>.<ext>`。日期优先取文件内的 capture/create/save 日期，其次取所属日期任务目录，再次取可信的文档日期；都没有时才用文件修改日期，并在本地 rename manifest 中标注来源。
- rename 保留原有语义名和真实实验/round ID；不新造 `round1`、`batch2` 等无语义序号。已有轮次号只有在它是可追溯实验标识时才保留。
- README、知识索引、稳定经验页，以及日期任务目录中的 `task_plan.md`、`findings.md`、`progress.md` 和固定 manifest 文件使用稳定语义名，不重复加日期。由 dataset/operator 等稳定键直接寻址的缓存文件名也保留该键，更新时间记录在 manifest/index 中；rename 前须先检查真实 consumer。
- 批量 rename 前生成 `old_path → new_path` 清单，检查目标冲突和源码/文档引用；完成后更新 consumer 引用并验证路径均存在。活动进程持有或正在写入的文件须等进程结束后再处理。

历史根文件和原有历史子目录已按用户授权一次性集中到 `tmp/archive/legacy_root/`，保留其相对目录树、文件名和历史路径文字；迁移映射与 SHA-256 见本地 centralization manifest。后续历史文件迁移须先审核消费者，默认只更新当前导航，不改写历史正文。`tmp/README.md` 是根层导航入口；新任务文档写入 `YYYY-MM-DD-<task-id>` 目录。归档资料不是缓存；不得只因日期旧或文件名含 `old`、`archive`、`summary` 就删除。

## 清理规则

### 可直接清理

- `__pycache__/**`、`*.pyc`、`*.pyo`：Python 可从源文件重建。
- 结项任务内明确标记为 cache 的文件：核实来源仍可用、重建不触发线上 Simulation POST 后清理。
- 已结项且完成相关研究/提交状态对账的临时日志。

### 必须先核对 owner 和来源

- `*.json`、`*.jsonl`、`*.md`、`*.txt`、`*.patch`、研究脚本和实验报告。
- 各种 rounds、candidate/shortlist、field、correlation、submission、checkpoint 和 guard 相关文件。
- `archive/`、历史 planning 文档、work playbook 与本地 memory。

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

清理后剩余 1,293 个文件（16,652,896 bytes）：根目录 1,003 个、`field_library/` 116 个、`glb_research/` 66 个、`v2-archive/` 108 个。根目录暂时维持原布局，等相关任务结项后再逐组迁入 task-id 目录；本轮不搬动这些文件。
