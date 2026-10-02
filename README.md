# WQB Alpha Factory

面向 AI Research Agent 的 WorldQuant BRAIN 工具。AI 根据 BRAIN 的实时证据选择研究问题和下一份 `SimulationSpec`；Python 提供平台访问与执行安全。

## 架构

```text
AI → wqb_agent.research_api → SimulationGateway → Simulator → WQBClient → BRAIN
```

`wqb_agent.research_api` 是唯一公开研究接口。默认阅读路径为本文件 → [`AGENTS.md`](AGENTS.md) → [`wqb_agent/research_api.py`](wqb_agent/research_api.py)。跨组件架构问题再按需阅读 [`docs/ARCHITECTURE_AGENT.md`](docs/ARCHITECTURE_AGENT.md)。

## 能力

- 查询 datasets、datafields、operators 和实时执行能力。
- 显式构造并验证 Simulation 请求，经唯一 Gateway 写入 BRAIN。
- 读取 BRAIN Alpha evidence；本地缓存是可重建视图，不是研究结果事实源。
- 默认 Agent 工具清单使用 CORE profile；按需能力见 [`research_api.py`](wqb_agent/research_api.py) 与 [`docs/README.md`](docs/README.md)。

## 使用与验证

配置格式见 [`config.example.json`](config.example.json)。安装可选只读 MCP 依赖后，可按 [`docs/MCP_READ_ONLY.md`](docs/MCP_READ_ONLY.md) 启动只读工具。

```powershell
python main.py diagnostics doctor
python main.py diagnostics audit
python main.py datasets
python -m unittest discover -s tests
python -m ruff check .
python scripts/check_repo_privacy.py
```

测试和隐私规则见 [`docs/TESTING.md`](docs/TESTING.md) 与 [`docs/PRIVACY.md`](docs/PRIVACY.md)。研究方法见唯一核心 Skill [`skills/wqb-research/SKILL.md`](skills/wqb-research/SKILL.md)。
