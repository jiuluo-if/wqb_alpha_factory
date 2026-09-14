# STATIC SYNTAX REFERENCE — NOT AVAILABILITY TRUTH

本文件只提供 evergreen FASTEXPR 语法名称、示例和一般类型/arity 提示。
它不描述当前账号能力、平台可用性、研究使用历史或研究结果；当前能力必须由
`WQBClient` 通过 `/operators` 的合法 live GET 响应证明。

## Syntax hints

### Scalar and logical

`abs(x)`, `add(x,y)`, `subtract(x,y)`, `multiply(x,y)`, `divide(x,y)`,
`inverse(x)`, `log(x)`, `max(x,y)`, `min(x,y)`, `power(x,y)`,
`signed_power(x,y)`, `sqrt(x)`, `reverse(x)`, `sign(x)`, `densify(x)`,
`and(a,b)`, `or(a,b)`, `not(x)`, `if_else(cond,a,b)`, `is_nan(x)`。

### Time series

时间序列算子通常以第二个或命名参数接收正整数窗口；具体 arity 以当前 live
平台 schema 为准：

`days_from_last_change(x)`, `hump(x)`, `kth_element(x,d,k)`,
`last_diff_value(x,d)`, `ts_delay(x,d)`, `ts_delta(x,d)`, `ts_mean(x,d)`,
`ts_sum(x,d)`, `ts_product(x,d)`, `ts_std_dev(x,d)`, `ts_count_nans(x,d)`,
`ts_backfill(x,d)`, `ts_arg_max(x,d)`, `ts_arg_min(x,d)`, `ts_av_diff(x,d)`,
`ts_corr(x,y,d)`, `ts_covariance(y,x,d)`, `ts_regression(y,x,d)`,
`ts_rank(x,d)`, `ts_scale(x,d)`, `ts_zscore(x,d)`, `ts_quantile(x,d)`。

### Cross-sectional, vector and group

`rank(x)`, `zscore(x)`, `normalize(x)`, `quantile(x)`, `winsorize(x)`,
`scale(x)`, `vec_avg(x)`, `vec_sum(x)`, `bucket(x)`, `trade_when(x,y,z)`,
`group_neutralize(x,g)`, `group_rank(x,g)`, `group_zscore(x,g)`,
`group_scale(x,g)`, `group_backfill(x,g,d)`, `group_mean(x,w,g)`。

## General safety notes

- 语法提示不是 capability allow-list；静态加载结果固定为
  `source=STATIC_SYNTAX_REFERENCE`、`availability=UNKNOWN`。
- VECTOR/MATRIX 类型、arity、命名参数和 region 差异必须在 live response 与当前
  discovery/schema 允许时确认；不得从本表推断当前可用性。
- 不把 operator 名称、语法示例或 coverage 变成随机搜索空间；研究模板仍须由
  `wqb_agent.alpha_templates` 的机制与语义约束负责。
