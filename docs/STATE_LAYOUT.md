# Local State Layout

Remote-First 架构中，BRAIN 是 Alpha/Simulation 结果的唯一事实来源。本地只保存远端写入安全边界、可重建的短期 metadata cache、外部 credentials 引用和进程锁。

## 允许的本地对象

| 对象 | 位置 | 用途 | canonical 事实 |
|---|---|---|---|
| ExecutionGuard | `.wqb_state/execution_guard.json` | 防止不确定 POST 被重发 | 仅未解决写入 identity |
| Remote Alpha cache | `.wqb_state/.alpha_feed_cache/remote.json` | 最近 N 天远端轻量 metadata 视图 | 否，可从 BRAIN 重建 |
| credentials | 外部文件/环境 | 创建认证 client | 否 |
| process lock | 临时/配置路径 | 防止本地并发 owner 冲突 | 否 |

## ExecutionGuard schema

每条记录只允许：

```text
execution_fingerprint
status = SUBMITTING | RUNNING | SUBMIT_UNKNOWN
progress_url
created_at
updated_at
remote_alpha_id (optional)
```

`SUBMITTING` 在进程重启时必须升级为 `SUBMIT_UNKNOWN`。没有 progress URL 的 UNKNOWN 不能自动清除；有 progress URL 只能只读轮询同一 URL。只有 BRAIN 结果已确认时才可删除已解决 guard。

## 明确不属于本地 canonical state

以下结果和研究生命周期不再由新架构维护：

```text
metrics / checks / PnL / aggregates / correlation
validation / reward / research classification / settlement
round / parent / child / lineage / research cycle
Trajectory / TrialLedger / ExperienceMemory
proposals inbox / factory session / optimizer state
```

迁移期旧文件可能仍被兼容代码读取；新代码不得向其中写入结果，也不得依赖其恢复 BRAIN evidence。

## 隐私与恢复

cache、guard、日志和 tracked fixtures 不得写入真实 Alpha、私有 field、完整研究表达式或 credentials。真实工作区的未完成旧 checkpoint/`SUBMIT_UNKNOWN` 不得手工编辑、删除或通过新路径绕过；先只读对账，必要时由用户明确授权本地恢复。
