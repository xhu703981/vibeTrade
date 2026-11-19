"""Legible guard blocks: what the buy was supposed to move vs what the transaction would actually move.

Numbers and fixed labels only. A wallet that is not the user's is shown as "unknown wallet" (never its address);
token names and any token text never appear here.
"""
from __future__ import annotations

import struct

from backend.config import USDC_MINT

TOKEN_PROGRAMS = {"TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", "TokenzQdBNbLqP5VEhdkAS6EPFLC1PR2BXb3pLRYzJ8Gx"}


def _usd(atomic: int | float) -> float:
    return round(abs(atomic) / 1e6, 6)


def outside_transfers(sim, wallet_token_accounts: set[str]) -> list[dict]:
    """Top-level SPL Transfer / TransferChecked instructions that move the wallet's tokens to an account that isn't the
    wallet's. (A normal swap moves tokens only inside the allowlisted swap programs, never as a top-level transfer.)"""
    out = []
    for ix in getattr(getattr(sim, "tx", None), "instructions", ()) or ():
        if ix.program_id not in TOKEN_PROGRAMS or not ix.data or ix.data[0] not in (3, 12) or len(ix.data) < 9:
            continue
        amount = struct.unpack("<Q", bytes(ix.data[1:9]))[0]
        src, dst = (ix.accounts[0], ix.accounts[1]) if ix.data[0] == 3 else (ix.accounts[0], ix.accounts[2])
        mint = USDC_MINT if ix.data[0] == 3 else ix.accounts[1]
        snap = (getattr(sim, "snaps", None) or {}).get(src)
        tok = getattr(snap, "pre_token", None)
        if tok is not None:
            mint = tok.mint
        if dst not in wallet_token_accounts:
            out.append({"mint": mint, "amount": amount, "instruction": ix.index})
    return out


def flow_summary(sim, wallet: str, mint: str, amount_usdc: float, expected_out: int) -> dict:
    """{expected: [...], simulated: [...], alerts: ["USDC -> unknown wallet: -0.10"]} for the screen."""
    from backend.copilot.redteam import ata
    own = {str(ata(wallet, USDC_MINT)), str(ata(wallet, mint)), wallet}
    for pk, (pre, post) in (getattr(sim, "user_token_accounts", None) or {}).items():
        own.add(pk)
    extra = outside_transfers(sim, own) if sim is not None else []
    expected = [{"what": "USDC out of your wallet, to the pool", "usdc": -round(amount_usdc, 6)},
                {"what": "coin in", "tokens": int(expected_out)}]
    simulated = []
    deltas = getattr(sim, "token_deltas", None) or {}
    ok = bool(getattr(sim, "success", False))
    if ok and USDC_MINT in deltas:
        simulated.append({"what": "USDC out of your wallet", "usdc": -_usd(deltas[USDC_MINT])})
        simulated.append({"what": "coin in", "tokens": int(deltas.get(mint, 0))})
    else:
        simulated.append({"what": "USDC out of your wallet, to the pool", "usdc": -round(amount_usdc, 6)})
    for x in extra:
        label = "USDC" if x["mint"] == USDC_MINT else "token"
        simulated.append({"what": f"{label} to an unknown wallet (not yours)", "usdc": -_usd(x["amount"]) if label == "USDC" else None,
                          "alert": True})
    alerts = [f"{'USDC' if x['mint'] == USDC_MINT else 'Token'} \u2192 unknown wallet: \u2212{_usd(x['amount']):g}"
              + (" USDC" if x["mint"] == USDC_MINT else "") for x in extra]
    return {"expected": expected, "simulated": simulated, "alerts": alerts, "simulation_ok": ok}
