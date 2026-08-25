"""Frame-rate measurement based on unique published frame sequences."""

from __future__ import annotations

import time


class UniqueFrameRate:
    """Count each descriptor sequence once and calculate its received rate."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self._last_sequence: int | None = None
        self._started_at: float | None = None
        self._count = 0

    def add(self, sequence: int, now: float | None = None) -> bool:
        if sequence == self._last_sequence:
            return False
        self._last_sequence = sequence
        self._count += 1
        if self._started_at is None:
            self._started_at = time.monotonic() if now is None else now
        return True

    @property
    def count(self) -> int:
        return self._count

    def fps(self, now: float | None = None) -> float | None:
        if self._started_at is None:
            return None
        elapsed = (time.monotonic() if now is None else now) - self._started_at
        return self._count / elapsed if elapsed > 0 else None


__all__ = ["UniqueFrameRate"]
