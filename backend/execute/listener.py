"""Take-profit / stop-loss listener. The sell path has no LLM / JEV: precomputed params -> /build -> rule-layer guard -> send.

Prices are measured the way a trader reads a stop loss: the coin's price move since the fill, NOT the net-of-fees
liquidation value versus the USDC spent. Right after the buy lands the position takes a baseline sell quote
(exit_ref_per_token, same ForkPriceSource quote); ratio = current executable unit sell value / that baseline.
So the round-trip fee / slippage (~1-2% on meme coins) is not a price move and cannot fire a 1% stop loss by itself.
If the baseline quote failed, the first successful poll becomes the baseline and never triggers on that same poll.
entry_usdc stays the real cost and is only used for realized P&L.

Triggers (highest priority first, at most one at a time):
  stop_loss        price ratio <= 1 - stop_loss_pct                                  -> sell all
  liquidity_drop   pool quote-side vault <= value at entry x (1 - liquidity_drop_pct) -> sell all
  trailing_stop    was up (peak ratio > 1) and fell >= trailing_stop_pct from the peak -> sell all
  max_hold         holding time (fork clock) >= max_hold_hours                       -> sell all
  take_profit      price ratio >= multiple (each rung fires once)                    -> sell sell_pct of the initial position
Price fetch failure -> price_feed_down (alert only, never sells). Sell failure -> sell_failed (loud broadcast); 3 failures in a row stop auto-selling.
After the mandate expires it only sells, never buys (the builder refuses buys); the listener keeps working.
"""
from __future__ import annotations

import asyncio
import base64
import math
import struct
import time
from dataclasses import asdict, dataclass, field

from backend.config import JUPITER_ROUTE_DEXES, USDC_MINT, route_dexes, settings
from backend.guard.pipeline import check_transaction
from backend.solana_utils import sync_fork_clock, token_balance
from backend.trade.builder import build_sell_intent
from backend.trade.jupiter import JupiterError, assemble_v0, build_instructions, build_swap, fetch_alts
from backend.execute.alerts import (
    mark_alerted, market_reasons, merge_speak, position_reasons, record_market_price,
)
from backend.execute.failure_speak import blocked_sell_speak, sell_failed_speak
from backend.market.ohlcv import jupiter_prices
import httpx
from backend.execute.price_source import PriceFeedDown, PriceQuote

MAX_SELL_FAILURES = 3
FULL_EXITS = ("stop_loss", "liquidity_drop", "trailing_stop", "max_hold", "emergency")
TRIGGER_LABEL = {"take_profit": "Take-profit", "stop_loss": "Stop-loss", "trailing_stop": "Trailing stop",
                 "max_hold": "Max hold time", "liquidity_drop": "Liquidity drop", "emergency": "Emergency exit",
                 "manual": "Requested"}


COPILOT_QUIET = True   # no generic price / market chatter while holding; only de-risking offers and exits are spoken


