"""Research budget ledger: reserve -> settle / release; top-ups go through a research_topup_request event and need user approval.

All amounts are USDC base units (integers). Every ledger change is written back to JSON.
"""
from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

from backend.config import HARD_RULES

ATOMIC = 10**6
HARD_MAX_RESEARCH_ATOMIC = int(HARD_RULES["budget.research_usdc"][1] * ATOMIC)  # even after top-ups the system cap still applies


class BudgetExhausted(RuntimeError):
    def __init__(self, needed: int, remaining: int):
        super().__init__(f"research budget too low: this step needs ${needed / ATOMIC:.2f}, only ${remaining / ATOMIC:.2f} left")
        self.needed, self.remaining = needed, remaining


class TopupDeclined(RuntimeError):
    pass


@dataclass
class SpendEntry:
    ts: float
    seller: str
    url: str
    amount_atomic: int
    coin_mint: str
    reason: str
    status: str                 # settled | failed | unknown
    tx_hash: str | None = None
    network: str | None = None
    payer: str | None = None
    pay_to: str | None = None
    verified_onchain: bool | None = None

    @property
    def amount_usdc(self) -> float:
        return self.amount_atomic / ATOMIC


@dataclass
class TopupRecord:
    ts: float
    needed_atomic: int
    requested_atomic: int
    outcome: str                # approved | rejected | timeout | over_hard_cap
    approved_atomic: int = 0


Emit = Callable[[str, dict], None]
Approver = Callable[[dict], Awaitable[float | None]]  # returns the approved top-up in dollars; None / 0 = declined


@dataclass
class Ledger:
    budget_atomic: int
    per_call_cap_atomic: int
    path: Path | None = None
    entries: list[SpendEntry] = field(default_factory=list)
    topups: list[TopupRecord] = field(default_factory=list)
    reserved_atomic: int = 0

    @classmethod
    def from_mandate(cls, m, path: Path | None = None) -> "Ledger":
        b = m.body.budget
        led = cls(budget_atomic=round(b.research_usdc * ATOMIC), per_call_cap_atomic=round(b.research_per_call_usdc * ATOMIC), path=path)
        if path and path.exists():
            d = json.loads(path.read_text(encoding="utf-8"))
            led.budget_atomic = d["budget_atomic"]
            led.entries = [SpendEntry(**e) for e in d["entries"]]
            led.topups = [TopupRecord(**t) for t in d["topups"]]
        return led

    # ---- queries ----
    @property
    def spent_atomic(self) -> int:  # unknown counts as spent (err on spending less)
        return sum(e.amount_atomic for e in self.entries if e.status in ("settled", "unknown"))

    @property
    def remaining_atomic(self) -> int:
        return self.budget_atomic - self.spent_atomic - self.reserved_atomic

    def can_afford(self, amount: int) -> bool:
        return amount <= self.remaining_atomic

    # ---- reserve / settle ----
    def reserve(self, amount: int) -> None:
        if amount > self.per_call_cap_atomic:
            raise BudgetExhausted(amount, self.remaining_atomic)
        if amount > self.remaining_atomic:  # not via can_afford: the hook's check must not depend on the pre-check
            raise BudgetExhausted(amount, self.remaining_atomic)
        self.reserved_atomic += amount

    def release(self, amount: int) -> None:
        self.reserved_atomic = max(0, self.reserved_atomic - amount)

    def record(self, e: SpendEntry, reserved: int) -> None:
        self.release(reserved)
        self.entries.append(e)
        self.save()

    # ---- top-ups ----
    async def request_topup(self, needed: int, approver: Approver | None, emit: Emit | None = None,
                            timeout_s: float = 60.0, suggest_atomic: int = 500_000) -> int:
        """When the budget runs out, emit research_topup_request. Approved -> raise the budget and return the amount; declined / timeout -> raise TopupDeclined."""
        req = {"needed_usdc": needed / ATOMIC, "spent_usdc": self.spent_atomic / ATOMIC,
               "budget_usdc": self.budget_atomic / ATOMIC, "suggest_usdc": suggest_atomic / ATOMIC,
               "speak": f"Research budget used up ({self.spent_atomic / ATOMIC:.2f} dollars spent; next step needs {needed / ATOMIC:.2f}). How much should I add? For example, say \"add {suggest_atomic / ATOMIC:g} dollars\".",
               "panel": {"type": "research_topup", "spent": self.spent_atomic, "budget": self.budget_atomic, "needed": needed}}
        if emit:
            emit("research_topup_request", req)
        outcome, approved = "rejected", 0
        if approver is not None:
            try:
                usd = await asyncio.wait_for(approver(req), timeout_s)
                approved = round((usd or 0) * ATOMIC)
                outcome = "approved" if approved > 0 else "rejected"
            except asyncio.TimeoutError:
                outcome = "timeout"
        if outcome == "approved" and self.budget_atomic + approved > HARD_MAX_RESEARCH_ATOMIC:
            outcome, approved = "over_hard_cap", 0
        self.topups.append(TopupRecord(time.time(), needed, suggest_atomic, outcome, approved))
        if outcome != "approved":
            self.save()
            if emit:
                emit("research_topup_result", {"outcome": outcome, "speak": "OK, no top-up. Research stops at this step." if outcome == "rejected"
                                               else "No answer, so no top-up. Research stops at this step." if outcome == "timeout"
                                               else "That top-up would exceed the 5 dollar system cap, so nothing was added. Research stops at this step."})
            raise TopupDeclined(outcome)
        self.budget_atomic += approved
        self.save()
        if emit:
            emit("research_topup_result", {"outcome": "approved", "approved_usdc": approved / ATOMIC,
                                           "speak": f"Added {approved / ATOMIC:.2f} dollars to the research budget."})
        return approved

    # ---- persistence ----
    def as_dict(self) -> dict:
        return {"budget_atomic": self.budget_atomic, "per_call_cap_atomic": self.per_call_cap_atomic,
                "spent_atomic": self.spent_atomic, "entries": [asdict(e) for e in self.entries],
                "topups": [asdict(t) for t in self.topups]}

    def save(self) -> None:
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.as_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.path)
