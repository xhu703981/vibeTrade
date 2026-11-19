"""M3: run the coin pipeline for a frozen mandate, print the concrete pass/fail reasons for each candidate, and save JSON.

Usage: python scripts/m3_run.py [--max-sell-sims 10]
Mandate: defaults + the M1 smoke required fields (total 50, per trade 10, fee 0.002 SOL, stop loss 30%, sell half at 2x, market cap max 50 million).
"""
from __future__ import annotations

import argparse
import asyncio
import collections
import json
import sys
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.config import redact  # noqa: E402
from backend.discovery.pipeline import run_pipeline  # noqa: E402
from backend.mandate.dialogue import compute_hash  # noqa: E402
from backend.mandate.schema import Mandate, MandateBody  # noqa: E402


def smoke_mandate() -> Mandate:
    d = MandateBody().model_dump(mode="json")
    d["budget"].update(total_usdc=50, per_trade_usdc=10)
    d["exec"]["max_fee_sol"] = 0.002
    d["exit"].update(stop_loss_pct=30, take_profit_ladder=[{"multiple": 2, "sell_pct": 50}])
    d["filters"]["market_cap_usd"] = [100_000, 50_000_000]
    body = MandateBody.model_validate(d)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    fa, ea = now.isoformat().replace("+00:00", "Z"), (now + timedelta(hours=24)).isoformat().replace("+00:00", "Z")
    mid = f"m_{now:%Y%m%d}_m3"
    return Mandate(mandate_id=mid, frozen_at=fa, expires_at=ea, body=body, hash=compute_hash(mid, fa, ea, body.model_dump(mode="json")))


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-sell-sims", type=int, default=10)
    a = ap.parse_args()
    m = smoke_mandate()
    reps = await run_pipeline(m, a.max_sell_sims)
    out = Path(__file__).resolve().parent.parent / "runs"
    out.mkdir(exist_ok=True)
    path = out / f"m3_{datetime.now():%Y%m%d_%H%M%S}.json"
    path.write_text(redact(json.dumps({"mandate_id": m.mandate_id, "mandate_hash": m.hash, "coins": [asdict(r) for r in reps]},
                                      ensure_ascii=False, indent=2)), encoding="utf-8")
    print("\n== results ==")
    print(f"{'mint':12} {'sym':10} {'dex':10} {'status':16} reasons / round-trip loss")
    for r in sorted(reps, key=lambda r: (r.status != "pending_research", r.status, r.failed_stage or "")):
        extra = f"loss {r.sell_sim['roundtrip_loss_pct']}%" if r.status == "pending_research" else "; ".join(r.reasons)[:150]
        print(redact(f"{r.mint[:10]}.. {r.symbol_display:10} {r.metrics.get('dex', '-'):10} {r.status:16} {extra}"))
    print("\n== summary ==")
    print("status:", dict(collections.Counter(r.status for r in reps)))
    print("first failed stage:", dict(collections.Counter(r.failed_stage for r in reps if r.failed_stage)))
    rc = collections.Counter()
    for r in reps:
        for x in r.reasons:
            key = x.split(":")[0] if ":" in x else x
            for tok in ("liquidity", "market cap", "token age", "volume", "mint authority", "freeze authority", "only PumpSwap", "LP burned", "extension", "round-trip loss", "Jupiter", "guard", "sell failed", "buy failed"):
                if tok in x:
                    key = tok
                    break
            rc[key] += 1
    print("top reasons:", rc.most_common(10))
    print(f"\nsaved {path}")


if __name__ == "__main__":
    asyncio.run(main())
