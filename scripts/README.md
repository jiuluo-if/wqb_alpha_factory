# 运维脚本

生产模拟不在本目录执行；请使用根目录的 `main.py` 或 `wqb_agent.research_api`。这里仅保留少量安全运维工具。

| 工具 | 用途 | 边界 |
|---|---|---|
| `check_correlation.py` | 读取 BRAIN self-correlation | 只读平台检查 |
| `check_health.py` | 读取 DONE Alpha 健康指标 | 只读平台检查 |
| `check_repo_privacy.py` | research-data 隐私回归 | 只扫 `git ls-files`，不扫整块磁盘 |

研究事实从 BRAIN 获取，执行安全只从 ExecutionGuard 恢复。

架构审计的依赖契约由 `tests/test_architecture_contracts.py` 保护；新增生产模块必须先加入 `scripts/run_targeted_tests.py` 的显式测试映射。该审计只使用 stdlib AST，不读取真实研究 payload，也不触发远端写操作。

```powershell
python main.py state doctor
python main.py state audit
```
