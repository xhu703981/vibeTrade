"""Fixed allowlist of Solana-ecosystem majors for the Majors desk section.

These bypass meme-only research filters and x402 spend. Execution still goes through
the guard (price impact, slippage, fees, simulation, JEV). K-lines use a discovered
GeckoTerminal pool when one exists; live price uses the Jupiter price API.
"""
from __future__ import annotations

import time

import httpx

from backend.config import MAJORS, USDC_MINT
from backend.discovery.dexscreener import sanitize_symbol
from backend.market.ohlcv import BASE, HEADERS, _limiter, jupiter_prices
from backend.ratelimit import RateLimiter

_pool_cache: dict[str, tuple[float, str | None]] = {}
_POOL_TTL = 3600.0
_gt_limiter = RateLimiter(per_minute=20)


async def find_pool(mint: str, client: httpx.AsyncClient | None = None) -> str | None:
    """Best GeckoTerminal pool for a mint (highest reserve), cached."""
    now = time.time()
    hit = _pool_cache.get(mint)
    if hit and now - hit[0] < _POOL_TTL:
        return hit[1]
    own = client is None
    client = client or httpx.AsyncClient(timeout=10, headers=HEADERS)
    pool = None
    try:
        await _gt_limiter.wait()
        r = await client.get(f"{BASE.replace('/pools', '')}/tokens/{mint}/pools",
                             params={"page": 1})
        if r.status_code == 200:
            rows = (r.json() or {}).get("data") or []
            best, best_liq = None, -1.0
            for row in rows:
                attrs = row.get("attributes") or {}
                liq = float((attrs.get("reserve_in_usd") or 0) or 0)
                pid = attrs.get("address") or row.get("id", "").split("_")[-1]
                if pid and liq > best_liq:
                    best, best_liq = pid, liq
            pool = best
    except (httpx.HTTPError, ValueError, TypeError, KeyError):
        pool = hit[1] if hit else None
    finally:
        if own:
            await client.aclose()
    _pool_cache[mint] = (now, pool)
    return pool


async def live_prices(mints: list[str]) -> dict[str, float]:
    async with httpx.AsyncClient(timeout=8) as c:
        return await jupiter_prices(mints, c)


def offer_template(ticker: str, mint: str, number: int) -> dict:
    sym = sanitize_symbol(ticker) or ticker
    return {
        "id": "maj-" + ticker.lower(),
        "number": number,
        "round": 0,
        "mint": mint,
        "symbol": sym,
        "ticker": ticker,
        "kind": "major",
        "pool": None,
        "dex": None,
        "paid_promo": False,
        "metrics": {},
        "status": "open",
        "card_ids": [],
        "market_live": False,
        "market_at": None,
    }


def all_templates(start_number: int = 101) -> list[dict]:
    """Majors use numbers 101+ so they never collide with meme coin 1..N."""
    out = []
    for i, (t, mint) in enumerate(MAJORS.items()):
        if mint == USDC_MINT:
            continue
        out.append(offer_template(t, mint, start_number + i))
    return out
