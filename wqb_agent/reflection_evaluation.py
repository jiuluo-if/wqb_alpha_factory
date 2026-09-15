"""Pure reflection evaluation and diagnosis primitives."""

import re

from .evidence import overlay_cached_checks
from .failures import classify_experiment
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
    """Apply the quality gate to an already normalized metric projection."""
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


def diagnose_error(error):
    """Classify execution errors without touching an owner or persistence."""
    error = error or ""
    if "Simulation rejected" in error or "422" in error or "400" in error:
        return f"syntax/settings rejection: {error[:120]}"
    if "timed out" in error.lower():
        return "polling timed out"
    if "PLATFORM_ERROR" in error or "500" in error:
        return f"platform error (not an alpha-quality issue): {error[:120]}"
    return f"runtime error: {error[:120]}"


def effective_metrics(experiment, evidence_cache=None, self_correlation_limit=0.5):
    """Normalize one experiment's effective metric/check view exactly once."""
    metrics = experiment.metrics or {}
    cached = (evidence_cache or {}).get(getattr(experiment, "alpha_id", None))
    if cached:
        metrics = overlay_cached_checks(metrics, cached, self_correlation_limit)
    return normalized_metrics(metrics)


def evaluate_experiment(experiment, *, evidence_cache=None,
                        self_correlation_limit=0.5, success_sharpe=1.0,
                        promising_sharpe=0.7, promising_fitness=0.5,
                        success_fitness=1.0, min_turnover=0.01,
                        max_turnover=1.5, max_drawdown=0.5,
                        check_diagnosis=()):
    """Return one immutable evaluation projection for an experiment."""
    if experiment.status in ("SKIPPED_STALE", "SKIPPED_UNKNOWN"):
        verdict = {"label": "SKIPPED", "reason": "execution skipped after repeated read-only reconciliation; no research conclusion",
                   "diagnosis": ["reconciliation_stale"], "kind": None, "notes": []}
        return {"metrics": {}, "verdict": verdict}
    if experiment.status == "UNKNOWN":
        verdict = {"label": "FAIL", "reason": "UNKNOWN: requires read-only reconciliation before retry",
                   "diagnosis": ["unknown"], "kind": None, "notes": []}
        return {"metrics": {}, "verdict": verdict}
    if experiment.status == "FAILED":
        kind = classify_experiment(experiment)
        verdict = {"label": "FAIL", "reason": diagnose_error(experiment.error),
                   "diagnosis": ["runtime"], "kind": kind, "notes": []}
        return {"metrics": {}, "verdict": verdict}
    metrics = effective_metrics(experiment, evidence_cache, self_correlation_limit)
    required_metrics = ("sharpe", "fitness", "turnover", "returns", "drawdown", "margin")
    checks = metrics.get("checks")
    malformed_checks = not (
        isinstance(checks, list) and checks
        and all(isinstance(check, dict) and check.get("name")
                and check_pass(check) is not None for check in checks)
    )
    missing_metrics = [name for name in required_metrics if metrics.get(name) is None]
    if malformed_checks or missing_metrics:
        verdict = {"label": "RECONCILE", "reason": "mandatory checks/metrics incomplete; promotion and research decision deferred",
                   "diagnosis": ((["missing_checks"] if malformed_checks else [])
                                 + (["missing_metrics"] if missing_metrics else [])),
                   "kind": None, "notes": []}
        return {"metrics": metrics, "verdict": verdict}
    sharpe = metrics.get("sharpe")
    health_reasons = (getattr(experiment, "health", None) or {}).get("reasons") or []
    concentrated = any(
        "concentrated_weight" in str(reason).lower()
        or "longcount=" in str(reason).lower()
        or "shortcount=" in str(reason).lower()
        for reason in health_reasons
    )
    if concentrated:
        verdict = {"label": "FAIL", "reason": "NOISE_TRAP: " + "; ".join(health_reasons),
                   "diagnosis": ["weight_concentration"], "notes": []}
        return {"metrics": metrics, "verdict": verdict}
    fitness = metrics.get("fitness")
    if (sharpe is not None and sharpe > 3) or (fitness is not None and fitness > 8):
        verdict = {"label": "SUSPICIOUS_HIGH_SIGNAL",
                   "reason": "SUSPICIOUS_HIGH_SIGNAL: requires independent perturbation validation",
                   "diagnosis": ["high_signal_unvalidated"], "notes": []}
        return {"metrics": metrics, "verdict": verdict}
    hard, soft = quality_gate(
        metrics, success_sharpe=success_sharpe, success_fitness=success_fitness,
        min_turnover=min_turnover, max_turnover=max_turnover, max_drawdown=max_drawdown,
    )
    if not hard:
        verdict = {"label": "SUCCESS", "reason": " or ".join(soft) or "passed comprehensive gate",
                   "diagnosis": [], "notes": soft}
    else:
        reason = " or ".join(hard + soft) or "below quality gate"
        signal = (sharpe is not None and sharpe > promising_sharpe) or (
            fitness is not None and fitness > promising_fitness
        )
        turnover_ok = not any("turnover" in issue for issue in hard)
        label = "PROMISING" if signal and turnover_ok else "FAIL"
        verdict = {"label": label, "reason": reason,
                   "diagnosis": diagnosis_from(hard + soft, check_diagnosis),
                   "notes": soft}
    return {"metrics": metrics, "verdict": verdict}
