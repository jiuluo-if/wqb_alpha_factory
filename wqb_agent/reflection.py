"""Reflection: classify results with a comprehensive quality gate, diagnose
FAILs, learn, update best, mark hypothesis outcomes, and generate next
experiments that carry the concrete fields/datasets.

Six metrics read from every real simulation (BRAIN definitions):

- Sharpe   = Avg. Annualized Returns / Annualized Std. Dev. of Returns
- Turnover = Value Traded / Value Held
- Fitness  = Sharpe * sqrt( Abs(Returns) / Max(Turnover, 0.125) )
- Returns  = annualized avg gain/loss (invested amount = half book size)
- Drawdown = largest reduction in PnL during the period (fraction)
- Margin   = PnL per dollar traded

The verdict is a comprehensive judgment: hard failures come from failed
submission checks, low Sharpe/Fitness, excessive turnover, non-positive
margin and too-deep drawdown; soft warnings (very low turnover, returns
sign mismatch) do not block SUCCESS on their own.
"""

import re

from .evidence import overlay_cached_checks
from .experiment import research_settlement_identity
from .failures import (
    classify_experiment,
    is_research_relevant,
)
from .metrics import check_pass, checks_passed, normalized_metrics, score_of
from .reflection_evaluation import (
    categorize_check,
    diagnosis_from,
    quality_gate,
)
from .reflection_learning import (
    direction_key,
    experiment_score,
    independence_blocker,
    interpretation,
)
from .research_guard import (
    is_direction_only_change,
    overfit_expression_reason,
    parameter_only_change_reason,
)

HYPOTHESIS_OUTCOMES = frozenset({"SUPPORTED", "CONTRADICTED", "INCONCLUSIVE"})
_LEARNING_METADATA_KEYS = (
    "unresolved_question", "competing_explanations",
    "next_discriminating_question", "evidence_needed",
)

# Failed-check names from BRAIN payloads -> diagnosis category.
CHECK_DIAGNOSIS = [
    (re.compile(r"sub[-_ ]?universe", re.I), "sub_universe"),
    (re.compile(r"self[-_ ]?correlation", re.I), "self_correlation"),
    (re.compile(r"concentration", re.I), "weight_concentration"),
    (re.compile(r"turnover", re.I), "turnover"),
    (re.compile(r"syntax", re.I), "syntax"),
    (re.compile(r"expression", re.I), "expression_logic"),
    (re.compile(r"coverage|nan|data", re.I), "data_coverage"),
]


