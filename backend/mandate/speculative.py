"""Speculative cache for the mandate extractor (shadow listener).

The shadow listener sees the user's finalized words ~1 s before the agent's tool call carries them to /mandate/fields.
It may *prime* the extractor with exactly the prompt submit() would build; submit() then *takes* the primed result
instead of calling the LLM again. This is a pure cache of a read-only function: the extraction result still goes
through every check in submit() (verbatim quotes, hard rules, explicit-ness judge, recap + confirmation), and a key
built from a different utterance or a different stage question simply misses. Nothing is ever committed from here.
"""
from __future__ import annotations

import asyncio
import hashlib
import time
from collections import OrderedDict
from typing import Awaitable, Callable


class SpecCache:
    def __init__(self, ttl_s: float = 30.0, max_items: int = 8, now: Callable[[], float] = time.monotonic):
        self.ttl_s, self.max_items, self.now = ttl_s, max_items, now
        self._d: OrderedDict[str, tuple[float, asyncio.Task]] = OrderedDict()
        self.stats = {"primed": 0, "hits": 0, "misses": 0, "expired": 0, "saved_ms": []}

    @staticmethod
    def key(system: str, user: str) -> str:
        return hashlib.sha256(f"{system}\0{user}".encode()).hexdigest()

    def _trim(self) -> None:
        t = self.now()
        for k in [k for k, (at, _) in self._d.items() if t - at > self.ttl_s]:
            _, task = self._d.pop(k)
            task.cancel()
            self.stats["expired"] += 1
        while len(self._d) > self.max_items:
            _, (_, task) = self._d.popitem(last=False)
            task.cancel()

    def prime(self, system: str, user: str, call: Callable[[str, str], Awaitable[dict]]) -> bool:
        """Start the call in the background (once per key). Returns False if already primed."""
        self._trim()
        k = self.key(system, user)
        if k in self._d:
            return False
        done_at: list[float] = []

        async def run() -> dict:
            try:
                return await call(system, user)
            finally:
                done_at.append(self.now())
        task = asyncio.ensure_future(run())
        task.add_done_callback(lambda t: t.cancelled() or t.exception())   # never "exception was never retrieved"
        task.done_at = done_at  # type: ignore[attr-defined]
        self._d[k] = (self.now(), task)
        self.stats["primed"] += 1
        return True

    async def take(self, system: str, user: str) -> dict | None:
        """The primed result for exactly this prompt, or None (then the caller makes the normal call)."""
        self._trim()
        hit = self._d.pop(self.key(system, user), None)
        if hit is None:
            self.stats["misses"] += 1
            return None
        at, task = hit
        asked = self.now()
        try:
            out = await task
        except Exception:  # noqa: BLE001  a failed speculation is just a miss
            self.stats["misses"] += 1
            return None
        self.stats["hits"] += 1
        # the part of the call the user did not wait for: from priming until submit() asked (or until it finished)
        end = min(asked, task.done_at[0]) if task.done_at else asked  # type: ignore[attr-defined]
        self.stats["saved_ms"] = (self.stats["saved_ms"] + [round(1000 * (end - at))])[-20:]
        return out

    def clear(self) -> None:
        for _, task in self._d.values():
            task.cancel()
        self._d.clear()
