"""Pure validation statistics kernel.

This module deliberately contains no state, I/O, client access, or report
assembly.  Evidence availability is preserved by returning the existing
UNKNOWN/UNAVAILABLE annotations rather than fabricating a decision.
"""

from __future__ import annotations

import itertools
import math
import statistics

from .evidence_status import annotate_evidence
from .metrics import num


def _finite_series(values):
    return [parsed for value in values or () if (parsed := num(value)) is not None]


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
    """Return PSR using the existing finite-sample formula."""
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
        "status": "AVAILABLE", "psr": _normal_cdf(z), "observed_sharpe": sharpe,
        "benchmark": threshold, "n_observations": moments["n"],
        "skew": moments["skew"], "kurtosis": moments["kurtosis"],
    }, status="INCONCLUSIVE", availability="AVAILABLE", quality="VERIFIED", decision="INCONCLUSIVE")


def _expected_max_standard_normal(n_trials):
    n_trials = max(1, int(n_trials))
    if n_trials == 1:
        return 0.0
    p1 = 1.0 - 1.0 / n_trials
    p2 = 1.0 - 1.0 / (n_trials * math.e)
    return (1.0 - 0.5772156649015329) * _normal_ppf(p1) + 0.5772156649015329 * _normal_ppf(p2)


def deflated_sharpe_ratio(returns, observed_sharpe=None, n_trials=1,
                          periods_per_year=1, trial_sharpes=None,
                          trial_mean=None, trial_std=None,
                          trial_stats_observed=None):
    """Compute DSR with the existing trial-count selection threshold."""
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
    result = probabilistic_sharpe_ratio(returns, observed_sharpe=observed_sharpe,
                                        periods_per_year=periods_per_year,
                                        benchmark=threshold)
    result.update({"n_trials": trials, "selection_threshold": threshold,
                   "trial_mean": trial_mean, "trial_std": trial_std,
                   "trial_sharpes_observed": (len(observed_trials) >= 2 if trial_stats_observed is None else bool(trial_stats_observed))})
    result = annotate_evidence(result, status="INCONCLUSIVE", availability="AVAILABLE",
                               quality="VERIFIED" if len(observed_trials) >= 2 else "APPROXIMATE",
                               decision="INCONCLUSIVE") if result.get("status") == "AVAILABLE" else annotate_evidence(result, status="UNAVAILABLE")
    result["method"] = "deflated_sharpe_ratio"
    return result


def pbo_proxy(aligned_return_series, n_splits=4):
    """Estimate PBO using the existing deterministic CSCV ranking procedure."""
    rows = [list(_finite_series(row)) for row in (aligned_return_series or [])]
    if len(rows) < 2:
        return annotate_evidence({"status": "UNAVAILABLE", "reason": "need at least 2 aligned return series"}, status="UNAVAILABLE")
    lengths = {len(row) for row in rows}
    try:
        splits = int(n_splits)
    except (TypeError, ValueError):
        splits = 0
    if len(lengths) != 1 or not lengths or next(iter(lengths)) < 2 * splits or splits < 4 or splits % 2:
        return annotate_evidence({"status": "UNAVAILABLE", "reason": "need equal aligned series and even CSCV splits >= 4"}, status="UNAVAILABLE")
    n_obs = next(iter(lengths))
    if n_obs % splits:
        return annotate_evidence({"status": "UNAVAILABLE", "reason": "CSCV requires equal non-overlapping folds", "n_observations": n_obs, "n_splits": splits}, status="UNAVAILABLE")
    fold_size = n_obs // splits
    folds = [range(i * fold_size, (i + 1) * fold_size) for i in range(splits)]
    logits = []
    for train_indices in itertools.combinations(range(splits), splits // 2):
        train = {index for fold in train_indices for index in folds[fold]}
        test = set(range(n_obs)) - train
        train_means = [statistics.fmean(row[i] for i in train) for row in rows]
        winner = max(range(len(rows)), key=lambda i: train_means[i])
        test_values = sorted(statistics.fmean(row[i] for i in test) for row in rows)
        rank = sum(value <= statistics.fmean(rows[winner][i] for i in test) for value in test_values) / len(test_values)
        rank = min(max(rank, 1e-6), 1.0 - 1e-6)
        logits.append(math.log(rank / (1.0 - rank)))
    return annotate_evidence({"status": "AVAILABLE", "pbo": sum(value <= 0 for value in logits) / len(logits), "n_series": len(rows), "n_observations": n_obs, "n_splits": splits, "logit_oos_rank": logits, "method": "pbo_proxy"}, status="APPROXIMATE")


def pbo_cscv(aligned_return_series, n_splits=4):
    """Compatibility name; output explicitly identifies the proxy method."""
    return pbo_proxy(aligned_return_series, n_splits=n_splits)
