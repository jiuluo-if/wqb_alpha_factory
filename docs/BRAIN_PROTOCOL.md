# BRAIN 协议事实层

本项目把协议事实集中在 `wqb_agent/protocol.py`。它只记录当前 client 已使用的 endpoint、请求/响应形状、Retry-After 解析和 capability 证据等级，不把社区猜测升级为官方契约。

## Capability 证据等级

| 等级 | 含义 |
|---|---|
| `OFFICIAL` | 来自平台公开协议或当前项目已采用的稳定官方接口说明 |
| `LIVE_VERIFIED` | 当前账号和环境的真实响应已验证 |
| `FIXTURE_VERIFIED` | 脱敏 fixture 通过结构校验；不代表线上可用 |
| `COMMUNITY_OBSERVED` | 社区观察到的接口，只能 probe/fixture，不能成为生产依赖 |
| `UNKNOWN` | 没有足够证据，必须 fail-closed |

当前官方接口登记：`POST /authentication`、`GET /authentication`、`data_sets`、`data_fields`、`OPTIONS /simulations`、`POST /simulations`、已知 progress URL、`users/self/alphas`、`GET /alphas/{id}`、`GET /alphas/{id}/recordsets`、`GET /alphas/{id}/recordsets/{name}` 和 `GET /users/{userid}/activities/diversity`。本轮只把这些官方接口作为生产协议事实；`/operators` 等仍按现有 provenance 处理。

成功的 `POST /simulations` response 中，`X-Ratelimit-Limit`、`X-Ratelimit-Remaining` 和 `X-Ratelimit-Reset` 是 BRAIN 提供的官方 Simulation quota observation；Multi response 中每个 child 分别计入配额，重复提交一个已经存在的 Alpha 也仍然计数。`/users/self/alphas` 的 Alpha 创建日期视图可能按 `alpha_id` 去重，不能作为精确 Simulation counter。当前 client 只在内存保留最近一次成功 response 的 bounded header projection；未观察到这些 headers 时，官方配额必须保持 `UNKNOWN`。

`X-Ratelimit-Reset` 只投影为有界的原始数值 `reset` header 值；无独立官方证据时，不推断其时间单位或时间基准。

每个被接受的 Simulation POST response 独立替换上一份内存中的 header projection；不同 response 的配额字段不合并。

`RemoteAlphaRepository` 行加上未解决 `ExecutionGuard` 只能形成 `APPROXIMATE` 的本地用量估算，不能覆盖 BRAIN 的官方剩余额度，也不创建配额历史、ledger 或数据库。回退估算的日期边界与远端元数据一致使用 `America/New_York`；旧的 `today_used`、`today_remaining` 等兼容字段必须明确标为估算值。

本地 `APPROXIMATE` 估算的观察窗口来自 `remote_cache.retention_days` / `RemoteAlphaRepository.retention_days`，不代表 BRAIN 官方配额周期。

`RemoteAlphaRepository` 缓存只按配置的保留窗口保存可重建的轻量 Alpha 元数据，不使用本地配额上限裁剪元数据行；这些行仍只是 `APPROXIMATE` 用量证据，不是精确 Simulation 计数。

当前顾问阶段的本地每日 Simulation 策略上限为 5000，默认也是 5000；配置只允许下调，不允许高于 5000。它只作用于 `APPROXIMATE` 本地配额投影，不覆盖 BRAIN 响应头，也不代表平台固定配额。

当前已确认的本地配额策略只有每日上限 5000；`RemoteAlphaRepository` 的配置保留窗口只产生 `APPROXIMATE` `window_used` 观察值，不代表存在滚动上限或滚动剩余额度，也不得从每日 5000 推导 7 日 35000。

`GET /authentication` 只投影当前 live 会话的 `authenticated`、`user_id`、`token_expiry` 和 `permissions`，不保存 JWT、cookie 或权限状态。`MULTI_SIMULATION` 权限是 Multi-Simulation 的账户能力前置条件；没有该权限时，客户端不得用 POST 探测或静默退化为大量 Single。

`OPTIONS /simulations` 的 `actions.POST` 是 Simulation settings 的平台事实源。客户端只投影 Simulation 类型选项、实际使用的 settings 允许取值/类型和必填字段；未知 vendor 字段忽略。当前 writer 的支持类型就是已完整建模并验证的 `REGULAR` 与 `REGION_AGNOSTIC` 白名单；平台广告本身不是写入契约，`SUPER` 仍不在其中。SUPER 只有在独立 `combo`/`selection` 请求 schema 实现并验证后才能进入生产 writer。`MULTI` 不是 Simulation 类型，而是由 `MULTI_SIMULATION` 权限授权的派发模式；当前 Multi child 仍只使用 REGULAR schema。OPTIONS 不可用时只能返回 `UNKNOWN`，并以最低本地 shape 校验继续提供 `LOCAL_ONLY` 结果，不能宣称 live 校验已通过。

Simulation mode availability 的阻塞原因按 `authentication → platform capability → Multi-Simulation permission` 投影；该只读投影不改变 Simulation 写入契约。

