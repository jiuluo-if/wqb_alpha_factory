# 批次设计

默认推理流程：

```text
观察
→ 多个竞争假设
→ 分组批次实验
→ BRAIN 结果
→ 比较假设家族
→ 扩展有前景方向
→ 证伪 / 稳健性
→ 停止或继续
```

每个批次记录：

1. 测试什么机制？
2. 哪些 proposal 是对照、本地兄弟、证伪或新探针？
3. 什么结果会改变下一次决策？

`proposal_id` 是临时结果的关联键；Agent 在写入前保留它与假设、`note`、`template_id` 的 mapping。Research MCP 对每项只投影 `proposal_id`、状态、reason code、fingerprint、Alpha ID 与字段校验结果；Agent 应从自己的 mapping 还原 note/template，不把这些解释信息写入 `ExecutionGuard` 或本地研究数据库。

字段从 `list_datasets()` 与 `list_datafields()`/`list_all_datafields()` 读取。自行选字段，并把每个字段 ID 与其 dataset provenance 传入 `SimulationSpec`。Gateway 的 live 校验是安全校验，不是经济适配度评分。

只构造区分解释所需的最少候选。大批次有用当且仅当其分组有不同解释；不得生成笛卡尔积或 Python 规划的搜索循环。
