# 仓库隐私边界

## 公开仓库

tracked 文件只允许：稳定代码、synthetic fixtures、常青契约、脱敏的平台引用和最小运维文档。公开示例必须使用占位符，如 `field_a`、`field_b`、`signal_x`、`X`、`Y`、`GROUP`。

## 本地私有研究

真实 Alpha 表达式、字段 ID 与配对、指标、Simulation 证据、报告和生成导出仅限本地，必须放在被忽略的 `.wqb_state/`、`research_data/`、`reports/` 或 `.planning/` 路径下。

Remote-First 路径只允许：`execution_guard.json`、可重建的远端元数据缓存、外部 credentials 引用和进程锁。

## 文档与提交卫生

tracked 文档、prompts、测试、日志或提交信息中不得出现研究历史、带日期的轮次报告、私有路径、credentials 或平台标识符。优先修改权威 owner 并删除过期历史产物；不得创建版本化副本或 tracked 归档。

## 自动执行

交付前运行 `python scripts/check_repo_privacy.py`。它扫描 `git ls-files`，因此被忽略的本地研究保持可用且不成为公开产物。另需运行 `git diff --check` 并单独检查已暂存变更。

## Git 历史限制

当前树清理不会清除已发布的 Git 历史。本仓库采用 `HISTORY_REWRITE = NO`；历史只是归档，不得被表述为当前公开研究证据。
