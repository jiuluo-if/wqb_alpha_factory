"""Pure reflection evaluation and diagnosis primitives."""

import re

from .metrics import check_pass, normalized_metrics


def categorize_check(name, patterns):
    for pattern, category in patterns:
        if pattern.search(name or ""):
            return category
    return None


def diagnosis_from(issues, patterns):
    diagnosis = []
    for issue in issues:
        if "missing metrics" in issue:
            diagnosis.append("missing_metrics")
        elif "submission checks missing" in issue:
            diagnosis.append("missing_checks")
        elif "sharpe" in issue:
            diagnosis.append("sharpe")
        elif "fitness" in issue:
            diagnosis.append("fitness")
        elif "turnover" in issue:
            diagnosis.append("turnover")
        elif "margin" in issue:
            diagnosis.append("margin")
        elif "drawdown" in issue:
            diagnosis.append("drawdown")
        elif "checks" in issue:
            for check in re.findall(r"\[([^\]]+)\]", issue):
                cat = categorize_check(check, patterns)
                if cat and cat not in diagnosis:
                    diagnosis.append(cat)
            if not diagnosis:
                diagnosis.append("checks_failed")
        elif "returns" in issue:
            diagnosis.append("returns_sign")
    return diagnosis or ["no_signal"]


def quality_gate(metrics, *, success_sharpe, success_fitness, min_turnover,
                 max_turnover, max_drawdown):
    metrics = normalized_metrics(metrics)
    hard, soft = [], []
    checks = metrics.get("checks") or []
    failed_checks = [c.get("name") for c in checks if check_pass(c) is not True]
    if failed_checks:
        hard.append(f"checks failed: {failed_checks}")
    elif not checks:
        hard.append("submission checks missing/unverified")
    sharpe, fitness = metrics.get("sharpe"), metrics.get("fitness")
    turnover, margin = metrics.get("turnover"), metrics.get("margin")
    returns, drawdown = metrics.get("returns"), metrics.get("drawdown")
    missing = [name for name, value in (
        ("sharpe", sharpe), ("fitness", fitness), ("turnover", turnover),
        ("returns", returns), ("drawdown", drawdown), ("margin", margin),
    ) if value is None]
    if missing:
        hard.append(f"missing metrics: {missing}")
    if sharpe is not None and sharpe < success_sharpe:
        hard.append(f"sharpe {sharpe:.2f} < {success_sharpe}")
    if fitness is not None and fitness < success_fitness:
        hard.append(f"fitness {fitness:.2f} < {success_fitness}")
    if turnover is not None:
        if turnover > max_turnover:
            hard.append(f"turnover {turnover:.2f} too high")
        elif turnover < min_turnover:
            soft.append(f"turnover {turnover:.3f} very low (thin book)")
    if margin is not None and margin <= 0:
        hard.append(f"margin {margin:.4f} <= 0 (loses per dollar traded)")
    if drawdown is not None and drawdown > max_drawdown:
        hard.append(f"drawdown {drawdown:.2f} too deep")
    if returns is not None and sharpe is not None and (returns > 0) != (sharpe > 0):
        soft.append("returns/sharpe sign mismatch")
    return hard, soft
