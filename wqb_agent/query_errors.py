"""Shared query-shaping errors used across transport adapters and workflows."""

from __future__ import annotations


class QueryTooBroadError(Exception):
    """A query must be narrowed before it can be retried safely."""
