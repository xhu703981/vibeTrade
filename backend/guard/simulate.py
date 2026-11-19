"""Guard simulation layer: decode the final transaction bytes + surfnet_profileTransaction (no state committed) + balance deltas.

- Trust only the transaction bytes: the guard expands the account list itself and fetches lookup tables (ALTs) from chain, never trusting Jupiter's mapping.
- Surfpool's simulateTransaction gives no pre/post balances, so surfnet_profileTransaction is used;
  it returns the full before/after state of each writable account; deltas are parsed here from raw bytes (no jsonParsed).
- This module does not depend on the LLM / dialogue code.
"""
from __future__ import annotations

import base64
import re
import struct
from dataclasses import dataclass, field
from typing import Any

from solders.address_lookup_table_account import AddressLookupTable
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
TOKEN_PROGRAMS = frozenset({TOKEN_PROGRAM, TOKEN_2022_PROGRAM})
COMPUTE_BUDGET = "ComputeBudget111111111111111111111111111111"
SYSTEM_PROGRAM = "11111111111111111111111111111111"
LAMPORTS_PER_SIGNATURE = 5000
MAX_CU = 1_400_000


class SimulationError(RuntimeError):
    pass


@dataclass(frozen=True)
class DecodedIx:
    index: int
    program_id: str
    accounts: tuple[str, ...]
    data: bytes


