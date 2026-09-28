# 本地状态布局

本地只保存未解决的远端写安全记录与可重建的 Alpha metadata cache，不维护研究结果数据库。BRAIN 是 Alpha 与 Simulation evidence 的事实源。

| 对象 | 默认位置 | 说明 |
|---|---|---|
| ExecutionGuard | `.wqb_state/execution_guard.json` | 仅记录未解决远端写的安全元数据；字段、exactly-once 与恢复规则见根目录 [`AGENTS.md`](../AGENTS.md)。 |
| Remote cache | `.wqb_state/.alpha_feed_cache/remote.json` | 保存有 retention window 的轻量远端视图，默认 7 天，可配置 1–90 天。 |
| credentials | 外部环境或文件 | 由 credentials resolver 读取。 |
| process lock | 临时或配置路径 | 防止本地并发 owner 冲突。 |

缓存缺失、无效或契约不匹配时按不可用/`UNKNOWN` 处理；过期状态通过 freshness 标为 `STALE`，不会自动清理主缓存。BRAIN live response 优先于缓存。

`refresh_remote_alphas` / `RemoteAlphaCache.refresh` 负责有界刷新和原子写入；`purge_remote_cache` 是显式主缓存删除入口。实现见 [`wqb_agent/remote_alpha_repository.py`](../wqb_agent/remote_alpha_repository.py) 与 [`wqb_agent/alpha_feed_cache.py`](../wqb_agent/alpha_feed_cache.py)。

真实 Alpha、私有字段和完整研究数据不得写入 tracked 文件、日志或 ExecutionGuard；共享隐私规则见 [`PRIVACY.md`](PRIVACY.md)。
