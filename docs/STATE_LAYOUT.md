# Local Safety Layout

本地不是研究结果数据库。BRAIN 是 Alpha 与 Simulation evidence 的唯一事实源。

允许的本地对象：

| 对象 | 默认位置 | 用途 |
|---|---|---|
| ExecutionGuard | `.wqb_state/execution_guard.json` | 防止不确定 POST 被重发 |
| Remote cache | `.wqb_state/.alpha_feed_cache/remote.json` | 最近 N 天的可重建远端视图 |
| credentials | 外部环境/文件 | 创建认证 client |
| lock | 临时或配置路径 | 防止本地并发 owner 冲突 |

ExecutionGuard 状态只有 `SUBMITTING`、`RUNNING`、`SUBMIT_UNKNOWN`。`SUBMITTING` 在重启后按不确定提交处理；有 progress URL 只能轮询该 URL；没有证据证明 POST 未发生时禁止重 POST。

ExecutionGuard 仍只保存未解决 remote-write safety metadata；每条记录可额外保存 bounded integer `simulation_count`，表示该 guarded write 如果已被平台接受所代表的 Simulation 数量。Single 默认为 1，Multi 使用实际 child 数量（2--10）。旧记录缺失该字段时按 1 读取；非法值按 1 fail closed。不得保存 child payload、expression、settings、result 或 child ID。

ExecutionGuard 的 lifetime 由 remote-write safety 决定，不由 quota observation window 决定。Quota fallback 只使用 durable `created_at` 作为本地 pre-POST time proxy，按 `America/New_York` 归属 daily/window estimate；`updated_at` 仅表示 guard state maintenance，不参与重新归日。超出当前 window 或时间未知的 guard 仍保留在 ExecutionGuard，未知时间按 conservative estimate inclusion 处理。

Remote cache 可删除并从 BRAIN 重建。读取 cache 时，invalid、contract mismatch 或缺失只视为 unavailable/UNKNOWN；已过期 cache 由 freshness 标为 `STALE`，不自动删除主 cache 或清理临时资源。`refresh_remote_alphas`/`RemoteAlphaCache.refresh` 是 bounded maintenance 与原子写入 owner，`purge_remote_cache` 是显式主 cache 删除 owner。cache 与 live 冲突时 live 优先，过期或缺失 cache 不得制造 PASS。credentials、真实 Alpha、私有 field 和完整研究数据不得写入 tracked 文件、日志或 guard。
