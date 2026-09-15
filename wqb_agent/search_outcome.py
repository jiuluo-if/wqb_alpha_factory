"""ROLE: INTERNAL
AGENT_RELEVANCE: LOW
PURPOSE: Derive versioned research outcomes from existing evidence.
READ WHEN: changing reward or promotion semantics.
DO NOT USE FOR: platform truth, remote writes, or independent research policy.

Small, versioned research outcome used by SearchPolicy.

This module intentionally knows only the evidence needed to value one
experiment.  It does not submit work, read state, or interpret the complete
ValidationReport schema.
"""

import math
from dataclasses import dataclass

from .experiment import UNKNOWN_STATUSES, UNRESOLVED_STATUSES

REWARD_VERSION = "reward_v1"


def resolve_reward(final_settlement, simulation_settlement, legacy_fitness):
    """Recover one reward using final > provisional > legacy precedence."""
    if isinstance(final_settlement, dict) and final_settlement.get("reward") is not None:
        return {"reward": final_settlement.get("reward"),
                "reward_version": final_settlement.get("reward_version", REWARD_VERSION),
                "quality": "FINAL_EVIDENCE"}
    if isinstance(simulation_settlement, dict) and simulation_settlement.get("reward") is not None:
        return {"reward": simulation_settlement.get("reward"),
                "reward_version": simulation_settlement.get("reward_version", REWARD_VERSION),
                "quality": "PROVISIONAL_EVIDENCE"}
    return {"reward": legacy_fitness, "reward_version": REWARD_VERSION,
            "quality": "LEGACY_APPROXIMATE"}


def extract_statistical_decision(validation_report):
    """Read only the canonical nested ValidationReport evidence path."""
    if not isinstance(validation_report, dict):
        return None
    evidence = validation_report.get("statistical_evidence")
    if not isinstance(evidence, dict):
        return None
    decision = evidence.get("statistical_decision")
    return str(decision).upper() if decision is not None else None


def staged_promotion(*, base_quality, validation_status=None,
                     budget_used=0, budget_limit=None):
    """Return the next bounded research stage and its explicit stop reason."""
    if budget_limit is not None and int(budget_used) >= int(budget_limit):
        return {"stage": "STOP", "decision": "STOP", "reason": "stage budget exhausted"}
    quality = str(base_quality or "").upper()
    validation = str(validation_status or "").upper()
    if validation in {"STABLE", "PASS"}:
        return {"stage": "STABLE", "decision": "PROMOTE", "reason": "robustness validation passed"}
    if quality in {"PROMISING", "SUCCESS", "SUSPICIOUS_HIGH_SIGNAL"}:
        return {"stage": "EXPLOIT", "decision": "CONTINUE", "reason": "quality gate passed; target one perturbation"}
    if quality in {"BASELINE", ""}:
        return {"stage": "BASELINE", "decision": "CONTINUE", "reason": "awaiting baseline quality evidence"}
    return {"stage": "CANDIDATE", "decision": "STOP", "reason": "quality gate failed"}


def _value(record, key, default=None):
    if isinstance(record, dict):
        return record.get(key, default)
    return getattr(record, key, default)


def _metrics(record):
    value = _value(record, "metrics", {}) or {}
    return value if isinstance(value, dict) else {}


def _finite(value):
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _label(record, validation):
    status = str(_value(record, "status", "UNKNOWN") or "UNKNOWN").upper()
    if status in UNRESOLVED_STATUSES:
        return "UNRESOLVED"
    if status == "FAILED":
        return "FAILED"
    if isinstance(validation, dict) and validation.get("status") == "PASS":
        return "STABLE"
    if str(_value(record, "validation_status", "") or "").upper() == "STABLE":
        return "STABLE"
    metrics = _metrics(record)
    checks = metrics.get("checks") or []
    if checks and any(isinstance(row, dict) and row.get("pass") is False for row in checks):
        return "FAILED"
    quality = _value(record, "quality_label") or _value(record, "verdict")
    if isinstance(quality, dict):
        quality = quality.get("label")
    quality = str(quality or "").upper()
    if quality in {"PROMISING", "SUCCESS", "SUSPICIOUS_HIGH_SIGNAL"}:
        return quality
    return "BASELINE"


def parent_relative_delta(child, parent):
    """Return a bounded, direction-aware parent-relative quality delta.

    Sharpe and fitness improve upward; turnover and drawdown improve downward.
    Missing metrics are ignored, and no PnL or behavioral novelty is invented.
    """
    if parent is None:
        return None
    child_metrics, parent_metrics = _metrics(child), _metrics(parent)
    signed = []
    for key, direction in (("sharpe", 1.0), ("fitness", 1.0),
                           ("turnover", -1.0), ("drawdown", -1.0)):
        left, right = _finite(child_metrics.get(key)), _finite(parent_metrics.get(key))
        if left is None or right is None:
            continue
        scale = max(abs(right), 1.0)
        signed.append(direction * (left - right) / scale)
    if not signed:
        return None
    return max(-1.0, min(1.0, sum(signed) / len(signed)))


