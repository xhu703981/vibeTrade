"""Jupiter Swap API v2 client: uses only GET /swap/v2/build (not /order or /execute).

- x-api-key is used only in the backend and never logged.
- Global 1 RPS rate limit (shared by quotes, re-quotes and the listener).
- /build returns raw instructions; we assemble the v0 transaction ourselves with a Surfpool blockhash.
"""
from __future__ import annotations

import asyncio
import base64
import time

import httpx
from solders.address_lookup_table_account import AddressLookupTableAccount
from solders.compute_budget import set_compute_unit_limit
from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

from backend.config import JUPITER_BASE, settings


class RateLimiter:
    def __init__(self, rps: float):
        self.interval = 1.0 / rps
        self._last = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            delay = self._last + self.interval - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            self._last = time.monotonic()


_limiter = RateLimiter(rps=1.0)


class JupiterError(RuntimeError):
    pass


async def build_swap(input_mint: str, output_mint: str, amount: int, taker: str, slippage_bps: int,
                     client: httpx.AsyncClient | None = None, **extra) -> dict:
    params = {"inputMint": input_mint, "outputMint": output_mint, "amount": str(amount), "taker": taker,
              "slippageBps": slippage_bps, "transactionVersion": "0", **extra}
    headers = {"x-api-key": settings.jupiter_api_key} if settings.jupiter_api_key else {}
    own = client is None
    client = client or httpx.AsyncClient(timeout=20)
    try:
        await _limiter.wait()
        r = await client.get(f"{JUPITER_BASE}/swap/v2/build", params=params, headers=headers)
    finally:
        if own:
            await client.aclose()
    if r.status_code != 200:
        raise JupiterError(f"/swap/v2/build HTTP {r.status_code}: {r.text[:300]}")
    return r.json()


def _ix(j: dict) -> Instruction:
    return Instruction(
        Pubkey.from_string(j["programId"]),
        base64.b64decode(j["data"]),
        [AccountMeta(Pubkey.from_string(a["pubkey"]), a["isSigner"], a["isWritable"]) for a in j["accounts"]],
    )


def build_instructions(build: dict, cu_limit: int = 1_400_000) -> list[Instruction]:
    """In order: CU limit, CU price, setup, swap, cleanup, other. No tip instruction (we do not use /submit)."""
    ixs = [set_compute_unit_limit(cu_limit)]
    ixs += [_ix(i) for i in build.get("computeBudgetInstructions") or []]
    ixs += [_ix(i) for i in build.get("setupInstructions") or []]
    ixs.append(_ix(build["swapInstruction"]))
    if build.get("cleanupInstruction"):
        ixs.append(_ix(build["cleanupInstruction"]))
    ixs += [_ix(i) for i in build.get("otherInstructions") or []]
    return ixs


async def fetch_alts(rpc, alt_addresses: list[str]) -> list[AddressLookupTableAccount]:
    """Fetch lookup tables from the target chain (Surfpool) ourselves instead of trusting Jupiter's mapping."""
    from solders.address_lookup_table_account import AddressLookupTable

    if not alt_addresses:
        return []
    res = await rpc.call("getMultipleAccounts", [alt_addresses, {"encoding": "base64", "commitment": "confirmed"}])
    out = []
    for addr, acc in zip(alt_addresses, res["value"]):
        if acc is None:
            raise JupiterError(f"ALT {addr} does not exist on the fork")
        table = AddressLookupTable.deserialize(base64.b64decode(acc["data"][0]))
        out.append(AddressLookupTableAccount(Pubkey.from_string(addr), list(table.addresses)))
    return out


def assemble_v0(payer: Keypair, ixs: list[Instruction], alts: list[AddressLookupTableAccount], blockhash: str) -> VersionedTransaction:
    msg = MessageV0.try_compile(payer.pubkey(), ixs, alts, Hash.from_string(blockhash))
    return VersionedTransaction(msg, [payer])
