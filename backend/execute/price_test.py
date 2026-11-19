"""Rehearsal-only price tests on the local Surfpool fork.

A throwaway "whale" wallet swaps against the coin's pool so the price crosses the position's take-profit or
stop-loss line; the normal listener then sells as usual and its trade_executed event is what the agent reads.
Local fork only (assert_local); the orchestrator also refuses it in live discovery / research mode.
Constant-product approximation: price ~ quote_reserve / base_reserve, so a price factor f needs
quote_in = q * (sqrt(f) - 1) (pump) or base_in = b * (sqrt(1 / f) - 1) (dump). The listener's measured ratio is
re-read after every nudge, so the approximation only has to be in the right direction.
"""
from __future__ import annotations

import asyncio
import base64
import math

from solders.pubkey import Pubkey

from backend.config import JUPITER_ROUTE_DEXES, WSOL_MINT, settings
from backend.discovery.free_checks import PUMPSWAP_PROGRAM, RAYDIUM_CPMM_PROGRAM
from backend.execute.executor import assert_local
from backend.solana_utils import load_or_create_keypair, send_and_confirm, sync_fork_clock
from backend.trade.jupiter import assemble_v0, build_instructions, build_swap, fetch_alts

WHALE_KEY = settings.trading_keypair_path.parent / "whale-keypair.json"
# Aim just past the trigger line, never far past it: a stop at 11% should sell around -11 to -12%, not -24%.
# Each nudge re-reads the listener's measured ratio, so small steps converge on the line from the near side.
TP_PAST = 1.006          # target x 1.006: about half a percent past the take-profit / offer line
SL_PAST = 0.994          # target x 0.994: about half a percent past the stop
MIN_NUDGE = 0.004        # once at the line, nudge by 0.4% at a time
MAX_ROUNDS = 8


async def pool_info(rpc, pool: str, mint: str) -> dict | None:
    """Vaults of a PumpSwap or Raydium CPMM pool, oriented so `base` is the coin."""
    acc = (await rpc.call("getAccountInfo", [pool, {"encoding": "base64"}]))["value"]
    if acc is None:
        return None
    d = base64.b64decode(acc["data"][0])
    P = lambda o: str(Pubkey.from_bytes(d[o:o + 32]))  # noqa: E731
    if acc["owner"] == PUMPSWAP_PROGRAM and len(d) >= 203:
        bm, qm, bv, qv = P(43), P(75), P(139), P(171)
    elif acc["owner"] == RAYDIUM_CPMM_PROGRAM and len(d) >= 232:
        bv, qv, bm, qm = P(72), P(104), P(168), P(200)
    else:
        return None
    if bm != mint:
        bm, qm, bv, qv = qm, bm, qv, bv
    return {"base_vault": bv, "quote_vault": qv, "quote_mint": qm}


async def reserves(rpc, info: dict) -> tuple[int, int]:
    b = int((await rpc.call("getTokenAccountBalance", [info["base_vault"]]))["value"]["amount"])
    q = int((await rpc.call("getTokenAccountBalance", [info["quote_vault"]]))["value"]["amount"])
    return b, q


async def whale_swap(rpc, kp, inp: str, out: str, amount: int) -> tuple[str, str | None]:
    await sync_fork_clock(rpc, settings.solana_rpc_url)
    b = await build_swap(inp, out, amount, str(kp.pubkey()), 9900, dexes=JUPITER_ROUTE_DEXES, maxAccounts=40)
    alts = await fetch_alts(rpc, list((b.get("addressesByLookupTableAddress") or {}).keys()))
    raw = bytes(assemble_v0(kp, build_instructions(b), alts, await rpc.latest_blockhash()))
    return await send_and_confirm(rpc, raw)


async def _fund(rpc, owner: str, mint: str, amount: int) -> None:
    """Fork cheat codes: give the whale what it needs to swap (never available on mainnet)."""
    await rpc.call("surfnet_setAccount", [owner, {"lamports": 1_000_000 * 10**9}])
    if mint == WSOL_MINT:
        return
    prog = (await rpc.call("getAccountInfo", [mint, {"encoding": "base64"}]))["value"]["owner"]
    await rpc.call("surfnet_setTokenAccount", [owner, mint, {"amount": int(amount)}, prog])