def reward_v1(*, status, infrastructure_failure, base_quality, robustness,
              statistical_decision=None, parent_delta=None):
    """Map explicit evidence stages to a finite research-value reward."""
    if infrastructure_failure or str(status or "").upper() in UNRESOLVED_STATUSES:
        return None
    if str(base_quality or "").upper() in {"FAILED", "FAIL", "UNRESOLVED"}:
        return 0.0

    reward = 0.25
    quality = str(base_quality or "").upper()
    if quality in {"PROMISING", "SUCCESS", "SUSPICIOUS_HIGH_SIGNAL"}:
        reward = 0.50
    if parent_delta is not None and parent_delta > 0:
        reward = max(reward, 0.70)
    if str(robustness or "").upper() in {"STABLE", "PASS"}:
        reward = max(reward, 0.85)
    if str(statistical_decision or "").upper() == "PASS":
        reward = max(reward, 1.0)
    return max(0.0, min(1.0, float(reward)))


def settle_search_outcome(provisional, *, validation_report=None,
                          incremental_decision=None, yearly_evidence=None,
                          platform_pass=None):
    """Replace a provisional observation with one final, evidence-backed value."""
    row = provisional.as_dict() if hasattr(provisional, "as_dict") else dict(provisional or {})
    report = validation_report if isinstance(validation_report, dict) else {}
    robustness = "PASS" if report.get("status") == "PASS" else row.get("robustness", "")
    statistical = extract_statistical_decision(report)
    base_quality = row.get("base_quality", "UNRESOLVED")
    reward = reward_v1(
        status="DONE" if row.get("evaluated") else "UNKNOWN",
        infrastructure_failure=bool(row.get("infrastructure_failure")),
        base_quality=base_quality,
        robustness=robustness,
        statistical_decision=statistical,
        parent_delta=row.get("parent_delta"),
    )
    row.update({
        "robustness": robustness,
        "statistical_decision": statistical,
        "incremental_decision": incremental_decision or "UNAVAILABLE",
        "yearly_coverage": (yearly_evidence or {}).get("coverage_status") if isinstance(yearly_evidence, dict) else None,
        "platform_pass": platform_pass,
        "reward": reward,
        "reward_quality": "FINAL_EVIDENCE",
        "outcome_kind": "FINAL",
    })
    return row


@dataclass(frozen=True)
class SearchOutcome:
    proposal_id: str
    arm: str
    research_role: str
    experiment_stage: str
    evaluated: bool
    infrastructure_failure: bool
    base_quality: str
    robustness: str
    novelty: float | None
    parent_delta: float | None
    reward: float | None
    reward_version: str = REWARD_VERSION
    reward_quality: str = "LEGACY_APPROXIMATE"

    @classmethod
    def from_experiment(cls, experiment, *, validation=None, parent=None,
                        quality_label=None):
        status = str(_value(experiment, "status", "UNKNOWN") or "UNKNOWN").upper()
        error = str(_value(experiment, "error", "") or "").upper()
        infrastructure_failure = status in UNKNOWN_STATUSES or any(
            token in error for token in ("AUTH", "RATE_LIMIT", "TIMEOUT", "INFRA", "NETWORK", "HTTP")
        )
        quality = quality_label or _label(experiment, validation)
        robustness = str(
            (validation or {}).get("status") if isinstance(validation, dict) else
            _value(experiment, "validation_status", "")
        ).upper()
        parent_delta = parent_relative_delta(experiment, parent)
        statistical = extract_statistical_decision(validation)
        reward = reward_v1(
            status=status,
            infrastructure_failure=infrastructure_failure,
            base_quality=quality,
            robustness=robustness,
            statistical_decision=statistical,
            parent_delta=parent_delta,
        )
        evaluated = reward is not None
        try:
            novelty = float(_value(experiment, "novelty_score"))
            if not math.isfinite(novelty):
                novelty = None
        except (TypeError, ValueError):
            novelty = None
        return cls(
            proposal_id=str(_value(experiment, "proposal_id") or _value(experiment, "allocation_key") or ""),
            arm=str(_value(experiment, "allocation_arm") or "unknown::unknown"),
            research_role=str(_value(experiment, "research_role") or "EXPLORE"),
            experiment_stage=str(_value(experiment, "experiment_stage") or "BASELINE"),
            evaluated=evaluated,
            infrastructure_failure=infrastructure_failure,
            base_quality=str(quality),
            robustness=robustness,
            novelty=novelty,
            parent_delta=parent_delta,
            reward=reward,
            reward_quality=("LEGACY_APPROXIMATE" if validation is None else "PROVISIONAL_EVIDENCE"),
        )

    def as_dict(self):
        return {
            "proposal_id": self.proposal_id,
            "arm": self.arm,
            "research_role": self.research_role,
            "experiment_stage": self.experiment_stage,
            "evaluated": self.evaluated,
            "infrastructure_failure": self.infrastructure_failure,
            "base_quality": self.base_quality,
            "robustness": self.robustness,
            "novelty": self.novelty,
            "parent_delta": self.parent_delta,
            "reward": self.reward,
            "reward_version": self.reward_version,
            "reward_quality": self.reward_quality,
            "outcome_kind": "PROVISIONAL",
        }
