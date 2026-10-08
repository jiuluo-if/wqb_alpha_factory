# Agent 架构地图

本文只标出组件职责与权威入口。架构、安全和执行约束由根目录 [`AGENTS.md`](../AGENTS.md) 维护；平台协议由 [`BRAIN_PROTOCOL.md`](BRAIN_PROTOCOL.md) 维护；研究方法由 [`skills/wqb-research/SKILL.md`](../skills/wqb-research/SKILL.md) 维护。

## 数据流

```text
Agent → research_api → SimulationGateway → Simulator → WQBClient → BRAIN
                              ↑                            ↓
                        ExecutionGuard          RemoteAlphaRepository
```

Agent 通过 `research_api` 读取平台证据并提交明确构造的 `SimulationSpec`。`SimulationGateway` 负责确定性校验和写入准入；`Simulator` 与 `WQBClient` 处理执行和传输。Alpha 与 Simulation evidence 以 BRAIN 为准，`RemoteAlphaRepository` 只维护可重建的远端 metadata 视图。上述安全和恢复规则以根目录 `AGENTS.md` 为准，不在此重复。

## 组件与 owner

| 组件 | 职责 | 入口 |
|---|---|---|
| Research facade | 唯一公开 Agent API，汇集 discovery、候选校验、Simulation 与 evidence 工具 | [`research_api.py`](../wqb_agent/research_api.py) |
| Simulation Gateway | Simulation 请求校验、准入和 dispatch | [`simulation_gateway.py`](../wqb_agent/simulation_gateway.py) |
| Simulator / client | Simulation 执行与 BRAIN transport | [`simulator.py`](../wqb_agent/simulator.py)、[`client.py`](../wqb_agent/client.py)、[`BRAIN_PROTOCOL.md`](BRAIN_PROTOCOL.md) |
| Templates | 模板模型、私有 catalog 和验证 | [`alpha_templates/AGENTS.md`](../wqb_agent/alpha_templates/AGENTS.md)、[`ALPHA_TEMPLATES.md`](ALPHA_TEMPLATES.md) |
| Remote evidence | Alpha/evidence 读取与可重建 metadata cache | [`remote_alpha_repository.py`](../wqb_agent/remote_alpha_repository.py)、[`STATE_LAYOUT.md`](STATE_LAYOUT.md) |
| MCP | facade 的 stdio transport；工具清单与连接状态按 MCP 文档核验 | [`MCP_READ_ONLY.md`](MCP_READ_ONLY.md) |
| Grouping / color metadata | family 分组、显式颜色预览计划和远端同步 | [`alpha_grouping.py`](../wqb_agent/alpha_grouping.py)、[`remote_colors.py`](../wqb_agent/remote_colors.py)、[`TESTING.md`](TESTING.md) |

`AlphaFactory` 只生成候选 `SimulationSpec`；它不拥有研究生命周期或结果状态。实现入口见 [`alpha_factory.py`](../wqb_agent/alpha_factory.py)。
