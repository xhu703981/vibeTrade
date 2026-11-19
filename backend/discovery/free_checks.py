"""Free checks: market thresholds (DexScreener numbers) + mint account (RPC) + LP state (RPC).

Principle: missing data always fails; every failure gives a concrete reason.
On-chain data is read as raw bytes from a mainnet RPC and parsed here, without jsonParsed and without trusting DexScreener's conclusions.
"""
from __future__ import annotations

import base64
import struct
import time
from dataclasses import dataclass, field

from solders.pubkey import Pubkey

from backend.discovery.dexscreener import Candidate
from backend.mandate.schema import Mandate

TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
PUMPSWAP_PROGRAM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
RAYDIUM_CPMM_PROGRAM = "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C"
RAYDIUM_LOCK_CPMM_AUTH = "3f7GcQFG397GAaEnv51zR6tsTVihYRydnydDD1cXekxH"  # Raydium Burn & Earn (raydium-sdk-V2 LOCK_CPMM_AUTH)
LP_BURNED_OR_LOCKED_MIN = 0.95  # burned + locked must be at least 95% of LP

# Token-2022 ExtensionType (spl-token-2022)
EXT = {1: "TransferFeeConfig", 3: "MintCloseAuthority", 4: "ConfidentialTransferMint", 6: "DefaultAccountState",
       9: "NonTransferable", 10: "InterestBearingConfig", 12: "PermanentDelegate", 14: "TransferHook",
       16: "ConfidentialTransferFeeConfig", 18: "MetadataPointer", 19: "TokenMetadata", 20: "GroupPointer",
       21: "TokenGroup", 22: "GroupMemberPointer", 23: "TokenGroupMember", 24: "ConfidentialMintBurn",
       25: "ScaledUiAmount", 26: "Pausable"}
# system level: these extensions do not affect transfers/balances and are allowed; all others (incl. unknown) fail
BENIGN_EXT = {3, 10, 18, 19, 20, 21, 22, 23, 25}
SYSTEM_BANNED_EXT = {"ConfidentialTransferMint", "ConfidentialTransferFeeConfig", "ConfidentialMintBurn", "Pausable"}


@dataclass
class MintInfo:
    program: str
    mint_authority: str | None
    freeze_authority: str | None
    supply: int
    decimals: int
    extensions: dict[str, dict] = field(default_factory=dict)
    unknown_extensions: list[int] = field(default_factory=list)


def _coption_pubkey(d: bytes, off: int) -> str | None:
    tag = struct.unpack_from("<I", d, off)[0]
    return str(Pubkey.from_bytes(d[off + 4:off + 36])) if tag == 1 else None


def parse_mint(data: bytes, owner: str) -> MintInfo:
    if owner not in (TOKEN_PROGRAM, TOKEN_2022_PROGRAM):
        raise ValueError("mint is not owned by the Token / Token-2022 program")
    if len(data) < 82:
        raise ValueError("mint data too short")
    mi = MintInfo(program=owner, mint_authority=_coption_pubkey(data, 0), supply=struct.unpack_from("<Q", data, 36)[0],
                  decimals=data[44], freeze_authority=_coption_pubkey(data, 46))
    if data[45] != 1:
        raise ValueError("mint not initialised")
    if owner == TOKEN_2022_PROGRAM and len(data) > 82:
        if len(data) < 166 or data[165] != 1:
            raise ValueError("malformed Token-2022 mint extension area")
        off = 166
        while off + 4 <= len(data):
            etype, elen = struct.unpack_from("<HH", data, off)
            val = data[off + 4: off + 4 + elen]
            off += 4 + elen
            if etype == 0:
                break
            name = EXT.get(etype)
            if name is None:
                mi.unknown_extensions.append(etype)
                continue
            info: dict = {}
            if etype == 1 and len(val) >= 108:
                older = struct.unpack_from("<QQH", val, 72)
                newer = struct.unpack_from("<QQH", val, 90)
                info = {"older_bps": older[2], "newer_bps": newer[2], "max_bps": max(older[2], newer[2])}
            elif etype == 6 and len(val) >= 1:
                info = {"state": val[0]}  # 1 = Initialized, 2 = Frozen
            elif etype == 14 and len(val) >= 64:
                info = {"program_id": str(Pubkey.from_bytes(val[32:64]))}
            elif etype == 12 and len(val) >= 32:
                info = {"delegate": str(Pubkey.from_bytes(val[0:32]))}
            mi.extensions[name] = info
    return mi


