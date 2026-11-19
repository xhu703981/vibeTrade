"""Poll a real GeckoTerminal pool for ~1 minute and report last-candle / live price changes.

  python scripts/price_probe.py
No x402 spend. Times are America/New_York.
"""
from __future__ import annotations

import asyncio
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402
from backend.market.ohlcv import HEADERS, fetch_ohlcv, fetch_pool_price  # noqa: E402

ET = ZoneInfo("America/New_York")
INTERVAL = 8
SAMPLES = 8  # ~56s plus fetch time


async def pick_pool() -> str:
    async with httpx.AsyncClient(timeout=20, headers=HEADERS) as c:
        r = await c.get("https://api.geckoterminal.com/api/v2/networks/solana/trending_pools", params={"page": 1})
        r.raise_for_status()
        for p in r.json().get("data") or []:
            addr = (p.get("attributes") or {}).get("address") or (p.get("id") or "").split("_", 1)[-1]
            if addr:
                return addr
    raise SystemExit("no trending pool from GeckoTerminal")


def ts() -> str:
    return datetime.now(ET).strftime("%H:%M:%S ET")


async def main() -> int:
    pool = await pick_pool()
    print(f"price probe start {ts()}  pool={pool[:8]}…  interval={INTERVAL}s")
    rows = []
    for i in range(SAMPLES):
        t0 = time.time()
        price = await fetch_pool_price(pool)
        candles = await fetch_ohlcv(pool, "15m")
        last = (candles.get("candles") or [None])[-1] if candles.get("ok") else None
        close = last[4] if last else None
        rows.append((ts(), price, close, candles.get("ok"), candles.get("stale"), candles.get("reason")))
        print(f"  {rows[-1][0]}  live={price}  last_close={close}  ohlcv_ok={candles.get('ok')} stale={candles.get('stale')} {candles.get('reason') or ''}")
        if i + 1 < SAMPLES:
            await asyncio.sleep(max(0, INTERVAL - (time.time() - t0)))
    lives = [r[1] for r in rows if r[1]]
    closes = [r[2] for r in rows if r[2]]
    moved = (len(set(round(x, 10) for x in lives)) > 1) or (len(set(round(x, 10) for x in closes)) > 1)
    print(f"samples={len(rows)} distinct_live={len(set(lives))} distinct_close={len(set(closes))} moved={moved}")
    if not lives and not closes:
        print("PROBE FAIL: no prices fetched")
        return 1
    print("PROBE OK" if lives or closes else "PROBE FAIL")
    return 0 if (lives or closes) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