Multi-Simulation payload 必须包含 2–10 个 child；单个余数由 `SimulationGateway` 走 Single Simulation。Multi child 数量是 payload contract，不是并发建议。`WAITING`/`SIMULATING` 是 pending，`COMPLETE`/带 alpha 的 `WARNING` 是成功，`CANCELLED`/`ERROR`/`TIMEOUT`/`FAIL` 是远端已知终态；未知 status 返回 `UNKNOWN_REMOTE_STATUS` 并 fail safe。远端 `TIMEOUT` 不等同于本地 polling deadline，后者只保留 known progress URL 做同任务只读对账。

`research_api.simulate_multi_batch()` 保留原有的 `list[child_result]` 形状，但每个实际 Multi child 结果都附带 bounded `parent` projection：parent fingerprint、最终状态、progress URL、child 数量、exception class、failure kind、HTTP status（若 transport 明确知道）、remote status/diagnostic、bounded status path 和 guard action。该 projection 只存在于当前返回值，不写入 `ExecutionGuard`；`SUBMIT_UNKNOWN` 与已知 URL 的 `UNKNOWN` 仍分别保留原有 exactly-once 和同 URL 对账语义。

Multi response 中每个 child 分别计入配额；因此 unresolved Multi parent 的 local `APPROXIMATE` estimate 按其实际 child Simulation 数量投影，一个 parent guard 不等于一次 Simulation。ExecutionGuard 只增加 bounded `simulation_count` 整数，不保存 child payload、expression、settings、结果或 child ID；缺失或非法的旧 metadata 按 1 fail closed，不能把 estimate 提升为 official quota。

未解决 guard 的近似每日/窗口贡献按持久 `created_at` 的 `America/New_York` 本地日投影；这是本地 POST 前时间代理，不是官方 BRAIN Simulation 时间戳。估算可分别暴露所有 active guard 的加权计数、当日/窗口 guard 贡献和未知时间贡献；`updated_at` 只表示 guard 状态维护，不得让同一未解决写入跨日漂移。缺失或非法时间戳保持保守的未知时间计入，且不改变 guard 的 exactly-once 生命周期；这些字段仍只形成 `APPROXIMATE` 估算，不能覆盖官方响应头。

Simulation 的 `ERROR`/`FAIL` 诊断只保留有界的远端 status、message、property、line、start、end 和 simulation id，不保存完整私有表达式。Multi parent 完成后仍逐个确认 child；child 的完成、远端失败和远端超时按 child 保存，不能把部分完成压成整批 `UNKNOWN`。

Alpha detail 是完成后的廉价证据。`get_alpha_aggregates`、`get_alpha_pnl` 和 `get_alpha_self_correlation` 是按需的命名只读 facade，不会隐式拉取其他深度证据。recordset 先经官方 list 端点发现，再按 AI 明确选择的名称读取；同一次证据收集共享内存中的发现快照，不建立持久能力缓存。recordset 解码器保留官方数值，不重新计算指标，也不把数据解释为优胜者/多元化判断。activity diversity 是账户覆盖诊断，只读，不参与自动选 Alpha、模板、字段排序或预算。

`GET /users/self/alphas` 仅用于有界只读同步，按配置的 retention window 分页读取用户 Alpha 的 `id`、`status`、`dateCreated` 和 `dateSubmitted`；已提交 Alpha 与模拟 Alpha 使用同一次刷新触发，并按 `America/New_York` 本地日分桶。`APPROXIMATE` Simulation 用量估算按 Alpha 的 `dateCreated` 归属日期；`dateSubmitted` 只表示 Alpha 提交生命周期，不作为 Simulation 用量日。平台单窗口超过 1000 条时自动切分时间窗口；只写入轻量元数据缓存，不写入结果侧车。

Agent 使用 `list_datasets()`、`list_datafields()` 和有页数上限的
`list_all_datafields()` 读取原始平台列表并自主选择字段。字段 capability 校验
使用所选 BRAIN dataset 的 live `data_fields` 页面，保留 ID 与 dataset provenance；
静态字段表、缓存或字段名猜测都不能替代 live response。没有 dataset provenance、
能力 reader 缺失或分页未完整时保持 `CAPABILITY_UNAVAILABLE`，不提交 Simulation。
平台 `alphaCount` 是一个字段元数据，不是字段优先级或经济价值排序信号。

字段校验的页预算按已观测的最大 dataset 设定（1748 个 datafield 约 35 页，默认预算 40 页），因此位于平台分页顺序靠后的真实字段仍可被验证；预算耗尽时保持 fail closed，不会把"没找到"当成"字段不存在"。每个 Simulation 结果都带 `field_validation`：`LIVE_VERIFIED` 表示声明的字段已按 live dataset 校验，`UNVERIFIED` 表示表达式含字段但没有任何 capability read 覆盖它，`NOT_REQUESTED` 表示表达式不含字段。未声明 `fields` 的提交不会显示为已验证。

仅观察登记：`operators`、`alpha_check`、`pnl`。这些接口没有被生产 client 自动调用；只有 capability probe 或脱敏 fixture 可以证明其当前可用性。

Retry-After 支持秒数和 HTTP-date，统一由 `retry_after_seconds()` 解析，并拒绝负数、非有限值和畸形值。429 仍受全局 gate 与预算约束，Simulation POST 的未知结果仍进入 `SUBMIT_UNKNOWN`。

## 脱敏 fixtures

`tests/fixtures/brain/` 只包含结构样例，不包含真实账号、alpha、token、字段目录或平台数据。fixture 验证只能产生 `FIXTURE_VERIFIED`，不能替代 live response。
