"""Compatibility names for the remote Alpha color policy.

Remote Alpha evidence is the only color input.  This module intentionally has
no Experiment, Trajectory, filesystem, or ownership state; new callers should
use :mod:`wqb_agent.remote_colors` through ``research_api``.
"""

from __future__ import annotations

from collections.abc import Mapping

from .remote_colors import classify_remote_color


def classify_alpha_color(evidence):
    """Classify one remote evidence row without reading local research state."""
    return classify_remote_color(evidence)


def has_research_signal(evidence):
    """Return whether remote evidence received a usable color classification."""
    return isinstance(evidence, Mapping) and classify_remote_color(evidence) is not None


__all__ = ["classify_alpha_color", "has_research_signal"]
