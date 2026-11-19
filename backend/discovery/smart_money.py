"""Candidate source: Nansen smart-money net flow on Solana (one x402 call, $0.05), DexScreener as the fallback.

Only addresses and numbers are kept from the response (token symbols / sectors are third-party text and are dropped).
Rehearsal reads the recorded response from the research cache and never pays.
"""
from __future__ import annotations

from backend.discovery.dexscreener import Candidate, valid_mint
from backend.research import cache
from backend.research.sellers import SELLERS

SELLER = "nansen_smart_money"
CACHE_KEY = "solana-netflow-24h"
SOURCE = "nansen_smart_money"
MAX_CANDIDATES = 25

NETFLOW_BODY = {
    "chains": ["solana"],
    "filters": {"include_stablecoins": False, "include_native_tokens": False,
                "market_cap_usd": {"min": 100_000, "max": 20_000_000}},
    "pagination": {"page": 1, "per_page": MAX_CANDIDATES},
    "order_by": [{"field": "net_flow_24h_usd", "direction": "DESC"}],
}


def _num(x):
    return float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) else None


def parse_netflow(data) -> dict[str, dict]:
    """mint -> {sm_netflow_24h_usd, sm_netflow_1h_usd, sm_netflow_7d_usd, sm_traders}; only net buying (24h > 0)."""
    rows = data.get("data") if isinstance(data, dict) else data
    out: dict[str, dict] = {}
    for r in rows if isinstance(rows, list) else []:
        if not isinstance(r, dict) or str(r.get("chain", "solana")).lower() != "solana":
            continue
        m = valid_mint(r.get("token_address"))
        f24 = _num(r.get("net_flow_24h_usd"))
        if not m or f24 is None or f24 <= 0:
            continue
        tc = _num(r.get("trader_count"))
        out[m] = {"sm_netflow_24h_usd": round(f24, 2), "sm_netflow_1h_usd": _num(r.get("net_flow_1h_usd")),
                  "sm_netflow_7d_usd": _num(r.get("net_flow_7d_usd")), "sm_traders": int(tc) if tc is not None else None}
    return out


async def smart_money_flows(payer, ttl_s: float | None = None) -> tuple[dict[str, dict], dict]:
    """(mint -> numbers, info). Cache first (rehearsal / same round); else one paid call. Never raises."""
    cached = cache.get(SELLER, CACHE_KEY, ttl_s)
    if cached is not None:
        return parse_netflow(cached["data"]), {"source": SOURCE, "cached": True, "tx_hash": cached.get("tx_hash"), "amount_usdc": 0.0}
    try:
        p = await payer.buy(SELLERS[SELLER], "smart-money", "smart-money net flow (24h)", body=NETFLOW_BODY)
    except Exception as e:  # noqa: BLE001  budget, refusal, network: fall back to DexScreener
        return {}, {"source": SOURCE, "error": type(e).__name__}
    if getattr(p, "data", None) is None or not getattr(p, "paid", False):
        return {}, {"source": SOURCE, "error": f"http {getattr(p, 'status_code', '?')}",
                    "amount_usdc": p.entry.amount_usdc if getattr(p, "entry", None) else 0.0}
    cache.put(SELLER, CACHE_KEY, p.data, p.entry.tx_hash if p.entry else None, p.entry.amount_usdc if p.entry else None)
    return parse_netflow(p.data), {"source": SOURCE, "cached": False, "tx_hash": p.entry.tx_hash if p.entry else None,
                                   "amount_usdc": p.entry.amount_usdc if p.entry else 0.0}


def candidates_from(flows: dict[str, dict]) -> list[Candidate]:
    return [Candidate(mint=m, sources=[SOURCE]) for m in list(flows)[:MAX_CANDIDATES]]
