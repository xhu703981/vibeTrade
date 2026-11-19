"""Combine the verdict: block / ask_human / allow.

Order: any rule-layer BLOCK -> block (final, JEV is not called);
otherwise optional JEV: P(risk) >= threshold or a JEV error -> ask_human; otherwise allow.
JEV can only ever tighten.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from backend.guard.rules import BLOCK, Finding, evaluate
from backend.guard.semantic import RISK_THRESHOLD, AsksJEV, semantic_check
from backend.guard.simulate import SimResult
from backend.trade.builder import TradeIntent

ALLOW, ASK_HUMAN = "allow", "ask_human"


@dataclass
class Verdict:
    decision: str
    reasons: list[str] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    jev_risk: float | None = None

    def as_dict(self) -> dict:
        return {"decision": self.decision, "reasons": self.reasons, "jev_risk": self.jev_risk,
                "findings": [f.__dict__ for f in self.findings]}


async def decide(sim: SimResult, intent: TradeIntent, quote: dict | None = None, jev: AsksJEV | None = None) -> Verdict:
    findings = evaluate(sim, intent, quote)
    blocks = [f for f in findings if f.severity == BLOCK]
    if blocks:
        return Verdict(BLOCK, [f.message for f in blocks], findings)
    asks = [f.message for f in findings]
    if jev is None:
        return Verdict(ASK_HUMAN if asks else ALLOW, asks, findings)
    try:
        p, why = await semantic_check(jev, sim, intent)
    except Exception as e:  # noqa: BLE001  JEV error -> hand to a human
        return Verdict(ASK_HUMAN, asks + [f"semantic check failed: {type(e).__name__}"], findings)
    if p >= RISK_THRESHOLD:
        return Verdict(ASK_HUMAN, asks + [why], findings, p)
    return Verdict(ASK_HUMAN if asks else ALLOW, asks or [why], findings, p)
