"""M4: buy research data (x402, real money, Base USDC) for M3 survivors and give the final pass/fail.

Usage:
  python scripts/m4_run.py                          # latest runs/m3_*.json, real budget (mandate research_usdc)
  python scripts/m4_run.py --rerun-m3               # rerun M3 first
  python scripts/m4_run.py --budget 0.02 --no-cache --approve none   # artificial small budget: triggers a top-up request and stops (no spending)
Spend list (real): runs/research_spend_<mandate_id>.json; --budget uses a separate test ledger.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.config import redact, settings  # noqa: E402
from backend.judge.jev import JEVClient  # noqa: E402
from backend.research import cache  # noqa: E402
from backend.research.budget import ATOMIC, Ledger  # noqa: E402
from backend.research.pipeline import run_research  # noqa: E402
from backend.research.sellers import BASE_USDC, basescan_tx  # noqa: E402
from backend.research.x402_client import ResearchPayer, payer_address, usdc_balance  # noqa: E402
from backend.solana_utils import Rpc  # noqa: E402
from scripts.m3_run import smoke_mandate  # noqa: E402

RUNS = Path(__file__).resolve().parent.parent / "runs"


def make_approver(spec: str):
    if spec == "none":
        return None
    if spec == "timeout":
        async def slow(_req):
            await asyncio.sleep(3600)
        return slow
    usd = float(spec)

    async def fixed(req):
        print(f"[simulated user] got a top-up request, approving ${usd:.2f}")
        return usd
    return fixed


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--m3-json")
    ap.add_argument("--rerun-m3", action="store_true")
    ap.add_argument("--budget", type=float, help="artificial research budget (USD), uses a separate test ledger")
    ap.add_argument("--no-cache", action="store_true", help="do not use already-bought cached data")
    ap.add_argument("--approve", default="none", help="simulated answer to top-up requests: none | timeout | <dollars>")
    a = ap.parse_args()

    m = smoke_mandate()
    if a.rerun_m3:
        from backend.discovery.pipeline import run_pipeline
        coins = [asdict(r) for r in await run_pipeline(m)]
    else:
        path = Path(a.m3_json) if a.m3_json else sorted(RUNS.glob("m3_*.json"))[-1]
        print(f"using M3 result {path.name}")
        coins = json.loads(path.read_text(encoding="utf-8"))["coins"]
    survivors = [c for c in coins if c["status"] == "pending_research"]
    print(f"M3 survivors: {len(survivors)}: {[c['symbol_display'] for c in survivors]}")
    if a.no_cache:
        cache.get = lambda *_a, **_k: None  # type: ignore[assignment]

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    if a.budget is not None:
        ledger = Ledger(budget_atomic=round(a.budget * ATOMIC), per_call_cap_atomic=round(m.body.budget.research_per_call_usdc * ATOMIC),
                        path=RUNS / f"research_spend_test_{ts}.json")
    else:
        ledger = Ledger.from_mandate(m, RUNS / f"research_spend_{m.mandate_id}.json")
    events: list[tuple[str, dict]] = []

    def emit(t, d):
        events.append((t, d))
        print(f"[event] {t}: {d.get('speak', '')}")

    payer = ResearchPayer(ledger, emit=emit)
    rpc = Rpc(settings.solana_rpc_url)
    bal0 = await usdc_balance(payer_address(), BASE_USDC)
    print(f"research wallet {payer_address()} balance ${bal0 / ATOMIC:.4f}; budget ${ledger.budget_atomic / ATOMIC:.2f}, "
          f"spent ${ledger.spent_atomic / ATOMIC:.2f}, per-call cap ${ledger.per_call_cap_atomic / ATOMIC:.2f}")
    try:
        res = await run_research(survivors, m, rpc, payer, ledger, JEVClient(), make_approver(a.approve), emit)
    finally:
        await rpc.close()
        await payer.close()
    bal1 = await usdc_balance(payer_address(), BASE_USDC)

    print("\n== results ==")
    for r in res:
        f = r.facts or {}
        nums = (f"top10={f.get('top10_pct') and round(f['top10_pct'], 1)}% dev={f.get('dev_pct') and round(f['dev_pct'], 2)}% "
                f"holders={f.get('holders')} rugger={f.get('known_rugger')}")
        print(redact(f"{r.mint[:10]}.. {r.symbol_display:8} {r.status:17} {nums}  {'; '.join(r.reasons)[:160]}"))
    print("\n== spend list (this ledger) ==")
    for e in ledger.entries:
        print(f"{e.seller:16} ${e.amount_usdc:.2f} {e.status:8} verified={e.verified_onchain} {e.coin_mint[:8]}… "
              f"{basescan_tx(e.tx_hash) if e.tx_hash else '-'}  {e.reason}")
    print(f"ledger spent ${ledger.spent_atomic / ATOMIC:.2f} / budget ${ledger.budget_atomic / ATOMIC:.2f}; top-ups {[t.outcome for t in ledger.topups]}")
    print(f"wallet balance ${bal0 / ATOMIC:.4f} -> ${bal1 / ATOMIC:.4f} (on-chain spend this run ${(bal0 - bal1) / ATOMIC:.4f})")
    out = RUNS / f"m4_{ts}.json"
    out.write_text(redact(json.dumps({"mandate_id": m.mandate_id, "results": [asdict(r) for r in res], "ledger": ledger.as_dict(),
                                      "events": [{"type": t, **{k: v for k, v in d.items()}} for t, d in events],
                                      "wallet_before": bal0, "wallet_after": bal1}, ensure_ascii=False, indent=2)), encoding="utf-8")
    print(f"saved {out}")


if __name__ == "__main__":
    asyncio.run(main())
