# Simulation Settings

`SimulationSpec` 的 `settings` 是 BRAIN Simulation 的有效设置输入。Gateway 会规范化并校验 schema，execution fingerprint 使用规范化后的完整 settings。

示例：

```python
SimulationSpec(
    expression="rank(close)",
    settings={"delay": 1, "decay": 4, "neutralization": "SUBINDUSTRY"},
)
```

设置变化会形成不同的 execution fingerprint；相同 expression 与相同有效 settings 的 active execution 不得重复 POST。设置本身不代表机制结论，研究解释由 AI 根据 BRAIN 返回结果完成。
