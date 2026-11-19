"""Guard semantic layer (JEV): runs only after the rule layer allows; it can only upgrade allow to ask_human, never loosen.

JEV only sees structured fields (intent, balance deltas, program categories), never token names, descriptions or other external text.
Addresses are replaced by role labels ("USDC" / "target token" / program name); in testing, raw base58 addresses made JEV give
inflated risk on normal trades. The question is "is this an ordinary swap within the mandate" (P(safe)); risk = 1 - P(safe).
Measured (M2, jev-1.14): normal swap P(safe) 0.79-0.88; other-token outflow / delegate approval 0.04-0.05.
After the M5 SOL-field change (jev-1.14): normal PumpSwap buy risk 0.06-0.07; other-token outflow 0.94, delegate 0.92,
output below min 0.92, overspend 0.93, fee over cap 0.74, extra SOL outflow 0.52 (the rule layer blocks that one deterministically anyway).
"""
from __future__ import annotations

import json
from typing import Awaitable, Protocol

from backend.config import USDC_MINT, WSOL_MINT
from backend.guard.simulate import COMPUTE_BUDGET, SYSTEM_PROGRAM, TOKEN_2022_PROGRAM, TOKEN_PROGRAM, SimResult
from backend.trade.builder import TradeIntent

SAFE_THRESHOLD = 0.5          # P(safe) below this -> ask_human
RISK_THRESHOLD = 1 - SAFE_THRESHOLD

_PROGRAM_NAMES = {
    COMPUTE_BUDGET: "ComputeBudget", SYSTEM_PROGRAM: "System", TOKEN_PROGRAM: "SPL Token", TOKEN_2022_PROGRAM: "Token-2022",
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL": "AssociatedTokenAccount",
    "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4": "JupiterAggregatorV6",
}


class AsksJEV(Protocol):
    def ask(self, state: str, name: str, question: dict) -> Awaitable[float]: ...


def _mint_label(m: str, intent: TradeIntent) -> str:
    if m == intent.input_mint:
        return "USDC (input)" if m == USDC_MINT else "input token"
    if m == intent.output_mint:
        return "USDC (output)" if m == USDC_MINT else "target token (output)"
    if m == WSOL_MINT:
        return "wrapped SOL"
    if m == USDC_MINT:
        return "USDC"
    return "other token"


def build_state(sim: SimResult, intent: TradeIntent) -> str:
    deltas = dict(sim.token_deltas)
    spent = -deltas.pop(intent.input_mint, 0)
    received = deltas.pop(intent.output_mint, 0)
    others: dict[str, int] = {}
    for i, (m, d) in enumerate(deltas.items()):
        if d:
            others[f"{_mint_label(m, intent)} #{i + 1}"] = d
    changes = []
    for _k, (pre, post) in sim.user_token_accounts.items():
        if pre and post and (pre.owner != post.owner or pre.delegate != post.delegate or pre.close_authority != post.close_authority):
            changes.append({"account": f"user {_mint_label(post.mint, intent)} account",
                            "owner_changed_to_other": post.owner != intent.wallet,
                            "delegate_after": "third party" if post.delegate else None,
                            "close_authority_after": None if post.close_authority in (None, intent.wallet) else "third party"})
    allowance = sim.tx.fee_lamports_max + sim.created_rent_lamports
    inner = {p for d, p in sim.invoked_programs if d > 1}
    known_inner = sorted(_PROGRAM_NAMES[p] for p in inner if p in _PROGRAM_NAMES)
    view = {
        "mandate_allows": {"side": intent.side, "input": _mint_label(intent.input_mint, intent),
                           "output": _mint_label(intent.output_mint, intent),
                           "max_input_amount": intent.amount_in, "min_output_amount": intent.min_out,
                           "max_fee_lamports": intent.max_fee_lamports},
        "simulated_effects_on_user_wallet": {
            "input_spent": spent, "output_received": received, "other_token_changes": others,
            # SOL is given only as two booleans (the rule layer already checked the exact numbers): with raw SOL deltas or deposit amounts
            # JEV misread "fee + deposit" as exceeding max_fee (M5 measured risk 0.55-0.77).
            "network_fee_within_max_fee": sim.tx.fee_lamports_max <= intent.max_fee_lamports,
            "sol_leaving_wallet_beyond_fee_and_refundable_rent": -sim.sol_delta > allowance,
            "token_account_authority_or_delegate_changes": changes,
        },
        "top_level_programs": [_PROGRAM_NAMES.get(ix.program_id, "unlisted program") for ix in sim.tx.instructions],
        "inner_programs_invoked_by_aggregator": known_inner + ([f"{len(inner) - len(known_inner)} AMM programs"] if len(inner) > len(known_inner) else []),
    }
    return json.dumps(view)


QUESTION = {
    "type": "noul",
    "instructions": (
        "A wallet is about to execute a swap for the user. Compare the simulated effects with what the mandate allows. "
        "Is this exactly a plain swap within the mandate: input spent <= max_input_amount, output received >= "
        "min_output_amount, no other tokens leaving, no authority or delegate changes, SOL decrease only fee plus rent?"),
    "criteria": {"true": "Yes, the effects are a plain swap fully within the mandate.",
                 "false": "No, something goes beyond what the mandate allows."},
}


async def semantic_check(jev: AsksJEV, sim: SimResult, intent: TradeIntent) -> tuple[float, str]:
    """Return (P(risk), note). P(risk) = 1 - P(safe)."""
    p_safe = await jev.ask(build_state(sim, intent), "is_safe", QUESTION)
    risk = 1.0 - p_safe
    return risk, f"JEV risk probability {risk:.2f}"
