# STATIC SYNTAX REFERENCE — NOT AVAILABILITY TRUTH

本文件只提供 evergreen FASTEXPR 语法名称、签名和一般说明。
它不描述当前账号能力、平台可用性、研究使用历史或研究结果；当前能力必须由
`WQBClient` 通过 `/operators` 的合法 live GET 响应证明。

## Arithmetic

`abs(x)` — absolute value。
`add(x, y, filter=false)` — element-wise addition; `filter=true` treats NaN as zero。
`densify(x)` — compresses a many-bucket grouping field to available buckets。
`divide(x, y)` — x divided by y。
`inverse(x)` — reciprocal of x。
`log(x)` — natural logarithm。
`max(x, y, ...)` — maximum of at least two inputs。
`min(x, y, ...)` — minimum of at least two inputs。
`multiply(x, y, ..., filter=false)` — element-wise multiplication。
`power(x, y)` — x raised to y; use `signed_power` when the sign must be preserved。
`reverse(x)` — negative x。
`sigmoid(x)` — logistic sigmoid。
`sign(x)` — sign of a number, preserving NaN。
`signed_power(x, y)` — power operation that preserves the sign of x。
`sqrt(x)` — non-negative square root。
`subtract(x, y, filter=false)` — subtracts inputs left to right。
`tanh(x)` — hyperbolic tangent。

## Logical

`and(input1, input2)` — true when both inputs are true。
`if_else(input1, input2, input3)` — returns the second input when the condition is true, otherwise the third。
`is_nan(input)` — returns one when the input is NaN, otherwise zero。
`not(x)` — logical negation。
`or(input1, input2)` — true when either input is true。

The comparison forms `input1 < input2`, `input1 <= input2`, `input1 == input2`,
`input1 > input2`, `input1 >= input2`, and `input1 != input2` return one or zero.

## Time Series

`days_from_last_change(x)` — days since the last change。
`hump(x, hump=0.01)` — limits the magnitude of changes to reduce turnover。
`kth_element(x, d, k, ignore="NaN")` — returns the k-th value within a d-day window。
`last_diff_value(x, d)` — most recent value in the window different from the current value。
`ts_arg_max(x, d)` — days since the maximum in the last d days。
`ts_arg_min(x, d)` — days since the minimum in the last d days。
`ts_av_diff(x, d)` — x minus its NaN-ignored rolling mean。
`ts_backfill(x, lookback=d, k=1)` — fills missing values from recent valid observations。
`ts_corr(x, y, d)` — rolling Pearson correlation。
`ts_count_nans(x, d)` — count of NaN values in the last d days。
`ts_covariance(y, x, d)` — rolling covariance。
`ts_decay_linear(x, d, dense=false)` — linearly decays recent time-series values。
`ts_delay(x, d)` — value from d days ago。
`ts_delta(x, d)` — difference from the delayed value。
`ts_entropy(x, d)` — histogram-based information entropy over the last d days。
`ts_mean(x, d)` — rolling arithmetic mean。
`ts_min_diff(x, d)` — x minus the rolling minimum。
`ts_min_max_cps(x, d, f=2)` — rolling minimum plus maximum minus f times x。
`ts_min_max_diff(x, d, f=0.5)` — x minus f times the rolling minimum plus maximum。
`ts_product(x, d)` — rolling product。
`ts_quantile(x, d, driver="gaussian")` — transforms rolling rank with a distribution quantile function。
`ts_rank(x, d, constant=0)` — rolling rank of the current value。
`ts_regression(y, x, d, lag=0, rettype=0)` — returns a selected rolling regression parameter。
`ts_scale(x, d, constant=0)` — scales values to a rolling zero-to-one range。
`ts_skewness(x, d)` — rolling skewness。
`ts_std_dev(x, d)` — rolling standard deviation。
`ts_step(1)` — daily incrementing counter。
`ts_sum(x, d)` — rolling sum。
`ts_target_tvr_decay(x, lambda_min=0, lambda_max=1, target_tvr=0.1)` — tunes decay toward a target turnover。
`ts_zscore(x, d)` — rolling z-score。

## Cross Sectional

`normalize(x, useStd=false, limit=0.0)` — centers a daily cross section, optionally standardizes and clamps。
`quantile(x, driver=gaussian, sigma=1.0)` — transforms cross-sectional rank with a selected distribution。
`rank(x, rate=2)` — cross-sectional rank scaled between zero and one。
`regression_proj(y, x)` — cross-sectional regression projection。
`scale(x, scale=1, longscale=1, shortscale=1)` — scales the cross section to the requested book size。
`vector_neut(x, y)` — removes the component of x aligned with y。
`vector_proj(x, y)` — projects x onto y。
`winsorize(x, std=4)` — limits values by a configurable number of standard deviations。
`zscore(x)` — cross-sectional z-score。

## Vector

`vec_avg(x)` — mean of vector elements。
`vec_count(x)` — number of vector elements。
`vec_max(x)` — maximum vector element。
`vec_min(x)` — minimum vector element。
`vec_range(x)` — maximum minus minimum vector element。
`vec_stddev(x)` — standard deviation of vector elements。
`vec_sum(x)` — sum of vector elements。

## Transformational

`bucket(rank(x), range="0, 1, 0.1", skipBoth=false, NaNGroup=false)` — creates custom rank buckets。
`trade_when(x, y, z)` — updates, holds, or closes an Alpha based on entry, value, and exit conditions。

## Group

`group_backfill(x, group, d, std=4.0)` — fills missing values with a winsorized group mean over d days。
`group_cartesian_product(g1, g2)` — combines two grouping fields by Cartesian product。
`group_extra(x, weight, group)` — replaces NaN values with corresponding group means。
`group_mean(x, weight, group)` — calculates the group harmonic mean。
`group_neutralize(x, group)` — subtracts each group's mean from its values。
`group_rank(x, group)` — ranks values within each group。
`group_scale(x, group)` — scales values within each group to zero through one。
`group_zscore(x, group)` — computes a z-score within each group。

## Special

`inst_pnl(x)` — generates per-instrument PnL; use is considered to utilize the pv1 dataset。

## General safety notes

- 语法提示不是 capability allow-list；静态加载结果固定为
  `source=STATIC_SYNTAX_REFERENCE`、`availability=UNKNOWN`。
- VECTOR/MATRIX 类型、arity、命名参数和 region 差异必须在 live response 与当前
  discovery/schema 允许时确认；不得从本表推断当前可用性。
- 不把 operator 名称、语法示例或 coverage 变成随机搜索空间；研究模板仍须由
  `wqb_agent.alpha_templates` 的机制与语义约束负责。
