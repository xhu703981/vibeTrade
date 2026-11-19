"""PriceSource: how many USDC the listener would get back by selling now.

- ForkPriceSource: simulates a pre-built "sell everything" transaction on the fork (never sent;
  min_out is set very low so the executable value is known however the price moves), and reads the pool's quote-side vault as liquidity.
  Jupiter quotes use mainnet state and cannot see price moves made by a whale on the fork, so quotes cannot be used directly.
  Surfpool is copy-on-read: once a pool / vault account has been read it is cached on the fork and later mainnet trades
  never reach it, so the fork price froze at buy time (measured: constant for 4 minutes while mainnet moved -2.3%).
  So before every quote the route's pool accounts (writable, not the wallet's) are reset with surfnet_resetAccount and
  refetched from mainnet. Exception: a rehearsal price test (whale pump / dump) freezes the coin with freeze(); a frozen
  coin keeps its fork-only pool state until unfreeze(), so the rehearsal move is not wiped.
- MainnetPriceSource: Jupiter Price API v3; missing key or failed request -> PriceFeedDown.
Any pricing failure raises PriceFeedDown; the listener only alerts and never sells blind.
"""
from __future__ import annotations

import base64
import time
from dataclasses import dataclass
from typing import Protocol

import httpx
from solders.pubkey import Pubkey

from backend.config import JUPITER_BASE, JUPITER_ROUTE_DEXES, USDC_MINT, route_dexes, settings
from backend.discovery.free_checks import PUMPSWAP_PROGRAM, RAYDIUM_CPMM_PROGRAM
from backend.guard.pipeline import pool_accounts_to_refresh
from backend.guard.simulate import refresh_accounts, wallet_token_deltas
from backend.solana_utils import sync_fork_clock
from backend.trade.jupiter import _limiter, assemble_v0, build_instructions, build_swap, fetch_alts

PRICING_SLIPPAGE_BPS = 9900   # only for the pricing transaction that is profiled (never sent)
PRICING_TX_MAX_AGE_S = 45     # rebuild before the blockhash expires


class PriceFeedDown(RuntimeError):
    pass


@dataclass
class PriceQuote:
    amount: int               # token amount priced (base units)
    value_usdc: int           # USDC received if sold now (base units)
    liquidity: int | None     # raw balance of the pool's quote-side vault (only compared with itself)
    source: str
    ts: float


class PriceSource(Protocol):
    async def quote(self, mint: str, amount: int, pool: str | None) -> PriceQuote: ...


async def pool_quote_vault(rpc, pool: str, mint: str) -> str | None:
    acc = (await rpc.call("getAccountInfo", [pool, {"encoding": "base64"}]))["value"]
    if acc is None:
        return None
    d = base64.b64decode(acc["data"][0])
    P = lambda o: str(Pubkey.from_bytes(d[o:o + 32]))  # noqa: E731
    if acc["owner"] == PUMPSWAP_PROGRAM and len(d) >= 203:
        base_mint, base_vault, quote_vault = P(43), P(139), P(171)
        return quote_vault if base_mint == mint else base_vault
    if acc["owner"] == RAYDIUM_CPMM_PROGRAM and len(d) >= 232:
        v0, v1, m0 = P(72), P(104), P(168)
        return v1 if m0 == mint else v0
    return None


