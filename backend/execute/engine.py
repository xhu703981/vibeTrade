"""Trading engine: confirmation card -> approval check -> re-quote + re-simulate + guard -> execute -> hand to the listener. Surfpool fork only."""
from __future__ import annotations

import asyncio
import math
import time
from dataclasses import asdict
from pathlib import Path

from backend.config import JUPITER_ROUTE_DEXES, USDC_MINT, route_dexes, settings, ticker_for_mint
from backend.discovery.dexscreener import sanitize_symbol
from backend.events import EventBus
from backend.execute.cards import (MAX_FAILED_ATTEMPTS, Card, CodeBook, new_card_id, verify_approval, verify_derisk_yes,
                                   verify_modify)
from backend.execute.executor import Executor, assert_local
from backend.execute.failure_speak import (
    blocked_buy_speak, expired_card_speak, reprice_void_speak, trade_failed_speak,
)
from backend.execute.listener import Listener, Position, fork_clock, money_words
from backend.execute.price_source import ForkPriceSource
from backend.guard.pipeline import check_transaction
from backend.guard import evidence as guard_evidence
from backend.solana_utils import Rpc, load_or_create_keypair, sync_fork_clock, token_balance
from backend.trade.builder import IntentError, build_buy_intent
from backend.trade.jupiter import JupiterError, assemble_v0, build_instructions, build_swap, fetch_alts

DECISION_LABEL = {"allow": "passed", "ask_human": "flagged for your attention", "block": "blocked"}
METRIC_KEYS = ("liquidity_usd", "market_cap", "volume_h24", "roundtrip_loss_pct", "top10_pct", "dev_pct", "holders")


def is_derisk(kind: str, adjust: dict | None, pos) -> bool:
    """Selling lowers risk. An exit-rule change lowers risk only if it tightens: break-even, or a stop closer than the
    current one. Take-profit changes and wider stops keep needing the code."""
    if kind == "sell":
        return True
    a = adjust or {}
    if a.get("break_even"):
        return True
    new = a.get("stop_loss_pct")
    cur = getattr(pos, "stop_loss_pct", None) if pos is not None else None
    return new is not None and cur is not None and float(new) < float(cur) and a.get("take_profit_pct") is None


def _numeric(d: dict) -> dict:
    return {k: d[k] for k in METRIC_KEYS if isinstance(d.get(k), (int, float)) and not isinstance(d.get(k), bool)}


