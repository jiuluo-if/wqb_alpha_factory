"""ROLE: INTERNAL
AGENT_RELEVANCE: MEDIUM
PURPOSE: Build bounded robustness perturbation work from experiment evidence.
READ WHEN: changing validation task generation or high-signal checks.
DO NOT USE FOR: direct Simulation submission or final economic interpretation.

Robustness validation of suspiciously high-signal alphas.

对应本地 AGENTS.md §6 纪律：Sharpe > 3 或 Fitness > 8 的高分信号必须先
交叉验证再判定，聚合指标可被集中权重噪声欺骗。本模块对疑似高分候选
做扰动验证（窗口步进 / ts_mean 平滑 / 语义相近字段替换），重跑真实
Simulation，只有「多数扰动存活 + 中位分数达标 + 相对原始 Alpha 保留
足够性能 + WQB checks 通过」才判定为稳健。单次幸运扰动永远不够。

用法：
    validator = HighSignalValidator(client, settings)
    ok, results = validator.validate(record, alt_fields=fields)
    # record: {"expression", "fields_used", "metrics", "round_no", ...}
"""

import math

from .diversity import extract_fields
from .experiment import Experiment
from .metrics import checks_passed, num, score_of
from .mutations import _relative_window_changes, _swap_field


def _pick_nearby_field(primary, alt_fields):
    """选择语义相关的替换字段：同 dataset，然后同 category，然后任意。"""
    metas = [f for f in alt_fields if isinstance(f, dict)]
    ids = [f["id"] for f in metas] if metas else list(alt_fields or [])
    primary_meta = next((m for m in metas if m.get("id") == primary), None)
    if primary_meta:
        same_ds = [
            m["id"]
            for m in metas
            if m.get("dataset") == primary_meta.get("dataset")
            and m["id"] != primary
        ]
        if same_ds:
            return same_ds[0]
        same_cat = [
            m["id"]
            for m in metas
            if m.get("category") == primary_meta.get("category")
            and m["id"] != primary
        ]
        if same_cat:
            return same_cat[0]
    for fid in ids:
        if fid != primary:
            return fid
    return None


