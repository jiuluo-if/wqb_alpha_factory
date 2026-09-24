# 测试契约

测试保护当前可观察行为、安全不变量、负责方边界和公开 `research_api` 契约。所有质量检查默认离线，不触发线上 BRAIN、Simulation 或 Alpha 提交。

## 本地

1. 审查 diff，识别直接受影响的行为。
2. 运行 1–5 个直接测试与一个最近邻回归。
3. 跨负责方、共享 helper、proposal/schema、状态合并或安全契约时，使用 `scripts/run_targeted_tests.py --files ...` 的扩展映射。
4. 对改动的 Python 文件运行 `py_compile` 和 Ruff；只有 typed frontier 改动才运行对应 mypy。

新生产模块必须在 `scripts/run_targeted_tests.py` 增加直接映射；缺映射必须 fail-closed，不能用未选择的测试掩盖缺口。

## CI 最终门

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

定向快速通道（Targeted Fast Lane）与全仓最终门（whole-repository final gate）是不同层次：前者验证变更映射，后者确认全局回归。CI 的全量门不替代定向映射，也不授权使用原始状态或网络写入。

## 契约重点

必须持续覆盖：唯一 Simulation 写链、ExecutionGuard 的 exactly-once、`SUBMIT_UNKNOWN` 不重 POST、已知 progress URL 只读轮询、远端证据缺失保持 UNKNOWN、live/缓存优先级、execution/strict structural/variant-family 三层 identity、显式 family 颜色 plan、颜色 PATCH 回读、过期 plan 不写入、facade 授权与隐私。

颜色和 family CI 使用 synthetic 证据与 mock setter；质量字段只能作为文本状态，不能自动产生颜色。`family_member_count` 只代表当前证据快照/窗口的可见成员数，`observed_execution_count` 只代表其中不同执行指纹的可观察下界，二者都不代表完整试验计数。每个生产 Python 模块必须在 `scripts/run_targeted_tests.py` 的 `DIRECT_TESTS` 中有显式路由；映射不存在或过期都 fail closed。
