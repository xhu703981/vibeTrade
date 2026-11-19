"""Free on-chain part of research: holder count, dev address (recorded in the pool), dev holding."""
from __future__ import annotations

import base64
import struct

from solders.pubkey import Pubkey

from backend.discovery.free_checks import PUMPSWAP_PROGRAM, RAYDIUM_CPMM_PROGRAM, TOKEN_2022_PROGRAM, TOKEN_PROGRAM

RAYDIUM_CPMM_AUTHORITY = "GpMZbSM2GgvTKHJirzeGfMFoaZ8UR2X7F4v8vHTvxFbL"
INCINERATOR = "1nc1nerator11111111111111111111111111111111"
_DEFAULT = str(Pubkey.default())


async def mint_supply(rpc, mint: str) -> tuple[int, int, str]:
    """(raw supply, decimals, token program)"""
    acc = (await rpc.call("getAccountInfo", [mint, {"encoding": "base64"}]))["value"]
    d = base64.b64decode(acc["data"][0])
    return struct.unpack_from("<Q", d, 36)[0], d[44], acc["owner"]


async def holder_count(rpc, mint: str, program: str) -> int:
    """Number of token accounts with balance > 0 (getProgramAccounts, fetching only the 8 amount bytes)."""
    filters: list = [{"memcmp": {"offset": 0, "bytes": mint}}]
    if program == TOKEN_PROGRAM:
        filters.append({"dataSize": 165})
    res = await rpc.call("getProgramAccounts", [program, {"encoding": "base64", "dataSlice": {"offset": 64, "length": 8},
                                                          "filters": filters}])
    return sum(1 for a in res if int.from_bytes(base64.b64decode(a["account"]["data"][0]), "little") > 0)


async def pool_creators(rpc, pool: str) -> tuple[list[str], list[str]]:
    """(dev address recorded in the pool, pool addresses to exclude from the top 10)"""
    acc = (await rpc.call("getAccountInfo", [pool, {"encoding": "base64"}]))["value"]
    if acc is None:
        return [], [pool]
    d = base64.b64decode(acc["data"][0])
    P = lambda o: str(Pubkey.from_bytes(d[o:o + 32]))  # noqa: E731
    if acc["owner"] == PUMPSWAP_PROGRAM and len(d) >= 243:
        c = P(211)  # coin_creator (the original pump.fun dev)
        return ([c] if c != _DEFAULT else []), [pool]
    if acc["owner"] == RAYDIUM_CPMM_PROGRAM and len(d) >= 72:
        return [P(40)], [pool, RAYDIUM_CPMM_AUTHORITY]  # pool_creator
    return [], [pool]


async def owner_balance(rpc, owner: str, mint: str) -> int:
    res = (await rpc.call("getTokenAccountsByOwner", [owner, {"mint": mint}, {"encoding": "jsonParsed"}]))["value"]
    return sum(int(a["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"]) for a in res)


__all__ = ["mint_supply", "holder_count", "pool_creators", "owner_balance", "INCINERATOR", "TOKEN_2022_PROGRAM"]
