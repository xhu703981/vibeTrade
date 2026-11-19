"""Text-mode probes for batch 2: wake-by-name, alerts, position management, majors.

  python scripts/batch2_probe.py
Uses the local Surfpool fork (8899) for buys/sells; never mainnet. No x402 research spend.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

ET = ZoneInfo("America/New_York")


def now_et() -> str:
    return datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S ET")


def ok(name: str, detail: str) -> None:
    print(f"OK  {name}: {detail}  [{now_et()}]")


def fail(name: str, detail: str) -> None:
    print(f"FAIL {name}: {detail}  [{now_et()}]")


# ---------- a) engagement ----------
def probe_a() -> bool:
    from fastapi.testclient import TestClient

    import backend.app as appmod
    from backend.voice.engagement import addressed_deterministic
    good = True
    if addressed_deterministic("two coffees please", "idle") is not False:
        fail("a-idle-coffee", "expected not addressed")
        good = False
    else:
        ok("a-idle-coffee", "IDLE + 'two coffees please' -> silent / not addressed")

    if addressed_deterministic("Vibe, how are my positions?", "idle") is not True:
        fail("a-wake", "expected wake")
        good = False
    else:
        ok("a-wake", "IDLE + 'Vibe, how are my positions?' -> ENGAGED")

    if addressed_deterministic("and how is coin 1?", "engaged") is not True:
        fail("a-followup", "follow-up without name should be addressed")
        good = False
    else:
        ok("a-followup", "ENGAGED follow-up without the name is answered")

    with TestClient(appmod.app) as c:
        r = c.post("/voice/partial", json={"text": "two coffees please", "final": True, "seq": 1, "mode": "idle"}).json()
        if r.get("addressed") is False and r.get("wake_name") is False:
            ok("a-partial-idle", f"POST /voice/partial idle coffee -> addressed={r.get('addressed')}")
        else:
            fail("a-partial-idle", str(r))
            good = False
        r2 = c.post("/voice/partial", json={"text": "Vibe, how are my positions?", "final": True, "seq": 2, "mode": "idle"}).json()
        if r2.get("addressed") is True and r2.get("wake_name") is True:
            ok("a-partial-wake", "wake name on /voice/partial")
        else:
            fail("a-partial-wake", str(r2))
            good = False
        r3 = c.post("/voice/partial", json={"text": "how are my positions?", "final": True, "seq": 3, "mode": "engaged"}).json()
        if r3.get("addressed") is True:
            ok("a-partial-followup", "ENGAGED follow-up addressed, no name needed")
        else:
            fail("a-partial-followup", str(r3))
            good = False
    return good


# ---------- b) alerts on a real (or test-threshold) price move ----------
async def probe_b(wait_s: float = 180.0) -> bool:
    import httpx

    from backend.config import ALERT_MARKET_MOVE_PCT, SOL_MINT
    from backend.execute.alerts import market_move, merge_speak, record_market_price
    from backend.market.ohlcv import _jupiter_price

    # Low test threshold so a small live move can fire within a few minutes.
    test_pct = 0.05
    started = time.time()
    print(f"  b: sampling SOL via Jupiter price API, test threshold {test_pct}% over up to {wait_s:.0f}s  [{now_et()}]")
    prices = []
    async with httpx.AsyncClient(timeout=8) as c:
        end = time.time() + wait_s
        while time.time() < end:
            px = await _jupiter_price(SOL_MINT, c)
            ts = time.time()
            if px:
                record_market_price(SOL_MINT, px, ts)
                prices.append((ts, px))
                mv = market_move(SOL_MINT, ts)
                if mv is not None:
                    print(f"    {now_et()}  SOL ${px:.4f}  window move {mv:+.3f}%")
                    if abs(mv) >= test_pct:
                        speak = merge_speak([f"SOL is {'up' if mv > 0 else 'down'} {abs(mv):.2f} percent in the last fifteen minutes".replace("  ", " ")])
                        ok("b-alert", f"would speak: {speak}  (n={len(prices)}, first={prices[0][1]:.4f}, last={px:.4f}, default cap is {ALERT_MARKET_MOVE_PCT}%)")
                        return True
            await asyncio.sleep(8)
    if len(prices) >= 2:
        first, last = prices[0][1], prices[-1][1]
        mv = (last / first - 1) * 100
        fail("b-alert", f"no move >= {test_pct}% in {wait_s:.0f}s (first ${first:.4f} last ${last:.4f} move {mv:+.3f}%). Samples={len(prices)}")
        return False
    fail("b-alert", "no Jupiter SOL prices received")
    return False


# ---------- c + d: fork trades ----------
async def probe_cd() -> bool:
    from backend.config import MAJORS, USDC_MINT, settings
    from backend.events import EventBus
    from backend.execute.engine import TradingEngine
    from datetime import datetime, timedelta, timezone
    from backend.mandate.schema import Mandate, MandateBody
    from backend.mandate.dialogue import compute_hash
    from backend.solana_utils import Rpc, token_balance
    from backend.orchestrator import Orchestrator

    good = True
    bus = EventBus()
    d = MandateBody().model_dump(mode="json")
    d["budget"].update(total_usdc=0.05, per_trade_usdc=0.01)
    d["exec"]["max_fee_sol"] = 0.002
    d["exit"].update(stop_loss_pct=1.5, take_profit_ladder=[{"multiple": 1.015, "sell_pct": 100}])
    body = MandateBody.model_validate(d)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    fa = now.isoformat().replace("+00:00", "Z")
    ea = (now + timedelta(hours=24)).isoformat().replace("+00:00", "Z")
    mid = f"m_{now:%Y%m%d}_batch2"
    m = Mandate(mandate_id=mid, frozen_at=fa, expires_at=ea, body=body, hash=compute_hash(mid, fa, ea, body.model_dump(mode="json")))

    orch = Orchestrator(bus)
    orch.mandate = m
    rpc = Rpc(settings.surfpool_rpc_url)
    try:
        eng = TradingEngine(m, bus=bus)
        orch.engine = eng
        w = eng.wallet
        try:
            await rpc.call("requestAirdrop", [w, 2_000_000_000])
        except Exception:
            pass
        await rpc.call("surfnet_setTokenAccount", [w, USDC_MINT, {"amount": 5_000_000}])  # 5 USDC
        usdc = await token_balance(rpc, w, USDC_MINT)
        ok("fund", f"wallet {w} USDC {usdc / 1e6:g} on fork")

        await orch.ensure_majors()
        await asyncio.sleep(0.2)
        # d: buy one cent of JUP
        r = await orch.place_orders(None, "buy one cent of JUP", "voice", wait=True)
        if not r.get("ok"):
            fail("d-jup-order", str(r))
            good = False
        else:
            cards = r.get("cards") or []
            summary = cards[0] if cards else None
            if not summary:
                fail("d-jup-order", f"no card in {r}")
                good = False
            else:
                cobj = next(c for c in eng.cards.values() if c.code == summary["code"])
                ok("d-jup-card", f"card {cobj.code} kind={cobj.kind} ticker={cobj.ticker} amount={cobj.amount_usdc} coin={cobj.coin_number}")
                ar = await eng.respond(cobj.id, "approve", f"approve {cobj.code}", channel="voice")
                if ar.get("ok") and cobj.status == "executed":
                    pos = eng.positions.get(MAJORS["JUP"])
                    ok("d-jup-fill", f"approve {cobj.code} -> {ar.get('status')} held={getattr(pos, 'held_tokens', None)}")
                else:
                    evs = [e.speak for e in bus.history if e.type in ("trade_executed", "trade_failed", "guard_blocked")]
                    fail("d-jup-fill", f"status={cobj.status} resp={ar} events={evs[-3:]}")
                    good = False

        # c: need an open position. If JUP filled, use that as coin 101; also try a meme if available.
        opens = [p for p in eng.positions.values() if p.status == "open"]
        if not opens:
            fail("c-sell-half", "no open position to sell (JUP buy did not fill)")
            return False

        pos = opens[0]
        if pos.coin_number is None:
            pos.coin_number = 101
        held0 = pos.held_tokens
        utt = f"sell half of coin {pos.coin_number}"
        r = await orch.manage_position(utt, "voice")
        if not r.get("ok"):
            fail("c-sell-half-card", str(r))
            good = False
        else:
            card = r["cards"][0]
            ok("c-sell-half-card", f"card {card['code']} fraction={card.get('sell_fraction')}")
            ar = await eng.respond(card["id"], "approve", f"approve {card['code']}", channel="voice")
            pos2 = eng.positions.get(pos.mint)
            if ar.get("ok") and pos2 and 0 < pos2.held_tokens < held0 and pos2.status == "open":
                ok("c-sell-half-fill", f"held {held0} -> {pos2.held_tokens}, still monitored ({pos2.status})")
            else:
                evs = [e.speak for e in bus.history if e.type in ("trade_executed", "trade_failed", "sell_failed", "guard_blocked")]
                fail("c-sell-half-fill", f"resp={ar} held {held0}->{getattr(pos2,'held_tokens',None)} ev={evs[-3:]}")
                good = False

        # bump last_ratio so break-even can apply
        pos = eng.positions.get(pos.mint)
        if pos and pos.last_ratio is None or (pos and pos.last_ratio <= 1):
            pos.last_ratio = 1.02
        r = await orch.manage_position("move my stop to break-even", "voice")
        if not r.get("ok"):
            fail("c-be-card", str(r))
            good = False
        else:
            card = r["cards"][0]
            ok("c-be-card", f"card {card['code']} adjust={card.get('adjust')}")
            ar = await eng.respond(card["id"], "approve", f"approve {card['code']}", channel="voice")
            pos = eng.positions.get(pos.mint)
            if ar.get("ok") and pos and pos.stop_loss_pct == 0.0:
                ok("c-be-applied", f"stop_loss_pct={pos.stop_loss_pct} (break-even); listener will use this override")
            else:
                fail("c-be-applied", f"resp={ar} stop={getattr(pos,'stop_loss_pct',None)}")
                good = False
        await eng.close()
    finally:
        await rpc.close()
    return good


async def main() -> int:
    print(f"batch2 probe start  [{now_et()}]")
    results = {}
    results["a"] = probe_a()
    # b: wait up to 2.5 minutes for a small SOL move
    results["b"] = await probe_b(45)
    try:
        results["cd"] = await probe_cd()
    except Exception as ex:
        fail("cd", f"{type(ex).__name__}: {ex}")
        results["cd"] = False
    print()
    print("SUMMARY", results, f"[{now_et()}]")
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
