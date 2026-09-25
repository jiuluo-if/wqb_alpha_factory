# 本地安全布局

本地不是研究结果数据库。BRAIN 是 Alpha 与 Simulation evidence 的唯一事实源。

允许的本地对象：

| 对象 | 默认位置 | 用途 |
|---|---|---|
| ExecutionGuard | `.wqb_state/execution_guard.json` | 防止不确定 POST 被重发 |
| Remote cache | `.wqb_state/.alpha_feed_cache/remote.json` | 最近 N 天的可重建远端视图 |
| credentials | 外部环境/文件 | 创建认证 client |
| lock | 临时或配置路径 | 防止本地并发 owner 冲突 |

ExecutionGuard 状态只有 `SUBMITTING`、`RUNNING`、`SUBMIT_UNKNOWN`。`SUBMITTING` 在重启后按不确定提交处理；有 progress URL 只能轮询该 URL；没有证据证明 POST 未发生时禁止重 POST。

ExecutionGuard 仍只保存未解决 remote-write safety metadata；每条记录可额外保存 bounded integer `simulation_count`，表示该 guarded write 如果已被平台接受所代表的 Simulation 数量。Single 默认为 1，Multi 使用实际 child 数量（2–10）。旧记录缺失该字段时按 1 读取；非法值按 1 fail closed。不得保存 child payload、expression、settings、result 或 child ID。

每条记录还保存执行安全用途的 `kind`（`SINGLE`、`MULTI_PARENT`、`MULTI_CHILD`）以及 `MULTI_CHILD` 的 `parent_fingerprint`。exact-once 以单个 Simulation 为单位：Multi POST 之前 parent 与每个 child 各自登记 fingerprint，所以 unknown 之后的重排、拆分、子集重试或把 child 改走 Single 都不会再次 POST 同一个 unresolved child；`kind` 同时也决定恢复路径（`MULTI_PARENT` 用 Multi 轮询，其余用 Single 轮询，child 跟随其 parent）。缺失 `kind` 的旧记录按 `SINGLE` 读取，这一读法只影响恢复方式，不影响 exactly-once。`parent_fingerprint` 是执行指纹，不是研究身份。

ExecutionGuard 的生命周期由远端写安全决定，不由配额观察窗口决定。配额回退只使用持久 `created_at` 作为本地 POST 前时间代理，按 `America/New_York` 归属每日/窗口估算；`updated_at` 仅表示 guard 状态维护，不参与重新归日。超出当前窗口或时间未知的 guard 仍保留在 ExecutionGuard，未知时间按保守估算计入处理。

远端缓存可删除并从 BRAIN 重建。读取缓存时，无效、契约不匹配或缺失只视为不可用/`UNKNOWN`；已过期缓存由 freshness 标为 `STALE`，不自动删除主缓存或清理临时资源。`refresh_remote_alphas`/`RemoteAlphaCache.refresh` 是 bounded maintenance 与原子写入 owner，`purge_remote_cache` 是显式主 cache 删除 owner。缓存与 live 冲突时 live 优先，过期或缺失缓存不得制造 `PASS`。credentials、真实 Alpha、私有字段和完整研究数据不得写入 tracked 文件、日志或 guard。
