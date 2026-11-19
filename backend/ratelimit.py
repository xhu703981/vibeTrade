"""Simple async rate limiter (one instance per endpoint)."""
from __future__ import annotations

import asyncio
import time


class RateLimiter:
    def __init__(self, per_minute: float):
        self.interval = 60.0 / per_minute
        self._last = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            delay = self._last + self.interval - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            self._last = time.monotonic()

    def try_acquire(self) -> bool:
        """Take a slot only if one is free right now (never waits). Used for optional extras like a live price tick."""
        if self._lock.locked():
            return False
        now = time.monotonic()
        if now - self._last < self.interval:
            return False
        self._last = now
        return True
