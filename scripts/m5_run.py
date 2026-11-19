"""M5 live acceptance (local Surfpool fork only, temporary wallet, never mainnet).

Main fork (8899):
  1. card A: wrong code -> approval refused, card stays pending;
  2. card A: simulate "price moved 10% after the card" (card quote raised 10%) -> re-quote after approval deviates > 5% -> void, new card B;
  3. card C: timeout (180 s shortened to 2 s in the test) -> card_expired, later approval refused;
  4. card B: approve via the API with the words "approve XX" -> re-quote + simulate + guard -> fill -> listener starts monitoring;
  5. a whale wallet really buys on the fork, pushing the price above 2x -> listener take-profit sells 50%;
  6. the whale dumps below -30% of entry -> listener stop-loss sells the rest.
Scratch fork (8999, --extras): price feed down only alerts; button-channel buy; timeTravel 25 hours -> max-hold sell; emergency exit.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import math
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
from solders.pubkey import Pubkey  # noqa: E402

from backend.app import app  # noqa: E402
from backend.config import JUPITER_ROUTE_DEXES, USDC_MINT, WSOL_MINT, settings  # noqa: E402
from backend.events import EventBus  # noqa: E402
from backend.execute.engine import TradingEngine  # noqa: E402
from backend.execute.listener import fork_clock  # noqa: E402
from backend.execute.price_source import MainnetPriceSource  # noqa: E402
from backend.judge.jev import JEVClient  # noqa: E402
from backend.solana_utils import Rpc, load_or_create_keypair, send_and_confirm, sync_fork_clock, token_balance  # noqa: E402
from backend.trade.jupiter import assemble_v0, build_instructions, build_swap, fetch_alts  # noqa: E402
from backend.execute.price_test import WHALE_KEY, whale_swap  # noqa: E402  (shared with the UI price test)
from scripts.m3_run import smoke_mandate  # noqa: E402

RUNS = Path(__file__).resolve().parent.parent / "runs"
SYMBOL = "GATO"


def load_coin(symbol: str) -> tuple[dict, dict]:
    m3 = json.loads(sorted(RUNS.glob("m3_*.json"))[-1].read_text(encoding="utf-8"))
    m4 = json.loads(sorted(RUNS.glob("m4_2*.json"))[-1].read_text(encoding="utf-8"))
    c = next(x for x in m3["coins"] if x["symbol_display"] == symbol)
    r = next(x for x in m4["results"] if x["mint"] == c["mint"])
    assert r["status"] == "pass", "only buy coins that passed M4"
    met = {**c["metrics"], "roundtrip_loss_pct": c["sell_sim"]["roundtrip_loss_pct"],
           "top10_pct": r["facts"]["top10_pct"], "dev_pct": r["facts"]["dev_pct"], "holders": r["facts"]["holders"]}
    return c, met


async def pool_reserves(rpc, pool: str) -> tuple[int, int]:
    d = base64.b64decode((await rpc.call("getAccountInfo", [pool, {"encoding": "base64"}]))["value"]["data"][0])
    bv, qv = str(Pubkey.from_bytes(d[139:171])), str(Pubkey.from_bytes(d[171:203]))
    b = int((await rpc.call("getTokenAccountBalance", [bv]))["value"]["amount"])
    q = int((await rpc.call("getTokenAccountBalance", [qv]))["value"]["amount"])
    return b, q


async def wait_for(bus: EventBus, pred, timeout: float) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if any(pred(e) for e in bus.history):
            return True
        await asyncio.sleep(0.5)
    return False


def printer(ev):
    print(f"  [event] {ev.type}{' (loud)' if ev.loud else ''}: {ev.speak}")


async def api(method: str, path: str, **kw):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local") as c:
        r = await c.request(method, path, **kw)
        return r.json()


async def main_flow(out: dict) -> None:
    m = smoke_mandate()
    coin, met = load_coin(SYMBOL)
    mint, pool = coin["mint"], coin["metrics"]["pair"]
    rpc = Rpc(settings.surfpool_rpc_url)
    await rpc.call("surfnet_resetNetwork")
    await sync_fork_clock(rpc, settings.solana_rpc_url)
    bus = EventBus()
    bus.subscribe(printer)
    eng = TradingEngine(m, bus=bus, jev=JEVClient(), log_path=RUNS / "m5_trades_main.json")
    app.state.engine = eng
    w = eng.wallet
    await rpc.call("surfnet_setAccount", [w, {"lamports": 2_000_000_000}])
    await rpc.call("surfnet_setTokenAccount", [w, USDC_MINT, {"amount": 50_000_000}])
    bal = lambda: asyncio.gather(token_balance(rpc, w, USDC_MINT), token_balance(rpc, w, mint), rpc.call("getBalance", [w]))  # noqa: E731
    u0, t0, s0 = await bal()
    out["start"] = {"usdc": u0, "token": t0, "sol": s0["value"]}
    print(f"trading wallet {w} (temporary fork wallet) USDC {u0 / 1e6:.2f} SOL {s0['value'] / 1e9:.3f}")
    amt = m.body.budget.per_trade_usdc

    print("\n[1] wrong code")
    A = await eng.create_card(mint, amt, met, SYMBOL, pool)
    wrong = f"{(int(A.code) + 11 - 10) % 90 + 10:02d}"
    r = await api("POST", f"/cards/{A.id}/respond", json={"action": "approve", "utterance": f"approve {wrong}"})
    out["wrong_code"] = {"card_code": A.code, "said": wrong, "resp": r, "card_status": A.status}
    assert not r["ok"] and A.status == "pending"

    print("\n[2] price deviation > 5%: the card quote is 10% better than now")
    A.expected_out = int(A.expected_out * 1.10)
    r = await api("POST", f"/cards/{A.id}/respond", json={"action": "approve", "utterance": f"OK, approve {A.code}"})
    out["reprice"] = {"resp": {k: v for k, v in r.items() if k != "new_card"}, "card_status": A.status}
    assert r["status"] == "voided" and r.get("new_card")
    B = eng.cards[r["new_card"]["id"]]

    print("\n[3] timeout (180 s shortened to 2 s in the test)")
    C = await eng.create_card(mint, amt, met, SYMBOL, pool)
    C.expires_at = time.time() + 2
    eng.start_sweeper(0.5)
    await asyncio.sleep(3)
    r = await api("POST", f"/cards/{C.id}/respond", json={"action": "approve", "utterance": f"approve {C.code}"})
    out["timeout"] = {"card_status": C.status, "late_approve": r}
    assert C.status == "expired" and not r["ok"]

    print(f"\n[4] approve card {B.code}")
    r = await api("POST", f"/cards/{B.id}/respond", json={"action": "approve", "utterance": f"approve {B.code}"})
    out["buy"] = r
    assert r["ok"], r
    pos = eng.positions[mint]
    b0, q0 = await pool_reserves(rpc, pool)
    out["entry"] = {"tokens": pos.initial_tokens, "usdc": pos.entry_usdc, "pool_base": b0, "pool_quote": q0}
    eng.listener.ps.freeze(mint)   # the whale below moves the fork-only price; do not refetch the pool from mainnet
    eng.listener.start()

    whale, _ = load_or_create_keypair(WHALE_KEY)
    wk = str(whale.pubkey())
    await rpc.call("surfnet_setAccount", [wk, {"lamports": 1_000_000 * 10**9}])
    print(f"\n[5] whale {wk[:8]}... pumps above 2x")
    out["whale"] = []
    for i in range(6):
        b, q = await pool_reserves(rpc, pool)
        ratio = (q / b) / (q0 / b0)
        if ratio >= 2.15:
            break
        dq = int(q * (math.sqrt(2.25 / ratio) - 1) * 1.02)
        sig, err = await whale_swap(rpc, whale, WSOL_MINT, mint, dq)
        out["whale"].append({"side": "buy", "lamports": dq, "sig": sig, "err": err, "pool_ratio_before": round(ratio, 3)})
        print(f"  whale buys {dq / 1e9:.1f} SOL, pool price multiple {ratio:.2f} -> ?  {sig[:16] if sig else ''} {err or ''}")
    b, q = await pool_reserves(rpc, pool)
    print(f"  pool price multiple {(q / b) / (q0 / b0):.2f}")
    tp = await wait_for(bus, lambda e: e.type == "trade_executed" and e.panel.get("trigger") == "take_profit", 60)
    out["take_profit_fired"] = tp
    assert tp, "take-profit did not fire"

    print("\n[6] whale dumps below -30%")
    b, q = await pool_reserves(rpc, pool)
    ratio = (q / b) / (q0 / b0)
    db = int(b * (math.sqrt(ratio / 0.55) - 1) * 1.03)
    have = await token_balance(rpc, wk, mint)
    if have < db:
        await rpc.call("surfnet_setTokenAccount", [wk, mint, {"amount": db + 10}, "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"])
    sig, err = await whale_swap(rpc, whale, mint, WSOL_MINT, db)
    b, q = await pool_reserves(rpc, pool)
    out["whale"].append({"side": "sell", "tokens": db, "sig": sig, "err": err, "pool_ratio_after": round((q / b) / (q0 / b0), 3)})
    print(f"  whale sells {db} tokens {sig[:16] if sig else ''} {err or ''}, pool price multiple {(q / b) / (q0 / b0):.2f}")
    sl = await wait_for(bus, lambda e: e.type == "trade_executed" and e.panel.get("trigger") == "stop_loss", 60)
    out["stop_loss_fired"] = sl
    await eng.listener.stop()
    u1, t1, s1 = await bal()
    out["end"] = {"usdc": u1, "token": t1, "sol": s1["value"]}
    out["position"] = eng.status()["positions"]
    out["trades"] = eng.status()["trades"]
    out["events"] = [e.as_dict() for e in bus.history]
    out["cards"] = [c.public() for c in eng.cards.values()]
    print(f"\ndone: USDC {u0 / 1e6:.4f} -> {u1 / 1e6:.4f}, {SYMBOL} {t0} -> {t1}, stop loss fired={sl}")
    await eng.close()
    await rpc.close()


async def extras(out: dict) -> None:
    """Scratch fork: price feed down / button channel / max hold (timeTravel) / emergency exit."""
    m = smoke_mandate()
    coin, met = load_coin(SYMBOL)
    mint, pool = coin["mint"], coin["metrics"]["pair"]
    url = settings.scratch_surfpool_rpc_url
    rpc = Rpc(url)
    await rpc.call("surfnet_resetNetwork")
    await sync_fork_clock(rpc, settings.solana_rpc_url)
    bus = EventBus()
    bus.subscribe(printer)
    eng = TradingEngine(m, rpc_url=url, bus=bus, jev=JEVClient(), ui_button_enabled=True, log_path=RUNS / "m5_trades_scratch.json")
    app.state.engine = eng
    w = eng.wallet
    await rpc.call("surfnet_setAccount", [w, {"lamports": 2_000_000_000}])
    await rpc.call("surfnet_setTokenAccount", [w, USDC_MINT, {"amount": 50_000_000}])

    print("\n[E1] button-channel buy (API switch on, with the on-screen code)")
    c = await eng.create_card(mint, 5.0, met, SYMBOL, pool)
    r = await api("POST", f"/cards/{c.id}/respond", json={"action": "approve", "channel": "button", "code": "00"})
    assert not r["ok"]
    r = await api("POST", f"/cards/{c.id}/respond", json={"action": "approve", "channel": "button", "code": c.code})
    out["button_buy"] = r
    assert r["ok"], r

    print("\n[E2] price feed down (MainnetPriceSource without a key) -> alert only, no sell")
    fork_ps = eng.listener.ps
    eng.listener.ps = MainnetPriceSource({mint: 6}, api_key="")
    fired = await eng.listener.poll_once()
    out["feed_down"] = {"fired": fired, "events": [e.speak for e in bus.of("price_feed_down")],
                        "held": eng.positions[mint].held_tokens}
    assert fired == [] and bus.of("price_feed_down")
    eng.listener.ps = fork_ps

    print("\n[E3] timeTravel 25 hours -> max-hold sell")
    t_before = await fork_clock(rpc)
    slot = await rpc.call("getSlot")
    await rpc.call("surfnet_timeTravel", [{"absoluteTimestamp": int((t_before + 25 * 3600) * 1000)}])
    t_after = await fork_clock(rpc)
    fired = await eng.listener.poll_once()
    out["max_hold"] = {"fork_clock_before": t_before, "fork_clock_after": t_after, "slot_before": slot, "fired": fired,
                       "position": eng.status()["positions"][0]}
    print(f"  fork clock +{(t_after - t_before) / 3600:.1f} hours, fired {fired}")

    print("\n[E4] emergency exit (API)")
    c = await eng.create_card(mint, 5.0, met, SYMBOL, pool)
    r = await api("POST", f"/cards/{c.id}/respond", json={"action": "modify", "amount_usdc": 4, "utterance": f"change {c.code} to 4 dollars and approve"})
    out["modify_buy"] = r
    r = await api("POST", "/emergency-exit")
    out["emergency"] = r
    out["scratch_trades"] = eng.status()["trades"]
    out["scratch_events"] = [e.as_dict() for e in bus.history]
    out["scratch_end"] = {"usdc": await token_balance(rpc, w, USDC_MINT), "token": await token_balance(rpc, w, mint)}
    await eng.close()
    await rpc.close()


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--extras", action="store_true", help="also run the extra tests on the scratch fork")
    ap.add_argument("--only-extras", action="store_true")
    a = ap.parse_args()
    out: dict = {}
    try:
        if not a.only_extras:
            await main_flow(out)
        if a.extras or a.only_extras:
            await extras(out)
    finally:
        p = RUNS / f"m5_{datetime.now():%Y%m%d_%H%M%S}.json"
        p.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        print(f"saved {p}")


if __name__ == "__main__":
    asyncio.run(main())
