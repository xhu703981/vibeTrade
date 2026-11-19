"""In-process event bus. Each event carries speak (code-templated, no external text) and panel (numbers + sanitised symbol)."""
from __future__ import annotations

import asyncio
import itertools
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field

EVENT_TYPES = {"session_reset", "amend_confirm_needed", "derisk_offer", "redteam_ready", "stop_suggested", "mandate_frozen", "candidates_found", "free_check_result", "research_payment", "research_topup_request",
               "research_topup_result", "card_created", "card_expired", "card_rejected", "card_voided", "approval_rejected",
               "guard_blocked", "trade_executed", "trade_failed", "exit_triggered", "sell_failed", "price_feed_down",
               "price_feed_restored", "emergency_exit",
               # M6: UI sync / pipeline progress (the frontend does not broadcast events with an empty speak)
               "mandate_updated", "pipeline_status", "research_done", "voice_rejected",
               # rehearsal-only price tests
               "price_test",
               # living session: selection desk, multi-coin orders, amended mandates and new research rounds
               "desk_ready", "cards_ready", "coins_skipped", "mandate_amended", "round_started", "amend_opened", "amend_cancelled",
               # duplex: background work (fast-returning tools) and interruptible research rounds
               "research_stopped", "cards_building", "task_failed",
               "position_alert"}


@dataclass
class Event:
    type: str
    speak: str
    panel: dict = field(default_factory=dict)
    ts: float = field(default_factory=time.time)
    loud: bool = False   # e.g. sell failures: broadcast loudly
    id: int = 0          # monotonically increasing; used by the frontend for dedupe / replay

    def as_dict(self) -> dict:
        return asdict(self)


class EventBus:
    def __init__(self, keep: int = 500):
        self.history: list[Event] = []
        self._subs: list[Callable[[Event], None]] = []
        self._queues: list[asyncio.Queue] = []
        self.keep = keep
        self._seq = itertools.count(1)

    def emit(self, type_: str, speak: str, panel: dict | None = None, loud: bool = False) -> Event:
        if type_ not in EVENT_TYPES:
            raise ValueError(f"unknown event type {type_}")
        ev = Event(type_, speak, panel or {}, loud=loud, id=next(self._seq))
        self.history.append(ev)
        del self.history[:-self.keep]
        for s in list(self._subs):
            s(ev)
        for q in list(self._queues):
            q.put_nowait(ev)
        return ev

    def new_session(self) -> Event:
        """Forget earlier sessions: history is cleared (a reconnect replays nothing old) and connected
        browsers get a session_reset event that empties their Events panel."""
        self.history.clear()
        return self.emit("session_reset", "", {})

    def subscribe(self, fn: Callable[[Event], None]) -> None:
        self._subs.append(fn)

    def queue(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._queues.append(q)
        return q

    def unqueue(self, q: asyncio.Queue) -> None:
        if q in self._queues:
            self._queues.remove(q)

    def of(self, type_: str) -> list[Event]:
        return [e for e in self.history if e.type == type_]
