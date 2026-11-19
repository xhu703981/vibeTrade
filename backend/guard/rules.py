"""Guard deterministic rule layer. BLOCK is final; no higher layer (JEV or a human) can override it.

Two kinds of checks that back each other up:
1. Instruction layer (transaction bytes): top-level program allowlist + per-program instruction allowlist (default deny).
2. State layer (profile pre/post state): balance deltas, delegate/owner changes, SOL flows.
   Even if a malicious action hides in a Jupiter inner CPI, the state layer sees its effect.

Top-level allowlist basis (Jupiter /swap/v2/build transactions observed in M0 and M2):
  ComputeBudget x2 (we add the CU limit, Jupiter sets the CU price)
  ATA CreateIdempotent (setup: creates the user's output token account)
  Jupiter v6 RouteV2 (swap) - AMMs (GoonFi, Kipseli, Byreal...) are Jupiter inner CPIs, never top level
  Token CloseAccount (cleanup: closes the temporary wSOL account when output is SOL; lamports return to the user)
System is allowlisted, but only for transfers to the wallet itself (e.g. wrapping SOL); every other System instruction is rejected.

Allowed SOL decrease = fee cap + rent-exempt deposits of program accounts created by this transaction. Deposits are matched exactly
against on-chain getMinimumBalanceForRentExemption (a first PumpSwap buy creates a 137-byte user PDA,
a Token-2022 ATA is 170 bytes); transfers to new wallets or extra lamports do not count as deposits.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

from backend.config import WSOL_MINT
from backend.guard.evidence import check as _check, instruction_result, observe
from backend.guard.simulate import (COMPUTE_BUDGET, SYSTEM_PROGRAM, TOKEN_2022_PROGRAM, TOKEN_PROGRAM, SimResult)
from backend.trade.builder import TradeIntent

ATA_PROGRAM = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
JUPITER_V6 = "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"

TOP_LEVEL_ALLOWLIST: frozenset[str] = frozenset({
    COMPUTE_BUDGET, SYSTEM_PROGRAM, TOKEN_PROGRAM, TOKEN_2022_PROGRAM, ATA_PROGRAM, JUPITER_V6,
})

# SPL Token instruction tags
TOKEN_IX = {0: "InitializeMint", 1: "InitializeAccount", 3: "Transfer", 4: "Approve", 5: "Revoke", 6: "SetAuthority",
            7: "MintTo", 8: "Burn", 9: "CloseAccount", 10: "FreezeAccount", 11: "ThawAccount", 12: "TransferChecked",
            13: "ApproveChecked", 14: "MintToChecked", 15: "BurnChecked", 17: "SyncNative"}
TOKEN_IX_ALLOWED = {9, 17}        # CloseAccount (destination must be the user), SyncNative
COMPUTE_BUDGET_ALLOWED = {1, 2, 3, 4}
ATA_ALLOWED = {0, 1}              # Create, CreateIdempotent (owner must be the user)

BLOCK = "block"
ASK = "ask_human"


@dataclass(frozen=True)
class Finding:
    code: str
    severity: str
    message: str


def _b(code: str, msg: str) -> Finding:
    return Finding(code, BLOCK, msg)


def check_instructions(sim: SimResult, intent: TradeIntent) -> list[Finding]:
    tx, w = sim.tx, intent.wallet
    out: list[Finding] = []
    if _check("Fee payer matches wallet", tx.fee_payer != w):
        out.append(_b("FEE_PAYER", "fee payer is not the trading wallet"))
    if _check("Only wallet signs", set(tx.signers) != {w}):
        out.append(_b("SIGNERS", "transaction needs signatures other than the wallet"))
    for ix in tx.instructions:
        p, d, acc = ix.program_id, ix.data, ix.accounts
        tag = d[0] if d else None
        if _check(f"Instruction {ix.index}: program allowlist", p not in TOP_LEVEL_ALLOWLIST):
            out.append(_b("PROGRAM_NOT_ALLOWED", f"instruction {ix.index} calls non-allowlisted program {p}"))
            continue
        if p == COMPUTE_BUDGET:
            if _check(f"Instruction {ix.index}: compute instruction allowlist", tag not in COMPUTE_BUDGET_ALLOWED):
                out.append(_b("CB_IX", f"instruction {ix.index}: unknown ComputeBudget instruction"))
        elif p in (TOKEN_PROGRAM, TOKEN_2022_PROGRAM):
            name = TOKEN_IX.get(tag, f"tag {tag}")
            if _check(f"Instruction {ix.index}: token instruction allowlist", tag not in TOKEN_IX_ALLOWED):
                code = {6: "SET_AUTHORITY", 4: "APPROVE", 13: "APPROVE", 3: "TOKEN_TRANSFER", 12: "TOKEN_TRANSFER"}.get(tag, "TOKEN_IX")
                out.append(_b(code, f"instruction {ix.index}: forbidden Token instruction {name}"))
            elif tag == 9:  # CloseAccount: [account, destination, owner]
                if _check(f"Instruction {ix.index}: close destination is wallet", len(acc) < 3 or acc[1] != w):
                    out.append(_b("CLOSE_TO_STRANGER", f"instruction {ix.index}: CloseAccount sends lamports to someone else"))
                if _check(f"Instruction {ix.index}: close authority is wallet", len(acc) >= 3 and acc[2] != w):
                    out.append(_b("CLOSE_AUTH", f"instruction {ix.index}: CloseAccount owner is not the wallet"))
        elif p == ATA_PROGRAM:
            t = 0 if not d else tag
            if _check(f"Instruction {ix.index}: ATA instruction allowlist", t not in ATA_ALLOWED):
                out.append(_b("ATA_IX", f"instruction {ix.index}: forbidden ATA instruction"))
            elif _check(f"Instruction {ix.index}: ATA owner is wallet", len(acc) < 3 or acc[2] != w):
                out.append(_b("ATA_OWNER", f"instruction {ix.index}: creates a token account for someone else"))
        elif p == SYSTEM_PROGRAM:
            st = struct.unpack_from("<I", d, 0)[0] if len(d) >= 4 else None
            # only Transfer(2) to accounts owned by the wallet itself (e.g. its wSOL ATA)
            ok = st == 2 and len(acc) >= 2 and acc[0] == w and _is_user_account(sim, acc[1], w)
            if _check(f"Instruction {ix.index}: system transfer destination", not ok):
                out.append(_b("SYSTEM_IX", f"instruction {ix.index}: forbidden System instruction (transfer to an unknown address or account modification)"))
        # JUPITER_V6: its internals are covered by the state layer
    for ix in tx.instructions:
        observe(instruction_result, ix.index, any(f.message.startswith((f"instruction {ix.index}:", f"instruction {ix.index} calls")) for f in out))
    return out


def _is_user_account(sim: SimResult, pk: str, w: str) -> bool:
    if pk == w:
        return True
    pair = sim.user_token_accounts.get(pk)
    return pair is not None


def check_state(sim: SimResult, intent: TradeIntent) -> list[Finding]:
    out: list[Finding] = []
    w = intent.wallet
    if _check("Simulation succeeded", not sim.success):
        return [_b("SIM_FAILED", f"simulation failed: {(sim.error or '')[:200]}")]
    ws = sim.snaps.get(w)
    if _check("Wallet profile available", ws is None or sim.sol_pre is None or sim.sol_post is None):
        return [_b("NO_WALLET_STATE", "no wallet state in the simulation result")]
    if _check("Wallet owner and data unchanged", ws.post_owner != ws.pre_owner or ws.post_owner != SYSTEM_PROGRAM or (ws.post_data_len or 0) != 0):
        out.append(_b("WALLET_MODIFIED", "the wallet account's owner or data was modified"))

    # authority changes on the user's token accounts (covers Approve / SetAuthority inside inner CPIs)
    for pk, (pre, post) in sim.user_token_accounts.items():
        if _check("Token owner unchanged", bool(pre and post and post.owner != pre.owner)):
            out.append(_b("SET_AUTHORITY", f"token account {pk[:8]}... owner changed to someone else"))
        if _check("No new or increased delegate", bool(post and post.delegate is not None and (pre is None or post.delegate != pre.delegate or post.delegated_amount > pre.delegated_amount))):
            out.append(_b("APPROVE", f"token account {pk[:8]}... delegated to {post.delegate[:8]}..."))
        if _check("No foreign close authority", bool(post and post.close_authority is not None and post.close_authority != w and (pre is None or pre.close_authority != post.close_authority))):
            out.append(_b("SET_AUTHORITY", f"token account {pk[:8]}... close authority given to someone else"))

    deltas = dict(sim.token_deltas)
    fee = sim.tx.fee_lamports_max
    if _check("Fee within mandate cap", fee > intent.max_fee_lamports):
        out.append(_b("FEE_CAP", f"fee cap {fee / 1e9:.6f} SOL exceeds the mandate's {intent.max_fee_lamports / 1e9:.6f} SOL"))

    # input token
    spent = -deltas.pop(intent.input_mint, 0)
    if _check("Input within budget", spent > intent.amount_in):
        out.append(_b("OVER_BUDGET", f"actual spend {spent} exceeds intent amount {intent.amount_in} (base units)"))
    # output token
    if intent.output_mint == WSOL_MINT:
        got = sim.sol_delta + fee + sim.created_rent_lamports + deltas.pop(WSOL_MINT, 0)
    else:
        got = deltas.pop(intent.output_mint, 0)
    if _check("Output meets minimum", got < intent.min_out):
        out.append(_b("MIN_OUT", f"received {got}, below the minimum {intent.min_out} (base units)"))
    # no other token may decrease
    for mint, dlt in deltas.items():
        if _check("Other token does not decrease", dlt < 0):
            out.append(_b("OTHER_TOKEN_DECREASE", f"another wallet token {mint[:8]}... decreased by {-dlt}"))
    # SOL: balance stays above the reserve; decrease at most fee + rent of newly created accounts
    if _check("SOL reserve maintained", sim.sol_post < intent.sol_reserve_lamports):
        out.append(_b("SOL_RESERVE", f"SOL after the trade {sim.sol_post / 1e9:.6f} is below the reserve {intent.sol_reserve_lamports / 1e9:.6f}"))
    if intent.output_mint != WSOL_MINT:
        allowed = fee + sim.created_rent_lamports  # fee + rent-exempt deposits of newly created program accounts (queried on-chain, exact match)
        if _check("SOL outflow within fee and rent", -sim.sol_delta > allowed):
            out.append(_b("SOL_OUTFLOW", f"SOL decreased by {-sim.sol_delta} lamports, more than fee + rent {allowed}"))
    return out


def check_quote(quote: dict | None, intent: TradeIntent) -> list[Finding]:
    """Values declared in the Jupiter quote are only secondary checks (the real constraints come from the state layer)."""
    if not quote:
        return []
    out = []
    fork_priced = intent.expected_source == "fork_profile"  # the quote is priced on mainnet and not comparable on the fork; min_out is checked against the simulation
    if not fork_priced and _check("Quoted slippage within cap", int(quote.get("slippageBps", 0)) > intent.max_slippage_bps):
        out.append(_b("SLIPPAGE", f"quoted slippage {quote.get('slippageBps')} bps exceeds the cap {intent.max_slippage_bps}"))
    if not fork_priced and _check("Quoted minimum output", int(quote.get("otherAmountThreshold", 0)) < intent.min_out):
        out.append(_b("MIN_OUT_QUOTE", "quoted minimum output is below the intent's minOut"))
    if _check("Price impact within cap", float(quote.get("priceImpactPct") or 0) * 100 > intent.max_price_impact_pct):
        out.append(_b("PRICE_IMPACT", f"price impact {float(quote['priceImpactPct']) * 100:.2f}% exceeds the cap {intent.max_price_impact_pct}%"))
    if _check("Quote tokens match intent", quote.get("inputMint") not in (None, intent.input_mint) or quote.get("outputMint") not in (None, intent.output_mint)):
        out.append(_b("QUOTE_MINTS", "quoted tokens do not match the intent"))
    if _check("Quote input within budget", int(quote.get("inAmount", 0)) > intent.amount_in):
        out.append(_b("QUOTE_AMOUNT", "quoted input amount exceeds the intent amount"))
    return out


def evaluate(sim: SimResult, intent: TradeIntent, quote: dict | None = None) -> list[Finding]:
    return check_instructions(sim, intent) + check_state(sim, intent) + check_quote(quote, intent)
