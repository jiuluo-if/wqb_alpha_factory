# Testing

## Principles

测试验证当前可观察行为和安全不变量。日常维护与 CI 都只执行与变更直接相关的定向测试；禁止用全仓回归掩盖测试映射缺失。

## Local Fast Lane

审查 diff 后，识别直接受影响的 behavior，运行 1–5 个相关 test method/class 和一个最近邻 regression；对 changed Python files 运行 `py_compile` 和 Ruff。只有 typed frontier 被改动时才运行对应 mypy。普通本地修改默认不跑 whole suite。

## Progressive Expansion

失败或涉及多个 owner、shared helper、proposal/schema、state merge semantics 或 safety contract 时，依次扩大到 nearby subsystem，再到相关 module/contract suite。改动 safety contract 必须有对应行为测试；这仍不是默认 whole-repository regression。

## CI Targeted Gate

GitHub CI 使用 Python 3.11，执行必要的静态/离线检查，并根据 base SHA 选择与变更文件直接相关的测试：

```powershell
python scripts/run_targeted_tests.py --base-sha <CI base SHA>
```

`scripts/run_targeted_tests.py` 只接受显式文件映射，绝不调用 `unittest discover`。未映射的代码变更直接 fail-closed；文档-only 变更可以没有 unit test。CI 禁止 `coverage run -m unittest`、无选择器的 `pytest` 和其他全仓测试等价物。

## Typed Frontier

typed frontier 是配置、运行时装配、凭据、Suggestion/Alpha Feed/Optimizer/Alpha Color 的九个模块。未触碰这些文件时，`MYPY = NOT_REQUIRED`；不扩大 mypy strict scope。

## Coverage

覆盖率不是 CI 的全仓测试入口。若未来需要覆盖率，必须对明确选定的相关测试模块运行，并单独说明范围；不得通过 coverage 恢复全量测试。安全关键 production 模块不得通过 omit 排除。

## Slow / Benchmark Tests

pytest、pytest-xdist 和 benchmark 工具可用于开发或性能实验。pytest targeted selection（如 `pytest path/to/test.py::TestClass::test_method`、`pytest -k ...`）允许用于局部验证；whole-suite pytest 不属于默认本地流程。testmon 未作为本轮依赖采用。

## Adding Tests

新增测试应覆盖当前 behavior/contract，并放在最近的职责边界；优先复用既有 helper，避免恢复历史单体测试文件或引入第二套 runner 契约。

## Commands

```powershell
# Local fast lane (replace placeholders with actual tests/files)
python -m unittest tests.test_<affected>.<TestClass>.<test_method>
python -m py_compile <changed-python-files>
python -m ruff check <changed-python-files>

# CI targeted lane
python -m mypy wqb_agent/config.py wqb_agent/runtime_policy.py wqb_agent/runtime_components.py wqb_agent/runtime_composition.py wqb_agent/credentials.py wqb_agent/suggestion_workflow.py wqb_agent/alpha_feed_workflow.py wqb_agent/optimizer_workflow.py wqb_agent/alpha_color_workflow.py
python -m ruff check .
python scripts/run_targeted_tests.py --base-sha <CI base SHA>
python main.py --state-dir tests/fixtures state doctor
python main.py --state-dir tests/fixtures state audit
python scripts/check_repo_privacy.py
```
