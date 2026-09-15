"""Bounded Inner-Agent context assembled from pure projections."""

from __future__ import annotations

from collections.abc import Mapping

from .research_cursor import cycle_projection


def build_research_context(*, runtime_context, cursor, quality_summaries=(),
                           optimizer_context=None, capabilities=None,
                           constraints=None, unresolved_gaps=(), limit=8):
    """Return a bounded, non-raw handoff surface for the Inner Agent."""
    bounded_quality = list(quality_summaries or ())[:max(1, min(int(limit), 8))]
    return {
        "runtime_state": (runtime_context or {}).get("runtime_state", "UNKNOWN"),
        "capabilities": dict(capabilities or {}),
        "research_cursor": (cursor or {}).get("research_cursor"),
        "quality_summaries": [
            item.as_dict() if hasattr(item, "as_dict") else dict(item)
            for item in bounded_quality
        ],
        "optimizer_context": optimizer_context if isinstance(optimizer_context, Mapping) else {},
        "unresolved_evidence_gaps": sorted({str(item) for item in (unresolved_gaps or ())})[:16],
        "constraints": dict(constraints or {"max_child": 4, "max_validate": 4}),
        "allowed_research_actions": [
            "INSPECT", "DISCOVER", "AUTHOR_DECISION", "REQUEST_MATERIALIZATION",
        ],
    }


def project_cycle(rows, cycle_id):
    """Keep cycle mapping behind the same pure projection boundary."""
    return cycle_projection(rows, cycle_id)


__all__ = ["build_research_context", "project_cycle"]
