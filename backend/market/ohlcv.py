"""K-line data for the selection desk: GeckoTerminal public OHLCV for a pool we already know (free, no key).

Only numbers leave this module: [[t, o, h, l, c, v], ...]. Token names / metadata in the response ("meta") are dropped.
Never used in voice or LLM context. Failures return ok=False with a short code-written reason; the UI then shows a fallback.
Rate limit: the public API allows about 30 calls/min, so results are cached per (pool, timeframe).
"""
from __future__ import annotations

import time

import httpx

from backend.discovery.dexscreener import _num, valid_mint
from backend.ratelimit import RateLimiter

BASE = "https://api.geckoterminal.com/api/v2/networks/solana/pools"
HEADERS = {"User-Agent": "vibe-trading/0.1", "Accept": "application/json"}
# UI timeframe -> (GeckoTerminal timeframe, aggregate, candle count)
TIMEFRAMES = {"5m": ("minute", 5, 288), "15m": ("minute", 15, 192), "1h": ("hour", 1, 168), "4h": ("hour", 4, 180), "1d": ("day", 1, 90)}
CACHE_TTL_S = 30.0
PRICE_TTL_S = 4.0
PERIOD_S = {"5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}
_limiter = RateLimiter(per_minute=25)
_cache: dict[tuple[str, str], tuple[float, dict]] = {}
_price_cache: dict[str, tuple[float, float]] = {}
_pool_mint: dict[str, str] = {}     # pool -> base mint, learned from GeckoTerminal once
# GeckoTerminal's pool price is cached upstream for ~1 minute, so the live tick comes from Jupiter's
# free price API (updates every few seconds) once we know the pool's base mint.
JUP_PRICE = "https://lite-api.jup.ag/price/v3"
_jup_limiter = RateLimiter(per_minute=50)
_jup_backoff_until = 0.0     # Jupiter's free price API answered 429: leave it alone for a minute
GT_PRICE_TTL_S = 15.0        # fallback price from GeckoTerminal is ~1 min stale upstream anyway


async def _jupiter_price(mint: str, c: httpx.AsyncClient) -> float | None:
    global _jup_backoff_until
    if time.monotonic() < _jup_backoff_until:
        return None
    try:
        await _jup_limiter.wait()
        r = await c.get(JUP_PRICE, params={"ids": mint})
        if r.status_code == 429:
            _jup_backoff_until = time.monotonic() + 60.0
            return None
        if r.status_code >= 400:
            return None
        row = (r.json() or {}).get(mint) or {}
        price = _num(row.get("usdPrice"))
        return price if price and price > 0 else None
    except (httpx.HTTPError, ValueError, TypeError, AttributeError):
        return None


async def jupiter_prices(mints: list[str], c: httpx.AsyncClient) -> dict[str, float]:
    """Several USD prices in ONE Jupiter call (the price API takes comma-separated ids): one limiter slot, not one per mint."""
    global _jup_backoff_until
    ids = [m for m in dict.fromkeys(mints) if m][:50]
    if not ids or time.monotonic() < _jup_backoff_until:
        return {}
    try:
        await _jup_limiter.wait()
        r = await c.get(JUP_PRICE, params={"ids": ",".join(ids)})
        if r.status_code == 429:
            _jup_backoff_until = time.monotonic() + 60.0
            return {}
        if r.status_code >= 400:
            return {}
        data = r.json() or {}
        out = {}
        for m in ids:
            px = _num((data.get(m) or {}).get("usdPrice"))
            if px and px > 0:
                out[m] = px
        return out
    except (httpx.HTTPError, ValueError, TypeError, AttributeError):
        return {}


def parse_ohlcv(data: object, limit: int = 1000) -> list[list[float]]:
    """GeckoTerminal JSON -> ascending [[t, o, h, l, c, v]] with finite positive numbers only."""
    try:
        rows = data["data"]["attributes"]["ohlcv_list"]  # type: ignore[index]
    except (KeyError, TypeError):
        return []
    out = []
    for r in rows if isinstance(rows, list) else []:
        if not isinstance(r, (list, tuple)) or len(r) < 6:
            continue
        vals = [_num(x) for x in r[:6]]
        if any(v is None for v in vals) or vals[0] <= 0 or min(vals[1:5]) <= 0 or vals[5] < 0:
            continue
        t, o, h, lo, c, v = vals
        out.append([int(t), o, max(h, o, c, lo), min(lo, o, c, h), c, v])
    out.sort(key=lambda x: x[0])
    dedup: dict[int, list[float]] = {int(x[0]): x for x in out}
    return list(dedup.values())[-limit:]


def apply_live_price(candles: list[list[float]], price: float, now_ts: float, tf: str) -> list[list[float]]:
    """Patch the forming candle (or start the next one) with a live USD price so the desk K-line moves."""
    if not candles or not price or price <= 0:
        return candles
    period = PERIOD_S.get(tf) or 900
    last = list(candles[-1])
    t0 = int(last[0])
    if now_ts <= t0 + period * 1.5:
        last[2] = max(last[2], price)
        last[3] = min(last[3], price)
        last[4] = price
        return candles[:-1] + [last]
    new_t = t0 + period * int((now_ts - t0) // period)
    return list(candles) + [[float(new_t), last[4], max(last[4], price), min(last[4], price), price, 0.0]]


async def fetch_pool_price(pool: str, client: httpx.AsyncClient | None = None, now=time.time) -> float | None:
    """Current USD price for a known pool (GeckoTerminal, free, cached for a few seconds)."""
    if not valid_mint(pool):
        return None
    hit = _price_cache.get(pool)
    ttl = PRICE_TTL_S if time.monotonic() >= _jup_backoff_until else GT_PRICE_TTL_S
    if hit and now() - hit[0] < ttl:
        return hit[1]
    own = client is None
    c = client or httpx.AsyncClient(timeout=8, headers=HEADERS)
    try:
        mint = _pool_mint.get(pool)
        if mint:
            jp = await _jupiter_price(mint, c)
            if jp:
                _price_cache[pool] = (now(), jp)
                return jp
        # The live tick is optional: never queue it behind K-line requests on the shared GeckoTerminal limit.
        if not _limiter.try_acquire():
            return hit[1] if hit else None
        r = await c.get(f"{BASE}/{pool}", params={"include": "base_token"})
        if r.status_code >= 400:
            return hit[1] if hit else None
        data = r.json().get("data") or {}
        attrs = data.get("attributes") or {}
        bid = str((((data.get("relationships") or {}).get("base_token") or {}).get("data") or {}).get("id") or "")
        if bid.startswith("solana_") and valid_mint(bid[7:]):
            _pool_mint[pool] = bid[7:]
            jp = await _jupiter_price(bid[7:], c)
            if jp:
                _price_cache[pool] = (now(), jp)
                return jp
        price = _num(attrs.get("base_token_price_usd"))
        if price and price > 0:
            _price_cache[pool] = (now(), price)
            return price
    except (httpx.HTTPError, ValueError, TypeError):
        return hit[1] if hit else None
    finally:
        if own:
            await c.aclose()
    return hit[1] if hit else None


async def fetch_ohlcv(pool: str, tf: str = "15m", client: httpx.AsyncClient | None = None, now=time.time) -> dict:
    if tf not in TIMEFRAMES:
        return {"ok": False, "reason": "unknown timeframe", "candles": []}
    if not valid_mint(pool):
        return {"ok": False, "reason": "invalid pool address", "candles": []}
    hit = _cache.get((pool, tf))
    if hit and now() - hit[0] < CACHE_TTL_S:
        out = dict(hit[1])
        if out.get("ok") and out.get("candles"):
            price = await fetch_pool_price(pool, now=now)
            if price:
                out["candles"] = apply_live_price(list(out["candles"]), price, now(), tf)
                out["price"] = price
            out["fetched_at"] = now()
        return out
    gt_tf, agg, limit = TIMEFRAMES[tf]
    own = client is None
    c = client or httpx.AsyncClient(timeout=12, headers=HEADERS)
    try:
        await _limiter.wait()
        r = await c.get(f"{BASE}/{pool}/ohlcv/{gt_tf}", params={"aggregate": agg, "limit": limit, "currency": "usd"})
        if r.status_code == 429:
            out = {"ok": False, "reason": "chart data rate-limited, try again in a minute", "candles": []}
        elif r.status_code >= 400:
            out = {"ok": False, "reason": f"chart data unavailable (HTTP {r.status_code})", "candles": []}
        else:
            candles = parse_ohlcv(r.json())
            out = {"ok": bool(candles), "reason": None if candles else "no candles for this pool yet", "candles": candles,
                   "source": "GeckoTerminal", "tf": tf}
    except (httpx.HTTPError, ValueError) as e:
        out = {"ok": False, "reason": f"chart data unavailable ({type(e).__name__})", "candles": []}
    finally:
        if own:
            await c.aclose()
    if out["ok"]:
        _cache[(pool, tf)] = (now(), {k: v for k, v in out.items() if k != "fetched_at"})
    elif hit:   # serve stale data rather than nothing
        out = {**hit[1], "stale": True}
    if out.get("ok") and out.get("candles"):
        price = await fetch_pool_price(pool, client=None if own else c, now=now)
        if price:
            out = {**out, "candles": apply_live_price(list(out["candles"]), price, now(), tf), "price": price}
        out["fetched_at"] = now()
    return out