@dataclass
class Position:
    mint: str
    symbol_display: str
    pool: str | None
    initial_tokens: int
    held_tokens: int
    entry_usdc: int                 # USDC spent on the buy (base units)
    opened_at: float                # fork clock (unix seconds)
    entry_liquidity: int | None = None
    peak_ratio: float = 1.0
    last_ratio: float | None = None
    tp_done: list[int] = field(default_factory=list)
    status: str = "open"            # open | closed | stuck
    sell_failures: int = 0
    feed_down: bool = False
    feed_misses: int = 0            # consecutive polls without a quote
    realized_usdc: int = 0
    sells: list[dict] = field(default_factory=list)
    mandate_id: str | None = None   # exit rules are frozen at open time: amending the mandate never rewrites them
    coin_number: int | None = None
    ticker: str | None = None                 # allowlisted major ticker; meme coins stay numbers
    stop_loss_pct: float | None = None        # per-position override; None = use the mandate
    take_profit_ladder: list | None = None    # list of {multiple, sell_pct}; None = use the mandate
    last_alert_at: float = 0.0
    last_alert_ratio: float | None = None
    exit_ref_per_token: float | None = None   # baseline sell value per token right after the fill (exit-rule reference)
    entry_exit_value: int | None = None       # that baseline for the whole fill (USDC base units); cost - this = round-trip fees
    last_value_usdc: int | None = None        # what the held tokens would sell for now (fork quote), for the P&L panel
    price_at: float | None = None             # unix seconds of the last live mark (for "updated Xs ago")
    opened_wall: float = field(default_factory=time.time)   # wall clock at the fill (daily max loss counts today's trades)
    offers_made: list[str] = field(default_factory=list)     # de-risking offers already made ("sell_half", "break_even")
    mark_seq: int = 0                         # +1 per live mark (fork clock time can repeat between polls; this cannot)
    rehearsal: str | None = None              # a rehearsal price test is running ("stop_loss" / "take_profit")

    @property
    def entry_per_token(self) -> float:
        return self.entry_usdc / self.initial_tokens

    @property
    def roundtrip_cost_pct(self) -> float | None:
        """Fees + slippage of buying and selling right away, as a % of the cost (None when unknown)."""
        if not self.entry_exit_value or not self.entry_usdc:
            return None
        return (1 - self.entry_exit_value / self.entry_usdc) * 100

    def mark_usdc(self) -> int | None:
        """What the held tokens would sell for now, fees and slippage included: the latest fork sell quote per token times
        the tokens held (ratio x post-fill sell value per token). One number for the top bar, the P&L panel, the daily
        loss limit and the co-pilot's spoken "up N percent". None when there is no quote yet."""
        if self.held_tokens <= 0:
            return 0
        if self.exit_ref_per_token is not None:
            return int((self.last_ratio if self.last_ratio is not None else 1.0) * self.exit_ref_per_token * self.held_tokens)
        return self.last_value_usdc

    def pnl_usdc(self) -> int | None:
        """Realized + what is left is worth now - what was paid (base units). Fees on both sides are in it."""
        m = self.mark_usdc()
        return None if m is None else self.realized_usdc + m - self.entry_usdc

    def pnl_pct(self) -> float | None:
        v = self.pnl_usdc()
        return None if v is None or not self.entry_usdc else v / self.entry_usdc * 100

    def set_exit_baseline(self, value_usdc: int, amount: int) -> None:
        self.exit_ref_per_token = value_usdc / amount
        self.entry_exit_value = int(self.exit_ref_per_token * self.initial_tokens)

    def add_fill(self, tokens: int, spent: int, baseline: int | None) -> None:
        """A second buy of the same coin: the reference becomes the token-weighted average (like an average entry price)."""
        old = self.initial_tokens
        self.initial_tokens += tokens
        self.held_tokens += tokens
        self.entry_usdc += spent
        if self.exit_ref_per_token is not None and baseline:
            self.exit_ref_per_token = (self.exit_ref_per_token * old + baseline) / self.initial_tokens
            self.entry_exit_value = int(self.exit_ref_per_token * self.initial_tokens)
        else:   # unknown for one of the fills: take a fresh baseline at the next poll (it will not trigger that poll)
            self.exit_ref_per_token = self.entry_exit_value = None


FEED_DOWN_AFTER_MISSES = 2   # consecutive failed polls before "price feed down" is announced


def decide_exit(pos: Position, q: PriceQuote, now: float, ex) -> tuple[str, int, int | None] | None:
    """(trigger, tokens to sell, ladder rung); None = do nothing. Pure apart from updating pos.peak_ratio / last_ratio."""
    if q.amount <= 0 or pos.held_tokens <= 0:
        return None
    if q.amount == pos.held_tokens:
        pos.last_value_usdc = q.value_usdc
    if pos.exit_ref_per_token is None:      # no baseline from the fill: this poll becomes the baseline, no trigger now
        pos.set_exit_baseline(q.value_usdc, q.amount)
        pos.last_ratio = 1.0
        pos.price_at = now
        return None
    ratio = (q.value_usdc / q.amount) / pos.exit_ref_per_token
    pos.last_ratio = ratio
    pos.price_at = now
    pos.mark_seq = getattr(pos, "mark_seq", 0) + 1
    pos.peak_ratio = max(pos.peak_ratio, ratio)
    held = pos.held_tokens
    sl = pos.stop_loss_pct if pos.stop_loss_pct is not None else ex.stop_loss_pct
    if sl is None:
        from backend.market.volatility import FALLBACK_STOP_PCT
        sl = FALLBACK_STOP_PCT
    if ratio <= 1 - sl / 100:
        return "stop_loss", held, None
    if getattr(pos, "rehearsal", None) == "stop_loss":
        # "Test stop loss" is walking the price down to the stop: a trailing stop, liquidity drop or time exit on the way
        # would sell through the wrong rule (after a take-profit test the trailing line sits far above the stop)
        return None
    if pos.entry_liquidity and q.liquidity is not None and q.liquidity <= pos.entry_liquidity * (1 - ex.liquidity_drop_pct / 100):
        return "liquidity_drop", held, None
    if pos.peak_ratio > 1 and ratio <= pos.peak_ratio * (1 - ex.trailing_stop_pct / 100):
        return "trailing_stop", held, None
    if now - pos.opened_at >= ex.max_hold_hours * 3600:
        return "max_hold", held, None
    ladder = pos.take_profit_ladder if pos.take_profit_ladder is not None else list(ex.take_profit_ladder or ())
    for i, step in enumerate(ladder):
        multiple = step.multiple if hasattr(step, "multiple") else step["multiple"]
        sell_pct = step.sell_pct if hasattr(step, "sell_pct") else step["sell_pct"]
        if i not in pos.tp_done and ratio >= multiple:
            amt = min(held, math.floor(pos.initial_tokens * sell_pct / 100))
            return ("take_profit", amt, i) if amt > 0 else None
    return None


