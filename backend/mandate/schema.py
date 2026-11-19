"""Mandate pydantic models + v2 defaults.

- Draft*: mutable draft during the dialogue; the 5 required fields default to None and must come from the user's own words.
- Mandate: read-only frozen version (frozen=True, lists as tuples), with a hash.
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

REQUIRED_FIELDS: tuple[str, ...] = (
    "budget.total_usdc",
    "budget.per_trade_usdc",
    "exec.max_fee_sol",
    "exit.take_profit_ladder",
)
# budget.daily_max_loss_usdc is one of the three opening limits (total, per trade, daily max loss); it comes from the saved
# settings, so it is not asked in the long first-time setup. When set, buys stop for the day once today's loss reaches it.
# exit.stop_loss_pct is optional: unset = each trade gets a stop from the coin's volatility (ATR), shown on its card.
# exit.take_profit_ladder = [] (the saved default): no automatic take profit; the co-pilot offers de-risking instead.

DEFAULT_BANNED_EXTENSIONS = ("TransferHook", "PermanentDelegate", "DefaultAccountStateFrozen", "NonTransferable")


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TakeProfitStep(_M):
    multiple: float = Field(gt=1.0)
    sell_pct: float = Field(gt=0, le=100)


class Funding(_M):
    # Wallet addresses are filled by code from trusted config; neither the LLM nor the dialogue can change them
    trading_wallet: str = "solana:<trading hot wallet>"
    input_token: str = "USDC"  # v2: USDC is the direct input; no swap to SOL first
    research_wallet_solana: str = "solana:<research wallet>"
    research_wallet_base: str = "base:<research wallet>"


class Budget(_M):
    total_usdc: float | None = Field(default=None, gt=0)
    per_trade_usdc: float | None = Field(default=None, gt=0)
    daily_max_loss_usdc: float | None = Field(default=None, gt=0)   # realized + unrealized loss today; at the limit buys stop
    sol_reserve: float = Field(default=0.05, ge=0)
    research_usdc: float = Field(default=1.0, ge=0)
    research_per_call_usdc: float = Field(default=0.10, ge=0)
    research_max_coins_per_round: int = Field(default=3, ge=0)   # only the top candidates are researched
    research_topup_requires_approval: bool = True


class Filters(_M):
    require_mint_revoked: bool = True
    require_freeze_revoked: bool = True
    banned_extensions: tuple[str, ...] = DEFAULT_BANNED_EXTENSIONS
    max_transfer_fee_bps: int = Field(default=0, ge=0)
    require_sell_sim: bool = True
    max_roundtrip_loss_pct: float = Field(default=5.0, ge=0)
    min_liquidity_usd: float = Field(default=50000.0, ge=0)
    require_lp_burned_or_locked: bool = True  # only PumpSwap and Raydium CPMM are recognised
    market_cap_usd: tuple[float, float] = (100_000.0, 20_000_000.0)
    token_age_hours_min: float = Field(default=2.0, ge=0)
    max_top10_pct: float = Field(default=40.0, ge=0, le=100)
    max_dev_pct: float = Field(default=5.0, ge=0, le=100)
    min_holders: int = Field(default=300, ge=0)
    max_volume_to_liquidity: float = Field(default=20.0, ge=0)
    reject_known_rugger: bool = True


class Slippage(_M):
    buy: int = Field(default=300, ge=0)
    stop_loss_sell: int = Field(default=1000, ge=0)


class Exec(_M):
    max_slippage_bps: Slippage = Slippage()
    max_price_impact_pct: float = Field(default=2.0, ge=0)
    max_fee_sol: float | None = Field(default=None, gt=0)
    max_fee_pct: float = Field(default=1.0, ge=0)
    quote_max_age_sec: int = Field(default=20, gt=0)
    reprice_max_deviation_pct: float = Field(default=5.0, ge=0)
    approval_timeout_sec: int = Field(default=180, gt=0)


class Exit(_M):
    stop_loss_pct: float | None = Field(default=None, gt=0, lt=100)
    take_profit_ladder: tuple[TakeProfitStep, ...] | None = None
    trailing_stop_pct: float = Field(default=30.0, gt=0, lt=100)
    max_hold_hours: float = Field(default=24.0, gt=0)
    liquidity_drop_pct: float = Field(default=50.0, gt=0, lt=100)
    poll_interval_sec: int = Field(default=5, gt=0)


class MandateBody(_M):
    """Mandate body (without id / timestamps / hash)."""
    funding: Funding = Funding()
    budget: Budget = Budget()
    filters: Filters = Filters()
    exec: Exec = Exec()
    exit: Exit = Exit()
    expires_in_hours: float = Field(default=24.0, gt=0)


class Mandate(_M):
    """Frozen mandate: read-only."""
    mandate_id: str
    frozen_at: str
    expires_at: str
    body: MandateBody
    hash: str


def default_body() -> MandateBody:
    return MandateBody()
