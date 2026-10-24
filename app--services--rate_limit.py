from __future__ import annotations

import time
from collections import defaultdict, deque


class SlidingWindowLimiter:
    """In-process sliding-window limiter (one bot instance). Keys are typically Telegram user ids."""

    def __init__(self, limit: int, window_seconds: float) -> None:
        self.limit = limit
        self.window = window_seconds
        self._hits: dict[int, deque[float]] = defaultdict(deque)
        self._last_cleanup = time.monotonic()

    def allow(self, key: int) -> bool:
        now = time.monotonic()
        hits = self._hits[key]
        while hits and now - hits[0] > self.window:
            hits.popleft()
        if len(hits) >= self.limit:
            return False
        hits.append(now)
        self._cleanup(now)
        return True

    def _cleanup(self, now: float) -> None:
        if now - self._last_cleanup < self.window * 5:
            return
        self._last_cleanup = now
        for key in [k for k, v in self._hits.items() if not v or now - v[-1] > self.window]:
            del self._hits[key]
