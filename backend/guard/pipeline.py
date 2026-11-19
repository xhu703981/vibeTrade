"""Guard entry point: transaction bytes + intent -> Verdict."""
from __future__ import annotations

import base64

from backend.guard.rules import JUPITER_V6
from backend.guard.semantic import AsksJEV
from backend.guard.simulate import (SYSTEM_PROGRAM, TOKEN_PROGRAMS, SimResult, alt_addresses_of, decode_transaction,
                                    fetch_alt_tables, parse_token_account, refresh_accounts, simulate)
from backend.guard.verdict import Verdict, decide
from backend.trade.builder import TradeIntent

_SKIP = {"Sysvar1nstructions1111111111111111111111111"}


async def pool_accounts_to_refresh(rpc, raw: bytes, wallet: str) -> list[str]:
    """Writable accounts used by the Jupiter route that do not belong to the user (pools, vaults). Only Jupiter instructions are inspected."""
    tx = decode_transaction(raw, await fetch_alt_tables(rpc, alt_addresses_of(raw)))
    writable = {k for k, w in zip(tx.account_keys, tx.writable) if w}
    route = {a for ix in tx.instructions if ix.program_id == JUPITER_V6 for a in ix.accounts}
    cand = sorted(k for k in route & writable if k != wallet and k not in _SKIP)
    if not cand:
        return []
    res = await rpc.call("getMultipleAccounts", [cand, {"encoding": "base64", "commitment": "confirmed"}])
    out = []
    for k, acc in zip(cand, res["value"]):
        if acc is None or acc.get("executable") or acc.get("owner") == SYSTEM_PROGRAM:
            continue
        if acc.get("owner") in TOKEN_PROGRAMS:
            t = parse_token_account(base64.b64decode(acc["data"][0]), acc["owner"])
            if t and t.owner == wallet:
                continue
        out.append(k)
    return out


async def check_transaction(rpc, raw: bytes, intent: TradeIntent, quote: dict | None = None,
                            jev: AsksJEV | None = None, refresh_pools: bool = False) -> tuple[Verdict, SimResult | None]:
    """Fail-closed: any decode/simulation error blocks.

    With refresh_pools=True the route's pools are first resetAccount'ed (refreshed to the latest mainnet state).
    Note: in fork mode M5 moves prices with a whale wallet; do not refresh then, or the fork's price changes are wiped.
    """
    try:
        refresh = await pool_accounts_to_refresh(rpc, raw, intent.wallet) if refresh_pools else None
        sim = await simulate(rpc, raw, intent.wallet, refresh)
    except Exception as e:  # noqa: BLE001
        return Verdict("block", [f"could not simulate or decode the transaction: {type(e).__name__}: {str(e)[:160]}"]), None
    return await decide(sim, intent, quote, jev), sim
