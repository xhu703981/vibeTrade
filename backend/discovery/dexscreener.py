"""DexScreener candidate discovery (free API, no key).

The adapter passes only mint addresses and numeric fields downstream. Free text such as name, symbol, description, links, icon, url
is dropped; only an allowlisted-character symbol_display is kept (UI display only, never in LLM or voice context).

Rate limits (official docs): profiles / boosts / takeovers 60/min; /tokens/v1 300/min with at most 30 addresses per call.
Note: the free API has no "new pairs" list; candidates come from profiles, paid boosts and community takeovers, so they skew towards paid promotion.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

import httpx
from solders.pubkey import Pubkey

from backend.ratelimit import RateLimiter

BASE = "https://api.dexscreener.com"
HEADERS = {"User-Agent": "vibe-trading/0.1", "Accept": "application/json"}

SOURCES = {
    "profile": "/token-profiles/latest/v1",
    "boost_latest": "/token-boosts/latest/v1",   # paid promotion
    "boost_top": "/token-boosts/top/v1",         # paid promotion
    "takeover": "/community-takeovers/latest/v1",
}
PAID_SOURCES = {"boost_latest", "boost_top"}
_slow = RateLimiter(per_minute=55)   # headroom under 60/min
_fast = RateLimiter(per_minute=280)  # headroom under 300/min

# DexScreener labels are external strings too: keep known values only
LABEL_WHITELIST = {"CPMM", "CLMM", "DLMM", "DYN", "DYN2", "v2", "v3", "v4", "wp"}
_SYM_OK = re.compile(r"[^A-Za-z0-9]")


def sanitize_symbol(s: object) -> str:
    """Keep only A-Z a-z 0-9, at most 10 characters; UI display only."""
    return _SYM_OK.sub("", s if isinstance(s, str) else "")[:10]


def valid_mint(s: object) -> str | None:
    if not isinstance(s, str) or not (32 <= len(s) <= 44):
        return None
    try:
        return str(Pubkey.from_string(s))
    except Exception:  # noqa: BLE001
        return None


def _num(x: object) -> float | None:
    if isinstance(x, bool):
        return None
    try:
        v = float(x)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return v if v == v and v not in (float("inf"), float("-inf")) else None


@dataclass
class PairInfo:
    pair_address: str
    dex_id: str
    labels: list[str]
    quote_mint: str | None
    liquidity_usd: float | None
    volume_h24: float | None
    market_cap: float | None
    fdv: float | None
    pair_created_at_ms: int | None
    price_usd: float | None
    # trader signals (numbers only): price change %, volume and buy/sell transaction counts per window
    price_change_h1: float | None = None
    price_change_h6: float | None = None
    price_change_h24: float | None = None
    volume_h1: float | None = None
    volume_h6: float | None = None
    buys_h1: int | None = None
    sells_h1: int | None = None
    buys_h24: int | None = None
    sells_h24: int | None = None


@dataclass
class Candidate:
    mint: str
    sources: list[str] = field(default_factory=list)
    paid_promo: bool = False
    symbol_display: str = ""
    pair: PairInfo | None = None

    def as_dict(self) -> dict:
        return asdict(self)


def _int(x: object) -> int | None:
    v = _num(x)
    return int(v) if v is not None and v >= 0 else None


def _sub(p: dict, key: str) -> dict:
    v = p.get(key)
    return v if isinstance(v, dict) else {}


def sanitize_pair(p: dict) -> PairInfo | None:
    """External pair JSON -> PairInfo with addresses and numbers only."""
    addr = valid_mint(p.get("pairAddress"))
    dex = p.get("dexId")
    if not addr or not isinstance(dex, str) or not re.fullmatch(r"[a-z0-9_-]{1,32}", dex):
        return None
    created = p.get("pairCreatedAt")
    return PairInfo(
        pair_address=addr, dex_id=dex,
        labels=[l for l in (p.get("labels") or []) if isinstance(l, str) and l in LABEL_WHITELIST],
        quote_mint=valid_mint((p.get("quoteToken") or {}).get("address")),
        liquidity_usd=_num((p.get("liquidity") or {}).get("usd")),
        volume_h24=_num((p.get("volume") or {}).get("h24")),
        market_cap=_num(p.get("marketCap")), fdv=_num(p.get("fdv")),
        pair_created_at_ms=int(created) if isinstance(created, (int, float)) and not isinstance(created, bool) else None,
        price_usd=_num(p.get("priceUsd")),
        price_change_h1=_num(_sub(p, "priceChange").get("h1")), price_change_h6=_num(_sub(p, "priceChange").get("h6")),
        price_change_h24=_num(_sub(p, "priceChange").get("h24")),
        volume_h1=_num(_sub(p, "volume").get("h1")), volume_h6=_num(_sub(p, "volume").get("h6")),
        buys_h1=_int(_sub(_sub(p, "txns"), "h1").get("buys")), sells_h1=_int(_sub(_sub(p, "txns"), "h1").get("sells")),
        buys_h24=_int(_sub(_sub(p, "txns"), "h24").get("buys")), sells_h24=_int(_sub(_sub(p, "txns"), "h24").get("sells")),
    )


MARKET_KEYS = ("liquidity_usd", "market_cap", "volume_h24", "pair_created_at_ms", "price_usd", "price_change_h1", "price_change_h6",
               "price_change_h24", "volume_h1", "volume_h6", "buys_h1", "sells_h1", "buys_h24", "sells_h24")


def pair_metrics(p: PairInfo) -> dict:
    """Numbers only, for the selection desk."""
    return {k: getattr(p, k) for k in MARKET_KEYS if getattr(p, k) is not None}


def pick_main_pair(mint: str, pairs: list[dict]) -> tuple[PairInfo | None, str]:
    """Pick the most liquid pair with this coin as base; return (pair, sanitised symbol)."""
    best, best_liq, sym = None, -1.0, ""
    for p in pairs:
        if p.get("chainId") != "solana" or (p.get("baseToken") or {}).get("address") != mint:
            continue
        sp = sanitize_pair(p)
        if sp is None:
            continue
        liq = sp.liquidity_usd or 0.0
        if liq > best_liq:
            best, best_liq, sym = sp, liq, sanitize_symbol((p.get("baseToken") or {}).get("symbol"))
    return best, sym


class DexScreener:
    def __init__(self, client: httpx.AsyncClient | None = None):
        self._c = client or httpx.AsyncClient(timeout=20, headers=HEADERS)

    async def close(self):
        await self._c.aclose()

    async def _get(self, path: str, limiter: RateLimiter, params: dict | None = None):
        await limiter.wait()
        r = await self._c.get(BASE + path, params=params)
        r.raise_for_status()
        return r.json()

    async def discover(self, sources: tuple[str, ...] = tuple(SOURCES)) -> list[Candidate]:
        by_mint: dict[str, Candidate] = {}
        for name in sources:
            try:
                data = await self._get(SOURCES[name], _slow)
            except httpx.HTTPError:
                continue
            for item in data if isinstance(data, list) else [data]:
                if not isinstance(item, dict) or item.get("chainId") != "solana":
                    continue
                m = valid_mint(item.get("tokenAddress"))
                if not m:
                    continue
                c = by_mint.setdefault(m, Candidate(mint=m))
                if name not in c.sources:
                    c.sources.append(name)
                c.paid_promo = c.paid_promo or name in PAID_SOURCES
        return list(by_mint.values())

    async def search(self, query: str) -> list[Candidate]:
        """Optional: /latest/dex/search (free-text search; results again keep only addresses and numbers)."""
        data = await self._get("/latest/dex/search", _slow, params={"q": query})
        out: dict[str, Candidate] = {}
        for p in data.get("pairs") or []:
            m = valid_mint((p.get("baseToken") or {}).get("address"))
            if p.get("chainId") == "solana" and m:
                out.setdefault(m, Candidate(mint=m, sources=["search"]))
        return list(out.values())

    async def pairs(self, pair_addresses: list[str]) -> dict[str, PairInfo]:
        """Fresh numbers for known pairs (/latest/dex/pairs/solana/{<=30}). Addresses must already be validated."""
        out: dict[str, PairInfo] = {}
        addrs = [a for a in pair_addresses if valid_mint(a)]
        for i in range(0, len(addrs), 30):
            try:
                data = await self._get("/latest/dex/pairs/solana/" + ",".join(addrs[i:i + 30]), _fast)
            except httpx.HTTPError:
                continue
            for p in (data.get("pairs") if isinstance(data, dict) else None) or []:
                if isinstance(p, dict) and p.get("chainId") == "solana":
                    sp = sanitize_pair(p)
                    if sp is not None and sp.pair_address in addrs:
                        out[sp.pair_address] = sp
        return out

    async def enrich(self, cands: list[Candidate]) -> None:
        """Batch /tokens/v1/solana/{<=30 addresses} to fill pair numbers."""
        for i in range(0, len(cands), 30):
            chunk = cands[i:i + 30]
            try:
                pairs = await self._get("/tokens/v1/solana/" + ",".join(c.mint for c in chunk), _fast)
            except httpx.HTTPError:
                continue
            pairs = pairs if isinstance(pairs, list) else []
            for c in chunk:
                c.pair, c.symbol_display = pick_main_pair(c.mint, pairs)
