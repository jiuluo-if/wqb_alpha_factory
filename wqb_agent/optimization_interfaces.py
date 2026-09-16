"""优化 Agent 的窄接口：证据采集、指标诊断和经验记账。"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from .client import (
    WQBCorrelationPendingError,
    WQBNotFoundError,
)


@dataclass(frozen=True)
class OptimizationEvidenceSnapshot:
    """一个 Alpha 的真实只读证据快照；raw payload 仍由既有 owner 保存。"""

    alpha_id: str
    alpha_detail: Mapping[str, Any]
    aggregates: Mapping[str, Any]
    pnl: Any
    self_correlation: Mapping[str, Any] | None
    status: Mapping[str, str] = field(default_factory=dict)
    availability: Mapping[str, str] = field(default_factory=dict)


class OptimizationEvidenceProvider(Protocol):
    def collect(self, alpha_id: str) -> OptimizationEvidenceSnapshot: ...


class ClientOptimizationEvidenceProvider:
    """将唯一 WQBClient 投影成优化专用的只读接口。"""

    def __init__(self, client):
        self.client = client

    def collect(self, alpha_id: str) -> OptimizationEvidenceSnapshot:
        alpha_id = str(alpha_id).strip()
        if not alpha_id:
            raise ValueError("alpha_id must be non-empty")
        alpha_detail = self.client.get_alpha(alpha_id)
        if not isinstance(alpha_detail, Mapping):
            raise ValueError("alpha detail response malformed")
        slots = {
            "alpha_detail": alpha_detail,
            "aggregates": self._slot("aggregates", self.client.get_aggregates, alpha_id,
                                      allow_not_found=True),
            "pnl": self._slot("pnl", self.client.get_pnl, alpha_id),
            "self_correlation": self._slot("self_correlation", self.client.get_self_correlation,
                                             alpha_id, pending_unknown=True),
        }
        status = {name: self._slot_value(value, "status", "AVAILABLE") for name, value in slots.items()}
        availability = {name: self._slot_value(value, "availability", "AVAILABLE") for name, value in slots.items()}
        return OptimizationEvidenceSnapshot(
            alpha_id=alpha_id,
            alpha_detail=slots["alpha_detail"], aggregates=slots["aggregates"],
            pnl=slots["pnl"], self_correlation=slots["self_correlation"],
            status=status, availability=availability,
        )

    @staticmethod
    def _slot_value(value, key, default):
        if isinstance(value, Mapping):
            return str(value.get(key) or default).upper()
        return default

    @staticmethod
    def _slot(name, operation, alpha_id, *, allow_not_found=False, pending_unknown=False):
        try:
            value = operation(alpha_id)
            if pending_unknown and isinstance(value, Mapping) and str(value.get("status") or "").upper() in {
                "PENDING", "RUNNING", "QUEUED", "UNSETTLED",
            }:
                value = dict(value)
                value.update({"status": "UNKNOWN", "availability": "AVAILABLE",
                              "reason_code": "CORRELATION_PENDING"})
            return value
        except WQBCorrelationPendingError:
            return {"status": "UNKNOWN", "availability": "AVAILABLE",
                    "reason_code": "CORRELATION_PENDING", "slot": name}
        except WQBNotFoundError:
            if not allow_not_found:
                raise
            return {"status": "UNAVAILABLE", "availability": "UNAVAILABLE",
                    "slot": name, "reason_code": "CAPABILITY_UNAVAILABLE"}
        except (AttributeError, NotImplementedError) as exc:
            return {"status": "UNAVAILABLE", "availability": "UNAVAILABLE",
                    "slot": name, "reason": str(exc) or "capability unavailable"}
        except Exception as exc:
            if str(getattr(exc, "kind", "")).upper() in {"UNAVAILABLE", "CAPABILITY_UNAVAILABLE"}:
                return {"status": "UNAVAILABLE", "availability": "UNAVAILABLE",
                        "slot": name, "reason": str(exc) or "capability unavailable"}
            raise


class RemoteAlphaEvidenceProvider(ClientOptimizationEvidenceProvider):
    """Remote-first name for the live Alpha evidence read boundary."""

    def get_alpha(self, alpha_id):
        return self.client.get_alpha(str(alpha_id).strip())

    def get_alpha_evidence(self, alpha_id, *, live=True):
        if not live:
            raise ValueError("LIVE_EVIDENCE_REQUIRED")
        snapshot = self.collect(alpha_id)
        return {
            "alpha_id": snapshot.alpha_id,
            "source": "LIVE",
            "fetched_at": time.time(),
            "age_sec": 0.0,
            "alpha": dict(snapshot.alpha_detail),
            "aggregates": snapshot.aggregates,
            "pnl": snapshot.pnl,
            "self_correlation": snapshot.self_correlation,
            "status": dict(snapshot.status),
            "availability": dict(snapshot.availability),
        }

    def get_alpha_metrics(self, alpha_id):
        return self.get_alpha_evidence(alpha_id)["alpha"].get("is", {})

    def get_alpha_aggregates(self, alpha_id):
        return self.get_alpha_evidence(alpha_id)["aggregates"]

    def get_alpha_pnl(self, alpha_id):
        return self.get_alpha_evidence(alpha_id)["pnl"]

    def get_alpha_self_correlation(self, alpha_id):
        return self.get_alpha_evidence(alpha_id)["self_correlation"]

    def compare_alphas(self, alpha_ids):
        return {"source": "LIVE", "alphas": [
            self.get_alpha_evidence(item) for item in (alpha_ids or ())
        ]}


@dataclass(frozen=True)
class OptimizationTrial:
    """可压缩写入 ExperienceMemory 的 trial 结论，不替代 TrialLedger。"""

    parent_id: str
    outcome: str
    mechanism: str
    changed_variable: str
    evidence_refs: Sequence[str] = ()
    competing_explanations: Sequence[str] = ()


class OptimizationExperienceSink(Protocol):
    def add_short_term(self, kind, text, round_no, *, evidence=1, detail=None): ...


def diagnose_optimization(metrics: Mapping[str, Any]) -> dict[str, Any]:
    """返回 bounded hint，不替 Agent 做经济决策。"""
    if not isinstance(metrics, Mapping):
        return {"primary_problem": "MISSING_EVIDENCE", "recommended_focus": "STOP"}
    sharpe, turnover, returns = (_number(metrics.get(key))
                                 for key in ("sharpe", "turnover", "returns"))
    if sharpe is None or turnover is None or returns is None:
        return {"primary_problem": "MISSING_EVIDENCE", "recommended_focus": "RECONCILE"}
    if sharpe < 1.0:
        return {"primary_problem": "LOW_SHARPE", "recommended_focus": "SIGNAL_OR_MECHANISM"}
    if turnover > 0.125:
        return {"primary_problem": "HIGH_TURNOVER", "recommended_focus": "HORIZON_OR_SMOOTHING"}
    if returns <= 0:
        return {"primary_problem": "LOW_RETURN", "recommended_focus": "INFORMATION_DENSITY"}
    return {"primary_problem": "NO_CLEAR_HEADLINE_BLOCKER", "recommended_focus": "ROBUSTNESS"}


def record_optimization_trial(sink: OptimizationExperienceSink, trial: OptimizationTrial, *, round_no: int):
    """把成功、失败或剪枝 trial 投影到既有短期 memory owner。"""
    detail = {
        "optimization_trial": True,
        "parent_id": trial.parent_id,
        "outcome": str(trial.outcome).upper(),
        "mechanism": trial.mechanism,
        "changed_variable": trial.changed_variable,
        "evidence_refs": list(trial.evidence_refs),
        "competing_explanations": list(trial.competing_explanations),
    }
    return sink.add_short_term("observation", trial.mechanism, round_no,
                               evidence=1, detail=detail)


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
