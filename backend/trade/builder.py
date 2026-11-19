"""Derive each trade's intent deterministically from the frozen mandate. No LLM involved.

The intent is the guard's reference: the guard trusts only the intent and the on-chain simulation, never Jupiter's quote claims.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from solders.pubkey import Pubkey

from backend.config import USDC_MINT
from backend.mandate.schema import Mandate

USDC_DECIMALS = 6
LAMPORTS_PER_SOL = 1_000_000_000


class IntentError(ValueError):
    pass


@dataclass(frozen=True)
class TradeIntent:
    mandate_id: str
    mandate_hash: str
    side: str                    # "buy" | "sell"
    wallet: str
    input_mint: str
    output_mint: str
    amount_in: int               # base units: spend at most this much input token
    min_out: int                 # base units: receive at least this much output token
    max_slippage_bps: int
    max_price_impact_pct: float
    max_fee_lamports: int
    sol_reserve_lamports: int
    # "jupiter": expected output comes from the Jupiter quote (mainnet state).
    # "fork_profile": expected output is the executable value from profiling a sell on the fork (used when fork prices diverge from mainnet);
    # the quote's slippage / thresholds are mainnet-based and meaningless then; the guard checks min_out against the simulation only.
    expected_source: str = "jupiter"


def _parse_ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _check_mint(m: str) -> str:
    try:
        Pubkey.from_string(m)
    except Exception as e:  # noqa: BLE001
        raise IntentError("invalid mint address") from e
    return m


def min_out_from_quote(expected_out: int, slippage_bps: int) -> int:
    """minOut = floor(expected_out * (1 - slippage)), integer arithmetic."""
    return expected_out * (10_000 - slippage_bps) // 10_000


def build_buy_intent(mandate: Mandate, wallet: str, output_mint: str, amount_usdc: float, expected_out: int,
                     spent_usdc: float = 0.0, now: datetime | None = None) -> TradeIntent:
    """Buy: USDC -> output_mint. expected_out is the card's quoted output (base units)."""
    b, e = mandate.body.budget, mandate.body.exec
    now = now or datetime.now(timezone.utc)
    if now >= _parse_ts(mandate.expires_at):
        raise IntentError("the mandate has expired: selling only, no buying")
    _check_mint(output_mint)
    if output_mint == USDC_MINT:
        raise IntentError("the output token cannot be USDC")
    if amount_usdc <= 0:
        raise IntentError("amount must be greater than 0")
    if amount_usdc > b.per_trade_usdc:
        raise IntentError(f"amount {amount_usdc:g} exceeds the per-trade cap of {b.per_trade_usdc:g} USDC")
    if spent_usdc + amount_usdc > b.total_usdc + 1e-9:
        raise IntentError(f"cumulative {spent_usdc + amount_usdc:g} exceeds the total budget of {b.total_usdc:g} USDC")
    if expected_out <= 0:
        raise IntentError("quoted output must be greater than 0")
    slip = e.max_slippage_bps.buy
    return TradeIntent(
        mandate_id=mandate.mandate_id, mandate_hash=mandate.hash, side="buy", wallet=wallet,
        input_mint=USDC_MINT, output_mint=output_mint,
        amount_in=round(amount_usdc * 10**USDC_DECIMALS), min_out=min_out_from_quote(expected_out, slip),
        max_slippage_bps=slip, max_price_impact_pct=e.max_price_impact_pct,
        max_fee_lamports=round(e.max_fee_sol * LAMPORTS_PER_SOL),
        sol_reserve_lamports=round(b.sol_reserve * LAMPORTS_PER_SOL),
    )


def build_sell_intent(mandate: Mandate, wallet: str, input_mint: str, amount_in: int, expected_usdc_out: int,
                      stop_loss: bool = False, expected_source: str = "jupiter") -> TradeIntent:
    """Sell: input_mint -> USDC. Selling is still allowed after the mandate expires."""
    e, b = mandate.body.exec, mandate.body.budget
    _check_mint(input_mint)
    if input_mint == USDC_MINT or amount_in <= 0:
        raise IntentError("invalid sell parameters")
    slip = e.max_slippage_bps.stop_loss_sell if stop_loss else e.max_slippage_bps.buy
    return TradeIntent(
        mandate_id=mandate.mandate_id, mandate_hash=mandate.hash, side="sell", wallet=wallet,
        input_mint=input_mint, output_mint=USDC_MINT, amount_in=amount_in,
        min_out=min_out_from_quote(expected_usdc_out, slip), max_slippage_bps=slip,
        max_price_impact_pct=e.max_price_impact_pct if not stop_loss else 100.0,
        max_fee_lamports=round(e.max_fee_sol * LAMPORTS_PER_SOL),
        sol_reserve_lamports=round(b.sol_reserve * LAMPORTS_PER_SOL),
        expected_source=expected_source,
    )
