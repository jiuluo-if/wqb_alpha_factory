"""Transient, throttled progress reporting for long-running workflows."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping


class HeartbeatSink:
    """Emit aggregate stage progress without owning research state."""

    def __init__(self, *, emit: Callable[..., object] | None = None,
                 clock: Callable[[], float] | None = None,
                 interval_sec: float = 20.0):
        try:
            interval = float(interval_sec)
        except (TypeError, ValueError):
            interval = 20.0
        self.interval_sec = max(1.0, interval)
        self._emit = emit or self._print_event
        self._clock = clock or time.time
        self._stage = None
        self._stage_started_at = None
        self._last_emit_at = None
        self._last_progress: Mapping[str, object] = {}

    @staticmethod
    def _print_event(stage, **metadata):
        summary = " ".join(
            f"{key}={value}" for key, value in metadata.items()
            if value is not None
        )
        print(f"[HEARTBEAT] {stage} {summary}".rstrip())

    def emit_stage(self, stage: str, **metadata) -> bool:
        now = float(self._clock())
        stage = str(stage)
        progress = {
            key: value for key, value in metadata.items()
            if key not in {
                "elapsed_sec", "last_progress_at",
            }
        }
        stage_changed = stage != self._stage
        meaningful_progress = progress != self._last_progress
        due = self._last_emit_at is None or now - self._last_emit_at >= self.interval_sec
        if not (stage_changed or meaningful_progress or due):
            return False
        if stage_changed or self._stage_started_at is None:
            self._stage_started_at = now
        payload = dict(metadata)
        payload.setdefault("elapsed_sec", max(0.0, now - self._stage_started_at))
        self._emit(stage, **payload)
        self._stage = stage
        self._last_emit_at = now
        self._last_progress = progress
        return True
