"""Small structured diagnostics object without replacing the print system."""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class DiagnosticEvent:
    code: str
    severity: str
    component: str
    message: str = ""

    def __post_init__(self):
        if str(self.severity).upper() not in {"INFO", "WARN", "ERROR"}:
            raise ValueError("诊断 severity 必须是 INFO/WARN/ERROR")

    def as_dict(self):
        return asdict(self)