class Reflector:
    def __init__(
        self,
        memory,
        success_sharpe=1.0,
        promising_sharpe=0.7,
        promising_fitness=0.5,
        success_fitness=1.0,
        min_turnover=0.01,
        max_turnover=1.5,
        max_drawdown=0.5,
        self_correlation_limit=0.5,
        evidence_cache=None,
    ):
        self.memory = memory
        self.success_sharpe = success_sharpe
        self.promising_sharpe = promising_sharpe
        self.promising_fitness = promising_fitness
        self.success_fitness = success_fitness
        self.min_turnover = min_turnover
        self.max_turnover = max_turnover
        self.max_drawdown = max_drawdown
        self.self_correlation_limit = self_correlation_limit
        # 平台证据缓存侧车（alpha_id -> 已结算 checks）；见 wqb_agent/evidence.py。
        self.evidence_cache = evidence_cache or {}

    @staticmethod
    def _settlement_key(exp, projection):
        return f"{research_settlement_identity(exp)}:{projection}"

    @staticmethod
    def _round_source_key(round_no, hypothesis, results, projection):
        identities = sorted(
            research_settlement_identity(result["experiment"])
            for result in results
        )
        return f"round:{round_no}:{hypothesis.get('id', '')}:{','.join(identities)}:{projection}"

    def reflect(self, round_no, hypothesis, experiments, validation_candidates=None):
        experiments = sorted(
            list(experiments or []),
            key=research_settlement_identity,
        )
        results = []
        for exp in experiments:
            verdict = self._classify(exp)
            results.append({"experiment": exp, "verdict": verdict})
            self._learn(round_no, hypothesis, exp, verdict)

        old_best_id = (self.memory.current_best or {}).get("id")
        best = self._update_best(results, validation_candidates=validation_candidates)
        self._update_lineages(round_no, results)
        confirmation = self.research_confirmation_view(hypothesis, results)
        hypothesis_outcome = self._mark_hypothesis_outcome(
            hypothesis, results, confirmation=confirmation,
            source_key=self._round_source_key(round_no, hypothesis, results, "hypothesis"),
        )
        self._record_mechanism_learning(
            round_no, hypothesis, results, hypothesis_outcome, confirmation=confirmation,
            source_key=self._round_source_key(round_no, hypothesis, results, "mechanism"),
        )
        self._generate_next(
            round_no, hypothesis, results,
            source_key=self._round_source_key(round_no, hypothesis, results, "next"),
        )
        self._recap(
            round_no, hypothesis, results, best, old_best_id,
            source_key=self._round_source_key(round_no, hypothesis, results, "recap"),
        )
        self.memory.expire_short_term(now_round=round_no)
        self.memory.updated_round = round_no
        self.memory.compress()
        self.memory.save()

        return self._summary(
            round_no, hypothesis, results, best,
            hypothesis_outcome=hypothesis_outcome,
        )

    # ----------------------------------------------------------- classify

    def _classify(self, exp):
        if exp.status in ("SKIPPED_STALE", "SKIPPED_UNKNOWN"):
            return {
                "label": "SKIPPED",
                "reason": "execution skipped after repeated read-only reconciliation; no research conclusion",
                "diagnosis": ["reconciliation_stale"],
                "kind": None,
                "notes": [],
            }
        if exp.status == "UNKNOWN":
            # 本地异常不证明 POST 未发生：先只读对账，不当作方向结论。
            return {
                "label": "FAIL",
                "reason": "UNKNOWN: requires read-only reconciliation before retry",
                "diagnosis": ["unknown"],
                "kind": None,
                "notes": [],
            }
        if exp.status == "FAILED":
            kind = classify_experiment(exp)
            return {
                "label": "FAIL",
                "reason": self._diagnose_error(exp),
                "diagnosis": ["runtime"],
                "kind": kind,
                "notes": [],
            }
        metrics = exp.metrics or {}
        # 平台证据缓存叠加：SELF_CORRELATION 等异步检查在模拟完成时通常仍为
        # PENDING；缓存侧车提供平台侧已结算结果，仅用于本判定视图。
        cached = (self.evidence_cache or {}).get(getattr(exp, "alpha_id", None))
        if cached:
            metrics = overlay_cached_checks(
                metrics, cached, self.self_correlation_limit
            )
        metrics = normalized_metrics(metrics)
        required_metrics = ("sharpe", "fitness", "turnover", "returns", "drawdown", "margin")
        checks = metrics.get("checks")
        malformed_checks = not (
            isinstance(checks, list) and checks
            and all(isinstance(check, dict) and check.get("name")
                    and check_pass(check) is not None for check in checks)
        )
        missing_metrics = [name for name in required_metrics if metrics.get(name) is None]
        if malformed_checks or missing_metrics:
            return {
                "label": "RECONCILE",
                "reason": "mandatory checks/metrics incomplete; promotion and research decision deferred",
                "diagnosis": (
                    (["missing_checks"] if malformed_checks else [])
                    + (["missing_metrics"] if missing_metrics else [])
                ),
                "kind": None,
                "notes": [],
            }
        sharpe = metrics.get("sharpe")
        if sharpe is None:
            return {"label": "FAIL", "reason": "missing sharpe metric",
                    "diagnosis": ["missing_metrics"], "kind": None, "notes": []}

        # 仅权重集中/极窄持仓才是噪声陷阱。健康检查还会报告
        # LOW_SUB_UNIVERSE_SHARPE；把后者误标为 weight_concentration 会污染
        # 后续的失败模式学习，因此它应继续走真实 checks 的诊断路径。
        health = getattr(exp, "health", None)
        health_reasons = (health or {}).get("reasons") or []
        concentrated = any(
            "concentrated_weight" in str(reason).lower()
            or "longcount=" in str(reason).lower()
            or "shortcount=" in str(reason).lower()
            for reason in health_reasons
        )
        if concentrated:
            return {
                "label": "FAIL",
                "reason": "NOISE_TRAP: " + "; ".join(health_reasons),
                "diagnosis": ["weight_concentration"],
                "notes": [],
            }

        fitness = metrics.get("fitness")
        if (sharpe is not None and sharpe > 3) or (fitness is not None and fitness > 8):
            return {
                "label": "SUSPICIOUS_HIGH_SIGNAL",
                "reason": "SUSPICIOUS_HIGH_SIGNAL: requires independent perturbation validation",
                "diagnosis": ["high_signal_unvalidated"],
                "notes": [],
            }

        hard, soft = self._quality_gate(metrics)
        if not hard:
            reason = " or ".join(soft) or "passed comprehensive gate"
            return {"label": "SUCCESS", "reason": reason,
                    "diagnosis": [], "notes": soft}

        reason = " or ".join(hard + soft) or "below quality gate"
        # 2026-08-22 用户政策：sharpe>promising_sharpe OR fitness>promising_fitness
        # 且换手在允许范围内（hard 中未含换手问题时）即视为有信号，可进入优化。
        signal = (sharpe is not None and sharpe > self.promising_sharpe) or (
            fitness is not None and fitness > self.promising_fitness
        )
        turnover_ok = not any("turnover" in h for h in hard)
        if signal and turnover_ok:
            return {"label": "PROMISING", "reason": reason,
                    "diagnosis": self._diagnosis_from(hard + soft), "notes": soft}
        return {"label": "FAIL", "reason": reason,
                "diagnosis": self._diagnosis_from(hard + soft), "notes": soft}

    def _quality_gate(self, metrics):
        """Comprehensive judgment over all six metrics + submission checks.
        Returns (hard_issues, soft_notes)."""
        return quality_gate(
            metrics, success_sharpe=self.success_sharpe,
            success_fitness=self.success_fitness, min_turnover=self.min_turnover,
            max_turnover=self.max_turnover, max_drawdown=self.max_drawdown,
        )

    @staticmethod
    def _diagnosis_from(issues):
        return diagnosis_from(issues, CHECK_DIAGNOSIS)

    @staticmethod
    def _categorize_check(name):
        return categorize_check(name, CHECK_DIAGNOSIS)

    def _diagnose_error(self, exp):
        error = exp.error or ""
        if "Simulation rejected" in error or "422" in error or "400" in error:
            return f"syntax/settings rejection: {error[:120]}"
        if "timed out" in error.lower():
            return "polling timed out"
        if "PLATFORM_ERROR" in error or "500" in error:
            return f"platform error (not an alpha-quality issue): {error[:120]}"
        return f"runtime error: {error[:120]}"

    # -------------------------------------------------------------- learn

    def _learn(self, round_no, hypothesis, exp, verdict):
        if exp.status in ("SKIPPED_STALE", "SKIPPED_UNKNOWN"):
            return
        if exp.status == "UNKNOWN":
            # 不确定结果不进入 lessons/avoid（不是方向结论），先入短期
            # 记忆待对账：只读核对后再决定晋升长期或移入垃圾。
            self.memory.forget_expression(exp.expression)
            self.memory.add_short_term(
                "pending",
                f"UNKNOWN experiment needs read-only reconciliation: "
                f"{exp.expression[:120]} (error={str(exp.error)[:80]})",
                round_no,
                evidence=1,
                detail="run reconciliation, then confirm_pending(verdict)",
                source_key=self._settlement_key(exp, "pending"),
            )
            return
        if verdict["label"] == "RECONCILE":
            self.memory.add_short_term(
                "pending",
                f"DONE experiment has incomplete mandatory evidence: {exp.expression[:120]}",
                round_no,
                evidence=1,
                detail="reconcile checks/metrics before any research decision",
                source_key=self._settlement_key(exp, "incomplete"),
            )
            return
        if exp.status == "FAILED":
            kind = classify_experiment(exp)
            if kind is None or not is_research_relevant(kind):
                # 系统级失败（auth/rate-limit/timeout/infra）或 UNKNOWN：
                # 不携带方向结论，禁止写入 avoid（避免污染研究记忆）。
                self.memory.forget_expression(exp.expression)
                self.memory.add_short_term(
                    "observation",
                    f"System-level failure (kind={kind}) not learned: "
                    f"{exp.expression[:80]} error={str(exp.error)[:80]}",
                    round_no,
                    evidence=1,
                    detail="environment issue, no direction signal",
                    source_key=self._settlement_key(exp, "system-failure"),
                )
                return
            self.memory.add_avoid(
                self._direction_key(exp),
                self._diagnose_error(exp),
                round_no,
                source_key=self._settlement_key(exp, "avoid-failure"),
            )
            return

        metrics = normalized_metrics(exp.metrics or {})
        fields = exp.fields_used
        field_label = ",".join(fields) if fields else "?"

        if verdict["label"] in ("SUCCESS", "PROMISING", "SUSPICIOUS_HIGH_SIGNAL"):
            # A single backtest is an observation, never a durable lesson or
            # a new champion. The concrete platform id makes it auditable.
            self.memory.add_short_term(
                "observation",
                f"{verdict['label']} alpha={exp.alpha_id or exp.id} fields=[{field_label}] "
                f"sharpe={metrics.get('sharpe')} fitness={metrics.get('fitness')}; "
                f"needs independent lineage confirmation before promotion.",
                round_no,
                evidence=1,
                detail={
                    "experiment_id": exp.id,
                    "alpha_id": exp.alpha_id,
                    "lineage": exp.hypothesis_id,
                },
                source_key=self._settlement_key(exp, "observation"),
            )
        else:
            self.memory.add_avoid(
                self._direction_key(exp),
                f"sharpe={metrics.get('sharpe')}, fitness={metrics.get('fitness')}, "
                f"turnover={metrics.get('turnover')}, drawdown={metrics.get('drawdown')}, "
                f"margin={metrics.get('margin')}; diagnosis={verdict.get('diagnosis')}",
                round_no,
                source_key=self._settlement_key(exp, "avoid-quality"),
            )

        turnover = metrics.get("turnover")
        if turnover is not None and turnover > self.max_turnover:
            self.memory.add_short_term(
                "observation",
                f"alpha={exp.alpha_id or exp.id} turnover={turnover:.2f} exceeds quality gate on [{field_label}]",
                round_no,
                evidence=1,
                detail={"experiment_id": exp.id, "lineage": exp.hypothesis_id},
                source_key=self._settlement_key(exp, "turnover"),
            )

    @staticmethod
    def _direction_key(exp):
        return direction_key(exp)

    # --------------------------------------------------------------- best

    @staticmethod
    def _exp_score(exp):
        """实验统一评分（复用 metrics.score_of，只算有指标的实验）。"""
        return experiment_score(exp)

    def _update_best(self, results, validation_candidates=None):
        done = [
            r for r in results
            if r["experiment"].metrics
            and r["verdict"]["label"] == "SUCCESS"
            and getattr(r["experiment"], "validation_status", None) == "STABLE"
            and isinstance(getattr(r["experiment"], "validation_report", None), dict)
            and r["experiment"].validation_report.get("status") == "PASS"
        ]
        for parent, report in validation_candidates or []:
            if (getattr(parent, "metrics", None)
                    and getattr(parent, "validation_status", None) == "STABLE"
                    and isinstance(report, dict) and report.get("status") == "PASS"):
                done.append({
                    "experiment": parent,
                    "verdict": {"label": "SUCCESS"},
                })
        if not done:
            current = self.memory.current_best
            metrics = (current or {}).get("metrics") or {}
            report = (current or {}).get("validation_report") or {}
            if (checks_passed(metrics) is True
                    and (current or {}).get("validation_status") == "STABLE"
                    and report.get("status") == "PASS"
                    and report.get("candidate") == "parent"):
                return current
            return None
        best_exp = max(done, key=lambda r: self._exp_score(r["experiment"]))["experiment"]
        best_score = self._exp_score(best_exp)
        current_score = None
        if self.memory.current_best and self.memory.current_best.get("metrics"):
            current_score = score_of(self.memory.current_best["metrics"])
        if current_score is None or best_score > current_score:
            self.memory.set_current_best(best_exp)
            return best_exp.to_dict()
        return self.memory.current_best

    # --------------------------------------------------------------- next

    def _update_lineages(self, round_no, results):
        """Convert resolved BRAIN evidence into bounded budget decisions."""
        for result in results:
            exp = result["experiment"]
            if exp.status != "DONE" or result["verdict"]["label"] == "RECONCILE":
                continue
            lineage_id = getattr(exp, "lineage_id", None) or exp.hypothesis_id
            label = result["verdict"]["label"]
            # 2026-08-22 用户政策：两次无增益 STOP / 三次无增益 KILL 的防过拟合
            # 纪律只适用于已达可提交标准（过硬质量门 SUCCESS）的候选；有信号但
            # 未达标（PROMISING）或未定论的谱系不因次数关闭，换优化变量类别继续。
            decision = self.memory.record_lineage_result(
                lineage_id, self._exp_score(exp), label, round_no,
                counts_toward_stop=(label == "SUCCESS"),
                source_key=self._settlement_key(exp, "lineage"),
                experiment_id=exp.id,
            )
            if decision in ("STOP", "KILL"):
                self.memory.add_short_term(
                    "observation",
                    f"{decision} lineage={lineage_id}: repeated resolved experiments "
                    "produced no material information gain.",
                    round_no,
                    evidence=1,
                    detail={"experiment_id": exp.id, "lineage": lineage_id},
                    source_key=self._settlement_key(exp, "lineage-decision"),
                )

    def _generate_next(self, round_no, hypothesis, results, source_key=None):
        """Persist only an Agent-authored, discriminating next experiment."""
        candidate = hypothesis.get("next_experiment")
        if not isinstance(candidate, dict):
            return
        required = (
            "change_reason", "unresolved_question",
            "next_discriminating_question",
        )
        if not all(
            isinstance(candidate.get(key), str) and candidate[key].strip()
            for key in required
        ):
            return
        competing = candidate.get("competing_explanations")
        evidence_needed = candidate.get("evidence_needed")
        if (
            not isinstance(competing, list)
            or len(competing) < 2
            or not all(isinstance(item, str) and item.strip() for item in competing)
            or not isinstance(evidence_needed, list)
            or not evidence_needed
            or not all(isinstance(item, str) and item.strip() for item in evidence_needed)
        ):
            return

        source = next(
            (result["experiment"] for result in results if result["experiment"].fields_used),
            results[0]["experiment"] if results else None,
        )
        if source is None:
            return
        parent_expression = candidate.get("parent_expression") or source.expression
        candidate_expression = candidate.get("expression")
        if isinstance(candidate_expression, str) and candidate_expression.strip():
            if parameter_only_change_reason(parent_expression, candidate_expression):
                return
            if is_direction_only_change(parent_expression, candidate_expression):
                return
            if overfit_expression_reason(candidate_expression):
                return

        fields = candidate.get("fields") or source.fields_used
        datasets = candidate.get("datasets") or source.datasets
        metadata = {
            "parent_hypothesis": hypothesis.get("id"),
            "parent_expression": parent_expression,
            "change_reason": candidate["change_reason"],
            "unresolved_question": candidate["unresolved_question"],
            "competing_explanations": list(competing),
            "next_discriminating_question": candidate["next_discriminating_question"],
            "evidence_needed": list(evidence_needed),
            "change_type": candidate.get("change_type"),
            "candidate_expression": candidate_expression,
        }
        self.memory.add_next(
            candidate.get("idea") or candidate["next_discriminating_question"],
            priority=candidate.get("priority", 4),
            source=round_no,
            round_no=round_no,
            fields=fields,
            datasets=datasets,
            metadata=metadata,
            source_key=source_key,
        )

    @staticmethod
    def _interpretation(hypothesis):
        return interpretation(hypothesis, HYPOTHESIS_OUTCOMES)

    def _metrics_view(self, exp):
        metrics = exp.metrics or {}
        cached = (self.evidence_cache or {}).get(getattr(exp, "alpha_id", None))
        if cached:
            metrics = overlay_cached_checks(
                metrics, cached, self.self_correlation_limit
            )
        return normalized_metrics(metrics)

    def _evidence_complete(self, result):
        exp = result["experiment"]
        label = result["verdict"]["label"]
        if exp.status != "DONE" or label == "RECONCILE":
            return False
        if label == "SUSPICIOUS_HIGH_SIGNAL":
            if not self._validation_confirmed(exp):
                return False
        metrics = self._metrics_view(exp)
        required = ("sharpe", "fitness", "turnover", "returns", "drawdown", "margin")
        if any(metrics.get(key) is None for key in required):
            return False
        if checks_passed(metrics) is not True:
            return False
        health = getattr(exp, "health", None)
        if isinstance(health, dict):
            reasons = [str(reason).lower() for reason in health.get("reasons") or []]
            if health.get("ok") is False or any(
                "concentrated_weight" in reason
                or "longcount=" in reason
                or "shortcount=" in reason
                for reason in reasons
            ):
                return False
        return True

    @staticmethod
    def _validation_confirmed(exp):
        report = getattr(exp, "validation_report", None)
        return (
            getattr(exp, "validation_status", None) == "STABLE"
            and isinstance(report, dict)
            and report.get("status") == "PASS"
            and report.get("candidate") == "parent"
        )

    @staticmethod
    def _lineage_id(exp):
        return str(getattr(exp, "lineage_id", None) or exp.hypothesis_id)

    @staticmethod
    def _independence_blocker(first, second):
        return independence_blocker(first, second)
        for exp in (first, second):
            parent = getattr(exp, "parent_expression", None)
            if not isinstance(parent, str) or not parent.strip():
                continue
            if parameter_only_change_reason(parent, exp.expression):
                return "parameter-only child is not independent evidence"
            if is_direction_only_change(parent, exp.expression):
                return "direction-only child is not independent evidence"
        return None

    def research_confirmation_view(self, hypothesis, results, interpretation=None):
        """Return one evidence ladder used by outcome and lesson learning."""
        view = {
            "eligible_results": [],
            "evidence_refs": [],
            "lineages": [],
            "independent_lineages": [],
            "confirmation_status": "UNCONFIRMED",
            "blocking_reasons": [],
        }
        interpretation = interpretation or self._interpretation(hypothesis)
        if not interpretation:
            view["blocking_reasons"].append("invalid Agent interpretation")
            return view
        refs = sorted(interpretation["evidence_refs"])
        if len(set(refs)) != len(refs):
            view["blocking_reasons"].append("duplicate evidence reference")
            return view

        by_identifier = {}
        for result in results:
            exp = result["experiment"]
            for value in (exp.id, exp.alpha_id, exp.proposal_id):
                if value is not None and str(value).strip():
                    by_identifier.setdefault(str(value), []).append(result)

        selected = []
        for ref in refs:
            matches = by_identifier.get(ref, [])
            if len(matches) != 1:
                view["blocking_reasons"].append(
                    f"evidence reference does not identify one experiment: {ref}"
                )
                continue
            selected.append(matches[0])
        if view["blocking_reasons"]:
            return view

        hypothesis_id = str(hypothesis.get("id") or "")
        for result in selected:
            exp = result["experiment"]
            if str(exp.hypothesis_id) != hypothesis_id:
                view["blocking_reasons"].append(
                    f"evidence belongs to another hypothesis: {exp.id}"
                )
                continue
            if not self._evidence_complete(result):
                view["blocking_reasons"].append(
                    f"mandatory evidence is incomplete or unresolved: {exp.id}"
                )
        if view["blocking_reasons"]:
            return view

        view["eligible_results"] = selected
        view["evidence_refs"] = list(refs)
        lineages = sorted({self._lineage_id(result["experiment"]) for result in selected})
        view["lineages"] = lineages
        if len(lineages) >= 2:
            for index, first in enumerate(selected):
                for second in selected[index + 1:]:
                    blocker = self._independence_blocker(
                        first["experiment"], second["experiment"]
                    )
                    if blocker:
                        view["blocking_reasons"].append(blocker)
                        break
                if view["blocking_reasons"]:
                    break
            if not view["blocking_reasons"]:
                view["independent_lineages"] = lineages
        if (
            not view["blocking_reasons"]
            and len(lineages) >= 2
        ) or (
            not view["blocking_reasons"]
            and len(selected) == 1
            and self._validation_confirmed(selected[0]["experiment"])
        ):
            view["confirmation_status"] = "INDEPENDENT_CONFIRMED"
        else:
            view["blocking_reasons"].append(
                "independent lineage or independent validation is missing"
            )
        return view

    def _mark_hypothesis_outcome(self, hypothesis, results, confirmation=None,
                                 source_key=None):
        hyp_id = hypothesis.get("id")
        if not hyp_id:
            return "INCONCLUSIVE"
        labels = [r["verdict"]["label"] for r in results]
        unresolved = any(
            result["experiment"].status in {"UNKNOWN", "SUBMIT_UNKNOWN", "PENDING", "RUNNING", "SUBMITTING"}
            or result["verdict"]["label"] == "RECONCILE"
            for result in results
        )
        if unresolved:
            legacy_verdict = "active"
        elif "SUCCESS" in labels:
            legacy_verdict = "success"
        elif "PROMISING" in labels:
            legacy_verdict = "promising"
        else:
            legacy_verdict = "failed"

        outcome = "INCONCLUSIVE"
        interpretation = self._interpretation(hypothesis)
        confirmation = confirmation or self.research_confirmation_view(
            hypothesis, results, interpretation
        )
        testable = (
            isinstance(hypothesis.get("statement"), str)
            and bool(hypothesis["statement"].strip())
        )
        if (
            confirmation["confirmation_status"] == "INDEPENDENT_CONFIRMED"
            and testable
            and interpretation
        ):
            requested = interpretation["outcome"]
            if requested == "SUPPORTED" and all(
                item["verdict"]["label"] in {"SUCCESS", "SUSPICIOUS_HIGH_SIGNAL"}
                for item in confirmation["eligible_results"]
            ):
                outcome = "SUPPORTED"
            elif (
                requested == "CONTRADICTED"
                and all(
                    item["verdict"]["label"] == "FAIL"
                    for item in confirmation["eligible_results"]
                )
                and isinstance(hypothesis.get("falsification"), str)
                and hypothesis["falsification"].strip()
                and interpretation.get("direct_relevance") is True
            ):
                outcome = "CONTRADICTED"
        reason = (
            "independent platform evidence confirmed Agent interpretation"
            if outcome in {"SUPPORTED", "CONTRADICTED"}
            else (confirmation["blocking_reasons"] or [
                "Agent interpretation remains unconfirmed"
            ])[0]
        )
        self.memory.mark_hypothesis(
            hyp_id, legacy_verdict, hypothesis.get("_round", 0), outcome=outcome,
            confirmation={
                "evidence_refs": confirmation["evidence_refs"],
                "lineages": confirmation["lineages"],
                "independent_lineages": confirmation["independent_lineages"],
                "confirmation_status": confirmation["confirmation_status"],
                "agent_interpretation": interpretation,
                "outcome_reason": reason,
            },
            source_key=source_key,
        )
        return outcome

    def _record_mechanism_learning(
        self, round_no, hypothesis, results, outcome, confirmation=None,
        source_key=None,
    ):
        interpretation = self._interpretation(hypothesis)
        if not interpretation:
            return
        confirmation = confirmation or self.research_confirmation_view(
            hypothesis, results, interpretation
        )
        if not confirmation["eligible_results"]:
            return
        if (
            outcome in {"SUPPORTED", "CONTRADICTED"}
            and confirmation["confirmation_status"] != "INDEPENDENT_CONFIRMED"
        ):
            return
        reason = (
            "independent platform evidence confirmed Agent interpretation"
            if outcome in {"SUPPORTED", "CONTRADICTED"}
            else (confirmation["blocking_reasons"] or [
                "Agent interpretation remains unconfirmed"
            ])[0]
        )
        for result in confirmation["eligible_results"]:
            source = result["experiment"]
            detail = {
                "hypothesis_outcome": outcome,
                "mechanism_learning": interpretation["mechanism_learning"],
                "evidence_refs": confirmation["evidence_refs"],
                "lineage": getattr(source, "lineage_id", None) or source.hypothesis_id,
                "lineages": confirmation["lineages"],
                "independent_lineages": confirmation["independent_lineages"],
                "confirmation_status": confirmation["confirmation_status"],
                "agent_interpretation": interpretation,
                "outcome_reason": reason,
            }
            for key in _LEARNING_METADATA_KEYS:
                if key in interpretation:
                    detail[key] = interpretation[key]
            self.memory.add_short_term(
                "observation", interpretation["mechanism_learning"], round_no,
                evidence=1, detail=detail,
                source_key=f"{source_key}:{self._settlement_key(source, 'mechanism')}" if source_key else self._settlement_key(source, "mechanism"),
            )

    # ------------------------------------------------------------ recap

    def _recap(self, round_no, hypothesis, results, best, old_best_id,
               source_key=None):
        """Write a short-term round recap — the working memory the model
        reads next round before deciding what to explore. Never a
        long-term claim: conclusions live in lessons/avoid; this is the
        ephemeral 'what just happened' view."""
        labels = {}
        for r in results:
            labels[r["verdict"]["label"]] = labels.get(r["verdict"]["label"], 0) + 1
        parts = [
            f"round {round_no}",
            f"hypothesis={hypothesis.get('statement', '')[:80]}",
            "verdicts=" + (",".join(f"{k}:{v}" for k, v in sorted(labels.items())) or "none"),
        ]
        top = None
        for r in results:
            m = r["experiment"].metrics or {}
            if not m:
                continue
            score = m.get("fitness") or m.get("sharpe") or -1
            if top is None or score > top[1]:
                top = (r["experiment"], score, m)
        if top:
            exp, _score, m = top
            parts.append(
                f"top sharpe={m.get('sharpe')} fitness={m.get('fitness')} "
                f"expr={exp.expression[:60]}"
            )
        self.memory.add_short_term(
            "recap", " | ".join(parts), round_no, source_key=source_key
        )

    # ------------------------------------------------------------ summary

    def _summary(self, round_no, hypothesis, results, best,
                 hypothesis_outcome=None):
        labels = {}
        for r in results:
            labels[r["verdict"]["label"]] = labels.get(r["verdict"]["label"], 0) + 1
        return {
            "round": round_no,
            "hypothesis": hypothesis.get("statement", ""),
            "hypothesis_outcome": hypothesis_outcome or "INCONCLUSIVE",
            "experiment_count": len(results),
            "verdicts": labels,
            "best": (
                {
                    "expression": best["expression"],
                    "sharpe": (best.get("metrics") or {}).get("sharpe"),
                    "fitness": (best.get("metrics") or {}).get("fitness"),
                    "drawdown": (best.get("metrics") or {}).get("drawdown"),
                }
                if best
                else None
            ),
        }
