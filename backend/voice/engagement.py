"""Wake-by-name and addressee checks (IDLE vs ENGAGED).

The browser owns the state machine and the mute; this module is the shared, testable
reading of a transcript: did they say the name, is it clearly side talk, or is it ambiguous
enough to ask a cheap LLM. When ENGAGED and unsure, the caller should prefer responding.
"""
from __future__ import annotations

import re

from backend.judge.llm import LLMError, chat_json

# ASR-tolerant: Vibe, Vibe Trader, hey Vibe, vibes, vibe-trader, "vibe trader".
WAKE_RE = re.compile(
    r"\b(?:hey|hi|yo|ok(?:ay)?|okay)?\s*,?\s*vibe(?:s)?(?:[\s\-]*trader)?\b",
    re.I,
)
# Clearly not for us (coffee run, chatting to a friend) and no trading words needed.
OTHER_RE = re.compile(
    r"\b(two|three|four|a|one)\s+(coffees?|lattes?|teas?|cappuccinos?)\b"
    r"|\b(starbucks|dunkin|drive[\s-]*through|drive[\s-]*thru)\b"
    r"|\b(can i (get|have|order)|i'?ll (have|take|get)|order(?:ing)?)\b.{0,40}\b(coffee|latte|grande|venti|tall)\b"
    r"|\b(thanks|thank you),?\s+(guys|man|buddy|dude|hon(?:ey)?)\b"
    r"|\b(see you|talk later|love you)\b"
    r"|\b(not (to |for )?you|i'?m (on a call|talking to))\b",
    re.I,
)
TRADING_RE = re.compile(
    r"\b(coin|buy|sell|approve|reject|confirm|mandate|budget|filter|research|positions?|stop|profit|"
    r"card|trade|status|exit|desk|major|jup|jto|pyth|bonk|wif|ray|cbbtc|wbtc|weth|"
    r"dollar|cent|penny|percent|break[\s-]*even)\b|\b\d{2}\b",
    re.I,
)
FILLER_RE = re.compile(
    r"^(?:\s*(?:h+m+|u+m+|u+h+|e+r+m*|okay|ok|so|well|yeah|right|wait|hold on)[\s,.!?…-]*)+$",
    re.I,
)

ADDRESSEE_SYSTEM = (
    "You classify whether a spoken line is for a trading assistant named Vibe / Vibe Trader, "
    "or for someone else (a friend, a barista, a phone call). "
    "Reply JSON {\"addressed\": true|false}. If unsure, addressed=true."
)


def has_wake_name(text: str | None) -> bool:
    return bool(WAKE_RE.search(text or ""))


def strip_wake_name(text: str) -> str:
    return WAKE_RE.sub(" ", text or "", count=1).strip(" ,.-")


def clearly_other(text: str | None) -> bool:
    t = (text or "").strip()
    if not t or has_wake_name(t) or TRADING_RE.search(t):
        return False
    return bool(OTHER_RE.search(t))


def clearly_trading(text: str | None) -> bool:
    t = (text or "").strip()
    return bool(t) and bool(TRADING_RE.search(t)) and not clearly_other(t)


def is_filler(text: str | None) -> bool:
    return bool(FILLER_RE.match((text or "").strip()))


def addressee_needed(text: str | None, mode: str) -> bool:
    """True when a cheap LLM check is worth it (ENGAGED, not clear either way)."""
    t = (text or "").strip()
    if mode != "engaged" or not t or is_filler(t):
        return False
    if has_wake_name(t) or clearly_trading(t) or clearly_other(t):
        return False
    return True


def addressed_deterministic(text: str | None, mode: str) -> bool | None:
    """True / False when sure; None when ambiguous (ENGAGED only). IDLE requires the name."""
    t = (text or "").strip()
    if not t:
        return False
    if has_wake_name(t):
        return True
    if mode != "engaged":
        return False
    if clearly_other(t):
        return False
    if clearly_trading(t) or is_filler(t):
        return True
    return None


async def classify_addressee(text: str, timeout: float = 0.8, agent_said: str | None = None) -> bool:
    """LLM fallback. On any failure or timeout, prefer addressed=True.
    agent_said: the assistant's previous line, so a short reply ("no, let's proceed") reads as an answer to it."""
    user = text[:500]
    if agent_said:
        user = "Assistant just said: " + agent_said[-300:] + "\nSpoken line: " + user
    try:
        out = await chat_json(ADDRESSEE_SYSTEM, user, timeout=timeout)
        if "addressed" in out:
            return bool(out["addressed"])
    except (LLMError, Exception):  # noqa: BLE001  never block the turn on a classifier miss
        return True
    return True
