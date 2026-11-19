"""Send signed transactions, confirm and record them. Stop on failure; never retry.

Default (fork mode): only a local RPC (127.0.0.1 / localhost) is allowed, so nothing ever reaches mainnet.
Mainnet mode (VT_NETWORK=mainnet, explicit opt-in): only the configured mainnet RPC is allowed and a local RPC is
refused, so a mainnet session can never silently trade on a fork (or the other way round). Every mainnet record
carries its signature and a Solscan link.
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from backend.config import explorer_tx_url, is_mainnet, mainnet_rpc_url
from backend.solana_utils import send_and_confirm


class NotForkError(RuntimeError):
    pass


def is_local(rpc) -> bool:
    return urlparse(rpc.url).hostname in ("127.0.0.1", "localhost")


def assert_local(rpc) -> None:
    if not is_local(rpc):
        raise NotForkError("refusing to send: transactions may only go to the local Surfpool fork")


def assert_send_allowed(rpc) -> None:
    """Fork mode: local fork only. Mainnet mode: exactly the configured mainnet RPC, never a local one."""
    if not is_mainnet():
        assert_local(rpc)
        return
    if is_local(rpc) or rpc.url != mainnet_rpc_url():
        raise NotForkError("refusing to send: mainnet mode only sends to the configured mainnet RPC")


@dataclass
class TradeRecord:
    ts: float
    kind: str                 # buy | sell
    trigger: str              # card | take_profit | stop_loss | trailing_stop | max_hold | liquidity_drop | emergency
    mint: str
    signature: str | None
    error: str | None
    amount_in: int
    expected_min_out: int
    received: int | None = None
    spent: int | None = None
    meta: dict = field(default_factory=dict)
    network: str = "fork"
    explorer_url: str | None = None   # https://solscan.io/tx/<sig> on mainnet; None on the fork


class Executor:
    def __init__(self, rpc, log_path: Path | None = None):
        assert_send_allowed(rpc)
        self.rpc = rpc
        self.log_path = log_path
        self.records: list[TradeRecord] = []

    async def send(self, raw: bytes, kind: str, trigger: str, mint: str, amount_in: int, min_out: int,
                   meta: dict | None = None) -> TradeRecord:
        assert_send_allowed(self.rpc)
        mainnet = is_mainnet()
        try:
            # mainnet: wait until the blockhash can no longer land (about 60-90 s) instead of the fork's 30 s
            sig, err = await send_and_confirm(self.rpc, raw, timeout_s=90.0 if mainnet else 30.0)
        except Exception as e:  # noqa: BLE001  the send itself failed: no retry
            sig, err = None, f"send failed: {type(e).__name__}: {str(e)[:150]}"
        rec = TradeRecord(time.time(), kind, trigger, mint, sig, err, amount_in, min_out, meta=meta or {},
                          network="mainnet" if mainnet else "fork", explorer_url=explorer_tx_url(sig))
        if mainnet:
            # signature + link only (never keys or the RPC URL)
            print(f"[mainnet] {kind} {trigger} {mint[:8]}... sig={sig} {'ERROR ' + err[:80] if err else 'confirmed'} "
                  f"{rec.explorer_url or ''}", flush=True)
        self.records.append(rec)
        self.save()
        return rec

    def save(self) -> None:
        if self.log_path:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            self.log_path.write_text(json.dumps([asdict(r) for r in self.records], ensure_ascii=False, indent=2), encoding="utf-8")