class TradingEngine:
    def __init__(self, mandate, rpc_url: str | None = None, keypair_path: Path | None = None, bus: EventBus | None = None,
                 jev=None, price_source=None, ui_button_enabled: bool = False, upstream_url: str | None = None,
                 log_path: Path | None = None, fork_mode: bool = True):
        self.m = mandate
        self.rpc = Rpc(rpc_url or settings.surfpool_rpc_url)
        assert_local(self.rpc)
        self.kp, _ = load_or_create_keypair(keypair_path or settings.trading_keypair_path)
        self.wallet = str(self.kp.pubkey())
        self.bus = bus or EventBus()
        self.jev = jev
        self.upstream = upstream_url or settings.solana_rpc_url
        self.ui_button_enabled = ui_button_enabled
        self.cards: dict[str, Card] = {}
        self.codes = CodeBook()
        self.redteam: dict[str, str] = {}      # rehearsal red-team only: mint -> attacker the injected token text names
        self.positions: dict[str, Position] = {}
        self.spent_usdc = 0.0
        self.executor = Executor(self.rpc, log_path)
        self.listener = Listener(self, price_source or ForkPriceSource(self.rpc, self.kp, self.upstream), fork_mode=fork_mode)
        self._sweeper: asyncio.Task | None = None
        self._pool: dict[str, str | None] = {}
        self.mandates: dict[str, object] = {mandate.mandate_id: mandate}   # every version, so open positions keep their rules
        self._exec_tasks: set[asyncio.Task] = set()      # approved swaps running in the background

    # ---------- mandate versions ----------
    def mandate_for(self, pos):
        return self.mandates.get(getattr(pos, "mandate_id", None) or "", self.m)

    def set_mandate(self, m) -> None:
        """New rules for new cards / buys only. Open positions keep the exit rules they were opened with."""
        self.mandates[m.mandate_id] = m
        self.m = m

    def buy_block_reason(self) -> str | None:
        """Buys stop when today's loss reaches the daily max loss (sells are always allowed). None = buying is fine."""
        limit = getattr(self.m.body.budget, "daily_max_loss_usdc", None)
        if not limit:
            return None
        lost = self.loss_today_usdc()
        if lost >= limit - 1e-9:
            return (f"today's loss is {money_words(round(lost * 1e6))}, which reached your daily max loss of "
                    f"{money_words(round(limit * 1e6))}, so buying is off for today")
        return None

    def loss_today_usdc(self) -> float:
        """Realized + unrealized loss today (dollars, >= 0). Unrealized uses each open position's latest sell quote."""
        import datetime as _dt
        start = _dt.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        pnl = 0.0
        for p in self.positions.values():
            if getattr(p, "opened_wall", time.time()) < start:
                continue
            value_now = 0
            if p.status in ("open", "stuck") and p.held_tokens > 0:
                m = p.mark_usdc()     # the same mark as the top bar and the spoken offers
                value_now = m if m is not None else int(p.entry_usdc * p.held_tokens / max(1, p.initial_tokens))
            pnl += (p.realized_usdc + value_now - p.entry_usdc) / 1e6
        return max(0.0, -pnl)

    def in_flight(self) -> list:
        """Cards approved and being executed right now (an on-chain swap may be in the air)."""
        return [c for c in self.cards.values() if c.status == "approved"]

    def void_pending(self, why: str) -> list[str]:
        codes = []
        for c in self.cards.values():
            if c.status == "pending":
                c.status = "voided"
                c.history.append({"ts": time.time(), "event": "voided", "why": why})
                codes.append(c.code)
        if codes:
            self.bus.emit("card_voided", f"Card{'s' if len(codes) > 1 else ''} {' and '.join(codes)} {'are' if len(codes) > 1 else 'is'} void because {why}; nothing was bought.",
                          {"codes": codes, "why": why})
        return codes

    async def close(self):
        await self.listener.stop()
        if self._sweeper:
            self._sweeper.cancel()
        await self.rpc.close()

    # ---------- quote + build + guard ----------
    async def _build_buy(self, mint: str, amount_usdc: float, refresh_pools: bool):
        await sync_fork_clock(self.rpc, self.upstream)
        b = await build_swap(USDC_MINT, mint, round(amount_usdc * 1e6), self.wallet, self.m.body.exec.max_slippage_bps.buy,
                             dexes=route_dexes(mint))
        intent = build_buy_intent(self.m, self.wallet, mint, amount_usdc, int(b["outAmount"]), self.spent_usdc)
        alts = await fetch_alts(self.rpc, list((b.get("addressesByLookupTableAddress") or {}).keys()))
        ixs = build_instructions(b)
        evil = getattr(self, "redteam", {}).get(mint)
        if evil:   # rehearsal red-team: a naive builder obeyed the token's injected text; the guard below must catch it
            from backend.copilot.redteam import obey_injection_ix
            ixs.append(obey_injection_ix(self.wallet, evil, round(amount_usdc * 1e6)))
        raw = bytes(assemble_v0(self.kp, ixs, alts, await self.rpc.latest_blockhash()))
        v, sim = await check_transaction(self.rpc, raw, intent, b, jev=self.jev, refresh_pools=refresh_pools)
        return b, raw, intent, v, sim

    # ---------- cards ----------
    async def stop_for(self, pool: str | None, user_stop: float | None = None) -> dict:
        """The stop a new position starts with: the user's own number, else the mandate's, else the coin's volatility."""
        if user_stop is not None:
            return {"stop_pct": float(user_stop), "basis": "you", "atr_pct": None}
        ms = self.m.body.exit.stop_loss_pct
        if ms is not None:
            return {"stop_pct": float(ms), "basis": "mandate", "atr_pct": None}
        from backend.market.volatility import suggest_for_pool
        return await suggest_for_pool(pool)

    async def create_card(self, mint: str, amount_usdc: float, metrics: dict | None = None, symbol: str = "",
                          pool: str | None = None, replaces: Card | None = None, coin_number: int | None = None,
                          announce: bool = True, stop: dict | None = None, risk_usdc: float | None = None,
                          cap_note: str | None = None) -> Card | None:
        why = self.buy_block_reason()
        if why:
            self.bus.emit("trade_failed", why[0].upper() + why[1:] + ". Selling is still allowed.",
                          {"mint": mint, "stage": "daily_loss", "coin_number": coin_number}, loud=True)
            return None
        try:
            b, raw, intent, v, sim = await self._build_buy(mint, amount_usdc, refresh_pools=True)
        except IntentError as e:
            self.bus.emit("trade_failed", trade_failed_speak(None, coin_number, str(e), stage="intent"),
                          {"mint": mint, "stage": "intent", "coin_number": coin_number}, loud=True)
            return None
        except JupiterError as ex:
            why = str(ex) or "Jupiter returned no quote"
            self.bus.emit("trade_failed", trade_failed_speak(None, coin_number, why, stage="quote"),
                          {"mint": mint, "stage": "quote", "coin_number": coin_number}, loud=True)
            return None
        if v.decision == "block":
            from backend.guard.flow import flow_summary
            try:
                flow = flow_summary(sim, self.wallet, mint, amount_usdc, int(b.get("outAmount") or 0))
            except Exception:  # noqa: BLE001 - the explanation is best effort; the block itself already happened
                flow = None
            self.bus.emit("guard_blocked", blocked_buy_speak(None, coin_number, v.reasons[:5], created=False),
                          {"mint": mint, "side": "buy", "reasons": v.reasons[:5], "coin_number": coin_number, "flow": flow,
                           "guard_run_id": getattr(v, "guard_run_id", None)}, loud=True)
            return None
        now = time.time()
        c = Card(id=new_card_id(), code=self.codes.new(), mint=mint, symbol_display=sanitize_symbol(symbol),
                 amount_usdc=amount_usdc, expected_out=int(b["outAmount"]), metrics=_numeric(metrics or {}),
                 sim={"token_out": sim.token_deltas.get(mint, 0) if sim else None,
                      "usdc_in": -(sim.token_deltas.get(USDC_MINT, 0)) if sim else None,
                      "sol_change_lamports": sim.sol_delta if sim else None,
                      "price_impact_pct": round(float(b.get("priceImpactPct") or 0) * 100, 3)},
                 guard={"decision": v.decision, "reasons": v.reasons[:5], "jev_risk": v.jev_risk,
                        "guard_run_id": getattr(v, "guard_run_id", None)},
                 created_at=now, expires_at=now + self.m.body.exec.approval_timeout_sec,
                 coin_number=coin_number if coin_number is not None else (replaces.coin_number if replaces else None),
                 mandate_id=self.m.mandate_id)
        if stop is None and replaces is not None and replaces.stop_loss_pct is not None:
            stop = {"stop_pct": replaces.stop_loss_pct, "basis": replaces.stop_basis, "atr_pct": replaces.atr_pct}
        if stop is None:
            stop = await self.stop_for(pool)
        c.stop_loss_pct, c.stop_basis, c.atr_pct = stop.get("stop_pct"), stop.get("basis"), stop.get("atr_pct")
        c.risk_usdc = risk_usdc if risk_usdc is not None else (replaces.risk_usdc if replaces else None)
        c.cap_note = cap_note if cap_note is not None else (replaces.cap_note if replaces else None)
        if c.stop_loss_pct:
            from backend.copilot.desk import loss_bounds
            c.loss_plan_usdc, c.loss_worst_usdc = loss_bounds(amount_usdc, float(c.stop_loss_pct),
                                                              self.m.body.exec.max_slippage_bps.stop_loss_sell)
        c.history.append({"ts": now, "event": "created"})
        self.cards[c.id] = c
        guard_evidence.observe(guard_evidence.bind, c.guard.get("guard_run_id"), c.id, c.code, "approval")
        self._pool[mint] = pool
        pre = f"The price moved, so card {replaces.code} is void. " if replaces else ""
        self.bus.emit("card_created", "" if not announce else
                      f"{pre}Card {c.code}: buy {money_words(round(amount_usdc * 1e6))}, guard {DECISION_LABEL[v.decision]}; details on screen. "
                      f"To approve, say \"approve {c.code}\". No answer in {self.m.body.exec.approval_timeout_sec} seconds counts as a reject.",
                      {"card": c.public()})
        return c

    def _expire_if_due(self, c: Card) -> bool:
        if c.status == "pending" and time.time() >= c.expires_at:
            c.status = "expired"
            c.history.append({"ts": time.time(), "event": "expired"})
            self.bus.emit("card_expired", expired_card_speak(c.code, c.coin_number), {"card_id": c.id, "coin_number": c.coin_number}, loud=True)
            return True
        return False

    async def expire_due(self) -> int:
        return sum(self._expire_if_due(c) for c in list(self.cards.values()))

    def start_sweeper(self, every_s: float = 1.0) -> None:
        async def loop():
            while True:
                await self.expire_due()
                await asyncio.sleep(every_s)
        if not self._sweeper or self._sweeper.done():
            self._sweeper = asyncio.create_task(loop())

    async def respond(self, card_id: str, action: str, utterance: str | None = None, amount_usdc: float | None = None,
                      channel: str = "voice", code: str | None = None, background: bool = False) -> dict:
        """Approval evidence (raw words + code, or the button code) and the mandate limits are checked here, synchronously.
        background=True: once approved, the swap (re-quote, guard, send, confirm) runs as a task and this returns
        {"ok": True, "status": "approved", "executing": True}; the result arrives as trade_executed / trade_failed /
        card_voided / guard_blocked events (duplex voice: the conversation is never held while a swap confirms)."""
        c = self.cards.get(card_id)
        if c is None:
            return {"ok": False, "reason": "card not found", "speak": "I could not find that card."}
        if self._expire_if_due(c) or c.status != "pending":
            return {"ok": False, "reason": f"card status is {c.status}", "speak": f"Card {c.code} is no longer valid and cannot be approved."}
        if action == "reject":  # safe direction: no check needed
            c.status = "rejected"
            c.history.append({"ts": time.time(), "event": "rejected", "channel": channel})
            self.bus.emit("card_rejected", f"OK, card {c.code} is rejected.", {"card_id": c.id})
            return {"ok": True, "status": c.status}
        if action not in ("approve", "modify"):
            return {"ok": False, "reason": "unknown action"}
        amount = c.amount_usdc if action == "approve" else amount_usdc
        # 0. daily max loss: no new buy once today's loss reached the limit (sells / de-risking always allowed)
        if c.kind == "buy":
            block = self.buy_block_reason()
            if block:
                c.status = "voided"
                c.history.append({"ts": time.time(), "event": "voided", "why": "daily_max_loss"})
                speak = f"Card {c.code} is void: {block}. Selling is still allowed."
                self.bus.emit("card_voided", speak, {"card_id": c.id, "why": "daily_max_loss", "coin_number": c.coin_number}, loud=True)
                return {"ok": False, "reason": block, "status": c.status, "speak": speak}
        # 1. approval evidence (buys: "approve" + the card code; de-risking cards: a plain "yes" to the offer is enough)
        if channel == "voice":
            if action == "approve" and c.derisk and c.kind in ("sell", "adjust_exit"):
                why = verify_derisk_yes(utterance or "", c.code)
            else:
                why = verify_approval(utterance or "", c.code) if action == "approve" else verify_modify(utterance or "", c.code, amount or 0)
        elif channel == "button":
            why = None if self.ui_button_enabled and code == c.code else ("the on-screen approve button is not enabled" if not self.ui_button_enabled else "the code does not match")
        else:
            why = "unknown channel"
        # 2. amount within the mandate (buy cards only; sell / exit-rule cards have no buy amount)
        if why is None and c.kind == "buy":
            try:
                build_buy_intent(self.m, self.wallet, c.mint, float(amount or 0), 1, self.spent_usdc)
            except IntentError as e:
                why = str(e)
        if why:
            c.failed_attempts += 1
            c.history.append({"ts": time.time(), "event": "approval_rejected", "why": why})
            voided = c.failed_attempts >= MAX_FAILED_ATTEMPTS
            if voided:
                c.status = "voided"
            self.bus.emit("approval_rejected",
                          (f"I could not confirm the approval ({why}). " + (f"That is {c.failed_attempts} failures in a row, so card {c.code} is void." if voided else
                                                     (f"Card {c.code} is still open; say yes to do it, or no to skip it." if c.derisk else
                                                      f"Once more: card {c.code}, buy {money_words(round(c.amount_usdc * 1e6))}. To approve, say \"approve {c.code}\"."))),
                          {"card_id": c.id, "why": why, "attempts": c.failed_attempts})
            return {"ok": False, "reason": why, "status": c.status}
        c.status = "approved"
        c.history.append({"ts": time.time(), "event": "approved", "channel": channel, "amount": amount})
        if background:
            t = asyncio.create_task(self._execute_reported(c, float(amount)))
            self._exec_tasks.add(t)
            t.add_done_callback(self._exec_tasks.discard)
            return {"ok": True, "status": "approved", "executing": True, "code": c.code}
        return await self._execute(c, float(amount))

    async def _execute_reported(self, c: Card, amount: float) -> None:
        try:
            await self._execute(c, amount)
        except asyncio.CancelledError:
            raise
        except Exception as ex:  # noqa: BLE001  a crash must never leave the card "approved" silently
            if c.status == "approved":
                c.status = "failed"
            self.bus.emit("trade_failed", trade_failed_speak(c.code, c.coin_number, type(ex).__name__, stage="execute"),
                          {"card_id": c.id, "stage": "execute", "error": type(ex).__name__, "coin_number": c.coin_number}, loud=True)

    async def _execute(self, c: Card, amount: float) -> dict:
        if c.kind == "sell":
            return await self._execute_sell(c)
        if c.kind == "adjust_exit":
            return await self._execute_adjust(c)
        e = self.m.body.exec
        block = self.buy_block_reason()          # re-checked right before sending (a stop-out may have landed meanwhile)
        if block:
            c.status = "voided"
            self.bus.emit("card_voided", f"Card {c.code} is void: {block}. Selling is still allowed.",
                          {"card_id": c.id, "why": "daily_max_loss", "coin_number": c.coin_number}, loud=True)
            return {"ok": False, "status": c.status, "reason": block}
        try:
            b, raw, intent, v, sim = await self._build_buy(c.mint, amount, refresh_pools=True)
        except (IntentError, JupiterError) as ex:
            c.status = "failed"
            why = "expired quote" if "expir" in str(ex).lower() else (str(ex) or type(ex).__name__)
            self.bus.emit("trade_failed", trade_failed_speak(c.code, c.coin_number, why, stage="requote"),
                          {"card_id": c.id, "stage": "requote", "error": type(ex).__name__, "coin_number": c.coin_number}, loud=True)
            return {"ok": False, "status": c.status, "reason": "re-quote failed"}
        guard_evidence.observe(guard_evidence.bind, getattr(v, "guard_run_id", None), c.id, c.code, "approval_recheck")
        card_rate = c.expected_out / c.amount_usdc
        new_rate = int(b["outAmount"]) / amount
        dev = abs(new_rate / card_rate - 1) * 100
        if dev > e.reprice_max_deviation_pct:
            c.status = "voided"
            c.history.append({"ts": time.time(), "event": "voided", "deviation_pct": round(dev, 2)})
            self.bus.emit("card_voided", reprice_void_speak(c.code, c.coin_number, dev, e.reprice_max_deviation_pct),
                          {"card_id": c.id, "deviation_pct": round(dev, 2), "coin_number": c.coin_number}, loud=True)
            new = await self.create_card(c.mint, amount, c.metrics, c.symbol_display, self._pool.get(c.mint), replaces=c)
            c.replaced_by = new.id if new else None
            return {"ok": False, "status": "voided", "deviation_pct": round(dev, 2), "new_card": new.public() if new else None}
        if v.decision == "block" or (v.decision == "ask_human" and c.guard["decision"] == "allow"):
            c.status = "failed" if v.decision == "block" else "voided"
            self.bus.emit("guard_blocked",
                          blocked_buy_speak(c.code, c.coin_number, v.reasons[:5],
                                            resim_block=v.decision == "block",
                                            new_concern=v.decision != "block"),
                          {"card_id": c.id, "side": "buy", "reasons": v.reasons[:5], "coin_number": c.coin_number,
                           "guard_run_id": getattr(v, "guard_run_id", None)},
                          loud=True)
            return {"ok": False, "status": c.status, "reasons": v.reasons}
        usdc0 = await token_balance(self.rpc, self.wallet, USDC_MINT)
        tok0 = await token_balance(self.rpc, self.wallet, c.mint)
        rec = await self.executor.send(raw, "buy", "card", c.mint, intent.amount_in, intent.min_out,
                                       {"card_id": c.id, "deviation_pct": round(dev, 3)})
        if rec.error:
            c.status = "failed"
            self.bus.emit("trade_failed", trade_failed_speak(c.code, c.coin_number, rec.error[:120]),
                          {"card_id": c.id, "signature": rec.signature, "error": rec.error[:120], "coin_number": c.coin_number}, loud=True)
            return {"ok": False, "status": c.status, "signature": rec.signature, "error": rec.error}
        got = await token_balance(self.rpc, self.wallet, c.mint) - tok0
        spent = usdc0 - await token_balance(self.rpc, self.wallet, USDC_MINT)
        rec.received, rec.spent = got, spent
        self.executor.save()
        c.status = "executed"
        self.spent_usdc += amount
        pool = self._pool.get(c.mint)
        pos = self.positions.get(c.mint)
        fresh = not (pos and pos.status == "open")
        if fresh and hasattr(self.listener.ps, "unfreeze"):   # a new position follows mainnet again (an old rehearsal freeze is over)
            self.listener.ps.unfreeze(c.mint)
        # baseline for the exit rules: what these tokens sell for right after the fill (same quote the listener uses).
        # Exit rules measure the price move from here, so the round-trip fee is not mistaken for a price drop.
        liq, baseline = None, None
        if got > 0:
            try:
                q = await self.listener.ps.quote(c.mint, got, pool)
                liq, baseline = q.liquidity, (q.value_usdc if q.amount == got and q.value_usdc > 0 else None)
            except Exception:  # noqa: BLE001  the first successful poll becomes the baseline instead (never triggers that poll)
                liq, baseline = None, None
        if not fresh:
            pos.add_fill(got, spent, baseline)
        else:
            pos = Position(c.mint, c.symbol_display, pool, got, got, spent, await fork_clock(self.rpc), liq,
                           mandate_id=self.m.mandate_id, coin_number=c.coin_number,
                           ticker=c.ticker or ticker_for_mint(c.mint))
            if c.stop_loss_pct is not None:
                pos.stop_loss_pct = float(c.stop_loss_pct)      # per-trade stop (volatility or the user's own)
            if baseline:
                pos.set_exit_baseline(baseline, got)
                pos.last_ratio, pos.last_value_usdc, pos.price_at = 1.0, baseline, time.time()
            self.positions[c.mint] = pos
        cost = (1 - baseline / spent) * 100 if baseline and spent else None
        fees = (f" About {max(1, round(cost))} percent of that is fees and slippage if you sold right now; the stop counts the price move from here."
                if cost is not None and cost >= 0.05 else "")
        stop = getattr(self.positions.get(c.mint), "stop_loss_pct", None)
        watch = (f"Watching the {round(stop)} percent stop" if stop else "Watching it") + "; I'll offer to sell some if it climbs."
        self.bus.emit("trade_executed", f"Card {c.code} filled: bought for {money_words(spent)}. {watch}{fees}",
                      {"side": "buy", "card_id": c.id, "mint": c.mint, "symbol": c.symbol_display, "usdc_in": spent,
                       "tokens_out": got, "signature": rec.signature, "deviation_pct": round(dev, 3),
                       "exit_baseline_usdc": baseline, "roundtrip_cost_pct": round(cost, 3) if cost is not None else None})
        return {"ok": True, "status": c.status, "signature": rec.signature, "tokens_out": got, "usdc_in": spent}

    async def emergency_exit(self) -> list[dict]:
        self.bus.emit("emergency_exit", "Emergency exit: selling all positions now.", {"positions": len(self.positions)}, loud=True)
        return await self.listener.emergency_exit()


    def create_action_card(self, kind: str, mint: str, *, coin_number: int | None = None,
                           ticker: str | None = None, symbol: str = "", sell_fraction: float | None = None,
                           adjust: dict | None = None, announce: bool = True, derisk: bool | None = None,
                           offer: str | None = None) -> Card:
        """A sell or exit-rule card: no buy simulation. De-risking cards (sell part, take profit, raise the stop / break-even)
        can be approved with a plain "yes" to the agent's offer; anything that adds risk still needs the two-digit code."""
        now = time.time()
        c = Card(id=new_card_id(), code=self.codes.new(), mint=mint, symbol_display=symbol or (ticker or ""),
                 amount_usdc=0, expected_out=0, metrics={}, sim={"token_out": None, "usdc_in": None,
                                                                  "sol_change_lamports": None, "price_impact_pct": 0},
                 guard={"decision": "allow", "reasons": [], "jev_risk": None},
                 created_at=now, expires_at=now + self.m.body.exec.approval_timeout_sec,
                 coin_number=coin_number, mandate_id=self.m.mandate_id, kind=kind, ticker=ticker,
                 sell_fraction=sell_fraction, adjust=adjust)
        c.derisk = is_derisk(kind, adjust, self.positions.get(mint)) if derisk is None else bool(derisk)
        c.offer = offer
        c.history.append({"ts": now, "event": "created", "kind": kind})
        self.cards[c.id] = c
        if announce:
            self.bus.emit("card_created", self._action_card_speak(c), {"card": c.public()})
        return c

    # ---------- co-pilot: proactive de-risking offers only ----------
    def derisk_offer(self, pos) -> Card | None:
        """While holding, the co-pilot speaks only to offer de-risking (never to suggest buying more): up >= 10% -> sell
        half; up >= 20% after that -> move the stop to break-even. One offer per level per position, one open offer at a time."""
        from backend.config import DERISK_BREAK_EVEN_UP_PCT, DERISK_SELL_HALF_UP_PCT
        r = pos.last_ratio
        if r is None or pos.status != "open" or pos.held_tokens <= 0:
            return None
        if any(c.status == "pending" and c.offer and c.mint == pos.mint for c in self.cards.values()):
            return None
        made = getattr(pos, "offers_made", None)
        if made is None:
            made = pos.offers_made = []
        # "up N percent" is the position's P&L after fees and slippage (what selling now would return vs what was paid),
        # the same number the top bar's Today shows, not the raw price move since the fill
        pct = pos.pnl_pct()
        up = pct if pct is not None else (r - 1) * 100
        ref = pos.ticker or (("coin " + str(pos.coin_number)) if pos.coin_number is not None else "your position")
        say_up = f"{ref[:1].upper() + ref[1:]} is up {up:.0f} percent."
        if up >= DERISK_SELL_HALF_UP_PCT and "sell_half" not in made:
            made.append("sell_half")
            offer = say_up + " Want to sell half?"
            c = self.create_action_card("sell", pos.mint, coin_number=pos.coin_number, ticker=pos.ticker, symbol=pos.symbol_display,
                                        sell_fraction=0.5, announce=False, derisk=True, offer=offer)
        elif up >= DERISK_BREAK_EVEN_UP_PCT and "break_even" not in made and (pos.stop_loss_pct or 1) > 0:
            made.append("break_even")
            offer = say_up + " Want to move the stop to break-even?"
            c = self.create_action_card("adjust_exit", pos.mint, coin_number=pos.coin_number, ticker=pos.ticker,
                                        symbol=pos.symbol_display, adjust={"break_even": True}, announce=False, derisk=True, offer=offer)
        else:
            return None
        self.bus.emit("derisk_offer", offer, {"card": c.public(), "up_pct": round(up, 1)}, loud=True)
        return c

    def _action_card_speak(self, c: Card) -> str:
        ref = c.ticker or (("coin " + str(c.coin_number)) if c.coin_number is not None else "this position")
        if c.kind == "sell":
            pct = int(round((c.sell_fraction or 1) * 100))
            what = "sell " + str(pct) + " percent of " + ref
        else:
            adj = c.adjust or {}
            if adj.get("break_even"):
                what = "move the stop on " + ref + " to break-even"
            elif adj.get("stop_loss_pct") is not None:
                what = "set the stop on " + ref + " to " + str(adj["stop_loss_pct"]).rstrip("0").rstrip(".") + " percent"
            elif adj.get("take_profit_pct") is not None:
                what = "set take profit on " + ref + " to " + str(adj["take_profit_pct"]).rstrip("0").rstrip(".") + " percent"
            else:
                what = "change the exit rule on " + ref
        if c.derisk:
            return ("Card " + c.code + ": " + what + ". Say yes to do it.")
        return ("Card " + c.code + ": " + what + ". To approve, say \"approve " + c.code + "\".")

    async def _execute_sell(self, c: Card) -> dict:
        pos = self.positions.get(c.mint)
        if not pos or pos.status != "open" or pos.held_tokens <= 0:
            c.status = "failed"
            self.bus.emit("trade_failed",
                          "Card " + c.code + " could not sell: no open position. Nothing was sold.",
                          {"card_id": c.id, "kind": "sell"}, loud=True)
            return {"ok": False, "status": c.status, "reason": "no open position"}
        frac = c.sell_fraction if c.sell_fraction is not None else 1.0
        amt = pos.held_tokens if frac >= 1 - 1e-9 else max(1, math.floor(pos.held_tokens * frac))
        ok = await self.listener.sell(pos, amt, "manual")
        if ok:
            c.status = "executed"
            c.history.append({"ts": time.time(), "event": "executed", "fraction": frac, "amount": amt})
            ref = c.ticker or (("coin " + str(c.coin_number)) if c.coin_number is not None else "the position")
            from backend.execute.listener import closed_words, money_words
            got = pos.sells[-1]["usdc"] if pos.sells else 0
            left = " The rest stays monitored." if pos.status == "open" and pos.held_tokens > 0 else closed_words(pos)
            self.bus.emit("trade_executed",
                          "Card " + c.code + " filled: sold " + str(int(round(frac * 100))) + " percent of " + ref
                          + ", got back " + money_words(got) + "." + left,
                          {"side": "sell", "card_id": c.id, "mint": c.mint, "fraction": frac, "amount": amt,
                           "held_after": pos.held_tokens})
            return {"ok": True, "status": c.status, "held_after": pos.held_tokens}
        c.status = "failed"
        c.history.append({"ts": time.time(), "event": "failed"})
        return {"ok": False, "status": c.status, "reason": "sell failed"}

    async def _execute_adjust(self, c: Card) -> dict:
        pos = self.positions.get(c.mint)
        adj = c.adjust or {}
        ref = c.ticker or (("coin " + str(c.coin_number)) if c.coin_number is not None else "the position")
        if not pos or pos.status not in ("open", "stuck"):
            c.status = "failed"
            self.bus.emit("trade_failed",
                          "Card " + c.code + " could not be applied: " + ref + " is not an open position.",
                          {"card_id": c.id, "kind": "adjust_exit"}, loud=True)
            return {"ok": False, "status": c.status, "reason": "no open position"}
        if adj.get("break_even") and pos.last_ratio is not None and pos.last_ratio <= 1.0:
            c.status = "failed"
            self.bus.emit("trade_failed",
                          "Card " + c.code + " was not applied: " + ref + " is not above break-even.",
                          {"card_id": c.id, "kind": "adjust_exit"}, loud=True)
            return {"ok": False, "status": c.status, "reason": "not above break-even"}
        if adj.get("break_even"):
            pos.stop_loss_pct = 0.0
        if adj.get("stop_loss_pct") is not None:
            pos.stop_loss_pct = float(adj["stop_loss_pct"])
        if adj.get("take_profit_pct") is not None:
            pos.take_profit_ladder = [{"multiple": 1 + float(adj["take_profit_pct"]) / 100, "sell_pct": 100}]
            pos.tp_done = []
        c.status = "executed"
        c.history.append({"ts": time.time(), "event": "executed", "adjust": adj})
        hook = getattr(self, "note_override", None)
        if callable(hook):
            hook({"mint": c.mint, "coin_number": c.coin_number, "ticker": c.ticker, "adjust": adj, "card": c.code})
        self.bus.emit("trade_executed",
                      "Card " + c.code + " approved: the new exit rule is on " + ref + ".",
                      {"side": "adjust", "card_id": c.id, "mint": c.mint, "adjust": adj})
        return {"ok": True, "status": c.status}

    def status(self) -> dict:
        return {"mandate_id": self.m.mandate_id, "wallet": self.wallet, "spent_usdc": self.spent_usdc,
                "cards": [c.public() for c in self.cards.values()],
                "positions": [{**asdict(p), "mark_usdc": p.mark_usdc(), "pnl_usdc": p.pnl_usdc()} for p in self.positions.values()],
                "trades": [asdict(r) for r in self.executor.records],
                "events": [ev.as_dict() for ev in self.bus.history[-50:]]}