class ForkPriceSource:
    def __init__(self, rpc, keypair, upstream_url: str | None = None, refresh: bool = True):
        self.rpc, self.kp = rpc, keypair
        self.upstream = upstream_url or settings.solana_rpc_url
        self.refresh = refresh                     # follow mainnet: refetch the route's pool accounts before each quote
        self._tx: dict[tuple[str, int], tuple[bytes, float]] = {}
        self._route: dict[bytes, list[str]] = {}   # pricing tx -> its pool accounts (computed once per built tx)
        self._vault: dict[str, str | None] = {}
        self._frozen: set[str] = set()

    # ---------- rehearsal freeze ----------
    def freeze(self, mint: str) -> None:
        """Stop refreshing this coin's pools (a rehearsal whale moved them on the fork; keep that move)."""
        self._frozen.add(mint)

    def unfreeze(self, mint: str) -> None:
        self._frozen.discard(mint)

    def follows_mainnet(self, mint: str) -> bool:
        return self.refresh and mint not in self._frozen

    async def refresh_route(self, raw: bytes) -> list[str]:
        """Reset the route's pool / vault accounts so the next read refetches the latest mainnet state."""
        accs = self._route.get(raw)
        if accs is None:
            accs = await pool_accounts_to_refresh(self.rpc, raw, str(self.kp.pubkey()))
            if not accs:
                raise PriceFeedDown("could not find the pool accounts of the pricing route")
            live = {t[0] for t in self._tx.values()}
            self._route = {k: v for k, v in self._route.items() if k in live}   # drop routes of rebuilt txs
            self._route[raw] = accs
        await refresh_accounts(self.rpc, accs)
        return accs

    async def _pricing_tx(self, mint: str, amount: int, fresh: bool = False) -> bytes:
        key = (mint, amount)
        hit = self._tx.get(key)
        if hit and not fresh and time.time() - hit[1] < PRICING_TX_MAX_AGE_S:
            return hit[0]
        await sync_fork_clock(self.rpc, self.upstream)
        b = await build_swap(mint, USDC_MINT, amount, str(self.kp.pubkey()), PRICING_SLIPPAGE_BPS, dexes=route_dexes(mint))
        alts = await fetch_alts(self.rpc, list((b.get("addressesByLookupTableAddress") or {}).keys()))
        raw = bytes(assemble_v0(self.kp, build_instructions(b), alts, await self.rpc.latest_blockhash()))
        self._tx = {k: v for k, v in self._tx.items() if k[0] != mint}
        self._tx[key] = (raw, time.time())
        return raw

    async def quote(self, mint: str, amount: int, pool: str | None) -> PriceQuote:
        try:
            for attempt in range(2):
                raw = await self._pricing_tx(mint, amount, fresh=attempt > 0)
                if self.follows_mainnet(mint):
                    await self.refresh_route(raw)
                # plain simulateTransaction (profiling on every poll grew the Surfpool fork without bound)
                ok, _err, deltas = await wallet_token_deltas(self.rpc, raw, str(self.kp.pubkey()), [mint, USDC_MINT])
                if ok and deltas.get(mint, 0) == -amount:
                    value = deltas.get(USDC_MINT, 0)
                    break
            else:
                raise PriceFeedDown("profiling the sell transaction on the fork failed")
            liq = None
            if pool:
                if pool not in self._vault:
                    self._vault[pool] = await pool_quote_vault(self.rpc, pool, mint)
                v = self._vault[pool]
                if v:
                    liq = int((await self.rpc.call("getTokenAccountBalance", [v]))["value"]["amount"])
            return PriceQuote(amount, value, liq, "fork_profile", time.time())
        except PriceFeedDown:
            raise
        except Exception as e:  # noqa: BLE001
            raise PriceFeedDown(f"fork pricing error: {type(e).__name__}") from None


class MainnetPriceSource:
    """Jupiter Price API v3 (mainnet prices). Cannot see fork price moves; mainnet mode only."""

    def __init__(self, decimals: dict[str, int], api_key: str | None = None, client: httpx.AsyncClient | None = None):
        self.key = api_key if api_key is not None else settings.jupiter_api_key
        self.decimals = decimals
        self._c = client

    async def quote(self, mint: str, amount: int, pool: str | None) -> PriceQuote:
        if not self.key:
            raise PriceFeedDown("JUPITER_API_KEY is not configured")
        c = self._c or httpx.AsyncClient(timeout=10)
        try:
            await _limiter.wait()
            r = await c.get(f"{JUPITER_BASE}/price/v3", params={"ids": mint}, headers={"x-api-key": self.key})
            if r.status_code != 200:
                raise PriceFeedDown(f"Price API HTTP {r.status_code}")
            p = (r.json() or {}).get(mint) or {}
            usd = p.get("usdPrice")
            if not isinstance(usd, (int, float)) or isinstance(usd, bool) or usd <= 0:
                raise PriceFeedDown("the Price API has no price for this token")
        except PriceFeedDown:
            raise
        except Exception as e:  # noqa: BLE001
            raise PriceFeedDown(f"Price API error: {type(e).__name__}") from None
        finally:
            if self._c is None:
                await c.aclose()
        dec = self.decimals.get(mint, 6)
        return PriceQuote(amount, int(usd * amount / 10**dec * 1e6), None, "jupiter_price_v3", time.time())


async def sol_usd_price() -> float | None:
    """Live SOL/USD from the Jupiter Price API (read-only, no spending). None if unavailable; callers must not guess."""
    from backend.config import WSOL_MINT
    try:
        q = await MainnetPriceSource({WSOL_MINT: 9}).quote(WSOL_MINT, 10**9, None)
    except PriceFeedDown:
        return None
    p = q.value_usdc / 1e6
    return p if 1 <= p <= 100_000 else None
