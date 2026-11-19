"""Calm spoken lines for card/trade failures (guard block, expired quote, reprice, sell fail, balance).

Token names never appear: card code and coin number only. Reasons are rewritten for TTS
(percent, not %). Optional one-line suggestion when it is natural.
"""
from __future__ import annotations

import re

# Find a percent amount written as 2.14% or 2.0 %.
_PCT = re.compile(r"(-?\d+(?:\.\d+)?)\s*%")
_BPS = re.compile(r"(\d+)\s*bps", re.I)
# Solana addresses must never be spoken.
_MINT = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")


def pct_plain(x: float) -> str:
    """2.14 -> '2.1 percent', 2.0 -> '2 percent'."""
    t = f"{abs(x):.1f}".rstrip("0").rstrip(".")
    return t + " percent"


def voice_reason(text: str | None) -> str:
    """Turn a guard/engine reason into a short TTS-friendly clause."""
    s = (text or "").strip()
    if not s:
        return ""
    s = _MINT.sub("that token", s)
    s = re.sub(
        r"exceeds the cap\s*(-?\d+(?:\.\d+)?)\s*%",
        lambda m: "is over your " + pct_plain(float(m.group(1))) + " cap",
        s,
    )
    s = _PCT.sub(lambda m: pct_plain(float(m.group(1))), s)
    s = _BPS.sub(lambda m: m.group(1) + " basis points", s)
    s = re.sub(r"\bexceeds\b", "is over", s)
    return s.strip(" .;")


def _suggestion(reason: str) -> str:
    r = reason.lower()
    if "price impact" in r:
        return "You can try a smaller amount."
    if any(k in r for k in ("insufficient", "not enough", "balance")):
        return "You can try a smaller amount."
    return ""


def _ref(code: str | None = None, coin: int | None = None) -> str:
    if code and coin is not None:
        return "Card " + str(code) + " for coin " + str(coin)
    if code:
        return "Card " + str(code)
    if coin is not None:
        return "Coin " + str(coin)
    return "The trade"


THEFT_RE = re.compile(r"forbidden Token instruction Transfer|TOKEN_TRANSFER|another wallet token .* decreased|actual spend .* exceeds intent", re.I)
THEFT_WHY = "it would also move USDC to a wallet that isn't yours"


def _why(reasons: list[str] | None, fallback: str) -> str:
    if any(THEFT_RE.search(r or "") for r in reasons or []):
        return THEFT_WHY
    for r in reasons or []:
        v = voice_reason(r)
        if v:
            return v
    return fallback


def blocked_buy_speak(code: str | None, coin: int | None, reasons: list[str] | None, *,
                      created: bool = True, resim_block: bool = False, new_concern: bool = False) -> str:
    why = _why(reasons, "the guard blocked it")
    if why == THEFT_WHY:
        lead = ("The buy for " + _ref(code, coin).lower() if not created else _ref(code, coin)) + " was blocked by the guard: " + why + "."
        return lead + " Nothing was sent."
    if not created:
        lead = "No card was created: " + why + "."
        tail = "Nothing was bought."
    elif new_concern:
        lead = _ref(code, coin) + " is void: the guard raised a new concern (" + why + ")."
        tail = "Please confirm again."
    else:
        lead = _ref(code, coin) + " was blocked: " + why + "."
        tail = "Nothing was bought."
    sug = _suggestion(why)
    return " ".join(p for p in (lead, tail, sug) if p)


def blocked_sell_speak(trigger: str, reasons: list[str] | None, coin: int | None = None) -> str:
    why = _why(reasons, "the guard blocked it")
    label = (trigger or "The").replace("_", "-")
    head = (label[:1].upper() + label[1:] + " sell was blocked") if trigger else "The sell was blocked"
    if coin is not None:
        head = head + " for coin " + str(coin)
    sug = _suggestion(why)
    return " ".join(p for p in (head + ": " + why + ".", "Nothing was sold.", sug) if p)


def trade_failed_speak(code: str | None, coin: int | None, reason: str | None, *,
                       stage: str = "", bought: bool = False) -> str:
    why = voice_reason(reason) or "the trade did not go through"
    ref = _ref(code, coin)
    if stage == "quote":
        lead = ref + " could not get a quote: " + why + "."
    elif stage in ("requote", "expired_quote"):
        lead = ref + " had an expired or failed re-quote: " + why + "."
    else:
        lead = ref + " did not go through: " + why + "."
    tail = "Nothing was bought." if not bought else "Stopped; it will not retry automatically."
    return " ".join(p for p in (lead, tail, _suggestion(why)) if p)


def expired_card_speak(code: str | None, coin: int | None = None) -> str:
    return _ref(code, coin) + " timed out and counts as rejected."


def reprice_void_speak(code: str | None, coin: int | None, dev_pct: float, cap_pct: float) -> str:
    return (_ref(code, coin) + " is void: after approval the re-quote moved " + pct_plain(dev_pct)
            + ", more than your " + pct_plain(cap_pct) + " cap, so a new card is coming.")


def sell_failed_speak(trigger: str, attempt: int, why: str, *, stuck: bool = False, coin: int | None = None) -> str:
    label = (trigger or "sell").replace("_", " ")
    head = label[:1].upper() + label[1:] + " sell failed"
    if coin is not None:
        head = head + " for coin " + str(coin)
    reason = voice_reason(why)
    mid = head + ", attempt " + str(attempt)
    if reason:
        mid = mid + ": " + reason
    mid = mid + "."
    tail = "Auto-selling has stopped; please handle it manually." if stuck else "I will check again next round."
    return mid + " " + tail