async def move_price(rpc, kp, pool: str, mint: str, factor: float) -> dict:
    info = await pool_info(rpc, pool, mint)
    if info is None:
        return {"err": "unsupported pool"}
    b, q = await reserves(rpc, info)
    owner = str(kp.pubkey())
    if factor > 1:
        amt = max(1, int(q * (math.sqrt(factor) - 1)))
        await _fund(rpc, owner, info["quote_mint"], amt * 2)
        sig, err = await whale_swap(rpc, kp, info["quote_mint"], mint, amt)
    else:
        amt = max(1, int(b * (math.sqrt(1 / factor) - 1)))
        await _fund(rpc, owner, mint, amt + 10)
        sig, err = await whale_swap(rpc, kp, mint, info["quote_mint"], amt)
    b2, q2 = await reserves(rpc, info)
    return {"sig": sig, "err": err, "pool_price_factor": round((q2 / b2) / (q / b), 4) if b and b2 else None}


def nudge_factor(cur: float, target: float, kind: str) -> float:
    """Pool price factor that moves the measured ratio from cur to just past target (and no further)."""
    if kind == "take_profit":
        return max(1 + MIN_NUDGE, target * TP_PAST / cur)
    return min(1 - MIN_NUDGE, target * SL_PAST / cur)


def _past(ratio, target: float, kind: str) -> bool:
    if ratio is None:
        return False
    return ratio >= target if kind == "take_profit" else ratio <= target


def target_ratio(pos, ex, kind: str) -> float | None:
    if kind == "stop_loss":
        sl = getattr(pos, "stop_loss_pct", None)
        sl = sl if sl is not None else ex.stop_loss_pct
        if sl is None:
            from backend.market.volatility import FALLBACK_STOP_PCT
            sl = FALLBACK_STOP_PCT
        return 1 - sl / 100
    own = getattr(pos, "take_profit_ladder", None)
    ladder = own if own is not None else list(ex.take_profit_ladder or ())
    left = [(s.multiple if hasattr(s, "multiple") else s["multiple"]) for i, s in enumerate(ladder) if i not in pos.tp_done]
    if ladder:
        return min(left) if left else None
    # no automatic take profit: push the price just past the co-pilot's "sell half?" offer level instead. The offer
    # counts the position's P&L after fees (realized + what selling now returns - cost), so aim at the price move that
    # makes that P&L reach the level, not at a bare +10% price move (which can still be a loss after fees).
    from backend.config import DERISK_SELL_HALF_UP_PCT
    return offer_ratio(pos, DERISK_SELL_HALF_UP_PCT)


def offer_ratio(pos, up_pct: float) -> float:
    """Price move since the fill (the listener's ratio) at which the position's P&L after fees is up `up_pct` percent."""
    ref, held, entry = getattr(pos, "exit_ref_per_token", None), getattr(pos, "held_tokens", 0), getattr(pos, "entry_usdc", 0)
    if not ref or not held or not entry:
        return 1 + up_pct / 100
    return (entry * (1 + up_pct / 100) - getattr(pos, "realized_usdc", 0)) / (ref * held)


def _offers(engine, mint: str) -> list:
    return [c for c in getattr(engine, "cards", {}).values() if getattr(c, "offer", None) and c.mint == mint]


def _outcome(pos, sells0: int, kind: str, offer_target: bool, offers0: int, engine) -> str | None:
    """'ok' once the test's own rule sold (or, with no take-profit rule, the sell-half offer was made); otherwise why not
    yet. A sell through another rule, or a position the sells gave up on, is not a pass."""
    new = pos.sells[sells0:]
    if any(s.get("trigger") == kind for s in new):
        return "ok"
    if offer_target and len(_offers(engine, pos.mint)) > offers0:
        return "ok"
    if new:
        return "other:" + str(new[-1].get("trigger"))
    if pos.status == "stuck":
        return "stuck"
    if pos.status != "open":
        return "other:closed"
    return None


def _has_ladder(pos, ex) -> bool:
    own = getattr(pos, "take_profit_ladder", None)
    return bool(own if own is not None else list(ex.take_profit_ladder or ()))


