"""Surfpool / Solana helpers: keypairs, balances, cheatcodes."""
from __future__ import annotations

import json
from pathlib import Path

import httpx
from solders.keypair import Keypair
from solders.pubkey import Pubkey

from backend.config import USDC_MINT
from backend.ratelimit import RateLimiter

from spl.token.constants import ASSOCIATED_TOKEN_PROGRAM_ID as ATA_PROGRAM, TOKEN_PROGRAM_ID as TOKEN_PROGRAM


def load_or_create_keypair(path: Path) -> tuple[Keypair, bool]:
    """Return (keypair, newly_created). The file is a solana-cli 64-byte array and its path is gitignored."""
    if path.exists():
        return Keypair.from_bytes(bytes(json.loads(path.read_text(encoding="utf-8")))), False
    path.parent.mkdir(parents=True, exist_ok=True)
    kp = Keypair()
    path.write_text(json.dumps(list(bytes(kp))), encoding="utf-8")
    path.chmod(0o600)
    return kp, True


def ata(owner: Pubkey, mint: Pubkey, token_program: Pubkey = TOKEN_PROGRAM) -> Pubkey:
    return Pubkey.find_program_address([bytes(owner), bytes(token_program), bytes(mint)], ATA_PROGRAM)[0]


class RpcError(RuntimeError):
    """RPC error. Messages never contain the RPC URL (it may hold an API key)."""


class Rpc:
    """Minimal async JSON-RPC client; surfnet_* cheatcodes go through it too."""

    def __init__(self, url: str, timeout: float = 30.0, max_rps: float | None = None):
        self.url = url  # may contain an API key: never put it in logs or exceptions
        self._c = httpx.AsyncClient(timeout=timeout)
        self._id = 0
        local = "127.0.0.1" in url or "localhost" in url
        self._limiter = None if local else RateLimiter(per_minute=(max_rps or 8) * 60)

    def __repr__(self) -> str:
        return "Rpc(<redacted>)"

    async def call(self, method: str, params: list | None = None):
        """JSON-RPC call. Remote RPCs are rate limited with exponential backoff on 429/5xx; errors never include the URL."""
        import asyncio

        for attempt in range(5):
            if self._limiter:
                await self._limiter.wait()
            self._id += 1
            try:
                r = await self._c.post(self.url, json={"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or []})
            except httpx.HTTPError as e:
                if attempt == 4:
                    raise RpcError(f"{method}: network error {type(e).__name__}") from None
                await asyncio.sleep(0.5 * 2 ** attempt)
                continue
            if r.status_code == 429 or r.status_code >= 500:
                if attempt == 4:
                    raise RpcError(f"{method}: HTTP {r.status_code}")
                await asyncio.sleep(0.5 * 2 ** attempt)
                continue
            if r.status_code != 200:
                raise RpcError(f"{method}: HTTP {r.status_code}")
            j = r.json()
            if "error" in j:
                raise RpcError(f"{method}: {j['error']}")
            return j["result"]
        raise RpcError(f"{method}: retries exhausted")

    async def close(self):
        await self._c.aclose()

    async def sol_lamports(self, owner: Pubkey) -> int:
        return (await self.call("getBalance", [str(owner), {"commitment": "confirmed"}]))["value"]

    async def token_amount(self, owner: Pubkey, mint: str = USDC_MINT) -> int:
        """Raw balance (base units) of the owner's ATA for this mint; 0 if the account does not exist."""
        res = await self.call("getAccountInfo", [str(ata(owner, Pubkey.from_string(mint))), {"encoding": "jsonParsed", "commitment": "confirmed"}])
        v = res.get("value")
        if not v:
            return 0
        return int(v["data"]["parsed"]["info"]["tokenAmount"]["amount"])

    async def latest_blockhash(self) -> str:
        return (await self.call("getLatestBlockhash", [{"commitment": "confirmed"}]))["value"]["blockhash"]


async def sync_fork_clock(fork: "Rpc", upstream_url: str) -> dict:
    """Align the fork clock with the current mainnet slot and time (surfnet_timeTravel absoluteSlotAt).

    Why: Surfpool's clock runs at its own slot pace and falls behind mainnet, while pool accounts are fetched on demand from mainnet
    with newer timestamps (e.g. Whirlpool reports InvalidTimestamp). Align before every build/simulation.
    Only moves forward: does nothing if the mainnet slot is not ahead of the fork slot.
    """
    import time

    up = Rpc(upstream_url)
    try:
        mainnet_slot = await up.call("getSlot", [{"commitment": "confirmed"}])
    finally:
        await up.close()
    fork_slot = await fork.call("getSlot")
    if mainnet_slot <= fork_slot:
        return {"synced": False, "fork_slot": fork_slot, "mainnet_slot": mainnet_slot}
    try:
        info = await fork.call("surfnet_timeTravel", [{"absoluteSlotAt": {"slot": mainnet_slot, "timestamp": int(time.time() * 1000)}}])
    except RuntimeError as e:
        if "past slot" in str(e):  # the fork moved ahead of mainnet between the two calls
            return {"synced": False, "fork_slot": fork_slot, "mainnet_slot": mainnet_slot}
        raise
    return {"synced": True, "fork_slot": fork_slot, "mainnet_slot": mainnet_slot, "epoch_info": info}


async def send_and_confirm(rpc: "Rpc", raw: bytes, timeout_s: float = 30.0) -> tuple[str, str | None]:
    """Send and wait for confirmed. Returns (signature, error or None). No retry."""
    import asyncio
    import base64 as _b64

    sig = await rpc.call("sendTransaction", [_b64.b64encode(raw).decode(), {"encoding": "base64", "skipPreflight": False,
                                                                            "preflightCommitment": "confirmed"}])
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout_s
    while loop.time() < end:
        st = (await rpc.call("getSignatureStatuses", [[sig]]))["value"][0]
        if st and st.get("confirmationStatus") in ("confirmed", "finalized"):
            return sig, (str(st["err"]) if st.get("err") else None)
        await asyncio.sleep(0.4)
    return sig, "confirmation timed out"


async def token_balance(rpc: "Rpc", owner: str, mint: str) -> int:
    """Sum of raw balances of all the owner's token accounts (Token or Token-2022) for this mint."""
    res = await rpc.call("getTokenAccountsByOwner", [owner, {"mint": mint}, {"encoding": "jsonParsed", "commitment": "confirmed"}])
    return sum(int(a["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"]) for a in res["value"])
