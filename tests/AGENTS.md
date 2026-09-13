# `tests/` 局部规则

测试保护机制不变量和可观察行为。使用临时目录、fake client、固定时钟和最小 fixture；绝不连接真实 BRAIN 或消耗真实 Simulation 预算。

facade 测试应验证 `research_api.py` 组合现有 discovery、执行、证据和 reconciliation 路径，而不是复制它们。只保护历史架构形状的测试可以删除；核心安全不变量必须迁移并保留。

代码变更必须运行直接相关的定向测试；禁止运行全仓测试：

```powershell
python scripts/run_targeted_tests.py --files <changed-files>
python -m py_compile <changed-python-files>
```

不得使用 `unittest discover`、无选择器的 `pytest` 或 coverage 驱动的全量测试。测试映射缺失时先补充 `scripts/run_targeted_tests.py` 的显式映射。
