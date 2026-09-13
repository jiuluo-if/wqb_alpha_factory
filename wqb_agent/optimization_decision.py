"""正式的 Agent Optimization Decision 契约（Phase III）。

Agent 负责经济判断（为什么改、改什么、预期改变什么、如何证伪）；Python 只做
确定性校验：字段完整性、parent identity、one-change rule、参数/方向/过拟合
拒绝、operator 合法性与既有 proposal contract 的 self-correlation 准入。

本模块不生成经济机制、不挑 operator、不写 child expression、不扫描参数、不写
研究状态，也不触发 Simulation。所有机会类别都只是从真实 evidence 派生的 Agent
context hint，不是自动 action。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field

from .expression import analyze_expression, expression_field_identifiers
from .metrics import check_pass, num
from .pre_correlation import metric_optimization_context
from .proposal_contract import CHILD_CHANGE_TYPES, validate_self_correlation_impact
from .research_guard import (
    is_direction_only_change,
    overfit_expression_reason,
    parameter_only_change_reason,
)

VALID_DECISIONS = ("CHILD", "VALIDATE", "REROUTE", "STOP")
CHILD_DECISION = "CHILD"
VALIDATE_DECISION = "VALIDATE"

# VALIDATE 只允许单变量、有界、平台已授权的研究变量；window / decay /
# truncation / universe 的变化不是新经济机制，只能进 ROBUSTNESS。
VALIDATION_VARIABLES = ("template_window", "decay", "truncation", "universe")
# settings 型单变量验证：parent 的真实当前值只能来自它自己的 settings，
# Agent 自报的 old_value 不是 provenance 来源。
SETTINGS_VALIDATION_VARIABLES = ("decay", "truncation", "universe")
VALIDATE_REQUIRED_TEXT_FIELDS = (
    "parent_id",
    "validation_variable",
    "expected_effect",
    "falsification",
    "reason",
)

# decay 有界邻域；truncation 只允许平台已授权 whitelist 内的相邻取值。
DECAY_MIN = 0
DECAY_MAX = 10
TRUNCATION_ALLOWED = (0.02, 0.04, 0.06, 0.08, 0.10, 0.12, 0.15)

# Legacy ``child_economic_hypothesis`` rows predate any identity requirement, so
# they are adapted with an explicit placeholder instead of inventing a real id.
LEGACY_PARENT_ID = "legacy-parent"

# CHILD 才需要完整经济机制；其余三类是"不再生成 child"的显式判断。
CHILD_REQUIRED_TEXT_FIELDS = (
    "parent_id",
    "economic_mechanism",
    "change_type",
    "changed_variable",
    "expression",
    "expected_effect",
    "falsification",
    "why_not_parameter_tuning",
)

OPPORTUNITY_CATEGORIES = (
    "CONCENTRATION_REPAIR",
    "SUB_UNIVERSE_REPAIR",
    "TURNOVER_REPAIR",
    "SELF_CORRELATION_REPAIR",
    "ROBUSTNESS_REPAIR",
    "SEMANTIC_REROUTE",
    "NO_CLEAR_OPPORTUNITY",
)

_NUMBER_RE = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)(?![\w.])")


def _text(value):
    return value.strip() if isinstance(value, str) else ""


def _transform(value):
    """direction_transform 复用 proposal contract 的 {applied, reason} 形态。"""
    if isinstance(value, Mapping):
        return dict(value)
    return _text(value)


@dataclass(frozen=True)
class OptimizationDecision:
    """Agent 已审阅一个 evidence-eligible parent 之后的显式优化决策。"""

    parent_id: str
    decision: str = "STOP"
    observed_evidence: str = ""
    economic_mechanism: str = ""
    change_type: str = ""
    changed_variable: str = ""
    expression: str = ""
    expected_effect: str = ""
    falsification: str = ""
    direction: str = ""
    direction_transform: Mapping = field(default_factory=dict)
    self_correlation_impact: Mapping = field(default_factory=dict)
    why_not_parameter_tuning: str = ""
    validation_variable: str = ""
    old_value: object = None
    new_value: object = None
    reason: str = ""
    external_evidence_refs: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self):
        decision = _text(self.decision).upper() or "STOP"
        if decision not in VALID_DECISIONS:
            raise ValueError(f"unknown optimization decision: {self.decision}")
        object.__setattr__(self, "decision", decision)
        if not _text(self.parent_id):
            raise ValueError("optimization decision requires parent_id")
        impact = self.self_correlation_impact or {}
        if not isinstance(impact, Mapping):
            raise ValueError("self_correlation_impact must be a mapping")
        object.__setattr__(self, "self_correlation_impact", dict(impact))
        object.__setattr__(
            self, "external_evidence_refs",
            tuple(str(value).strip() for value in (self.external_evidence_refs or ()) if str(value).strip()),
        )

    @property
    def is_child(self):
        return self.decision == CHILD_DECISION

    def missing_fields(self):
        """CHILD 决策尚未由 Agent 补齐的字段（其余决策为空）。"""
        if not self.is_child:
            return []
        return [
            name for name in CHILD_REQUIRED_TEXT_FIELDS
            if not _text(getattr(self, name, None))
        ]

    @property
    def is_validate(self):
        return self.decision == VALIDATE_DECISION

    def missing_validation_fields(self):
        """VALIDATE 决策尚未由 Agent 补齐的字段（其余决策为空）。"""
        if not self.is_validate:
            return []
        return [
            name for name in VALIDATE_REQUIRED_TEXT_FIELDS
            if not _text(getattr(self, name, None))
        ]

    def to_child_hypothesis(self):
        """适配既有 proposal contract；不发明任何未声明的经济内容。"""
        return {
            "observed_evidence": self.observed_evidence,
            "expression": self.expression,
            "economic_mechanism": self.economic_mechanism,
            "change_type": self.change_type,
            "changed_variable": self.changed_variable,
            "expected_effect": self.expected_effect,
            "falsification": self.falsification,
            "direction": self.direction,
            "direction_transform": (
                dict(self.direction_transform)
                if isinstance(self.direction_transform, Mapping)
                else self.direction_transform
            ),
            "self_correlation_impact": dict(self.self_correlation_impact or {}),
            "why_not_parameter_tuning": self.why_not_parameter_tuning,
            "external_evidence_refs": list(self.external_evidence_refs),
        }

    @classmethod
    def from_child_hypothesis(cls, parent_id, child, *, decision=CHILD_DECISION):
        """兼容适配：把早期的 ``child_economic_hypothesis`` dict 正式化。"""
        child = child if isinstance(child, Mapping) else {}
        return cls(
            parent_id=_text(parent_id) or LEGACY_PARENT_ID,
            decision=decision,
            observed_evidence=_text(child.get("observed_evidence")),
            economic_mechanism=_text(child.get("economic_mechanism")),
            change_type=_text(child.get("change_type")),
            changed_variable=_text(child.get("changed_variable")),
            expression=_text(child.get("expression")),
            expected_effect=_text(child.get("expected_effect")),
            falsification=_text(child.get("falsification")),
            direction=_text(child.get("direction")),
            direction_transform=_transform(child.get("direction_transform")),
            self_correlation_impact=dict(child.get("self_correlation_impact") or {}),
            why_not_parameter_tuning=_text(child.get("why_not_parameter_tuning")),
        )

    @classmethod
    def from_mapping(cls, payload):
        if not isinstance(payload, Mapping):
            raise TypeError("optimization decision must be an object")
        return cls(
            parent_id=str(payload.get("parent_id") or ""),
            decision=payload.get("decision") or "STOP",
            observed_evidence=_text(payload.get("observed_evidence")),
            economic_mechanism=_text(payload.get("economic_mechanism")),
            change_type=_text(payload.get("change_type")),
            changed_variable=_text(payload.get("changed_variable")),
            expression=_text(payload.get("expression")),
            expected_effect=_text(payload.get("expected_effect")),
            falsification=_text(payload.get("falsification")),
            direction=_text(payload.get("direction")),
            direction_transform=_transform(payload.get("direction_transform")),
            self_correlation_impact=dict(payload.get("self_correlation_impact") or {}),
            why_not_parameter_tuning=_text(payload.get("why_not_parameter_tuning")),
            validation_variable=_text(payload.get("validation_variable")),
            old_value=payload.get("old_value"),
            new_value=payload.get("new_value"),
            reason=_text(payload.get("reason")),
            external_evidence_refs=tuple(payload.get("external_evidence_refs") or ()),
        )

    def as_dict(self):
        payload = self.to_child_hypothesis()
        payload["parent_id"] = self.parent_id
        payload["decision"] = self.decision
        payload["validation_variable"] = self.validation_variable
        payload["old_value"] = self.old_value
        payload["new_value"] = self.new_value
        payload["reason"] = self.reason
        return payload


def optimization_decision_identity(decision):
    """Return the stable identity shared by workflow, ledger, and inbox."""
    payload = decision.as_dict() if hasattr(decision, "as_dict") else dict(decision or {})
    semantic = {
        "parent_id": payload.get("parent_id"),
        "decision": str(payload.get("decision") or "STOP").upper(),
        "fields": {key: payload.get(key) for key in (
            "economic_mechanism", "change_type", "changed_variable",
            "expression", "expected_effect", "falsification", "direction",
            "direction_transform", "self_correlation_impact", "validation_variable",
            "old_value", "new_value", "reason",
        )},
    }
    return "optimization-selection|" + hashlib.sha256(
        json.dumps(semantic, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    ).hexdigest()


def _structure(expression):
    analysis = analyze_expression(expression)
    return {
        "operators": set(analysis.operators),
        "fields": {value.casefold() for value in expression_field_identifiers(analysis)},
        "numbers": set(_NUMBER_RE.findall(str(expression or ""))),
    }


def declared_change_reasons(parent_expression, child_expression):
    """最小 declared-change audit：捕捉"改名一个变量、实为多处改动"。"""
    if not _text(parent_expression) or not _text(child_expression):
        return []
    parent = _structure(parent_expression)
    child = _structure(child_expression)
    field_delta = parent["fields"] ^ child["fields"]
    number_delta = parent["numbers"] ^ child["numbers"]
    reasons = []
    if len(field_delta) > 1:
        reasons.append("DECLARED_CHANGE_MULTIPLE_FIELDS")
    if field_delta and number_delta:
        reasons.append("DECLARED_CHANGE_FIELD_AND_PARAMETER")
    return reasons


def decision_rejections(decision, parent, *, allowed_operators=None):
    """对一个 OptimizationDecision 做确定性校验，返回拒绝原因列表。"""
    if not isinstance(decision, OptimizationDecision):
        return ["DECISION_INVALID"]
    record = parent.to_dict() if hasattr(parent, "to_dict") else parent
    if not isinstance(record, Mapping):
        return ["PARENT_INVALID"]
    reasons = []
    if str(record.get("id") or "") != str(decision.parent_id or ""):
        reasons.append("PARENT_IDENTITY_MISMATCH")
    if not decision.is_child:
        return reasons
    if decision.missing_fields():
        reasons.append("DECISION_FIELDS_MISSING")
    change_type = _text(decision.change_type)
    if change_type and change_type not in CHILD_CHANGE_TYPES:
        reasons.append("CHANGE_TYPE_NOT_IN_PROPOSAL_CONTRACT")
    transform = decision.direction_transform
    if (not isinstance(transform, Mapping)
            or not isinstance(transform.get("applied"), bool)
            or not _text(transform.get("reason"))):
        reasons.append("DIRECTION_TRANSFORM_INVALID")
    parent_expression = _text(record.get("expression"))
    child_expression = _text(decision.expression)
    if parent_expression and child_expression:
        if parameter_only_change_reason(parent_expression, child_expression):
            reasons.append("PARAMETER_ONLY_CHANGE")
        if is_direction_only_change(parent_expression, child_expression):
            reasons.append("DIRECTION_ONLY_CHANGE")
        reasons.extend(declared_change_reasons(parent_expression, child_expression))
        if overfit_expression_reason(child_expression):
            reasons.append("OVERFIT_EXPRESSION")
    if validate_self_correlation_impact(dict(decision.self_correlation_impact or {})):
        reasons.append("SELF_CORRELATION_IMPACT_INVALID")
    if allowed_operators is not None and child_expression:
        operators = set(analyze_expression(child_expression).operators)
        if not operators.issubset({str(value).lower() for value in allowed_operators}):
            reasons.append("OPERATOR_ILLEGAL")
    return reasons


def _scalar_number(value):
    """有限数值；bool / 非数值返回 None。"""
    if isinstance(value, bool) or value is None:
        return None
    number = num(value)
    return number


def _as_int(value):
    number = _scalar_number(value)
    if number is None or not float(number).is_integer():
        return None
    return int(number)


def _values_equal(left, right):
    left_number = _scalar_number(left)
    right_number = _scalar_number(right)
    if left_number is None or right_number is None:
        return str(left) == str(right)
    return left_number == right_number


def parent_setting_value(parent, variable):
    """parent 记录的真实当前取值；无法确认时返回 ``None``（不猜、不造）。"""
    record = parent.to_dict() if hasattr(parent, "to_dict") else parent
    if not isinstance(record, Mapping):
        return None
    settings = record.get("settings")
    if not isinstance(settings, Mapping):
        return None
    name = _text(variable)
    if not name or name not in settings:
        return None
    return settings.get(name)


def validation_candidate_values(variable, *, current=None, allowed_universes=()):
    """Python 给出的合法有界候选值；绝不生成笛卡尔积或参数全扫描。

    Agent 只选择"验证哪个变量"；具体数值必须来自这个 bounded pool，不能由
    LLM 自由书写（例如 ``decay=73`` / ``window=937``）。
    """
    name = str(variable or "")
    if name == "decay":
        base = _as_int(current)
        if base is None:
            return ()
        return tuple(
            value for value in (base - 1, base + 1)
            if DECAY_MIN <= value <= DECAY_MAX
        )
    if name == "truncation":
        base = _scalar_number(current)
        if base is None:
            return ()
        below = [value for value in TRUNCATION_ALLOWED if value < base]
        above = [value for value in TRUNCATION_ALLOWED if value > base]
        candidates = []
        if below:
            candidates.append(below[-1])
        if above:
            candidates.append(above[0])
        return tuple(candidates)
    if name == "universe":
        return tuple(
            str(value) for value in (allowed_universes or ())
            if isinstance(value, str) and value.strip()
            and str(value) != str(current)
        )
    # template_window 的合法值由模板自己声明的 numeric slot 提供。
    return ()


def validation_rejections(decision, parent, *, allowed_values=None,
                          allow_universe=False):
    """VALIDATE 单变量 contract 的确定性校验（fail-closed）。"""
    if not isinstance(decision, OptimizationDecision):
        return ["DECISION_INVALID"]
    record = parent.to_dict() if hasattr(parent, "to_dict") else parent
    if not isinstance(record, Mapping):
        return ["PARENT_INVALID"]
    if str(record.get("id") or "") != str(decision.parent_id or ""):
        return ["PARENT_IDENTITY_MISMATCH"]
    if not decision.is_validate:
        return ["NOT_A_VALIDATE_DECISION"]
    reasons = []
    if decision.missing_validation_fields():
        reasons.append("VALIDATION_FIELDS_MISSING")
    variable = _text(decision.validation_variable)
    if variable and variable not in VALIDATION_VARIABLES:
        reasons.append("VALIDATION_VARIABLE_UNKNOWN")
    new_value = decision.new_value
    if isinstance(new_value, Mapping):
        keys = {str(key) for key in new_value}
        if keys != {variable}:
            reasons.append("VALIDATION_MULTIPLE_VARIABLES")
        else:
            new_value = new_value[variable]
    elif isinstance(new_value, (list, tuple, set)):
        reasons.append("VALIDATION_MULTIPLE_VARIABLES")
    if variable == "universe" and not allow_universe:
        reasons.append("VALIDATION_UNIVERSE_NOT_JUSTIFIED")
    if variable in SETTINGS_VALIDATION_VARIABLES:
        current = parent_setting_value(record, variable)
        if current is not None and not _values_equal(decision.old_value, current):
            # provenance 只承认 parent 自己记录的真实取值，不相信 Agent 自报。
            reasons.append("VALIDATION_OLD_VALUE_MISMATCH")
    if allowed_values is not None and variable and not reasons:
        if not any(_values_equal(new_value, value) for value in allowed_values):
            reasons.append("VALIDATION_NEW_VALUE_OUT_OF_POOL")
    return reasons


def numeric_variant_provenance(decision, *, source_template=None, slot=None,
                               parent_default=None, candidate=None):
    """参数变化必须自带 provenance；不靠 diff 猜（§30）。"""
    return {
        "source_template": source_template,
        "slot": slot or _text(getattr(decision, "validation_variable", "")),
        "parent_default_value": parent_default,
        "candidate_value": candidate,
        "change_count": 1,
        "reason": _text(getattr(decision, "reason", "")),
    }


def _checks(record):
    metrics = record.get("metrics") if isinstance(record, Mapping) else None
    checks = metrics.get("checks") if isinstance(metrics, Mapping) else None
    return checks if isinstance(checks, list) else []


def _check_names(record, *, failing_only, tokens):
    names = []
    for check in _checks(record):
        if not isinstance(check, Mapping):
            continue
        name = str(check.get("name") or "UNKNOWN_CHECK").upper()
        passed = check_pass(check)
        if failing_only and passed is not False:
            continue
        if tokens and not any(token in name for token in tokens):
            continue
        names.append(name)
    return names


def parent_opportunity(record):
    """从真实 evidence 派生的优化机会提示；证据不足时明确 NO_CLEAR_OPPORTUNITY。"""
    if not isinstance(record, Mapping):
        return "NO_CLEAR_OPPORTUNITY"
    self_correlation = record.get("self_correlation")
    status = ""
    if isinstance(self_correlation, Mapping):
        status = str(self_correlation.get("status") or "").upper()
    if status in {"FAIL", "FAILED", "BLOCK", "HIGHER"}:
        return "SELF_CORRELATION_REPAIR"
    if _check_names(record, failing_only=True, tokens=("CONCENTRAT", "CONCENTRATION")):
        return "CONCENTRATION_REPAIR"
    if _check_names(record, failing_only=True, tokens=("SUB_UNIVERSE", "SUBUNIVERSE", "UNIVERSE")):
        return "SUB_UNIVERSE_REPAIR"
    if _check_names(record, failing_only=True, tokens=("TURNOVER",)):
        return "TURNOVER_REPAIR"
    report = record.get("validation_report")
    if isinstance(report, Mapping) and str(report.get("status") or "").upper() not in {"", "PASS"}:
        return "ROBUSTNESS_REPAIR"
    classification = record.get("research_classification")
    label = ""
    if isinstance(classification, Mapping):
        label = str(classification.get("label") or "").upper()
    elif isinstance(classification, str):
        label = classification.upper()
    if label in {"LOW_INFORMATION", "SEMANTIC_EXHAUSTED", "NO_INFORMATION_GAIN"}:
        return "SEMANTIC_REROUTE"
    return "NO_CLEAR_OPPORTUNITY"


def summarize_parent(record, *, mechanism_state=None, opportunity=None,
                     delay=None, quality_policy=None):
    """有限 parent evidence summary；不复制完整 metrics/checks 历史。"""
    if isinstance(record, Mapping):
        get = record.get
    else:
        get = lambda key, default=None: getattr(record, key, default)  # noqa: E731
    metrics = get("metrics") if isinstance(get("metrics"), Mapping) else {}
    health = get("health")
    if delay is None:
        settings = get("settings")
        if isinstance(settings, Mapping) and "delay" in settings:
            delay = settings.get("delay")
        else:
            delay = get("delay")
    summary = {
        "parent_id": get("id"),
        "expression": get("expression"),
        "mechanism": get("economic_mechanism"),
        "change_type": get("change_type"),
        "fields": list(get("fields_used") or []),
        "datasets": list(get("datasets") or []),
        "metrics": {
            key: metrics.get(key)
            for key in ("sharpe", "fitness", "turnover", "margin", "returns")
        },
        "failed_checks": _check_names(
            {"metrics": metrics}, failing_only=True, tokens=()
        ),
        "pending_checks": [
            str(check.get("name") or "UNKNOWN_CHECK").upper()
            for check in (metrics.get("checks") or [])
            if isinstance(check, Mapping) and check_pass(check) is None
        ],
        "self_correlation_status": (
            str((get("self_correlation") or {}).get("status") or "UNKNOWN").upper()
            if isinstance(get("self_correlation"), Mapping) else "UNKNOWN"
        ),
        "validation_status": get("validation_status") or "UNKNOWN",
        "incremental_status": (
            str((get("incremental_evidence") or {}).get("decision") or "UNKNOWN").upper()
            if isinstance(get("incremental_evidence"), Mapping) else "UNKNOWN"
        ),
        "research_classification": get("research_classification"),
        "mechanism_state": mechanism_state or "UNKNOWN",
        "opportunity": opportunity or parent_opportunity(record),
        "metric_optimization_context": metric_optimization_context(
            metrics, delay=delay, quality_policy=quality_policy, health=health
        ),
    }
    return summary
