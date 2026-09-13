"""ROLE: INTERNAL
AGENT_RELEVANCE: MEDIUM
PURPOSE: Evaluate validation evidence and statistical diagnostics.
READ WHEN: changing robustness/statistical report semantics.
DO NOT USE FOR: direct BRAIN access or choosing research direction.

Canonical validation plans, aggregate reports, and selection-aware statistics.

This module is pure: it does not submit simulations, read state, or call a
client.  A ValidationPlan is registered before robustness work starts; a
ValidationReport is the only evidence allowed to promote a parent candidate.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import math
import re
import statistics
import time

from .evidence_status import annotate_evidence
from .metrics import checks_passed, num, score_of
from .robustness import evaluate_robustness
from .schema import CREATED_BY_VERSION, VALIDATION_PLAN_VERSION, VALIDATION_VERSION

REQUIRED_VARIABLES = (
    "window_locality",
    "semantic_field_swap",
    "universe_robustness",
    "yearly_aggregates",
)


class ValidationPlan(dict):
    """Mapping-compatible pre-registration artifact."""

    @classmethod
    def from_parent(cls, parent, **kwargs):
        return cls(default_validation_plan(parent, **kwargs))

    def validate(self):
        return validate_plan(self)


class ValidationReport(dict):
    """Mapping-compatible canonical aggregate decision artifact."""

    @property
    def status(self):
        return self.get("status")

    @property
    def stable(self):
        return self.get("stable") is True


def _finite_series(values):
    result = []
    for value in values or ():
        parsed = num(value)
        if parsed is not None:
            result.append(parsed)
    return result


def _normal_cdf(value):
    return 0.5 * (1.0 + math.erf(value / math.sqrt(2.0)))


def _normal_ppf(probability):
    """Acklam's rational approximation for the standard normal quantile."""
    p = min(max(float(probability), 1e-12), 1.0 - 1e-12)
    a = (-39.6968302866538, 220.946098424521, -275.928510446969,
         138.357751867269, -30.6647980661472, 2.50662827745924)
    b = (-54.4760987982241, 161.585836858041, -155.698979859887,
         66.8013118877197, -13.2806815528857)
    c = (-0.00778489400243029, -0.322396458041136, -2.40075827716184,
         -2.54973253934373, 4.37466414146497, 2.93816398269878)
    d = (0.00778469570904146, 0.32246712907004, 2.445134137143,
         3.75440866190742)
    if p < 0.02425:
        q = math.sqrt(-2.0 * math.log(p))
        numerator = (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5])
        denominator = (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0
        return numerator / denominator
    if p > 1.0 - 0.02425:
        q = math.sqrt(-2.0 * math.log(1.0 - p))
        numerator = (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5])
        denominator = (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0
        return -numerator / denominator
    q = p - 0.5
    r = q * q
    numerator = (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q
    denominator = ((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1.0
    return numerator / denominator


def _moments(values):
    values = _finite_series(values)
    n = len(values)
    if n < 3:
        return None
    mean = statistics.fmean(values)
    centered = [value - mean for value in values]
    m2 = statistics.fmean(value ** 2 for value in centered)
    if m2 <= 0:
        return None
    m3 = statistics.fmean(value ** 3 for value in centered)
    m4 = statistics.fmean(value ** 4 for value in centered)
    return {
        "n": n,
        "mean": mean,
        "std": math.sqrt(m2),
        "skew": m3 / (m2 ** 1.5),
        "kurtosis": m4 / (m2 ** 2),
    }


def probabilistic_sharpe_ratio(returns, observed_sharpe=None, periods_per_year=1,
                               benchmark=0.0):
    """Return PSR using Bailey & Lopez de Prado's finite-sample formula.

    The denominator uses sample skewness and *non-excess* kurtosis:
    ``sqrt(1 - skew*SR + (kurtosis-1)*SR**2/4)``.  This matches the PSR
    equation in *The Deflated Sharpe Ratio: Correcting for Selection Bias,
    Backtest Overfitting and Non-Normality* (2014).
    """
    moments = _moments(returns)
    if moments is None:
        return annotate_evidence({"status": "UNAVAILABLE", "reason": "need at least 3 non-constant returns"}, status="UNAVAILABLE")
    scale = math.sqrt(max(num(periods_per_year) or 1.0, 1.0))
    sharpe = (moments["mean"] / moments["std"]) * scale if observed_sharpe is None else num(observed_sharpe)
    threshold = num(benchmark)
    if sharpe is None or threshold is None:
        return annotate_evidence({"status": "UNAVAILABLE", "reason": "invalid Sharpe or benchmark"}, status="UNAVAILABLE")
    variance = 1.0 - moments["skew"] * sharpe + ((moments["kurtosis"] - 1.0) / 4.0) * sharpe ** 2
    if variance <= 0 or not math.isfinite(variance):
        return annotate_evidence({"status": "UNAVAILABLE", "reason": "non-positive PSR variance"}, status="UNAVAILABLE")
    z = (sharpe - threshold) * math.sqrt(moments["n"] - 1.0) / math.sqrt(variance)
    return annotate_evidence({
        "status": "AVAILABLE",
        "psr": _normal_cdf(z),
        "observed_sharpe": sharpe,
        "benchmark": threshold,
        "n_observations": moments["n"],
        "skew": moments["skew"],
        "kurtosis": moments["kurtosis"],
    }, status="INCONCLUSIVE", availability="AVAILABLE", quality="VERIFIED", decision="INCONCLUSIVE")


def _expected_max_standard_normal(n_trials):
    n_trials = max(1, int(n_trials))
    if n_trials == 1:
        return 0.0
    # Euler approximation used for the expected maximum of independent
    # standard normals; it is a threshold, not a quality score.
    p1 = 1.0 - 1.0 / n_trials
    p2 = 1.0 - 1.0 / (n_trials * math.e)
    return (1.0 - 0.5772156649015329) * _normal_ppf(p1) + 0.5772156649015329 * _normal_ppf(p2)


def deflated_sharpe_ratio(returns, observed_sharpe=None, n_trials=1,
                          periods_per_year=1, trial_sharpes=None,
                          trial_mean=None, trial_std=None,
                          trial_stats_observed=None):
    """Compute DSR as PSR against a trial-count-adjusted expected maximum."""
    trials = max(1, int(n_trials or 1))
    observed_trials = _finite_series(trial_sharpes)
    if len(observed_trials) >= 2:
        trial_mean = statistics.fmean(observed_trials)
        trial_std = statistics.pstdev(observed_trials)
    elif num(trial_mean) is not None and num(trial_std) is not None and num(trial_std) >= 0:
        trial_mean = num(trial_mean)
        trial_std = num(trial_std)
        threshold = trial_mean + trial_std * _expected_max_standard_normal(trials)
    else:
        trial_mean = 0.0
        trial_std = 1.0
        threshold = _expected_max_standard_normal(trials)
    if len(observed_trials) >= 2:
        threshold = trial_mean + trial_std * _expected_max_standard_normal(trials)
    result = probabilistic_sharpe_ratio(
        returns, observed_sharpe=observed_sharpe, periods_per_year=periods_per_year,
        benchmark=threshold,
    )
    result.update({"n_trials": trials, "selection_threshold": threshold,
                   "trial_mean": trial_mean, "trial_std": trial_std,
                   "trial_sharpes_observed": (
                       len(observed_trials) >= 2 if trial_stats_observed is None
                       else bool(trial_stats_observed)
                   )})
    if result.get("status") != "AVAILABLE":
        result = annotate_evidence(result, status="UNAVAILABLE")
    else:
        result = annotate_evidence(
            result,
            status="INCONCLUSIVE",
            availability="AVAILABLE",
            quality="VERIFIED" if len(observed_trials) >= 2 else "APPROXIMATE",
            decision="INCONCLUSIVE",
        )
    result["method"] = "deflated_sharpe_ratio"
    return result


def pbo_proxy(aligned_return_series, n_splits=4):
    """Estimate PBO only when multiple aligned series permit CSCV.

    Each row is one candidate and each column is the same time point.  The
    implementation follows the CSCV ranking procedure: select the in-sample
    winner for every half split, then measure whether it ranks below the
    median out-of-sample.  Fewer than two candidates or four even splits is
    explicitly UNAVAILABLE, never fabricated from one return series.
    """
    rows = [list(_finite_series(row)) for row in (aligned_return_series or [])]
    if len(rows) < 2:
        return annotate_evidence({"status": "UNAVAILABLE", "reason": "need at least 2 aligned return series"}, status="UNAVAILABLE")
    lengths = {len(row) for row in rows}
    try:
        splits = int(n_splits)
    except (TypeError, ValueError):
        splits = 0
    if len(lengths) != 1 or not lengths or next(iter(lengths)) < 2 * splits or splits < 2 or splits % 2:
        return annotate_evidence({"status": "UNAVAILABLE", "reason": "need equal aligned series and even CSCV splits >= 4"}, status="UNAVAILABLE")
    n_obs = next(iter(lengths))
    logits = []
    for train_indices in itertools.combinations(range(splits), splits // 2):
        folds = [range(i * (n_obs // splits), (i + 1) * (n_obs // splits)) for i in range(splits)]
        train = set(index for fold in train_indices for index in folds[fold])
        test = set(range(n_obs)) - train
        train_means = [statistics.fmean(row[i] for i in train) for row in rows]
        winner = max(range(len(rows)), key=lambda i: train_means[i])
        test_values = sorted(statistics.fmean(row[i] for i in test) for row in rows)
        rank = sum(value <= statistics.fmean(rows[winner][i] for i in test) for value in test_values) / len(test_values)
        rank = min(max(rank, 1e-6), 1.0 - 1e-6)
        logits.append(math.log(rank / (1.0 - rank)))
    return annotate_evidence({
        "status": "AVAILABLE",
        "pbo": sum(value <= 0 for value in logits) / len(logits),
        "n_series": len(rows),
        "n_observations": n_obs,
        "n_splits": splits,
        "logit_oos_rank": logits,
        "method": "pbo_proxy",
    }, status="APPROXIMATE")


def pbo_cscv(aligned_return_series, n_splits=4):
    """Compatibility name; output explicitly identifies the proxy method."""
    return pbo_proxy(aligned_return_series, n_splits=n_splits)


def default_validation_plan(parent, *, budget=7, pnl_capability="UNKNOWN", timestamp=None,
                            statistical_policy="required_when_available",
                            robustness_policy=None):
    expression = parent.get("expression") if isinstance(parent, dict) else getattr(parent, "expression", "")
    fingerprint = parent.get("submission_fingerprint") if isinstance(parent, dict) else getattr(parent, "submission_fingerprint", None)
    settings = parent.get("settings", {}) if isinstance(parent, dict) else getattr(parent, "settings", {})
    fields = parent.get("fields_used", []) if isinstance(parent, dict) else getattr(parent, "fields_used", [])
    field_analysis = parent.get("field_analysis") if isinstance(parent, dict) else getattr(parent, "field_analysis", None)
    has_ts_window = bool(re.search(r"\bts_[a-z_]+\s*\(", str(expression).lower()))
    explicit_metadata = bool(settings is not None or fields)
    semantic_requirement = "REQUIRED"
    if explicit_metadata and (not isinstance(field_analysis, dict) or len(field_analysis) < 2):
        semantic_requirement = "NOT_APPLICABLE"
    has_decay = isinstance(settings, dict) and "decay" in settings
    has_truncation = isinstance(settings, dict) and "truncation" in settings
    policy = {
        "min_sharpe_retention": 0.7,
        "min_fitness_retention": 0.6,
        "max_turnover_multiple": 1.5,
        "max_drawdown_multiple": 1.5,
        "require_checks_passed": True,
    }
    if isinstance(robustness_policy, dict):
        policy.update(robustness_policy)
    variables = [
        {"variable": "window_locality", "reason": "检验局部窗口变化而非固定 ±1 的偶然性", "budget": 1,
         "requirement": "REQUIRED" if has_ts_window or not explicit_metadata else "NOT_APPLICABLE",
         "falsification": "相对窗口变化后 headline 或 checks 明显恶化", "stopping_rule": "完成一个预注册局部窗口集合或首个明确失败"},
        {"variable": "semantic_field_swap", "reason": "检验经济语义相近字段替换后的机制可迁移性", "budget": 1,
         "requirement": semantic_requirement,
         "falsification": "语义相近替换后信号消失或健康失败", "stopping_rule": "完成一个语义匹配替换"},
        {"variable": "universe_robustness", "reason": "检验信号是否只依赖单一股票覆盖层", "budget": 1,
         "requirement": "REQUIRED",
         "falsification": "替代 universe 后指标或 checks 失败", "stopping_rule": "完成一个预注册 universe 对照"},
        {"variable": "decay", "reason": "只改变 decay，避免与 truncation 混淆", "budget": 1,
         "requirement": "REQUIRED" if has_decay else "NOT_APPLICABLE",
         "falsification": "只改变 decay 后收益或健康不可接受", "stopping_rule": "完成一个 decay 对照"},
        {"variable": "truncation", "reason": "只改变 truncation，避免与 decay 混淆", "budget": 1,
         "requirement": "REQUIRED" if has_truncation else "NOT_APPLICABLE",
         "falsification": "只改变 truncation 后收益或健康不可接受", "stopping_rule": "完成一个 truncation 对照"},
        {"variable": "yearly_aggregates", "reason": "要求跨年度结果一致而非单段表现", "budget": 0,
         "requirement": "REQUIRED",
         "falsification": "任一可用年度未通过阈值", "stopping_rule": "读取完整 yearly aggregates"},
    ]
    if str(pnl_capability).upper() == "LIVE_VERIFIED":
        variables.append({"variable": "pnl_diagnostics", "reason": "PnL capability 已验证，检查 rolling stability 与相关性", "budget": 0,
                          "falsification": "rolling stability 或 bootstrap 诊断失败", "stopping_rule": "使用完整可用 return series"})
    for item in variables:
        if item.get("requirement") == "REQUIRED" and item.get("variable") != "yearly_aggregates":
            item["acceptance"] = dict(policy)
    canonical = {
        "schema_version": VALIDATION_PLAN_VERSION,
        "parent_fingerprint": fingerprint,
        "parent_expression": expression,
        "settings": settings or {},
        "variables": variables,
        "budget": int(budget),
        "falsification": "任一 required variable 未通过则 parent 不得 STABLE",
        "stopping_rule": "预算耗尽、预注册变量全部结算或任一硬失败后停止扩展",
        "statistical_policy": statistical_policy,
        "pnl_capability": str(pnl_capability).upper(),
        "robustness_policy": policy,
    }
    identity = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return ValidationPlan({
        "schema_version": VALIDATION_PLAN_VERSION,
        "plan_id": hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16],
        "parent_expression": expression,
        "parent_fingerprint": fingerprint,
        "variables": variables,
        "budget": int(budget),
        "falsification": "任一 required variable 未通过则 parent 不得 STABLE",
        "stopping_rule": "预算耗尽、预注册变量全部结算或任一硬失败后停止扩展",
        "pnl_capability": str(pnl_capability).upper(),
        "preregistered_at": timestamp if timestamp is not None else time.time(),
        "statistical_policy": statistical_policy,
        "robustness_policy": policy,
    })


def migrate_validation_plan(plan):
    """Migrate legacy v1/v2 plans without rewriting append-only evidence."""
    if not isinstance(plan, dict):
        return plan
    version = int(num(plan.get("schema_version")) or 1)
    if version >= 3:
        return dict(plan)
    migrated = dict(plan)
    variables = [dict(item) for item in (plan.get("variables") or []) if isinstance(item, dict)]
    legacy = next((item for item in variables if item.get("variable") == "decay_truncation"), None)
    variables = [item for item in variables if item.get("variable") != "decay_truncation"]
    if legacy:
        for name in ("decay", "truncation"):
            variables.append(dict(legacy, variable=name))
    for item in variables:
        item.setdefault("requirement", "REQUIRED")
        if (item.get("requirement") == "REQUIRED"
                and item.get("variable") != "yearly_aggregates"):
            item.setdefault("acceptance", {
                "min_sharpe_retention": 0.7,
                "min_fitness_retention": 0.6,
                "max_turnover_multiple": 1.5,
                "max_drawdown_multiple": 1.5,
                "require_checks_passed": True,
            })
    migrated["variables"] = variables
    migrated["schema_version"] = 3
    return migrated


def validate_plan(plan):
    plan = migrate_validation_plan(plan)
    if not isinstance(plan, dict):
        return False, ["validation_plan 必须是对象"]
    problems = []
    for key in ("plan_id", "parent_expression", "falsification", "stopping_rule", "preregistered_at"):
        if not isinstance(plan.get(key), str) or not plan[key].strip():
            if key == "preregistered_at" and num(plan.get(key)) is not None:
                continue
            problems.append(f"validation_plan 缺少 {key}")
    variables = plan.get("variables")
    if not isinstance(variables, list):
        problems.append("validation_plan.variables 必须是数组")
        return False, problems
    names = {item.get("variable") for item in variables if isinstance(item, dict)}
    missing = [name for name in REQUIRED_VARIABLES if name not in names]
    if missing:
        problems.append(f"validation_plan 缺少变量: {missing}")
    for item in variables:
        if not isinstance(item, dict):
            problems.append("validation_plan.variables 含非对象")
            continue
        for key in ("variable", "reason", "falsification", "stopping_rule"):
            if not isinstance(item.get(key), str) or not item[key].strip():
                problems.append(f"validation_plan 变量缺少 {key}")
        if item.get("requirement", "REQUIRED") not in {"REQUIRED", "OPTIONAL", "NOT_APPLICABLE"}:
            problems.append("validation_plan variable requirement 非法")
        if num(item.get("budget")) is None or num(item.get("budget")) < 0:
            problems.append("validation_plan 变量 budget 必须为非负数")
        if item.get("requirement") == "REQUIRED" and item.get("variable") != "yearly_aggregates":
            if not isinstance(item.get("acceptance"), dict):
                problems.append("required robustness variable 缺少 acceptance")
    if num(plan.get("budget")) is None or num(plan.get("budget")) < 0:
        problems.append("validation_plan budget 必须为非负数")
    elif sum(num(item.get("budget")) or 0 for item in variables if isinstance(item, dict)) > num(plan.get("budget")):
        problems.append("validation_plan 变量预算总和不能超过总预算")
    return not problems, problems


def _child_dimension(child):
    if isinstance(child, dict):
        return child.get("changed_variable") or child.get("validation_variable") or child.get("change_type")
    return getattr(child, "changed_variable", None) or getattr(child, "validation_variable", None) or getattr(child, "change_type", None)


def _child_metrics(child):
    return child.get("metrics") if isinstance(child, dict) else getattr(child, "metrics", None)


def _child_status(child):
    return child.get("status") if isinstance(child, dict) else getattr(child, "status", None)


def build_validation_report(parent, robustness_children, plan, *, yearly_evidence=None,
                            trial_summary=None, pnl_evidence=None, return_series=None,
                            aligned_return_series=None, platform_evidence=None):
    """Aggregate all required evidence; a single child can never pass."""
    plan = migrate_validation_plan(plan)
    plan_ok, plan_errors = validate_plan(plan)
    dimensions = {}
    children = list(robustness_children or [])
    plan_variables = {
        item.get("variable"): item for item in (plan.get("variables") or [])
        if isinstance(item, dict) and item.get("variable")
    }
    for variable in REQUIRED_VARIABLES:
        requirement = (plan_variables.get(variable) or {}).get("requirement", "REQUIRED")
        if requirement == "NOT_APPLICABLE":
            dimensions[variable] = {"status": "NOT_APPLICABLE", "evidence_status": "NOT_APPLICABLE",
                                    "requirement": requirement,
                                    "reason": (plan_variables.get(variable) or {}).get("reason")}
            continue
        matches = [child for child in children if _child_dimension(child) == variable]
        if variable == "yearly_aggregates":
            evidence = yearly_evidence or {}
            passed = evidence.get("status") == "VERIFIED" and evidence.get("stable") is True
            dimensions[variable] = {"status": "PASS" if passed else "FAIL",
                                    "evidence_status": "PASS" if passed else "FAIL",
                                    "requirement": requirement, "evidence": evidence}
            continue
        valid = []
        valid_evidence = []
        criterion = (plan_variables.get(variable) or {}).get("acceptance") or {}
        for child in matches:
            if _child_status(child) != "DONE":
                continue
            evidence = child.get("robustness_evidence") if isinstance(child, dict) else getattr(child, "robustness_evidence", None)
            if not isinstance(evidence, dict):
                evidence = evaluate_robustness(
                    variable, _child_metrics(parent) or {}, _child_metrics(child) or {},
                    criterion, checks_passed(_child_metrics(child)),
                ).as_dict()
            if evidence.get("decision") == "PASS":
                valid.append(child)
                valid_evidence.append(evidence)
        dimensions[variable] = {
            "status": "PASS" if valid else "FAIL",
            "evidence_status": "PASS" if valid else "FAIL",
            "requirement": requirement,
            "count": len(matches),
            "passed": len(valid),
            "scores": [score_of(_child_metrics(child)) for child in valid],
            "evidence": valid_evidence,
        }
    for variable in ("decay", "truncation"):
        spec = plan_variables.get(variable) or {}
        requirement = spec.get("requirement", "NOT_APPLICABLE")
        if requirement == "NOT_APPLICABLE":
            dimensions[variable] = {"status": "NOT_APPLICABLE", "evidence_status": "NOT_APPLICABLE",
                                    "requirement": requirement, "reason": spec.get("reason")}
            continue
        matches = [child for child in children if _child_dimension(child) == variable]
        valid = []
        valid_evidence = []
        criterion = spec.get("acceptance") or {}
        for child in matches:
            if _child_status(child) != "DONE":
                continue
            evidence = child.get("robustness_evidence") if isinstance(child, dict) else getattr(child, "robustness_evidence", None)
            if not isinstance(evidence, dict):
                evidence = evaluate_robustness(
                    variable, _child_metrics(parent) or {}, _child_metrics(child) or {},
                    criterion, checks_passed(_child_metrics(child)),
                ).as_dict()
            if evidence.get("decision") == "PASS":
                valid.append(child)
                valid_evidence.append(evidence)
        dimensions[variable] = {"status": "PASS" if valid else "FAIL",
                                "evidence_status": "PASS" if valid else "FAIL",
                                "requirement": requirement, "count": len(matches), "passed": len(valid),
                                "evidence": valid_evidence}
    pnl_status = (plan.get("pnl_capability") or "UNKNOWN").upper() if isinstance(plan, dict) else "UNKNOWN"
    if pnl_status == "LIVE_VERIFIED":
        pnl_ok = isinstance(pnl_evidence, dict) and pnl_evidence.get("status") == "PASS"
        dimensions["pnl_diagnostics"] = {"status": "PASS" if pnl_ok else "FAIL",
                                          "evidence_status": "PASS" if pnl_ok else "FAIL", "evidence": pnl_evidence}
    platform = platform_evidence or {}
    parent_platform = platform.get("parent") if isinstance(platform, dict) else None
    child_platform = platform.get("children") if isinstance(platform, dict) else None
    platform_ok = (
        isinstance(parent_platform, dict)
        and isinstance(parent_platform.get("health"), dict)
        and parent_platform["health"].get("ok") is True
        and isinstance(parent_platform.get("correlation"), dict)
        and parent_platform["correlation"].get("status") == "PASS"
        and isinstance(child_platform, list)
        and len(child_platform) >= len(children)
        and all(
            isinstance(item, dict)
            and isinstance(item.get("health"), dict)
            and item["health"].get("ok") is True
            and isinstance(item.get("correlation"), dict)
            and item["correlation"].get("status") == "PASS"
            for item in child_platform
        )
    )
    dimensions["platform_quality"] = {
        "status": "PASS" if platform_ok else "FAIL",
        "evidence_status": "PASS" if platform_ok else "FAIL",
        "evidence": platform_evidence,
    }
    selection = (trial_summary or {}).get("selection_trial_count")
    if selection is None:
        selection = (trial_summary or {}).get("candidate_count")
    if selection is None:
        selection = (trial_summary or {}).get("generated_trials")
    if selection is None:
        selection = (trial_summary or {}).get("trial_count", 0)
    trial_sharpes = (trial_summary or {}).get("trial_sharpes")
    stats = {
        "psr": probabilistic_sharpe_ratio(return_series) if return_series else annotate_evidence({"status": "UNAVAILABLE", "reason": "return series unavailable"}, status="UNAVAILABLE"),
        "dsr": deflated_sharpe_ratio(
            return_series, n_trials=max(1, selection), trial_sharpes=trial_sharpes,
            trial_mean=(trial_summary or {}).get("trial_sharpe_mean"),
            trial_std=(trial_summary or {}).get("trial_sharpe_std"),
            trial_stats_observed=(trial_summary or {}).get("trial_sharpe_count", 0) >= 2,
        ) if return_series else annotate_evidence({"status": "UNAVAILABLE", "reason": "return series unavailable", "n_trials": max(1, selection)}, status="UNAVAILABLE"),
        "pbo_cscv": pbo_proxy(aligned_return_series),
        "trial_events": selection,
        "raw_trial_count": selection,
        "effective_trial_count": max(1, int((trial_summary or {}).get("effective_trial_count", selection) or selection or 1)),
    }
    if (trial_summary or {}).get("history_completeness") == "INCOMPLETE_LEGACY":
        stats["dsr"] = annotate_evidence(
            {"status": "UNAVAILABLE", "reason_code": "INCOMPLETE_TRIAL_HISTORY",
             "n_trials": selection}, status="UNAVAILABLE"
        )
    statistical_policy = (plan or {}).get("statistical_policy", "required_when_available")
    if isinstance(statistical_policy, dict):
        statistical_mode = str(statistical_policy.get("mode", "required_when_available"))
        min_psr = statistical_policy.get("min_psr")
        min_dsr = statistical_policy.get("min_dsr")
        max_pbo = statistical_policy.get("max_pbo_proxy")
    else:
        statistical_mode = str(statistical_policy)
        min_psr = min_dsr = None
        max_pbo = None
    has_returns = bool(return_series)
    dsr = stats["dsr"]
    psr = stats["psr"]
    if not has_returns or psr.get("availability") == "UNAVAILABLE" or dsr.get("availability") == "UNAVAILABLE":
        stats["statistical_status"] = "UNAVAILABLE"
        stats["statistical_decision"] = "INCONCLUSIVE"
    elif dsr.get("quality") == "APPROXIMATE":
        stats["statistical_status"] = "APPROXIMATE"
        stats["statistical_decision"] = "INCONCLUSIVE"
    else:
        checks = []
        if num(min_psr) is not None:
            checks.append((num(psr.get("psr")), num(min_psr)))
        if num(min_dsr) is not None:
            checks.append((num(dsr.get("psr")), num(min_dsr)))
        passed = all(value is not None and value >= threshold for value, threshold in checks)
        pbo_value = num(stats.get("pbo_cscv", {}).get("pbo"))
        if num(max_pbo) is not None and pbo_value is not None and pbo_value > num(max_pbo):
            passed = False
        has_policy_threshold = bool(checks) or num(max_pbo) is not None
        if not has_policy_threshold:
            stats["statistical_decision"] = "INCONCLUSIVE"
            stats["statistical_status"] = "INCONCLUSIVE"
        else:
            stats["statistical_decision"] = "PASS" if passed else "FAIL"
            stats["statistical_status"] = stats["statistical_decision"]
    stats["policy_mode"] = statistical_mode
    if stats["statistical_status"] == "UNAVAILABLE" and statistical_mode == "required":
        dimensions["statistical_evidence"] = {"status": "FAIL", "evidence_status": "FAIL", "requirement": "REQUIRED", "reason": "statistical evidence required"}
    else:
        dimensions["statistical_evidence"] = {
            "status": stats["statistical_status"],
            "evidence_status": stats["statistical_status"],
            "requirement": "OPTIONAL" if statistical_mode == "required_when_available" else "REQUIRED",
        }
    parent_done = _child_status(parent) == "DONE"
    parent_checks = checks_passed(_child_metrics(parent)) is True
    required_pass = all(
        item.get("status") == "PASS"
        for item in dimensions.values()
        if item.get("requirement", "REQUIRED") == "REQUIRED"
    )
    status = "PASS" if plan_ok and parent_done and parent_checks and required_pass else "FAIL"
    return ValidationReport({
        "schema_version": VALIDATION_VERSION,
        "created_by_version": CREATED_BY_VERSION,
        "status": status,
        "stable": status == "PASS",
        "candidate": "parent",
        "parent_expression": parent.get("expression") if isinstance(parent, dict) else getattr(parent, "expression", None),
        "plan_id": plan.get("plan_id") if isinstance(plan, dict) else None,
        "plan_valid": plan_ok,
        "plan_errors": plan_errors,
        "dimensions": dimensions,
        "statistical_evidence": stats,
        "selection_adjustment": {"trial_events": selection, "dsr_required": True},
        "reason": "aggregate validation passed" if status == "PASS" else "required validation evidence incomplete or failed",
    })