async def run_price_test(engine, pos, kind: str, rounds: int = MAX_ROUNDS) -> dict:
    assert_local(engine.rpc)
    ex = engine.m.body.exit
    bus = engine.bus
    offer_target = kind == "take_profit" and not _has_ladder(pos, ex)
    if offer_target:
        if any(c.status == "pending" for c in _offers(engine, pos.mint)):
            bus.emit("price_test", "The sell-half offer is already waiting for your answer.", {"kind": kind, "ok": True, "steps": []})
            return {"ok": True, "steps": []}
        made = getattr(pos, "offers_made", None)
        if made and "sell_half" in made:
            made.remove("sell_half")       # rehearsal: let the offer be made again, or the test could never pass twice
    target = target_ratio(pos, ex, kind)
    if target is None or not pos.pool:
        bus.emit("price_test", "There is no take-profit step left to test." if target is None else "This position has no pool to test with.",
                 {"kind": kind, "ok": False})
        return {"ok": False}
    bus.emit("price_test", "", {"kind": kind, "target_ratio": target, "started": True})
    kp, _ = load_or_create_keypair(WHALE_KEY)
    sells0 = len(pos.sells)
    offers0 = len(_offers(engine, pos.mint))
    steps = []
    await freeze_for_rehearsal(engine, pos.mint)
    pos.rehearsal = kind               # the listener sells this position only through the rule under test
    result = None
    try:
        for _ in range(rounds):
            factor = nudge_factor(pos.last_ratio or 1.0, target, kind)
            seen_seq, seen_at = getattr(pos, "mark_seq", None), getattr(pos, "price_at", None)
            steps.append(await move_price(engine.rpc, kp, pos.pool, pos.mint, factor))
            for _ in range(int((ex.poll_interval_sec * 3 + 10) / 0.5)):
                result = _outcome(pos, sells0, kind, offer_target, offers0, engine)
                if result is not None:
                    break
                fresh = (getattr(pos, "mark_seq", None) != seen_seq) if seen_seq is not None else (getattr(pos, "price_at", None) != seen_at)
                if fresh and not _past(pos.last_ratio, target, kind):
                    break          # a fresh reading still short of the line: nudge again (from the near side)
                await asyncio.sleep(0.5)
            if result is not None:
                break
    except Exception as e:  # noqa: BLE001
        bus.emit("price_test", f"The test price move failed ({type(e).__name__}); nothing was sold.", {"kind": kind, "ok": False, "steps": steps})
        return {"ok": False, "error": type(e).__name__}
    finally:
        pos.rehearsal = None
        release_rehearsal(engine, pos)
    if result == "ok":
        bus.emit("price_test", "", {"kind": kind, "ok": True, "steps": steps})
        return {"ok": True, "steps": steps}
    rule = "stop loss" if kind == "stop_loss" else "take profit"
    why = ("The sells kept failing, so the position is marked stuck; nothing more is sold automatically." if result == "stuck"
           else f"The position was sold by another rule ({result.split(':', 1)[1].replace('_', ' ')}), not the {rule}." if result
           else f"The test price move did not reach the {rule}; nothing was sold.")
    bus.emit("price_test", why, {"kind": kind, "ok": False, "steps": steps, "outcome": result})
    return {"ok": False, "steps": steps, "outcome": result}


async def freeze_for_rehearsal(engine, mint: str) -> None:
    """The listener normally refetches the pool from mainnet before every quote, which would wipe the whale's move.
    Freeze the coin under the listener lock, so no poll that is already resetting the pool overlaps the first whale swap."""
    lst = getattr(engine, "listener", None)
    ps = getattr(lst, "ps", None)
    if hasattr(ps, "freeze"):
        async with lst._lock:
            ps.freeze(mint)


def release_rehearsal(engine, pos) -> None:
    """Follow mainnet again only once the position is gone. While it is still open (e.g. a take-profit sold half),
    its peak / trailing-stop state came from the rehearsal price, and snapping back to the mainnet price would fire a
    trailing stop nobody asked for; the coin stays on its fork-only price until the position closes (or a new buy)."""
    ps = getattr(getattr(engine, "listener", None), "ps", None)
    if hasattr(ps, "unfreeze") and (pos.status != "open" or pos.held_tokens <= 0):
        ps.unfreeze(pos.mint)
