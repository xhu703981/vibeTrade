"""Configuration and system hard rules. Secrets are read only from the environment and never printed."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
# Process environment wins; .env only fills in missing values
load_dotenv(ROOT / ".env", override=False)

USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
WSOL_MINT = "So11111111111111111111111111111111111111112"
JUPITER_BASE = "https://api.jup.ag"

# System hard rules (not part of the mandate; the user cannot loosen them). M2 fills in the full list.
PROGRAM_ALLOWLIST: frozenset[str] = frozenset({
    "11111111111111111111111111111111",              # System
    "ComputeBudget111111111111111111111111111111",   # Compute Budget
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",   # SPL Token
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",   # Token-2022
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL",  # Associated Token
    "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4",   # Jupiter v6
})
RESEARCH_SELLER_ALLOWLIST: frozenset[str] = frozenset({
    "api.nansen.ai", "x402.quickintel.io", "pro-api.coinmarketcap.com",
})


def _env(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name)
    return v.strip() if v and v.strip() else default


_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


def normalize_rpc_url(v: str | None) -> str | None:
    """SOLANA_RPC_URL may be a full URL or just a Helius API key (UUID), in which case the Helius mainnet URL is built."""
    if not v:
        return None
    if _UUID.match(v):
        return f"https://mainnet.helius-rpc.com/?api-key={v}"
    return v


@dataclass(frozen=True)
class Settings:
    jupiter_api_key: str | None = field(default_factory=lambda: _env("JUPITER_API_KEY"), repr=False)
    solana_rpc_url: str | None = field(default_factory=lambda: normalize_rpc_url(_env("SOLANA_RPC_URL")), repr=False)
    surfpool_rpc_url: str = field(default_factory=lambda: _env("SURFPOOL_RPC_URL", "http://127.0.0.1:8899"))
    scratch_surfpool_rpc_url: str = field(default_factory=lambda: _env("SCRATCH_SURFPOOL_RPC_URL", "http://127.0.0.1:8999"))
    elevenlabs_api_key: str | None = field(default_factory=lambda: _env("ELEVENLABS_API_KEY"), repr=False)
    elevenlabs_agent_id: str | None = field(default_factory=lambda: _env("ELEVENLABS_AGENT_ID"))
    llm_api_key: str | None = field(default_factory=lambda: _env("LLM_API_KEY"), repr=False)
    jev_api_key: str | None = field(default_factory=lambda: _env("JEV_API_KEY"), repr=False)
    trading_keypair_path: Path = field(default_factory=lambda: ROOT / _env("TRADING_WALLET_KEYPAIR_PATH", "secrets/trading-keypair.json"))
    research_keypair_path: Path = field(default_factory=lambda: ROOT / _env("RESEARCH_WALLET_KEYPAIR_PATH", "secrets/research-keypair.json"))
    base_research_private_key: str | None = field(default_factory=lambda: _env("BASE_RESEARCH_WALLET_PRIVATE_KEY"), repr=False)

    def status(self) -> dict[str, bool]:
        """Report only whether each value is set, never the value."""
        return {
            "JUPITER_API_KEY": bool(self.jupiter_api_key),
            "SOLANA_RPC_URL": bool(self.solana_rpc_url),
            "ELEVENLABS_API_KEY": bool(self.elevenlabs_api_key),
            "LLM_API_KEY": bool(self.llm_api_key),
            "JEV_API_KEY": bool(self.jev_api_key),
            "BASE_RESEARCH_WALLET_PRIVATE_KEY": bool(self.base_research_private_key),
        }


settings = Settings()


# ---------------------------------------------------------------------------
# Network: the local Surfpool fork (default) or Solana MAINNET with real funds (explicit opt-in).
# ---------------------------------------------------------------------------
MAINNET_GENESIS_HASH = "5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d"
MAINNET_PUBLIC_RPC = "https://api.mainnet-beta.solana.com"
SOLSCAN_TX = "https://solscan.io/tx/"
MAINNET_DEFAULT_MAX_TRADE_USD = 5.0
MAINNET_DEFAULT_MIN_SOL_LAMPORTS = 10_000_000    # 0.01 SOL kept for fees / token-account rent before any buy


def network() -> str:
    """"mainnet" only when VT_NETWORK=mainnet is set explicitly; anything else is the local fork."""
    return "mainnet" if (os.environ.get("VT_NETWORK") or "").strip().lower() == "mainnet" else "fork"


def is_mainnet() -> bool:
    return network() == "mainnet"


def mainnet_rpc_url() -> str:
    """The mainnet RPC: SOLANA_RPC_URL (full URL or Helius key) or the public endpoint. Never logged."""
    return normalize_rpc_url(_env("SOLANA_RPC_URL")) or MAINNET_PUBLIC_RPC


def trading_rpc_url() -> str:
    """Where trades are simulated and sent: mainnet in mainnet mode, otherwise the local Surfpool fork."""
    return mainnet_rpc_url() if is_mainnet() else settings.surfpool_rpc_url


def mainnet_max_trade_usd() -> float:
    """Hard per-trade USD ceiling in mainnet mode (VT_MAINNET_MAX_TRADE_USD, default $5). On top of the mandate; a
    missing, unparsable or non-positive value falls back to the default (fail closed, never "unlimited")."""
    v = os.environ.get("VT_MAINNET_MAX_TRADE_USD")
    try:
        f = float(v.strip()) if v and v.strip() else MAINNET_DEFAULT_MAX_TRADE_USD
    except ValueError:
        f = MAINNET_DEFAULT_MAX_TRADE_USD
    return f if f > 0 and f == f and f != float("inf") else MAINNET_DEFAULT_MAX_TRADE_USD


def mainnet_min_sol_lamports() -> int:
    v = os.environ.get("VT_MAINNET_MIN_SOL")
    try:
        sol = float(v.strip()) if v and v.strip() else None
    except ValueError:
        sol = None
    return int(sol * 1e9) if sol is not None and sol > 0 else MAINNET_DEFAULT_MIN_SOL_LAMPORTS


def explorer_tx_url(signature: str | None) -> str | None:
    """Solscan link for a mainnet signature; None on the fork (fork transactions are not on any public explorer)."""
    return SOLSCAN_TX + signature if signature and is_mainnet() else None


def network_status() -> dict:
    """Public facts about the network for the UI (never the RPC URL: it may carry an API key)."""
    if not is_mainnet():
        return {"network": "fork", "real_funds": False}
    return {"network": "mainnet", "real_funds": True, "max_trade_usd": mainnet_max_trade_usd(),
            "min_sol": mainnet_min_sol_lamports() / 1e9, "explorer": "solscan",
            "rpc": "public" if mainnet_rpc_url() == MAINNET_PUBLIC_RPC else "custom"}


# ---------------------------------------------------------------------------
# Mandate hard rules (M1): the user may only tighten within these bounds, never loosen.
#   ("max", x)  user value must be <= x      ("min", x)  user value must be >= x
#   ("must", v) user value must equal v      ("superset", S) user list must contain all of S
# Changing these requires a code change + review; the dialogue cannot change them.
# ---------------------------------------------------------------------------
HARD_RULES: dict[str, tuple[str, object]] = {
    # funds
    "budget.total_usdc": ("max", 500.0),
    "budget.per_trade_usdc": ("max", 100.0),
    "budget.daily_max_loss_usdc": ("max", 500.0),
    "budget.sol_reserve": ("min", 0.02),
    "budget.research_usdc": ("max", 5.0),
    "budget.research_per_call_usdc": ("max", 0.10),
    "budget.research_max_coins_per_round": ("max", 10),
    "budget.research_topup_requires_approval": ("must", True),
    # coin filters (safety switches cannot be turned off)
    "filters.require_mint_revoked": ("must", True),
    "filters.require_freeze_revoked": ("must", True),
    "filters.banned_extensions": ("superset", ("TransferHook", "PermanentDelegate", "DefaultAccountStateFrozen", "NonTransferable")),
    "filters.max_transfer_fee_bps": ("max", 0),
    "filters.require_sell_sim": ("must", True),
    "filters.max_roundtrip_loss_pct": ("max", 10.0),
    "filters.min_liquidity_usd": ("min", 20000.0),
    "filters.require_lp_burned_or_locked": ("must", True),
    "filters.token_age_hours_min": ("min", 1.0),
    "filters.max_top10_pct": ("max", 60.0),
    "filters.max_dev_pct": ("max", 10.0),
    "filters.min_holders": ("min", 100),
    "filters.max_volume_to_liquidity": ("max", 50.0),
    "filters.reject_known_rugger": ("must", True),
    # execution
    "exec.max_slippage_bps.buy": ("max", 500),
    "exec.max_slippage_bps.stop_loss_sell": ("max", 1500),
    "exec.max_price_impact_pct": ("max", 5.0),
    "exec.max_fee_sol": ("max", 0.01),
    "exec.max_fee_pct": ("max", 3.0),
    "exec.quote_max_age_sec": ("max", 30),
    "exec.reprice_max_deviation_pct": ("max", 10.0),
    "exec.approval_timeout_sec": ("max", 300),
    # exits
    "exit.stop_loss_pct": ("max", 90.0),
    "exit.trailing_stop_pct": ("max", 90.0),
    "exit.max_hold_hours": ("max", 72.0),
    "exit.liquidity_drop_pct": ("max", 80.0),
    "exit.poll_interval_sec": ("max", 30),
    "expires_in_hours": ("max", 72.0),
}

# These are system hard rules that are not in the mandate at all; attempts to set them via the dialogue are rejected.
SYSTEM_ONLY_KEYS: frozenset[str] = frozenset({
    "program_allowlist", "banned_ix", "seller_allowlist", "research_seller_allowlist",
    "recipient_allowlist", "trading_wallet", "research_wallet",
})

# Jupiter routes only through DEXes whose LP we can verify (matching free_checks: PumpSwap / Raydium CPMM).
# Labels come from Jupiter /swap/v1/program-id-to-label and are case-sensitive.
JUPITER_ROUTE_DEXES = "Pump.fun Amm,Raydium CP"


_SECRET_ENV = ("JUPITER_API_KEY", "SOLANA_RPC_URL", "LLM_API_KEY", "JEV_API_KEY", "ELEVENLABS_API_KEY",
               "BASE_RESEARCH_WALLET_PRIVATE_KEY")


def redact(text: str) -> str:
    """Scrub known secret values and api-key=... patterns from text (call before logging / saving JSON / printing)."""
    import re as _re

    out = text
    for name in _SECRET_ENV:
        v = os.environ.get(name, "").strip()
        if len(v) >= 8:
            out = out.replace(v, "<redacted>")
    return _re.sub(r"(api[-_]?key=)[^&\s'\"]+", r"\1<redacted>", out, flags=_re.I)


# ---------------------------------------------------------------------------
# Voice engagement, proactive alerts, and majors desk (batch 2)
# ---------------------------------------------------------------------------
def _envf(name: str, default: float) -> float:
    v = os.environ.get(name)
    try:
        return float(v.strip()) if v and v.strip() else default
    except ValueError:
        return default


# Return to IDLE after this many seconds with no exchange (user addressed us or we spoke).
WAKE_IDLE_TIMEOUT_S = _envf("WAKE_IDLE_TIMEOUT_S", 25.0)
# Position move since the last alert (or from the fill if none yet) that is worth a spoken line.
ALERT_MOVE_PCT = _envf("ALERT_MOVE_PCT", 5.0)
# Speak when the price is this close to a stop-loss or take-profit trigger.
ALERT_NEAR_EXIT_PCT = _envf("ALERT_NEAR_EXIT_PCT", 0.5)
# SOL / BTC (and the same for each) move over this window.
ALERT_MARKET_MOVE_PCT = _envf("ALERT_MARKET_MOVE_PCT", 2.5)
ALERT_MARKET_WINDOW_S = _envf("ALERT_MARKET_WINDOW_S", 15 * 60)
# At most one spoken alert per position per this many seconds; simultaneous ones are merged.
ALERT_THROTTLE_S = _envf("ALERT_THROTTLE_S", 60.0)
# Liquidity warning (before the hard liquidity-drop sell): pool quote vault down this much from entry.
ALERT_LIQ_WARN_PCT = _envf("ALERT_LIQ_WARN_PCT", 30.0)
# Volume spike vs the previous hour (when the desk metrics have both).
ALERT_VOLUME_SPIKE_MULT = _envf("ALERT_VOLUME_SPIKE_MULT", 3.0)

# Trusted tickers only (not user-generated metadata). Wrapped SOL is the input side, so it is omitted.
# Names here may be spoken; meme coins on the desk stay "coin 1", "coin 2", ...
MAJORS: dict[str, str] = {
    "JUP": "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN",
    "JTO": "jtojtomepa8beP8AuQc6eXt5FriJwfFMwQx2v2f9mCL",
    "PYTH": "HZ1JovNiVvGrGNiiYvEozEVgZ58xaU3RKwX8eACQBCt3",
    "BONK": "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263",
    "WIF": "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm",
    "RAY": "4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R",
    "CBBTC": "cbbtcf3aa214zXHbiAZQwf4122FBYbraNdFqgw4iMij",
    "WBTC": "3NZ9JMVBmGAqocybic2c7LQCJScmgsAZ6vQqTDzcqmJh",
    "WETH": "7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs",
}
# Broader Jupiter routes for majors (meme coins still use JUPITER_ROUTE_DEXES).
# ZeroFi is omitted: on the Surfpool fork its swap_v4 fails with custom error 0xd (program log 13)
# even after surfnet_resetAccount on every ZeroFi-owned account and a fresh clock sync. The first
# buy often succeeds because Jupiter picked another venue; a later quote that hops through ZeroFi then fails sim.
MAJORS_ROUTE_DEXES = (
    "Raydium,Raydium CLMM,Raydium CP,Whirlpool,Orca (Whirlpools),Meteora,Meteora DLMM,"
    "Phoenix,OpenBook,OpenBook V2,Lifinity,Lifinity V2,SolFi,Stabble"
)

# SOL and BTC mints used only for market-move alerts (not tradable majors; SOL is the input).
SOL_MINT = WSOL_MINT
BTC_MINT = MAJORS["CBBTC"]


def is_major_mint(mint: str) -> bool:
    return mint in MAJORS.values()


def ticker_for_mint(mint: str) -> str | None:
    for t, m in MAJORS.items():
        if m == mint:
            return t
    return None


def route_dexes(mint: str | None = None) -> str | None:
    """Jupiter dexes filter. Majors omit the meme-only Pump/Raydium-CP restriction."""
    if mint and is_major_mint(mint):
        return MAJORS_ROUTE_DEXES
    return JUPITER_ROUTE_DEXES

# Co-pilot de-risking offers (while holding, the agent only ever offers to lower risk)
# worst-case loss on a card: the stop-loss sell may fill up to the configured stop-loss slippage below the stop, and a
# fast meme-coin drop can gap past the stop between two price polls by about this much more
STOP_GAP_PCT = 3.0
DERISK_SELL_HALF_UP_PCT = 10.0
DERISK_BREAK_EVEN_UP_PCT = 20.0
