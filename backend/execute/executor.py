"""Send signed transactions to the Surfpool fork, confirm and record them. Stop on failure; never retry.

Only a local RPC (127.0.0.1 / localhost) is allowed: this project never sends transactions to mainnet.
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from backend.solana_utils import send_and_confirm


class NotForkError(RuntimeError):
    pass


def assert_local(rpc) -> None:
    host = urlparse(rpc.url).hostname
    if host not in ("127.0.0.1", "localhost"):
        raise NotForkError("refusing to send: transactions may only go to the local Surfpool fork")


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


class Executor:
    def __init__(self, rpc, log_path: Path | None = None):
        assert_local(rpc)
        self.rpc = rpc
        self.log_path = log_path
        self.records: list[TradeRecord] = []

    async def send(self, raw: bytes, kind: str, trigger: str, mint: str, amount_in: int, min_out: int,
                   meta: dict | None = None) -> TradeRecord:
        assert_local(self.rpc)
        try:
            sig, err = await send_and_confirm(self.rpc, raw)
        except Exception as e:  # noqa: BLE001  the send itself failed: no retry
            sig, err = None, f"send failed: {type(e).__name__}: {str(e)[:150]}"
        rec = TradeRecord(time.time(), kind, trigger, mint, sig, err, amount_in, min_out, meta=meta or {})
        self.records.append(rec)
        self.save()
        return rec

    def save(self) -> None:
        if self.log_path:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            self.log_path.write_text(json.dumps([asdict(r) for r in self.records], ensure_ascii=False, indent=2), encoding="utf-8")
