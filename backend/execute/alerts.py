"""Proactive position and market alerts: short, calm, one sentence, coin number or allowlisted ticker.

Thresholds live in backend.config. Alerts are throttled per position, merged into one
speak line when several fire together, and emitted as a loud event so the voice agent reads them
(the frontend then marks the session ENGAGED). Stop-loss / take-profit fills already speak
on their own events and are not repeated here.
"""
from __future__ import annotations

import time
from collections import defaultdict

from backend.config import (
    ALERT_LIQ_WARN_PCT, ALERT_MARKET_MOVE_PCT, ALERT_MARKET_WINDOW_S, ALERT_MOVE_PCT,
    ALERT_NEAR_EXIT_PCT, ALERT_THROTTLE_S, ALERT_VOLUME_SPIKE_MULT, BTC_MINT, SOL_MINT,
)
from typing import TYPE_CHECKING, Any

from backend.execute.failure_speak import pct_plain

if TYPE_CHECKING:
    from backend.execute.listener import Position

# (unix_ts, usd_price) samples for SOL / BTC
_market: dict[str, list[tuple[float, float]]] = defaultdict(list)


def _label(pos: Position) -> str:
    if getattr(pos, "ticker", None):
        return str(pos.ticker)
    if pos.coin_number is not None:
        return "coin " + str(pos.coin_number)
    return "a position"


def _pct_move(ratio: float) -> float:
    return (ratio - 1.0) * 100


def record_market_price(mint: str, usd: float, now: float | None = None) -> None:
    now = now or time.time()
    buf = _market[mint]
    buf.append((now, usd))
    cut = now - ALERT_MARKET_WINDOW_S - 60
    _market[mint] = [(t, p) for t, p in buf if t >= cut][-120:]


def market_move(mint: str, now: float | None = None) -> float | None:
    """Percent change from the oldest sample in the window to the latest. None if not enough data."""
    now = now or time.time()
    buf = _market.get(mint) or []
    if len(buf) < 2:
        return None
    first = next((p for t, p in buf if now - t >= min(60.0, ALERT_MARKET_WINDOW_S / 3)), None)
    last = buf[-1][1]
    if not first or first <= 0 or last <= 0:
        return None
    return (last / first - 1.0) * 100


def near_exit(pos: Position, ratio: float, ex) -> str | None:
    """'stop loss' / 'take profit' when within ALERT_NEAR_EXIT_PCT of a trigger that has not fired."""
    sl = pos.stop_loss_pct if pos.stop_loss_pct is not None else ex.stop_loss_pct
    if sl is not None and ratio <= 1 - (sl - ALERT_NEAR_EXIT_PCT) / 100 and ratio > 1 - sl / 100:
        return "stop loss"
    ladder = pos.take_profit_ladder if pos.take_profit_ladder is not None else list(ex.take_profit_ladder or ())
    for i, step in enumerate(ladder):
        multiple = step.multiple if hasattr(step, "multiple") else step["multiple"]
        if i in pos.tp_done:
            continue
        # within 0.5 percentage points of the multiple (1.015 is 1.5% up; near at 1.0%)
        if ratio >= multiple - ALERT_NEAR_EXIT_PCT / 100 and ratio < multiple:
            return "take profit"
    return None


def liq_warning(pos: Position, q_liq: int | None, ex) -> bool:
    if not pos.entry_liquidity or q_liq is None:
        return False
    warn = ALERT_LIQ_WARN_PCT
    hard = getattr(ex, "liquidity_drop_pct", 80.0)
    # warn before the hard sell; if warn >= hard, use half the hard threshold
    bar = min(warn, hard * 0.6) if hard else warn
    return q_liq <= pos.entry_liquidity * (1 - bar / 100) and q_liq > pos.entry_liquidity * (1 - hard / 100)


def volume_spike(metrics: dict | None) -> bool:
    if not metrics:
        return False
    h1 = metrics.get("volume_h1")
    h6 = metrics.get("volume_h6")
    h24 = metrics.get("volume_h24")
    baseline = h6 / 6 if h6 else (h24 / 24 if h24 else None)
    return bool(h1 and baseline and baseline > 0 and h1 >= baseline * ALERT_VOLUME_SPIKE_MULT)


def position_reasons(pos: Position, q, now: float, ex, metrics: dict | None = None) -> list[str]:
    """Fresh alert clauses for one position (empty if throttled or nothing notable)."""
    last = pos.last_alert_at or 0
    if now - last < ALERT_THROTTLE_S:
        return []
    ratio = pos.last_ratio
    if ratio is None:
        return []
    reasons: list[str] = []
    ref = pos.last_alert_ratio if pos.last_alert_ratio is not None else 1.0
    move = (ratio / ref - 1.0) * 100 if ref else _pct_move(ratio)
    if abs(move) >= ALERT_MOVE_PCT:
        direction = "up" if move > 0 else "down"
        reasons.append(_label(pos) + " is " + direction + " " + pct_plain(move) + " since the last check")
    near = near_exit(pos, ratio, ex)
    if near:
        reasons.append(_label(pos) + " is close to its " + near)
    if liq_warning(pos, getattr(q, "liquidity", None), ex):
        reasons.append("liquidity on " + _label(pos) + " has dropped")
    if volume_spike(metrics):
        reasons.append("volume on " + _label(pos) + " just spiked")
    return reasons


def market_reasons(now: float | None = None) -> list[str]:
    now = now or time.time()
    out = []
    for mint, name in ((SOL_MINT, "SOL"), (BTC_MINT, "Bitcoin")):
        mv = market_move(mint, now)
        if mv is not None and abs(mv) >= ALERT_MARKET_MOVE_PCT:
            direction = "up" if mv > 0 else "down"
            out.append(name + " is " + direction + " " + pct_plain(mv) + " in the last fifteen minutes")
    return out


def merge_speak(reasons: list[str]) -> str:
    if not reasons:
        return ""
    if len(reasons) == 1:
        return reasons[0][0].upper() + reasons[0][1:] + "."
    body = reasons[0] + ", and " + ", and ".join(reasons[1:])
    return body[0].upper() + body[1:] + "."


def mark_alerted(positions: list[Position], now: float) -> None:
    for pos in positions:
        if pos.last_ratio is not None:
            pos.last_alert_at = now
            pos.last_alert_ratio = pos.last_ratio