async def fork_clock(rpc) -> float:
    acc = (await rpc.call("getAccountInfo", ["SysvarC1ock11111111111111111111111111111111", {"encoding": "base64"}]))["value"]
    return float(struct.unpack_from("<q", base64.b64decode(acc["data"][0]), 32)[0])


class Listener:
    def __init__(self, engine, price_source, fork_mode: bool = True, clock=None):
        self.e = engine
        self.ps = price_source
        self.fork_mode = fork_mode
        self.clock = clock or ((lambda: fork_clock(engine.rpc)) if fork_mode else _wall)
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    # ---------- loop ----------
    def start(self) -> None:
        if not self._task or self._task.done():
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    async def _run(self) -> None:
        while True:
            try:
                await self.poll_once()
            except Exception as e:  # noqa: BLE001  the loop itself must never die
                self.e.bus.emit("price_feed_down", f"Monitoring error ({type(e).__name__}). Auto-selling is paused; alerting only.", {"error": type(e).__name__}, loud=True)
            await asyncio.sleep(self.e.m.body.exit.poll_interval_sec)

    async def poll_once(self) -> list[str]:
        fired = []
        alert_reasons: list[str] = []
        alerted_positions: list[Position] = []
        async with self._lock:
            now = await self.clock()
            for pos in [p for p in self.e.positions.values() if p.status == "open"]:
                try:
                    q = await self.ps.quote(pos.mint, pos.held_tokens, pos.pool)
                except PriceFeedDown as e:
                    # One missed quote (a rate-limit blip right after a sell) is not announced; nothing is sold either way.
                    pos.feed_misses += 1
                    if not pos.feed_down and pos.feed_misses >= FEED_DOWN_AFTER_MISSES:
                        pos.feed_down = True
                        self.e.bus.emit("price_feed_down", "Price feed down: auto stop loss and take profit paused; nothing is sold blind.",
                                        {"mint": pos.mint, "reason_code": type(e).__name__}, loud=True)
                    continue
                pos.feed_misses = 0
                if pos.feed_down:
                    pos.feed_down = False
                    self.e.bus.emit("price_feed_restored", "The price feed is back. Automatic take-profit and stop-loss resumed.", {"mint": pos.mint})
                ex = self.e.mandate_for(pos).body.exit
                d = decide_exit(pos, q, now, ex)
                if d is None:
                    # co-pilot: while holding, speak only to offer de-risking (sell half when up, then break-even stop)
                    offer = getattr(self.e, "derisk_offer", None)
                    if callable(offer):
                        offer(pos)
                    elif not COPILOT_QUIET:
                        rs = position_reasons(pos, q, time.time(), ex)
                        if rs:
                            alert_reasons.extend(rs)
                            alerted_positions.append(pos)
                    continue
                trig, amt, level = d
                self.e.bus.emit("exit_triggered", _exit_speak(trig, pos, amt),
                                {"mint": pos.mint, "symbol": pos.symbol_display, "trigger": trig, "ratio": round(pos.last_ratio or 0, 4),
                                 "amount": amt, "held": pos.held_tokens})
                ok = await self.sell(pos, amt, trig, q if amt == q.amount else None)
                if ok and trig == "take_profit" and level is not None:
                    pos.tp_done.append(level)
                fired.append(trig)
        if alert_reasons:
            speak = merge_speak(alert_reasons)
            if speak:
                self.e.bus.emit("position_alert", speak, {"count": len(alert_reasons)}, loud=True)
                mark_alerted(alerted_positions, time.time())
        if not COPILOT_QUIET:
            await self._market_alerts(alert_reasons)
        return fired

    _last_market_alert = 0.0

    async def _market_alerts(self, already: list[str]) -> None:
        """SOL / BTC window moves; skipped if we just spoke or recently alerted the market."""
        from backend.config import ALERT_THROTTLE_S, BTC_MINT, SOL_MINT
        now = time.time()
        if now - Listener._last_market_alert < ALERT_THROTTLE_S:
            return
        try:
            async with httpx.AsyncClient(timeout=6) as c:
                for mint, px in (await jupiter_prices([SOL_MINT, BTC_MINT], c)).items():   # one call for both
                    record_market_price(mint, px, now)
        except Exception:  # noqa: BLE001  market alerts must never break the listener
            return
        extra = market_reasons(now)
        if not extra:
            return
        Listener._last_market_alert = now
        speak = merge_speak(extra)
        if speak:
            self.e.bus.emit("position_alert", speak, {"market": True}, loud=True)

    # ---------- selling ----------
    async def sell(self, pos: Position, amount: int, trigger: str, q: PriceQuote | None = None) -> bool:
        e = self.e
        pm = e.mandate_for(pos)        # the rules the position was opened under
        stop_loss = trigger != "take_profit"
        slip = pm.body.exec.max_slippage_bps.stop_loss_sell if stop_loss else pm.body.exec.max_slippage_bps.buy
        try:
            if self.fork_mode:   # expected output = what the fork actually returns (not Jupiter's mainnet quote)
                q = q if q and q.amount == amount else await self.ps.quote(pos.mint, amount, pos.pool)
                expected = q.value_usdc
            await sync_fork_clock(e.rpc, e.upstream)
            b = await build_swap(pos.mint, USDC_MINT, amount, e.wallet, slip, dexes=route_dexes(pos.mint))
            if not self.fork_mode:
                expected = int(b["outAmount"])
            elif expected * (10_000 - slip) < int(b["outAmount"]) * (10_000 - slip):
                # fork price is below the mainnet quote: loosen the threshold inside the Jupiter instruction so it can execute; the real min_out is checked by the guard at the fork price
                need = math.ceil((1 - expected * (10_000 - slip) / 10_000 / int(b["outAmount"])) * 10_000) + 10
                b = await build_swap(pos.mint, USDC_MINT, amount, e.wallet, min(9900, need), dexes=route_dexes(pos.mint))
            alts = await fetch_alts(e.rpc, list((b.get("addressesByLookupTableAddress") or {}).keys()))
            raw = bytes(assemble_v0(e.kp, build_instructions(b), alts, await e.rpc.latest_blockhash()))
            intent = build_sell_intent(pm, e.wallet, pos.mint, amount, expected, stop_loss=stop_loss,
                                       expected_source="fork_profile" if self.fork_mode else "jupiter")
        except (JupiterError, PriceFeedDown, Exception) as ex:  # noqa: BLE001
            return self._sell_failed(pos, trigger, f"sell quote failed ({type(ex).__name__})")
        # rule layer only (no JEV). Fork mode: the route's pools are refreshed from mainnet first so the sell runs against
        # current market state, unless a rehearsal price test froze this coin's fork-only pool state.
        refresh = self.fork_mode and bool(getattr(self.ps, "follows_mainnet", lambda _m: False)(pos.mint))
        v, _ = await check_transaction(e.rpc, raw, intent, b, jev=None, refresh_pools=refresh)
        if v.decision == "block":
            # Speak once via sell_failed (loud); keep guard_blocked for the on-screen banner type.
            e.bus.emit("guard_blocked", "", {"mint": pos.mint, "side": "sell", "reasons": v.reasons[:5]})
            return self._sell_failed(pos, trigger, "blocked by guard: " + "; ".join(v.reasons)[:120],
                                     speak=blocked_sell_speak(trigger, v.reasons))
        usdc0 = await token_balance(e.rpc, e.wallet, USDC_MINT)
        rec = await e.executor.send(raw, "sell", trigger, pos.mint, amount, intent.min_out, {"expected": expected})
        if rec.error:
            return self._sell_failed(pos, trigger, f"send failed: {rec.error[:100]}")
        got = await token_balance(e.rpc, e.wallet, USDC_MINT) - usdc0
        rec.received = got
        e.executor.save()
        # The position only owns the tokens it bought: other tokens of the same mint in the wallet (an earlier
        # session on this fork) must never be counted, re-sold by a trigger, or booked as profit.
        bal = await token_balance(e.rpc, e.wallet, pos.mint)
        pos.held_tokens = max(0, min(pos.held_tokens - amount, bal))
        pos.realized_usdc += got
        pos.sell_failures = 0
        pos.sells.append({"trigger": trigger, "amount": amount, "usdc": got, "sig": rec.signature, "ratio": pos.last_ratio})
        if pos.held_tokens <= 0:
            pos.status = "closed"
        pnl = (pos.realized_usdc - pos.entry_usdc) / 1e6
        # a card sell ("manual") is announced once by the engine with the card code; no second line here
        e.bus.emit("trade_executed", "" if trigger == "manual" else _sell_speak(trigger, got, pos, pnl),
                   {"side": "sell", "trigger": trigger, "mint": pos.mint, "symbol": pos.symbol_display, "amount": amount,
                    "usdc_out": got, "signature": rec.signature, "held_after": pos.held_tokens, "realized_usdc": pos.realized_usdc})
        return True

    def _sell_failed(self, pos: Position, trigger: str, why: str, speak: str | None = None) -> bool:
        pos.sell_failures += 1
        stuck = pos.sell_failures >= MAX_SELL_FAILURES
        if stuck:
            pos.status = "stuck"
        self.e.bus.emit("sell_failed", speak or sell_failed_speak(trigger, pos.sell_failures, why, stuck=stuck),
                        {"mint": pos.mint, "trigger": trigger, "why": why, "failures": pos.sell_failures}, loud=True)
        return False

    async def emergency_exit(self) -> list[dict]:
        out = []
        async with self._lock:
            for pos in [p for p in self.e.positions.values() if p.status in ("open", "stuck") and p.held_tokens > 0]:
                self.e.bus.emit("exit_triggered", "",   # already announced once by emergency_exit; keep the voice short
                                {"mint": pos.mint, "trigger": "emergency", "amount": pos.held_tokens})
                pos.status = "open"
                ok = await self.sell(pos, pos.held_tokens, "emergency")
                out.append({"mint": pos.mint, "ok": ok})
        return out


