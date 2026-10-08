# Simulation 设置参考

`SimulationSpec` 的 `settings` 是 BRAIN Simulation 的有效设置输入。Gateway 会规范化并校验 schema，execution fingerprint 使用规范化后的完整 settings。

示例：

```python
SimulationSpec(
    expression="rank(close)",
    settings={"delay": 1, "decay": 4, "neutralization": "SUBINDUSTRY"},
)
```

Gateway 会使用规范化后的完整 settings 计算 execution fingerprint；去重与写入安全规则见根目录 [`AGENTS.md`](../../AGENTS.md)。设置本身不代表机制结论，研究解释由 AI 根据 BRAIN 返回结果完成。

## Simulation 模式

- `simulate()` / `simulate_batch()`：分别执行一个或一组独立 Single Simulation；批次默认最多 10 个并发。
- `simulate_multi_batch()`：Multi dispatch 入口；每个 parent 含 2–10 个 child（默认 10），writer 的 parent 并发上限为 8。未解决的 parent guard 会减少当前可用槽位；准入状态由根目录 [`AGENTS.md`](../../AGENTS.md) 定义。一个 parent 内的 child 必须共享 region 和 delay。
- `REGION_AGNOSTIC`：当前 writer 实现的独立 Simulation 类型，SimulationSpec 使用 `region="ALL"`。字段能力按子地区核验，细节见根目录 [`AGENTS.md`](../../AGENTS.md)；它不会悄悄降级为 Multi-Simulation。
