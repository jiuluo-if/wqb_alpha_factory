# 运维脚本

生产模拟不在本目录执行；请使用根目录的 `main.py` 或 `wqb_agent.research_api`。这里仅保留少量安全运维工具。

| 工具 | 用途 | 边界 |
|---|---|---|
| `reconcile_pending.py` | 兼容遗留状态的只读观测 | 新执行请使用 `research_api.reconcile_execution()`，已知 URL 只轮询，绝不重 POST |
| `validate_integrity.py` | 兼容遗留状态的只读诊断 | 新架构以 ExecutionGuard/cache doctor 为准，输出不替代 BRAIN |
| `check_correlation.py` | 读取 BRAIN self-correlation | 只读平台检查 |
| `check_health.py` | 读取 DONE Alpha 健康指标 | 只读平台检查 |
| `refresh_self_correlation.py` | 兼容遗留状态的 self-correlation 读取 | 新代码直接调用 `get_alpha_self_correlation()` |
| `research_quality_audit.py` | 兼容遗留研究报告 | 不属于 Remote-First 运行入口 |
| `check_repo_privacy.py` | research-data 隐私回归 | 只扫 `git ls-files`，不扫整块磁盘 |
| `benchmark_local_io.py` | 遗留本地 IO 基准 harness | synthetic 临时数据，无 network/`Client`/`.wqb_state` |

遗留脚本不再是当前工程入口；完整历史由 Git 保留，研究事实从 BRAIN 获取，执行安全只从 ExecutionGuard 恢复。

架构审计的依赖契约由 `tests/test_architecture_contracts.py` 保护；新增生产模块必须先加入 `scripts/run_targeted_tests.py` 的显式测试映射。该审计只使用 stdlib AST，不读取真实研究 payload，也不触发远端写操作。

```powershell
python main.py state doctor
python main.py state audit
```
