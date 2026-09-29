# 运维与离线验证脚本

生产模拟不在本目录执行；Research Agent 使用 `wqb_agent.research_api` 暴露的 canonical facade/MCP。脚本只支持隐私检查、测试路由和可重复的离线行为验证，不创建第二套研究 API。

| 脚本 | 用途 | 边界 |
|---|---|---|
| `check_repo_privacy.py` | research-data 隐私回归 | 只扫 `git ls-files`，不扫本机 tmp 或整块磁盘 |
| `run_targeted_tests.py` | 按变更文件或 base SHA 选择直接相关的离线测试 | 未登记的生产模块 fail closed |
| `run_research_agent_eval.py` | 解析/核验合成 Research Agent JSONL traces，并通过 fake MCP 检查工具顺序、写入边界与 evidence | 默认只验证 fixtures；`--run-agent` 是 opt-in，使用 fake MCP，不连接 BRAIN |
| `run_cold_start_eval.py` | 复用 Research Agent eval 的 parser/fake server，测量 synthetic cold-start/progressive-disclosure 的有用工具、上下文与响应体大小 | 不新增 evaluator/state；不连接 BRAIN |

两个 eval runner 有不同且已测试的用途：主 runner 拥有 trace/fake-tool 解析和行为判定；cold-start runner 重用这些 owner，只计算启动路径指标。保持二者分开可让普通 safety eval 不必承担 cold-start 数据收集逻辑。

测试选择与质量门见 [`docs/TESTING.md`](../docs/TESTING.md)。项目级离线诊断见根目录 [`README.md`](../README.md)。
