# Testing contract

测试保护当前可观察行为、安全不变量、owner 边界和 public research_api contract。所有质量检查默认离线，不触发 live BRAIN、Simulation 或 Alpha submission。

## Local

1. 审查 diff，识别直接受影响的 behavior。
2. 运行 1–5 个直接测试与一个最近邻 regression。
3. 跨 owner、shared helper、proposal/schema、state merge 或 safety contract 时，使用 `scripts/run_targeted_tests.py --files ...` 的 expanded mapping。
4. 对 changed Python files 运行 `py_compile` 和 Ruff；只有 typed frontier 改动才运行对应 mypy。

新 production module 必须在 `scripts/run_targeted_tests.py` 增加直接 mapping；缺 mapping 必须 fail-closed，不能用未选择的测试掩盖缺口。

## CI final gate

GitHub Actions 对变更 base SHA 执行：

```text
python -m compileall -q wqb_agent scripts tests
typed frontier mypy
python -m ruff check .
python scripts/run_targeted_tests.py --base-sha <CI base SHA>
python -m unittest discover -s tests
python main.py --state-dir tests/fixtures diagnostics doctor --offline
python main.py --state-dir tests/fixtures diagnostics audit --offline
python scripts/check_repo_privacy.py
```

Targeted Fast Lane 与 whole-repository final gate 是不同层次：前者验证变更映射，后者确认全局回归。CI 的全量 gate 不替代 targeted mapping，也不授权使用 raw state 或网络写入。

## Contract focus

必须持续覆盖：唯一 Simulation 写链、ExecutionGuard exactly-once、`SUBMIT_UNKNOWN` 不重 POST、known progress URL 只读轮询、远端 evidence 缺失保持 UNKNOWN、live/cache 优先级、结构分组与显式颜色 plan、颜色 PATCH readback、stale plan 不写入、facade authorization 与 privacy。

颜色 CI 使用 synthetic evidence 和 mock setter；质量字段只能作为文本状态，不能自动产生颜色。每个生产 Python 模块必须在 `scripts/run_targeted_tests.py` 的 `DIRECT_TESTS` 中有显式路由；映射不存在或过期都 fail closed。
