# 运维脚本

生产模拟不在本目录执行；请使用根目录的 `main.py` 或 `wqb_agent.research_api`。这里仅保留少量安全运维工具。

| 工具 | 用途 | 边界 |
|---|---|---|
| `check_repo_privacy.py` | research-data 隐私回归 | 只扫 `git ls-files`，不扫整块磁盘 |
| `run_targeted_tests.py` | 按变更文件或 base SHA 选择直接相关的离线测试 | 未登记的生产模块 fail closed |

测试选择与质量门见 [`docs/TESTING.md`](../docs/TESTING.md)。架构审计使用 stdlib AST，不读取真实研究 payload，也不触发远端写操作。

项目级只读诊断命令见根目录 [`README.md`](../README.md)。
