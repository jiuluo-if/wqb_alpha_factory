# 运维脚本

生产模拟不在本目录执行；请使用根目录的 `main.py` 或 `wqb_agent.research_api`。这里仅保留少量安全运维工具。

| 工具 | 用途 | 边界 |
|---|---|---|
| `reconcile_pending.py` | `READ_ONLY=YES` 的 UNKNOWN/PENDING Simulation 观测 | 已知 URL 只轮询，绝不重 POST；`--commit` 已退役，使用 canonical checkpoint recovery / `run-proposals` |
| `validate_integrity.py` | 审计本地证据、账本和状态一致性 | 输出是派生诊断，不替代事实源 |
| `archive_completed_rounds.py` | 归档已完成的派生 round/checkpoint | 默认 dry-run；`--apply` 前需锁、checkpoint 审计和用户确认 |
| `check_correlation.py` | 读取 BRAIN self-correlation | 只读平台检查 |
| `check_health.py` | 读取 DONE Alpha 健康指标 | 只读平台检查 |
| `refresh_self_correlation.py` | 限窗只读回填已结算 `SELF_CORRELATION` | 只发 GET，只更新可重取的 evidence cache |
| `replay_research_yield.py` | 只读重放历史证据为 mechanism funnel | 不写 `.wqb_state`，不触发 Simulation |
| `research_quality_audit.py` | 生成 raw research audit | 默认输出 local-only `research_data/`，禁止写 tracked docs |
| `check_repo_privacy.py` | research-data 隐私回归 | 只扫 `git ls-files`，不扫整块磁盘 |
| `benchmark_local_io.py` | offline 本地 IO/JSONL 基准 harness | synthetic 临时数据，无 network/`Client`/`.wqb_state` |

已删除的 report、ledger、schema enhance、memory maintenance 和一次性导入脚本不再是当前工程入口；完整历史由 Git 保留，研究事实仍从 `.wqb_state` 和 BRAIN 获取。

```powershell
python scripts/reconcile_pending.py --state-dir .wqb_state
python scripts/validate_integrity.py
python scripts/archive_completed_rounds.py --state-dir .wqb_state
```