def check_mint(mi: MintInfo, m: Mandate) -> list[str]:
    f = m.body.filters
    r = []
    if f.require_mint_revoked and mi.mint_authority is not None:
        r.append("mint authority not revoked (unlimited supply possible)")
    if f.require_freeze_revoked and mi.freeze_authority is not None:
        r.append("freeze authority not revoked (holder accounts can be frozen)")
    banned = set(f.banned_extensions) | SYSTEM_BANNED_EXT
    for name, info in mi.extensions.items():
        if name == "DefaultAccountState":
            if info.get("state") == 2 and "DefaultAccountStateFrozen" in banned:
                r.append("Token-2022 extension DefaultAccountState=Frozen (new accounts frozen by default)")
            continue
        if name == "TransferFeeConfig":
            if info.get("max_bps", 10_000) > f.max_transfer_fee_bps:
                r.append(f"transfer tax {info.get('max_bps')} bps above the cap of {f.max_transfer_fee_bps} bps")
            continue
        if name in banned:
            r.append(f"forbidden Token-2022 extension {name}")
        elif name not in {EXT[i] for i in BENIGN_EXT}:
            r.append(f"Token-2022 extension not allowed: {name}")
    for t in mi.unknown_extensions:
        r.append(f"unknown Token-2022 extension type {t}")
    return r


def check_market(c: Candidate, m: Mandate, now_ms: int | None = None) -> list[str]:
    f = m.body.filters
    p = c.pair
    if p is None:
        return ["no Solana pair with this coin as base"]
    r = []
    now_ms = now_ms or int(time.time() * 1000)
    if p.liquidity_usd is None:
        r.append("missing liquidity data")
    elif p.liquidity_usd < f.min_liquidity_usd:
        r.append(f"liquidity ${p.liquidity_usd:,.0f} below ${f.min_liquidity_usd:,.0f}")
    mc = p.market_cap
    lo, hi = f.market_cap_usd
    if mc is None:
        r.append("missing market cap data")
    elif not (lo <= mc <= hi):
        r.append(f"market cap ${mc:,.0f} outside ${lo:,.0f}-${hi:,.0f}")
    if p.pair_created_at_ms is None:
        r.append("missing pair creation time")
    else:
        age_h = (now_ms - p.pair_created_at_ms) / 3_600_000
        if age_h < f.token_age_hours_min:
            r.append(f"token age {age_h:.1f} hours, under {f.token_age_hours_min:g} hours")
    if p.volume_h24 is None or not p.liquidity_usd:
        r.append("missing volume data")
    else:
        ratio = p.volume_h24 / p.liquidity_usd
        if ratio > f.max_volume_to_liquidity:
            r.append(f"24h volume/liquidity {ratio:.1f} above {f.max_volume_to_liquidity:g} (possible wash trading)")
    return r


def lp_kind(c: Candidate) -> str | None:
    p = c.pair
    if p is None:
        return None
    if p.dex_id == "pumpswap":
        return "pumpswap"
    if p.dex_id == "raydium" and "CPMM" in p.labels:
        return "raydium_cpmm"
    return None


@dataclass
class PoolInfo:
    kind: str
    mints: tuple[str, str]
    lp_mint: str
    pool_lp_supply: int


