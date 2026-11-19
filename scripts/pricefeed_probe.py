"""Does the fork listener see REAL mainnet price moves? (local Surfpool fork only, temporary wallet, never mainnet)

Buys a small amount of a busy coin on the main fork through the normal card path, then every few seconds compares
  fork:    ForkPriceSource.quote (what the listener would get by selling now on the fork)
  mainnet: Jupiter /build outAmount for the same sell (mainnet state, same route DEXes) and the DexScreener price.
--live-trigger: also run the listener with the normal exit rules (--tp multiple, --sl percent; the exit rules measure the
price move since the fill, so the round-trip fee alone must not fire them) and wait for an automatic sell from a real move.

Usage: python scripts/pricefeed_probe.py --mint <mint> --pool <pool> [--usd 0.5] [--minutes 4] [--live-trigger --tp 1.01 --sl 1]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from backend.config import JUPITER_ROUTE_DEXES, USDC_MINT, settings  # noqa: E402
from backend.events import EventBus  # noqa: E402
from backend.execute.engine import TradingEngine  # noqa: E402
from backend.execute.price_source import PriceFeedDown  # noqa: E402
from backend.mandate.schema import MandateBody  # noqa: E402
from backend.solana_utils import Rpc, sync_fork_clock  # noqa: E402
from backend.trade.jupiter import build_swap  # noqa: E402
from scripts.m3_run import smoke_mandate  # noqa: E402

RUNS = Path(__file__).resolve().parent.parent / "runs"


def ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


async def dexscreener_price(c: httpx.AsyncClient, pool: str) -> float | None:
    try:
        r = await c.get(f"https://api.dexscreener.com/latest/dex/pairs/solana/{pool}", timeout=8)
        return float(r.json()["pairs"][0]["priceUsd"])
    except Exception:  # noqa: BLE001
        return None


def mandate(tp_multiple: float | None, sl_pct: float | None):
    m = smoke_mandate()
    d = m.body.model_dump(mode="json")
    d["budget"].update(total_usdc=5, per_trade_usdc=1)
    if tp_multiple:
        d["exit"]["take_profit_ladder"] = [{"multiple": tp_multiple, "sell_pct": 100}]
    if sl_pct:
        d["exit"]["stop_loss_pct"] = sl_pct
    d["exit"]["poll_interval_sec"] = 3
    body = MandateBody.model_validate(d)   # the normal schema validation; nothing loosened
    return m.model_copy(update={"body": body})


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mint", required=True)
    ap.add_argument("--pool", required=True)
    ap.add_argument("--symbol", default="COIN")
    ap.add_argument("--usd", type=float, default=0.5)
    ap.add_argument("--minutes", type=float, default=4)
    ap.add_argument("--every", type=float, default=6)
    ap.add_argument("--live-trigger", action="store_true")
    ap.add_argument("--tp", type=float, default=1.01)
    ap.add_argument("--sl", type=float, default=1.0)
    a = ap.parse_args()
    m = mandate(a.tp, a.sl) if a.live_trigger else mandate(None, None)
    rpc = Rpc(settings.surfpool_rpc_url)
    await rpc.call("surfnet_resetNetwork")
    await sync_fork_clock(rpc, settings.solana_rpc_url)
    bus = EventBus()
    bus.subscribe(lambda ev: print(f"{ts()}  [event] {ev.type}: {ev.speak}") if ev.type not in ("price_test",) else None)
    eng = TradingEngine(m, bus=bus, log_path=RUNS / "pricefeed_probe_trades.json")
    w = eng.wallet
    await rpc.call("surfnet_setAccount", [w, {"lamports": 2_000_000_000}])
    await rpc.call("surfnet_setTokenAccount", [w, USDC_MINT, {"amount": 50_000_000}])
    out: dict = {"mint": a.mint, "pool": a.pool, "symbol": a.symbol, "usd": a.usd, "samples": [],
                 "refresh": bool(getattr(eng.listener.ps, "refresh", False)), "live_trigger": a.live_trigger}
    card = await eng.create_card(a.mint, a.usd, None, a.symbol, a.pool)
    assert card, "no card"
    r = await eng.respond(card.id, "approve", f"approve {card.code}")
    assert r.get("ok"), r
    pos = eng.positions[a.mint]
    print(f"{ts()}  bought {pos.held_tokens} base units for {pos.entry_usdc / 1e6:.6f} USDC on the fork")
    out["buy"] = {"t": ts(), "tokens": pos.held_tokens, "usdc": pos.entry_usdc}
    ps = eng.listener.ps
    async with httpx.AsyncClient() as c:
        if a.live_trigger:
            base = pos.entry_exit_value
            print(f"{ts()}  cost {pos.entry_usdc} | exit baseline (sell value right after the fill) {base} | round trip "
                  f"{pos.roundtrip_cost_pct:.2f}% | TP x{a.tp} -> {base * a.tp:.0f}, SL -{a.sl}% -> {base * (1 - a.sl / 100):.0f}"
                  f" | old rule would compare {base / pos.entry_usdc:.4f} with the SL line {1 - a.sl / 100:.4f}")
            out["baseline"] = {"t": ts(), "cost": pos.entry_usdc, "exit_baseline": base, "roundtrip_pct": pos.roundtrip_cost_pct,
                               "tp": a.tp, "sl": a.sl}
            eng.listener.start()
        end = time.time() + a.minutes * 60
        while time.time() < end:
            s = {"t": ts()}
            try:
                if not a.live_trigger:
                    q = await ps.quote(a.mint, pos.held_tokens, pos.pool)
                    s["fork_usdc"] = q.value_usdc
                else:
                    s["fork_price_ratio"] = round(pos.last_ratio, 6) if pos.last_ratio else None
                    s["fork_value_usdc"] = pos.last_value_usdc
            except PriceFeedDown as e:
                s["fork_err"] = str(e)
            try:
                b = await build_swap(a.mint, USDC_MINT, pos.initial_tokens, w, 300, dexes=JUPITER_ROUTE_DEXES)
                s["mainnet_jup_usdc"] = int(b["outAmount"])
            except Exception as e:  # noqa: BLE001
                s["mainnet_err"] = type(e).__name__
            s["dexscreener_usd"] = await dexscreener_price(c, a.pool)
            out["samples"].append(s)
            print(f"{s['t']}  " + "  ".join(f"{k}={v}" for k, v in s.items() if k != "t"))
            if a.live_trigger and pos.status != "open":
                break
            await asyncio.sleep(a.every)
    await eng.listener.stop()
    out["position"] = {"status": pos.status, "sells": pos.sells, "last_ratio": pos.last_ratio, "peak_ratio": pos.peak_ratio}
    out["events"] = [e.as_dict() for e in bus.history if e.type in ("exit_triggered", "trade_executed", "sell_failed", "price_feed_down", "guard_blocked")]
    RUNS.mkdir(exist_ok=True)
    p = RUNS / f"pricefeed_probe_{datetime.now():%Y%m%d_%H%M%S}.json"
    p.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    print("saved", p.name)
    await eng.close()
    await rpc.close()


if __name__ == "__main__":
    asyncio.run(main())