class HighSignalValidator:
    def __init__(
        self,
        client,
        settings,
        max_concurrent=3,
        poll_timeout_sec=900,
        min_valid_fitness=1.0,
        majority_ratio=0.6,
        min_pass=2,
        retention_ratio=0.5,
    ):
        self.settings = dict(settings)
        # ``client`` is retained for source compatibility, but execution is
        # intentionally owned by Agent/run-proposals.  A validator must not
        # create a second production submission path behind the caller's
        # back.
        self.client = client
        self.max_concurrent = max_concurrent
        self.poll_timeout_sec = poll_timeout_sec
        self.min_valid_fitness = min_valid_fitness
        self.majority_ratio = majority_ratio
        self.min_pass = min_pass
        self.retention_ratio = retention_ratio

    def perturbations(self, expression, fields_used, alt_fields=None,
                       max_perturbs=4):
        """生成扰动表达式列表 [(new_expr, label), ...]。

        顺序：窗口 +1、窗口 -1、ts_mean 平滑 5、语义字段替换。
        """
        perms = []
        seen = set()

        def add(new_expr, label):
            if new_expr and new_expr != expression and new_expr not in seen:
                seen.add(new_expr)
                perms.append((new_expr, label))

        for index, changed in enumerate(_relative_window_changes(expression)):
            add(changed, f"window-relative-{index + 1}")
        if "ts_mean(" not in expression:
            add(f"ts_mean({expression}, 5)", "smooth-ts-mean-5")
        if alt_fields:
            primary = fields_used[0] if fields_used else None
            if primary:
                nearby = _pick_nearby_field(primary, alt_fields)
                if nearby:
                    add(_swap_field(expression, primary, nearby),
                        f"field-swap->{nearby}")
        return perms[:max_perturbs]

    def build_perturbation_jobs(self, record, alt_fields=None, round_no=0):
        """生成带准确字段溯源的扰动实验列表。

        实际执行器由外层生产编排器注入；本模块只构造 jobs，不创建
        Simulator、checkpoint 或第二条 BRAIN 提交通道。
        """
        expression = record["expression"]
        fields_used = record.get("fields_used") or []
        perms = self.perturbations(expression, fields_used, alt_fields=alt_fields)
        known_fields = list(dict.fromkeys(
            [str(field) for field in fields_used if field]
            + [str(field.get("id")) for field in (alt_fields or [])
               if isinstance(field, dict) and field.get("id")]
            + [str(field) for field in (alt_fields or [])
               if not isinstance(field, dict) and field]
        ))
        specs = [
            (new_expr, label, dict(self.settings),
             "window_locality" if label.startswith("window-relative") else
             "semantic_field_swap" if label.startswith("field-swap") else "window_locality")
            for new_expr, label in perms
        ]
        if len(specs) < 4 and self.settings.get("universe"):
            altered = dict(self.settings)
            altered["universe"] = "TOP1000" if str(self.settings["universe"]).upper() != "TOP1000" else "TOP500"
            specs.append((record["expression"], "universe-robustness", altered, "universe_robustness"))
        if len(specs) < 4 and ("decay" in self.settings or "truncation" in self.settings):
            altered = dict(self.settings)
            if "decay" in altered:
                try:
                    altered["decay"] = max(1, round(float(altered["decay"]) * 1.25))
                except (TypeError, ValueError):
                    altered["decay"] = altered["decay"]
            else:
                try:
                    altered["truncation"] = round(float(altered["truncation"]) * 0.8, 4)
                except (TypeError, ValueError):
                    altered["truncation"] = altered["truncation"]
            specs.append((record["expression"], "decay-truncation", altered, "decay_truncation"))
        jobs = []
        for new_expr, label, settings, variable in specs[:4]:
            job = Experiment(
                round_no,
                record.get("hypothesis_id"),
                new_expr,
                settings,
                extract_fields(new_expr, known_fields),
                datasets=record.get("datasets") or [],
            )
            job.mutation = f"validation-{label}"
            job.changed_variable = variable
            jobs.append(job)
        return jobs, perms

    def decide(self, record, results):
        """综合稳健性判定。

        results: 每项 {expression, score, sharpe, turnover, checks_passed,
        error}。要求：多数扰动通过 + 中位分达标 + checks 通过 + 最低
        存活数。
        """
        if not results:
            return False, []
        original = score_of(record.get("metrics"))
        scores = [num(r.get("score")) for r in results]
        comparable_scores = [
            score if score is not None else float("-inf") for score in scores
        ]
        passed = [
            r for r, score in zip(results, comparable_scores)
            if score >= self.min_valid_fitness and r.get("checks_passed") is True
        ]
        total = len(results)
        need = max(self.min_pass, int(math.ceil(total * self.majority_ratio)))
        if len(passed) < need:
            return False, results

        ordered = sorted(comparable_scores)
        median_score = ordered[len(ordered) // 2]
        retention = (median_score / original) if original and original > 0 else 0.0
        retention_ok = True if (not original or original <= 0) else (
            retention >= self.retention_ratio
        )
        stable = (
            median_score >= self.min_valid_fitness
            and retention_ok
            and len(passed) >= self.min_pass
        )
        return stable, results

    def validate(self, record, alt_fields=None, round_no=0,
                 simulation_runner=None):
        """生成扰动并交给外层执行，再综合判定。

        ``simulation_runner`` 必须由外层生产编排器显式注入，负责在同一
        proposal/checkpoint/lock 边界内把 ``jobs`` 跑到终态。缺少 runner
        时 fail-closed，不直接调用 BRAIN。返回 (stable, detail)，detail
        含每项扰动结果与统计量。
        """
        jobs, perms = self.build_perturbation_jobs(
            record, alt_fields=alt_fields, round_no=round_no
        )
        if not jobs:
            return False, {"stable": False, "results": [], "reason": "no perturbations"}
        if simulation_runner is None:
            return False, {
                "stable": False,
                "results": [],
                "reason": "simulation_runner_required",
            }
        simulation_runner(jobs)
        results = []
        for exp in jobs:
            passed = checks_passed(exp.metrics) is True
            results.append(
                {
                    "expression": exp.expression,
                    "label": exp.mutation,
                    "score": score_of(exp.metrics) if exp.metrics else -1.0,
                    "sharpe": (exp.metrics or {}).get("sharpe"),
                    "turnover": (exp.metrics or {}).get("turnover"),
                    "checks_passed": passed,
                    "error": exp.error,
                }
            )
        stable, _ = self.decide(record, results)
        scores = [r["score"] for r in results]
        ordered = sorted(scores)
        median = ordered[len(ordered) // 2] if ordered else None
        return stable, {
            "stable": stable,
            "results": results,
            "median_score": median,
            "n_perturbations": len(results),
        }
