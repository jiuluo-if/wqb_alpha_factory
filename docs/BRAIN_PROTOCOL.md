# BRAIN Protocol Truth Layer

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

`GET /authentication` 只投影当前 live session 的 `authenticated`、`user_id`、`token_expiry` 和 `permissions`，不保存 JWT、cookie 或权限状态。`MULTI_SIMULATION` permission 是 Multi-Simulation 的账户能力前置条件；没有该 permission 时，客户端不得用 POST 探测或静默退化为大量 Single。

`OPTIONS /simulations` 的 `actions.POST` 是 Simulation settings 的平台 truth。客户端只投影 Simulation type choices、实际使用的 settings allowed values/type 和 required fields；未知 vendor 字段忽略。当前 child POST 使用的 Simulation type 是 `REGULAR`（平台还可能声明 `SUPER`）；`MULTI` 不是 Simulation type，而是由 `MULTI_SIMULATION` permission 授权的 dispatch mode。OPTIONS 不可用时只能返回 `UNKNOWN`，并以最低本地 shape 校验继续提供 `LOCAL_ONLY` 结果，不能宣称 live validation 已通过。

Multi-Simulation payload 必须包含 2--10 个 child；单个余数由 `SimulationGateway` 走 Single Simulation。Multi child 数量是 payload contract，不是并发建议。`WAITING`/`SIMULATING` 是 pending，`COMPLETE`/带 alpha 的 `WARNING` 是成功，`CANCELLED`/`ERROR`/`TIMEOUT`/`FAIL` 是远端已知终态；未知 status 返回 `UNKNOWN_REMOTE_STATUS` 并 fail safe。远端 `TIMEOUT` 不等同于本地 polling deadline，后者只保留 known progress URL 做同任务只读对账。

`research_api.simulate_multi_batch()` 保留原有的 `list[child_result]` 形状，但每个实际 Multi child 结果都附带 bounded `parent` projection：parent fingerprint、最终状态、progress URL、child 数量、exception class、failure kind、HTTP status（若 transport 明确知道）、remote status/diagnostic、bounded status path 和 guard action。该 projection 只存在于当前返回值，不写入 `ExecutionGuard`；`SUBMIT_UNKNOWN` 与已知 URL 的 `UNKNOWN` 仍分别保留原有 exactly-once 和同 URL 对账语义。

Simulation 的 `ERROR`/`FAIL` 诊断只保留 bounded 的 remote status、message、property、line、start、end 和 simulation id，不保存完整 private expression。Multi parent 完成后仍逐个确认 child；child 的完成、远端失败和远端超时按 child 保存，不能把 partial completion 压成整个 batch UNKNOWN。

Alpha detail 是完成后的 cheap evidence。`get_alpha_aggregates`、`get_alpha_pnl` 和 `get_alpha_self_correlation` 是按需的 named read-only facade，不会隐式拉取其他 deep evidence。recordset 先通过官方 list endpoint discovery，再按 AI 明确选择的名称读取；同一次 evidence collection 共享内存 discovery snapshot，不建立持久 capability cache。recordset decoder 保留官方数值，不重新计算指标或把数据解释为 winner/diversification judgment。activity diversity 是账户覆盖诊断，只读，不参与自动选 Alpha、模板、字段 ranking 或预算。

`GET /users/self/alphas` 仅用于有界只读同步，按日期窗口分页读取用户 Alpha 的 `id`、`status`、`dateCreated` 和 `dateSubmitted`；提交 Alpha 与模拟 Alpha 使用同一次刷新触发，按当前工作日前推 7 个自然日及 `America/New_York` 本地日分桶。平台单窗口超过 1000 条时自动切分时间窗口；只写入轻量元数据缓存，不写入结果侧车。

字段查重使用 `data_fields` 响应中的平台 `alphaCount`（兼容内部标准化键
`alpha_count`）。查重键必须是 `(dataset_id, field_id)`，不能只用字段名；它表示字段在平台现有 Alpha 中的使用量，是本地不保留
Simulation/Alpha 结果时的唯一字段使用事实源。字段发现命中本地目录或
`fields_cache.json` 时，生产配置仍会对候选数据集发起只读刷新；刷新失败或
缺少 `alphaCount` 保持 `UNKNOWN`，严格模式不进入工厂批次。该刷新不保存
Simulation 结果、Alpha payload 或提交历史。

字段目录按 `America/New_York` 本地日固化为
`platform_field_catalog_YYYYMMDD/manifest.json` 加数据集字段文件。manifest
记录查询范围、抓取时间、字段数量、字段哈希和平台使用量状态；目录只包含
平台字段元数据，不包含 Simulation/Alpha 结果。多数据集发现使用可复现种子做
分层轮询，优先保证配置的 `min_datasets` 覆盖，再按字段评分和随机扰动取样。
当前选择必须在 discovery bundle 中暴露数据集池、顺序、选中数量和拒绝原因。

仅观察登记：`operators`、`alpha_check`、`pnl`。这些接口没有被生产 client 自动调用；只有 capability probe 或脱敏 fixture 可以证明其当前可用性。

Retry-After 支持秒数和 HTTP-date，统一由 `retry_after_seconds()` 解析，并拒绝负数、非有限值和畸形值。429 仍受全局 gate 与预算约束，Simulation POST 的未知结果仍进入 `SUBMIT_UNKNOWN`。

## 脱敏 fixtures

`tests/fixtures/brain/` 只包含结构样例，不包含真实账号、alpha、token、字段目录或平台数据。fixture 验证只能产生 `FIXTURE_VERIFIED`，不能替代 live response。
