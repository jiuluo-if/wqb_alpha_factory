# 静态语法参考 — 非可用性事实

本文件只提供 evergreen FASTEXPR 语法名称、签名和一般说明。
它不描述当前账号能力、平台可用性、研究使用历史或研究结果；当前能力必须由
`WQBClient` 通过 `/operators` 的合法 live GET 响应证明。

## 算术

`abs(x)` — 绝对值。
`add(x, y, filter=false)` — 逐元素相加；`filter=true` 把 NaN 当作零。
`densify(x)` — 把多桶分组字段压缩到可用桶。
`divide(x, y)` — x 除以 y。
`inverse(x)` — x 的倒数。
`log(x)` — 自然对数。
`max(x, y, ...)` — 至少两个输入的取最大。
`min(x, y, ...)` — 至少两个输入的取最小。
`multiply(x, y, ..., filter=false)` — 逐元素相乘。
`power(x, y)` — x 的 y 次幂；需保留符号时用 `signed_power`。
`reverse(x)` — x 的负值。
`sigmoid(x)` — logistic sigmoid。
`sign(x)` — 数的符号，保留 NaN。
`signed_power(x, y)` — 保留 x 符号的幂运算。
`sqrt(x)` — 非负平方根。
`subtract(x, y, filter=false)` — 输入从左到右依次相减。
`tanh(x)` — 双曲正切。

## 逻辑

`and(input1, input2)` — 两个输入都为真时真。
`if_else(input1, input2, input3)` — 条件为真返回第二个输入，否则返回第三个。
`is_nan(input)` — 输入为 NaN 时返回一，否则返回零。
`not(x)` — 逻辑非。
`or(input1, input2)` — 任一输入为真时真。

比较形式 `input1 < input2`、`input1 <= input2`、`input1 == input2`、`input1 > input2`、`input1 >= input2`、`input1 != input2` 返回一或零。

## 时序

`days_from_last_change(x)` — 距上次变化的天数。
`hump(x, hump=0.01)` — 限制变化幅度以降低换手。
`kth_element(x, d, k, ignore="NaN")` — 返回 d 日窗口内第 k 个值。
`last_diff_value(x, d)` — 窗口内最近一个与当前值不同的值。
`ts_arg_max(x, d)` — 距最近 d 日最大值的天数。
`ts_arg_min(x, d)` — 距最近 d 日最小值的天数。
`ts_av_diff(x, d)` — x 减去其忽略 NaN 的滚动均值。
`ts_backfill(x, lookback=d, k=1)` — 用近期有效观察值回填缺失值。
`ts_corr(x, y, d)` — 滚动 Pearson 相关。
`ts_count_nans(x, d)` — 最近 d 日 NaN 值个数。
`ts_covariance(y, x, d)` — 滚动协方差。
`ts_decay_linear(x, d, dense=false)` — 对近期时序值线性衰减。
`ts_delay(x, d)` — d 日前的值。
`ts_delta(x, d)` — 与延迟值的差。
`ts_entropy(x, d)` — 最近 d 日基于直方图的信息熵。
`ts_mean(x, d)` — 滚动算术均值。
`ts_min_diff(x, d)` — x 减去滚动最小值。
`ts_min_max_cps(x, d, f=2)` — 滚动最小值加最大值减去 f 倍 x。
`ts_min_max_diff(x, d, f=0.5)` — x 减去 f 倍滚动最小值与最大值之和。
`ts_product(x, d)` — 滚动乘积。
`ts_quantile(x, d, driver="gaussian")` — 用分布分位数函数变换滚动秩。
`ts_rank(x, d, constant=0)` — 当前值的滚动秩。
`ts_regression(y, x, d, lag=0, rettype=0)` — 返回选定的滚动回归参数。
`ts_scale(x, d, constant=0)` — 把值缩放到滚动 0-1 区间。
`ts_skewness(x, d)` — 滚动偏度。
`ts_std_dev(x, d)` — 滚动标准差。
`ts_step(1)` — 每日递增计数器。
`ts_sum(x, d)` — 滚动求和。
`ts_target_tvr_decay(x, lambda_min=0, lambda_max=1, target_tvr=0.1)` — 朝目标换手率调节衰减。
`ts_zscore(x, d)` — 滚动 z-score。

## 横截面

`normalize(x, useStd=false, limit=0.0)` — 对每日横截面中心化，可选标准化并截断。
`quantile(x, driver=gaussian, sigma=1.0)` — 用选定分布变换横截面秩。
`rank(x, rate=2)` — 横截面秩缩放到 0 到 1 之间。
`regression_proj(y, x)` — 横截面回归投影。
`scale(x, scale=1, longscale=1, shortscale=1)` — 把横截面缩放到请求的 book 规模。
`vector_neut(x, y)` — 去掉 x 中与 y 对齐的分量。
`vector_proj(x, y)` — 把 x 投影到 y 上。
`winsorize(x, std=4)` — 按可配置的标准差倍数限制取值。
`zscore(x)` — 横截面 z-score。

## 向量

`vec_avg(x)` — 向量元素均值。
`vec_count(x)` — 向量元素个数。
`vec_max(x)` — 向量元素最大。
`vec_min(x)` — 向量元素最小。
`vec_range(x)` — 向量元素最大减最小。
`vec_stddev(x)` — 向量元素标准差。
`vec_sum(x)` — 向量元素求和。

## 变换

`bucket(rank(x), range="0, 1, 0.1", skipBoth=false, NaNGroup=false)` — 创建自定义秩桶。
`trade_when(x, y, z)` — 按入场、持有与离场条件更新、持有或平掉 Alpha。

## 分组

`group_backfill(x, group, d, std=4.0)` — 用 d 日 winsorize 后的组均值回填缺失值。
`group_cartesian_product(g1, g2)` — 按笛卡尔积组合两个分组字段。
`group_extra(x, weight, group)` — 用对应组均值替换 NaN 值。
`group_mean(x, weight, group)` — 计算组调和均值。
`group_neutralize(x, group)` — 从各组取值中减去组均值。
`group_rank(x, group)` — 在各组内排名。
`group_scale(x, group)` — 把各组内取值缩放到 0 到 1。
`group_zscore(x, group)` — 计算各组内 z-score。

## 特殊

`inst_pnl(x)` — 生成逐标的 PnL；使用视为采用 pv1 数据集。

## 一般安全说明

- 语法提示不是 capability allow-list；静态加载结果固定为
  `source=STATIC_SYNTAX_REFERENCE`、`availability=UNKNOWN`。
- VECTOR/MATRIX 类型、arity、命名参数和 region 差异必须在 live response 与当前
  discovery/schema 允许时确认；不得从本表推断当前可用性。
- 不把 operator 名称、语法示例或 coverage 变成随机搜索空间；研究模板仍须由
  `wqb_agent.alpha_templates` 的机制与语义约束负责。
