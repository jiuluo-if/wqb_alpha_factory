# 批次设计

默认推理流程：

```text
Observation
→ several competing hypotheses
→ grouped batch experiments
→ BRAIN results
→ compare hypothesis families
→ expand promising directions
→ falsification / robustness
→ stop or continue
```

每个批次记录：

1. 测试什么机制？
2. 哪些 proposal 是对照、本地兄弟、证伪或新探针？
3. 什么结果会改变下一次决策？

`proposal_id` 是唯一的临时结果链接，`note` 写假设/分组解释，`template_id` 记模板出处。Gateway 随每个 child 结果返回这些标签；不存入 `ExecutionGuard` 或本地研究数据库。

字段从 `list_datasets()` 与 `list_datafields()`/`list_all_datafields()` 读取。自行选字段，并把每个字段 ID 与其 dataset provenance 传入 `SimulationSpec`。Gateway 的 live 校验是安全校验，不是经济适配度评分。

只构造区分解释所需的最少候选。大批次有用当且仅当其分组有不同解释；不得生成笛卡尔积或 Python 规划的搜索循环。
