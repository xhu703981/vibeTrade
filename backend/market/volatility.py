"""Volatility-based stop suggestion (replaces the fixed 1.5% default stop).

A trader sets a stop just outside the coin's normal noise. Noise = ATR (average true range) of recent candles,
as a percent of price. Suggested stop = ATR% x multiple, clamped to a sane band for meme coins.
Only numbers go in and out; candles come from the free GeckoTerminal OHLCV the desk already uses.
"""
from __future__ import annotations

import math

STOP_MIN_PCT = 5.0
STOP_MAX_PCT = 25.0
FALLBACK_STOP_PCT = 10.0          # no candles: a middle-of-the-band stop, said as such
MULT_15M = 1.75                   # 15m ATR x 1.75: outside a typical 15-minute swing
MULT_1H = 1.5                     # 1h ATR x 1.5 when 15m candles are missing
ATR_PERIOD = 14


def atr_pct(candles: list, period: int = ATR_PERIOD) -> float | None:
    """Mean true range / close over the last `period` candles, in percent. candles: ascending [[t, o, h, l, c, v], ...]."""
    rows = [c for c in candles or [] if isinstance(c, (list, tuple)) and len(c) >= 5]
    if len(rows) < 3:
        return None
    rows = rows[-(period + 1):]
    trs = []
    for prev, cur in zip(rows, rows[1:]):
        h, l, c, pc = float(cur[2]), float(cur[3]), float(cur[4]), float(prev[4])
        if not all(math.isfinite(x) and x > 0 for x in (h, l, c, pc)):
            continue
        tr = max(h - l, abs(h - pc), abs(l - pc))
        trs.append(tr / c * 100)
    if len(trs) < 2:
        return None
    return sum(trs) / len(trs)


def stop_from_atr(atr: float | None, multiple: float) -> float:
    if atr is None or not math.isfinite(atr) or atr <= 0:
        return FALLBACK_STOP_PCT
    raw = atr * multiple
    return float(min(STOP_MAX_PCT, max(STOP_MIN_PCT, round(raw))))


def suggest_stop(c15: list | None, c1h: list | None = None) -> dict:
    """{stop_pct, atr_pct, tf, multiple, basis} with basis 'atr' or 'fallback'."""
    a15 = atr_pct(c15 or [])
    if a15 is not None:
        return {"stop_pct": stop_from_atr(a15, MULT_15M), "atr_pct": round(a15, 2), "tf": "15m", "multiple": MULT_15M,
                "basis": "atr"}
    a1h = atr_pct(c1h or [])
    if a1h is not None:
        return {"stop_pct": stop_from_atr(a1h, MULT_1H), "atr_pct": round(a1h, 2), "tf": "1h", "multiple": MULT_1H,
                "basis": "atr"}
    return {"stop_pct": FALLBACK_STOP_PCT, "atr_pct": None, "tf": None, "multiple": None, "basis": "fallback"}


async def suggest_for_pool(pool: str | None) -> dict:
    """Fetch 15m (then 1h) candles for a known pool and suggest a stop. Never raises."""
    if not pool:
        return suggest_stop(None)
    from backend.market.ohlcv import fetch_ohlcv
    try:
        r15 = await fetch_ohlcv(pool, "15m")
        c15 = r15.get("candles") if r15.get("ok") else None
        c1h = None
        if atr_pct(c15 or []) is None:
            r1h = await fetch_ohlcv(pool, "1h")
            c1h = r1h.get("candles") if r1h.get("ok") else None
        return suggest_stop(c15, c1h)
    except Exception:  # noqa: BLE001
        return suggest_stop(None)


def stop_words(s: dict, ref: str) -> str:
    """Spoken, rounded the way people talk: 'Coin 1 usually swings about 14 percent, so I'd put the stop at about 24 percent.'
    (The card keeps the precise numbers.)"""
    pct = f"{round(s['stop_pct'])}"
    if s.get("basis") != "atr":
        return f"I don't have enough candles for {ref}, so I'd start with about a {pct} percent stop."
    return (f"{ref[:1].upper() + ref[1:]} usually swings about {round(s['atr_pct'])} percent, "
            f"so I'd put the stop at about {pct} percent.")
