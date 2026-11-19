"""Research seller registry. payTo / unit price / network / asset come from each seller's live 402 challenge on 2026-11-10 and are hard-coded here.

Payments use Base USDC only (x402 V2, scheme=exact, EIP-3009). Sellers not registered here are never paid.
If a live quote changes (new address, higher price), the x402_client hook refuses to pay; a human must verify and update this file.
"""
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

from backend.config import RESEARCH_SELLER_ALLOWLIST

BASE_NETWORK = "eip155:8453"
BASE_USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
USDC_DECIMALS = 6


@dataclass(frozen=True)
class Seller:
    key: str
    name: str
    url: str
    method: str
    network: str
    asset: str
    pay_to: str
    unit_price_atomic: int     # USDC base units (6 decimals)
    fields: tuple[str, ...]    # research fields this seller provides

    @property
    def host(self) -> str:
        return urlparse(self.url).hostname or ""

    @property
    def unit_price_usdc(self) -> float:
        return self.unit_price_atomic / 10**USDC_DECIMALS


SELLERS: dict[str, Seller] = {
    "nansen_holders": Seller(
        key="nansen_holders", name="Nansen", url="https://api.nansen.ai/api/v1/tgm/holders", method="POST",
        network=BASE_NETWORK, asset=BASE_USDC, pay_to="0x93053f1e7A5eFEDa532Fe69CbbE43cBEc3A0F13f",
        unit_price_atomic=50_000, fields=("max_top10_pct",)),
    "nansen_smart_money": Seller(   # discovery: smart-money net flow (same Nansen payTo / price, 402 challenge 2026-11-13)
        key="nansen_smart_money", name="Nansen", url="https://api.nansen.ai/api/v1/smart-money/netflow", method="POST",
        network=BASE_NETWORK, asset=BASE_USDC, pay_to="0x93053f1e7A5eFEDa532Fe69CbbE43cBEc3A0F13f",
        unit_price_atomic=50_000, fields=("smart_money_netflow",)),
    "quickintel_scan": Seller(
        key="quickintel_scan", name="Quick Intel", url="https://x402.quickintel.io/v1/scan/full", method="POST",
        network=BASE_NETWORK, asset=BASE_USDC, pay_to="0x3dBDfB6E5dCFa8f51AA07bC3aDf18a62b186C362",
        unit_price_atomic=30_000, fields=("reject_known_rugger", "scan_flags")),
    "cmc_dex_search": Seller(
        key="cmc_dex_search", name="CoinMarketCap", url="https://pro-api.coinmarketcap.com/x402/v1/dex/search", method="GET",
        network=BASE_NETWORK, asset=BASE_USDC, pay_to="0x3C5f3a6cE224BB89D72f5EB4232ecC27F67B3eeA",
        unit_price_atomic=10_000, fields=("market_crosscheck",)),
}

for _s in SELLERS.values():  # startup self-check: every registered seller must be in the system allowlist
    assert _s.host in RESEARCH_SELLER_ALLOWLIST, _s.host
    assert _s.network == BASE_NETWORK and _s.asset == BASE_USDC


def basescan_tx(tx_hash: str) -> str:
    return f"https://basescan.org/tx/{tx_hash}"