async def _wall() -> float:
    return time.time()


def pct_words(x: float) -> str:
    """+0.4% / -1.6% / +49%: one decimal for small moves so a 0.4% move is not read as +0%."""
    return f"{x:+.1f}%" if abs(x) < 3 else f"{x:+.0f}%"      # spoken: whole percents ("-12%"), a decimal only for tiny moves


def money_words(usdc_base: int | float) -> str:
    """Spoken amount precise enough for cent-sized trades: '0.984 cents', '2.5 cents', '1 cent', '12.3 cents', '1.23 dollars'."""
    d = abs(usdc_base) / 1e6
    if d >= 1:
        return _trim(f"{d:.2f}") + (" dollar" if _trim(f"{d:.2f}") == "1" else " dollars")
    c = d * 100
    if c >= 10:
        txt = _trim(f"{c:.1f}")
    else:
        txt = _trim(f"{c:.2f}" if c >= 1 else f"{c:.3f}" if c >= 0.001 else f"{c:.4f}")
    return txt + (" cent" if txt == "1" else " cents")


def _trim(num: str) -> str:
    """'1.00' -> '1', '2.50' -> '2.5' (spoken numbers without trailing zeros)."""
    return num.rstrip("0").rstrip(".") if "." in num else num


def price_move_words(pos: Position) -> str:
    r = pos.last_ratio
    return f"price {pct_words((r - 1) * 100)} since the buy" if r is not None else "price move since the buy not known yet"


def _exit_speak(trig: str, pos: Position, amt: int) -> str:
    share = round(amt / pos.held_tokens * 100) if pos.held_tokens else 0
    return f"{TRIGGER_LABEL.get(trig, trig)} triggered: {price_move_words(pos)}, selling {share}% of the position."


def _sell_speak(trig: str, got: int, pos: Position, pnl: float) -> str:
    s = f"{TRIGGER_LABEL.get(trig, trig)} sell filled: got back {money_words(got)}."
    if trig == "take_profit" and pos.realized_usdc >= pos.entry_usdc:
        s += " The original stake is recovered."
    return s + closed_words(pos)


def closed_words(pos: Position) -> str:
    """' Closed, loss 0.03 cents (...)' once the position is fully sold, else ''."""
    if pos.status != "closed":
        return ""
    net = pos.realized_usdc - pos.entry_usdc
    pct = net / pos.entry_usdc * 100 if pos.entry_usdc else 0.0
    return (f" Closed, {'profit' if net >= 0 else 'loss'} {money_words(net)} ({pct_words(pct)} including fees;"
            f" the {price_move_words(pos)}).")


__all__ = ["Position", "decide_exit", "Listener", "fork_clock", "asdict"]
