"""Best-effort, read-only evidence from a single Guard execution. Never replays a transaction.

Only fixed role labels/numbers and the already-safe JEV state are kept. No raw profile, addresses,
transaction body, token metadata or exception text is stored. The prefix is signed tx base64 (60 chars).
Context-local tracing isolates concurrent runs; bounded snapshots are frozen on completion.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import time
import uuid
from collections import OrderedDict
from contextvars import ContextVar

_CURRENT: ContextVar[dict | None] = ContextVar("guard_evidence", default=None)
_RECORDS: OrderedDict[str, dict] = OrderedDict()
MAX_RECORDS = 512


def observe(fn, *args, **kwargs):
    """Instrumentation must never change a Guard decision, even if capture/storage fails."""
    try:
        return fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 - observational, never trading logic
        try:
            run = _CURRENT.get()
            if run is not None:
                run["errors"].append({"stage": "capture_" + fn.__name__, "error": type(exc).__name__})
        except Exception:  # noqa: BLE001
            pass
        return None


def begin(raw: bytes):
    run = {"guard_run_id": uuid.uuid4().hex, "captured_at": time.time(), "status": "capturing",
           "transaction_sha256": hashlib.sha256(raw).hexdigest(), "tx_bytes_len": len(raw),
           "tx_base64_prefix": base64.b64encode(raw).decode("ascii")[:60],
           "decoded": None, "lookup_tables": None, "simulation": None, "rules": [],
           "structured_view": None, "structured_json": None, "structured_status": "unavailable",
           "jev": {"status": "unavailable", "risk": None}, "verdict": None, "errors": []}
    return run, _CURRENT.set(run)


def finish(handle, verdict):
    if handle is None:
        return None
    run, token = handle
    try:
        run["verdict"] = verdict.decision
        run["status"] = "complete"
        # A run id is published only after this immutable snapshot exists.
        _RECORDS[run["guard_run_id"]] = copy.deepcopy(run)
        while len(_RECORDS) > MAX_RECORDS:
            _RECORDS.popitem(last=False)
        return run["guard_run_id"]
    finally:
        _CURRENT.reset(token)


def abandon(handle):
    if handle is not None:
        _CURRENT.reset(handle[1])


def get(run_id: str) -> dict | None:
    record = _RECORDS.get(run_id)
    return copy.deepcopy(record) if record is not None else None


def bind(run_id: str | None, card_id: str, code: str, stage: str):
    if run_id in _RECORDS:
        # Binding provenance does not overwrite the immutable transaction evidence.
        _RECORDS[run_id] = {**_RECORDS[run_id], "card_id": card_id, "card_code": code, "stage": stage}


def error(stage: str, exc):
    run = _CURRENT.get()
    if run is not None:
        run["errors"].append({"stage": stage, "error": type(exc).__name__})


def check(name: str, blocked: bool) -> bool:
    """Called at each actually evaluated predicate, not inferred from missing findings."""
    def add():
        run = _CURRENT.get()
        if run is not None:
            run["rules"].append({"name": name, "result": "block" if blocked else "pass"})
    observe(add)
    return blocked


def instruction_result(index: int, blocked: bool):
    run = _CURRENT.get()
    if run is not None and run.get("decoded") is not None:
        for row in run["decoded"]:
            if row["index"] == index:
                row["allowed"] = not blocked


def decoded(raw: bytes, tx):
    from solders.transaction import VersionedTransaction
    from backend.guard.rules import ATA_PROGRAM, JUPITER_V6, TOKEN_IX
    from backend.guard.semantic import _PROGRAM_NAMES
    from backend.guard.simulate import COMPUTE_BUDGET, SYSTEM_PROGRAM, TOKEN_PROGRAMS
    run = _CURRENT.get()
    if run is None:
        return
    rows = []
    for ix in tx.instructions:
        tag = ix.data[0] if ix.data else None
        if ix.program_id == COMPUTE_BUDGET:
            name = {1: "RequestHeapFrame", 2: "SetComputeUnitLimit", 3: "SetComputeUnitPrice", 4: "SetLoadedAccountsDataSizeLimit"}.get(tag, "Unknown compute instruction")
        elif ix.program_id in TOKEN_PROGRAMS:
            name = TOKEN_IX.get(tag, "Unknown token instruction")
        elif ix.program_id == ATA_PROGRAM:
            name = {0: "Create", 1: "CreateIdempotent"}.get(tag or 0, "Unknown ATA instruction")
        elif ix.program_id == SYSTEM_PROGRAM:
            name = "Transfer" if int.from_bytes(ix.data[:4], "little") == 2 else "Other system instruction"
        elif ix.program_id == JUPITER_V6:
            # Do not guess a route variant from the program id; show the actual discriminator.
            name = "Aggregator instruction " + ix.data[:8].hex()
        else:
            name = "Unlisted instruction"
        rows.append({"index": ix.index, "program": _PROGRAM_NAMES.get(ix.program_id, "Unlisted program"),
                     "instruction": name, "allowed": None})
    run["decoded"] = rows
    lookups = list(getattr(VersionedTransaction.from_bytes(raw).message, "address_table_lookups", []) or [])
    run["lookup_tables"] = {"tables": len(lookups),
                            "accounts_resolved": sum(len(l.writable_indexes) + len(l.readonly_indexes) for l in lookups),
                            "source": "ALT indexes resolved by Guard decode from on-chain lookup tables"}


def simulation(sim, intent):
    from backend.config import USDC_MINT
    run = _CURRENT.get()
    if run is None:
        return
    rows = []
    target = intent.output_mint if intent.output_mint != USDC_MINT else intent.input_mint
    for label, mint in (("USDC", USDC_MINT), ("target token", target)):
        snaps = [s for s in sim.snaps.values() if any(t is not None and t.mint == mint and t.owner == sim.wallet
                                                     for t in (s.pre_token, s.post_token))]
        # Missing pre/post is 0 only when the profile explicitly says created/deleted. Otherwise unavailable.
        def amount(s, before):
            t = s.pre_token if before else s.post_token
            if t is not None:
                return t.amount if t.owner == sim.wallet and t.mint == mint else 0
            if (before and s.change == "create") or (not before and s.change == "delete"):
                return 0
            return None
        pre = [amount(s, True) for s in snaps]
        post = [amount(s, False) for s in snaps]
        a = sum(pre) if pre and all(v is not None for v in pre) else None
        b = sum(post) if post and all(v is not None for v in post) else None
        rows.append({"asset": label, "unit": "base units", "before": a, "after": b,
                     "delta": b - a if a is not None and b is not None else None,
                     "profiled_accounts": len(snaps)})
    rows.append({"asset": "SOL", "unit": "lamports", "before": sim.sol_pre, "after": sim.sol_post,
                 "delta": sim.sol_post - sim.sol_pre if sim.sol_pre is not None and sim.sol_post is not None else None})
    run["simulation"] = {"source": getattr(sim, "source", "surfnet_profileTransaction"), "scope": "profiled wallet accounts, not total portfolio",
                         "success": sim.success, "error": "simulation failed" if sim.error else None, "balances": rows}


def sent(state: str, question: dict, threshold: float):
    run = _CURRENT.get()
    if run is not None:
        # This is the very same safe string passed to ask(), not a second build_state call.
        run["structured_json"] = state
        run["structured_view"] = json.loads(state)
        run["structured_status"] = "sent_to_jev"
        run["jev"] = {"question": copy.deepcopy(question), "risk": None, "threshold": threshold, "status": "asked"}


def risk(value: float):
    run = _CURRENT.get()
    if run is not None:
        run["jev"]["risk"] = value
        run["jev"]["status"] = "answered"


def not_asked(reason: str):
    run = _CURRENT.get()
    if run is not None:
        run["jev"] = {"not_asked": reason}
        run["structured_status"] = "not_sent"