def parse_pool(kind: str, data: bytes, owner: str) -> PoolInfo:
    P = lambda o: str(Pubkey.from_bytes(data[o:o + 32]))  # noqa: E731
    if kind == "pumpswap":
        if owner != PUMPSWAP_PROGRAM or len(data) < 211:
            raise ValueError("not a PumpSwap pool account")
        return PoolInfo(kind, (P(43), P(75)), P(107), struct.unpack_from("<Q", data, 203)[0])
    if kind == "raydium_cpmm":
        if owner != RAYDIUM_CPMM_PROGRAM or len(data) < 341:
            raise ValueError("not a Raydium CPMM pool account")
        return PoolInfo(kind, (P(168), P(200)), P(136), struct.unpack_from("<Q", data, 333)[0])
    raise ValueError(kind)


def lp_safe_fraction(pool: PoolInfo, lp_mint_supply: int, locked_amount: int) -> tuple[float, float]:
    """Return (burned share, locked share) relative to the LP supply recorded in the pool."""
    if pool.pool_lp_supply <= 0:
        return 0.0, 0.0
    burned = max(0, pool.pool_lp_supply - lp_mint_supply) / pool.pool_lp_supply
    locked = min(locked_amount, lp_mint_supply) / pool.pool_lp_supply
    return burned, locked


async def check_lp(rpc, c: Candidate, m: Mandate) -> tuple[list[str], dict]:
    if not m.body.filters.require_lp_burned_or_locked:
        return [], {}
    kind = lp_kind(c)
    if kind is None:
        dex = c.pair.dex_id if c.pair else "unknown"
        return [f"pair is on {dex}{'/' + ','.join(c.pair.labels) if c.pair and c.pair.labels else ''}: only PumpSwap and Raydium CPMM are accepted"], {}
    acc = (await rpc.call("getAccountInfo", [c.pair.pair_address, {"encoding": "base64"}]))["value"]
    if acc is None:
        return ["pool account does not exist"], {}
    try:
        pool = parse_pool(kind, base64.b64decode(acc["data"][0]), acc["owner"])
    except ValueError as e:
        return [f"could not parse pool account: {e}"], {}
    if c.mint not in pool.mints:
        return ["the pool does not contain this coin (DexScreener data disagrees with chain)"], {}
    supply = int((await rpc.call("getTokenSupply", [pool.lp_mint]))["value"]["amount"])
    locked = 0
    if kind == "raydium_cpmm" and supply > 0:
        largest = (await rpc.call("getTokenLargestAccounts", [pool.lp_mint]))["value"]
        addrs = [x["address"] for x in largest]
        if addrs:
            accs = (await rpc.call("getMultipleAccounts", [addrs, {"encoding": "base64"}]))["value"]
            for x, a in zip(largest, accs):
                if a and len(base64.b64decode(a["data"][0])) >= 64:
                    owner = str(Pubkey.from_bytes(base64.b64decode(a["data"][0])[32:64]))
                    if owner == RAYDIUM_LOCK_CPMM_AUTH:
                        locked += int(x["amount"])
    burned, lk = lp_safe_fraction(pool, supply, locked)
    detail = {"kind": kind, "lp_burned_pct": round(burned * 100, 2), "lp_locked_pct": round(lk * 100, 2)}
    if burned + lk < LP_BURNED_OR_LOCKED_MIN:
        return [f"LP burned {burned * 100:.1f}% + locked {lk * 100:.1f}%, below {LP_BURNED_OR_LOCKED_MIN * 100:.0f}%"], detail
    return [], detail


async def fetch_mints(rpc, mints: list[str]) -> dict[str, tuple[bytes, str] | None]:
    out: dict[str, tuple[bytes, str] | None] = {}
    for i in range(0, len(mints), 100):
        chunk = mints[i:i + 100]
        res = (await rpc.call("getMultipleAccounts", [chunk, {"encoding": "base64"}]))["value"]
        for mnt, a in zip(chunk, res):
            out[mnt] = (base64.b64decode(a["data"][0]), a["owner"]) if a else None
    return out