@dataclass(frozen=True)
class DecodedTx:
    fee_payer: str
    signers: tuple[str, ...]
    num_required_signatures: int
    account_keys: tuple[str, ...]          # full expanded account list
    writable: tuple[bool, ...]
    instructions: tuple[DecodedIx, ...]
    alt_addresses: tuple[str, ...]
    cu_limit: int | None
    cu_price_micro_lamports: int

    @property
    def fee_lamports_max(self) -> int:
        """Fee cap = signature fee + ceil(CU price x CU limit / 1e6)."""
        limit = self.cu_limit if self.cu_limit is not None else MAX_CU
        prio = -(-self.cu_price_micro_lamports * limit // 1_000_000)
        return LAMPORTS_PER_SIGNATURE * self.num_required_signatures + prio


@dataclass(frozen=True)
class TokenAcct:
    mint: str
    owner: str
    amount: int
    delegate: str | None
    delegated_amount: int
    close_authority: str | None
    state: int
    program: str


@dataclass
class AccountSnap:
    pubkey: str
    pre_lamports: int | None
    post_lamports: int | None
    pre_owner: str | None
    post_owner: str | None
    pre_token: TokenAcct | None
    post_token: TokenAcct | None
    pre_data_len: int | None
    post_data_len: int | None
    change: str


@dataclass
class SimResult:
    tx: DecodedTx
    wallet: str
    success: bool
    error: str | None
    compute_units: int
    logs: list[str]
    snaps: dict[str, AccountSnap]
    sol_pre: int | None
    sol_post: int | None
    token_deltas: dict[str, int]                 # mint -> net change across all of the wallet's token accounts
    user_token_accounts: dict[str, tuple[TokenAcct | None, TokenAcct | None]]
    invoked_programs: list[tuple[int, str]]      # (depth, program) from logs, including inner CPIs
    created_user_token_accounts: int = 0
    # program-owned (non-System) accounts created by this transaction: (pubkey, owner, data length, lamports)
    created_program_accounts: list[tuple[str, str, int, int]] = field(default_factory=list)
    # sum of the rent of those whose lamports exactly equal the rent-exempt minimum (filled by simulate() via chain queries)
    created_rent_lamports: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def sol_delta(self) -> int:
        return (self.sol_post or 0) - (self.sol_pre or 0)


# ---------------------------------------------------------------------------
# transaction decoding
# ---------------------------------------------------------------------------
def _parse_compute_budget(ixs: list[DecodedIx]) -> tuple[int | None, int]:
    limit, price = None, 0
    for ix in ixs:
        if ix.program_id != COMPUTE_BUDGET or not ix.data:
            continue
        tag = ix.data[0]
        if tag == 2 and len(ix.data) >= 5:
            limit = struct.unpack_from("<I", ix.data, 1)[0]
        elif tag == 3 and len(ix.data) >= 9:
            price = struct.unpack_from("<Q", ix.data, 1)[0]
    return limit, price


def decode_transaction(raw: bytes, alt_tables: dict[str, list[str]]) -> DecodedTx:
    """raw: transaction bytes; alt_tables: ALT address -> address list (must come from chain, see fetch_alt_tables)."""
    tx = VersionedTransaction.from_bytes(raw)
    msg = tx.message
    h = msg.header
    static = [str(k) for k in msg.account_keys]
    nsig, ro_signed, ro_unsigned = h.num_required_signatures, h.num_readonly_signed_accounts, h.num_readonly_unsigned_accounts
    writable = [(i < nsig - ro_signed) if i < nsig else (i < len(static) - ro_unsigned) for i in range(len(static))]
    keys = list(static)
    lookups = list(getattr(msg, "address_table_lookups", []) or [])
    alt_w, alt_r, alt_addrs = [], [], []
    for lk in lookups:
        addr = str(lk.account_key)
        alt_addrs.append(addr)
        table = alt_tables.get(addr)
        if table is None:
            raise SimulationError(f"missing ALT {addr}")
        try:
            alt_w += [table[i] for i in bytes(lk.writable_indexes)]
            alt_r += [table[i] for i in bytes(lk.readonly_indexes)]
        except IndexError as e:
            raise SimulationError(f"ALT {addr} index out of range") from e
    keys += alt_w + alt_r
    writable += [True] * len(alt_w) + [False] * len(alt_r)
    ixs = []
    for n, ci in enumerate(msg.instructions):
        ixs.append(DecodedIx(n, keys[ci.program_id_index], tuple(keys[a] for a in bytes(ci.accounts)), bytes(ci.data)))
    limit, price = _parse_compute_budget(ixs)
    return DecodedTx(
        fee_payer=static[0], signers=tuple(static[:nsig]), num_required_signatures=nsig,
        account_keys=tuple(keys), writable=tuple(writable), instructions=tuple(ixs),
        alt_addresses=tuple(alt_addrs), cu_limit=limit, cu_price_micro_lamports=price,
    )


def alt_addresses_of(raw: bytes) -> list[str]:
    msg = VersionedTransaction.from_bytes(raw).message
    return [str(lk.account_key) for lk in (getattr(msg, "address_table_lookups", []) or [])]


async def fetch_alt_tables(rpc, addrs: list[str]) -> dict[str, list[str]]:
    """Fetch and parse ALTs from chain (the main Surfpool instance)."""
    if not addrs:
        return {}
    res = await rpc.call("getMultipleAccounts", [addrs, {"encoding": "base64", "commitment": "confirmed"}])
    out = {}
    for a, acc in zip(addrs, res["value"]):
        if acc is None:
            raise SimulationError(f"ALT {a} does not exist on chain")
        if acc.get("owner") != "AddressLookupTab1e1111111111111111111111111":
            raise SimulationError(f"ALT {a} is not owned by the ALT program")
        out[a] = [str(k) for k in AddressLookupTable.deserialize(base64.b64decode(acc["data"][0])).addresses]
    return out


# ---------------------------------------------------------------------------
# SPL token account parsing (Token and Token-2022 share the first 165-byte layout)
# ---------------------------------------------------------------------------
def parse_token_account(data: bytes, program: str) -> TokenAcct | None:
    if program not in TOKEN_PROGRAMS or len(data) < 165:
        return None
    if program == TOKEN_2022_PROGRAM and len(data) > 165 and data[165] != 2:
        return None  # not an Account type (e.g. a Mint extension)
    mint = str(Pubkey.from_bytes(data[0:32]))
    owner = str(Pubkey.from_bytes(data[32:64]))
    amount = struct.unpack_from("<Q", data, 64)[0]
    deleg_tag = struct.unpack_from("<I", data, 72)[0]
    delegate = str(Pubkey.from_bytes(data[76:108])) if deleg_tag == 1 else None
    state = data[108]
    delegated_amount = struct.unpack_from("<Q", data, 121)[0]
    close_tag = struct.unpack_from("<I", data, 129)[0]
    close_auth = str(Pubkey.from_bytes(data[133:165])) if close_tag == 1 else None
    return TokenAcct(mint, owner, amount, delegate, delegated_amount, close_auth, state, program)


def _ui_account(a: dict | None) -> tuple[int | None, str | None, bytes | None]:
    if not a:
        return None, None, None
    d = a.get("data")
    raw = base64.b64decode(d[0]) if isinstance(d, list) and d and d[1] == "base64" else None
    if raw is None:
        raise SimulationError("account data is not base64 (use encoding=base64)")
    return int(a["lamports"]), a.get("owner"), raw


def parse_account_states(states: dict[str, Any]) -> dict[str, AccountSnap]:
    snaps: dict[str, AccountSnap] = {}
    for pk, st in states.items():
        if st.get("type") != "writable":
            continue
        ch = st.get("accountChange") or {}
        kind = ch.get("type")
        data = ch.get("data")
        if kind == "update":
            pre, post = data
        elif kind == "create":
            pre, post = None, data
        elif kind == "delete":
            pre, post = data, None
        else:  # unchanged
            pre = post = data
        pl, po, pd = _ui_account(pre)
        ql, qo, qd = _ui_account(post)
        snaps[pk] = AccountSnap(
            pubkey=pk, pre_lamports=pl, post_lamports=ql, pre_owner=po, post_owner=qo,
            pre_token=parse_token_account(pd, po) if pd is not None else None,
            post_token=parse_token_account(qd, qo) if qd is not None else None,
            pre_data_len=len(pd) if pd is not None else None, post_data_len=len(qd) if qd is not None else None,
            change=kind or "unknown",
        )
    return snaps


_INVOKE = re.compile(r"^Program (\w+) invoke \[(\d+)\]$")


def invoked_programs(logs: list[str]) -> list[tuple[int, str]]:
    out = []
    for line in logs:
        m = _INVOKE.match(line)
        if m:
            out.append((int(m.group(2)), m.group(1)))
    return out


def summarize(tx: DecodedTx, wallet: str, profile_value: dict) -> SimResult:
    tp = profile_value["transactionProfile"]
    snaps = parse_account_states(tp.get("accountStates") or {})
    logs = tp.get("logMessages") or []
    w = snaps.get(wallet)
    deltas: dict[str, int] = {}
    user_tokens: dict[str, tuple[TokenAcct | None, TokenAcct | None]] = {}
    created = 0
    for pk, s in snaps.items():
        pre_t = s.pre_token if s.pre_token and s.pre_token.owner == wallet else None
        post_t = s.post_token if s.post_token and s.post_token.owner == wallet else None
        # ownership moved away from the wallet: pre owned, post not -> counts as a full decrease
        if s.pre_token and s.pre_token.owner == wallet and s.post_token and s.post_token.owner != wallet:
            user_tokens[pk] = (s.pre_token, s.post_token)
            deltas[s.pre_token.mint] = deltas.get(s.pre_token.mint, 0) - s.pre_token.amount
            continue
        if not pre_t and not post_t:
            continue
        user_tokens[pk] = (pre_t, post_t)
        if pre_t is None and s.pre_lamports is None:
            created += 1
        mint = (post_t or pre_t).mint  # type: ignore[union-attr]
        deltas[mint] = deltas.get(mint, 0) + (post_t.amount if post_t else 0) - (pre_t.amount if pre_t else 0)
    created_prog = [(pk, s.post_owner or "", s.post_data_len or 0, s.post_lamports or 0)
                    for pk, s in snaps.items()
                    if s.change == "create" and s.post_owner and s.post_owner != SYSTEM_PROGRAM and (s.post_data_len or 0) > 0]
    return SimResult(
        created_program_accounts=created_prog,
        tx=tx, wallet=wallet, success=tp.get("errorMessage") is None, error=tp.get("errorMessage"),
        compute_units=int(tp.get("computeUnitsConsumed") or 0), logs=logs, snaps=snaps,
        sol_pre=w.pre_lamports if w else None, sol_post=w.post_lamports if w else None,
        token_deltas=deltas, user_token_accounts=user_tokens, invoked_programs=invoked_programs(logs),
        created_user_token_accounts=created,
    )


async def refresh_accounts(rpc, pubkeys: list[str]) -> None:
    """surfnet_resetAccount: drop the fork's local state; the next access refetches the latest mainnet value."""
    for pk in pubkeys:
        await rpc.call("surfnet_resetAccount", [pk, {"includeOwnedAccounts": False}])


async def simulate(rpc, raw: bytes, wallet: str, refresh: list[str] | None = None) -> SimResult:
    """Full flow: fetch ALTs -> decode -> (optionally) refresh pool accounts -> profile -> summarise."""
    tables = await fetch_alt_tables(rpc, alt_addresses_of(raw))
    tx = decode_transaction(raw, tables)
    if refresh:
        await refresh_accounts(rpc, refresh)
    res = await rpc.call("surfnet_profileTransaction", [base64.b64encode(raw).decode(), None, {"encoding": "base64"}])
    sim = summarize(tx, wallet, res["value"])
    await fill_created_rent(rpc, sim)
    return sim


MAX_RENT_ACCOUNTS = 4


async def fill_created_rent(rpc, sim: SimResult) -> None:
    """Count as rent only new program accounts whose lamports exactly equal the rent-exempt minimum (at most MAX_RENT_ACCOUNTS).
    Transfers to new wallets (System accounts, no data) or extra lamports are not rent and get blocked by SOL_OUTFLOW."""
    cache: dict[int, int] = {}
    total, n = 0, 0
    for _pk, _owner, size, lamports in sim.created_program_accounts:
        if size not in cache:
            cache[size] = int(await rpc.call("getMinimumBalanceForRentExemption", [size]))
        if lamports == cache[size] and n < MAX_RENT_ACCOUNTS:
            total += lamports
            n += 1
    sim.created_rent_lamports = total


def route_pool_accounts(tx: DecodedTx, wallet: str, user_token_accounts: set[str]) -> list[str]:
    """Writable accounts in the route that do not belong to the user (pools, vaults, ...), refreshed via resetAccount."""
    skip = {wallet, *user_token_accounts, "Sysvar1nstructions1111111111111111111111111"}
    return [k for k, w in zip(tx.account_keys, tx.writable) if w and k not in skip]


async def wallet_token_deltas(rpc, raw: bytes, wallet: str, mints: list[str]) -> tuple[bool, str | None, dict[str, int]]:
    """Lightweight pricing simulation: plain simulateTransaction with post-state of the wallet's token accounts.

    Why not surfnet_profileTransaction here: on Surfpool 1.7 every profile call keeps several MB on the fork
    (measured ~3-14 MB per call even with --max-profiles 16), and the position listener prices every few seconds,
    which grew the fork to ~15 GB over a long session. simulateTransaction keeps nothing. Only for read-only
    pricing; the guard still profiles every transaction it judges.
    Returns (success, error, {mint: net change of the wallet's balance}); a token account the tx would create counts
    from 0 (the canonical ATA is always watched)."""
    from solders.pubkey import Pubkey as _Pk
    from backend.solana_utils import ata
    addrs: list[str] = []
    pre: dict[str, int] = {}
    owner_mint: dict[str, str] = {}
    for m in mints:
        res = await rpc.call("getTokenAccountsByOwner", [wallet, {"mint": m}, {"encoding": "base64", "commitment": "confirmed"}])
        for it in res["value"]:
            d = base64.b64decode(it["account"]["data"][0])
            pre[it["pubkey"]] = struct.unpack_from("<Q", d, 64)[0]
            owner_mint[it["pubkey"]] = m
        canon = str(ata(_Pk.from_string(wallet), _Pk.from_string(m)))
        owner_mint.setdefault(canon, m)
    addrs = list(owner_mint)
    res = await rpc.call("simulateTransaction", [base64.b64encode(raw).decode(), {
        "encoding": "base64", "sigVerify": False, "replaceRecentBlockhash": True, "commitment": "confirmed",
        "accounts": {"addresses": addrs, "encoding": "base64"}}])
    v = res["value"]
    if v.get("err"):
        return False, str(v["err"])[:200], {}
    deltas = {m: 0 for m in mints}
    for a, acc in zip(addrs, v.get("accounts") or []):
        post = 0
        if acc is not None:
            d = base64.b64decode(acc["data"][0])
            post = struct.unpack_from("<Q", d, 64)[0] if len(d) >= 72 else 0
        deltas[owner_mint[a]] += post - pre.get(a, 0)
    return True, None, deltas
