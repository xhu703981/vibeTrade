"""Record a rehearsal round whose coins come from Nansen smart money, with matching research receipts.

1. Smart-money net flow (Nansen via x402): the cached response is reused (free); --fresh-flows buys a new one ($0.05).
2. Free checks on those candidates (DexScreener, on-chain, sell simulation on the local fork) -> runs/m3_<ts>_smartmoney.json
   (rehearsal replays the newest m3 file).
3. Free ranking (same rank_score as the app) -> paid research ONLY for the top N that the desk will show.
   Hard-capped by its own test ledger (--budget, default $0.25). Without --pay it stops after printing the plan.

Usage: PYTHONPATH=. .venv/bin/python scripts/smart_money_record.py [--pay] [--budget 0.25]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.config import redact, settings  # noqa: E402
from backend.copilot.desk import rank_score  # noqa: E402
from backend.discovery.pipeline import run_pipeline  # noqa: E402
from backend.discovery.smart_money import candidates_from, smart_money_flows  # noqa: E402
from backend.judge.jev import JEVClient  # noqa: E402
from backend.mandate.dialogue import compute_hash  # noqa: E402
from backend.mandate.preset import load_preset  # noqa: E402
from backend.mandate.schema import Mandate, MandateBody  # noqa: E402
from backend.orchestrator import CacheOnlyPayer  # noqa: E402
from backend.research.budget import ATOMIC, Ledger  # noqa: E402
from backend.research.pipeline import run_research  # noqa: E402
from backend.research.sellers import BASE_USDC, basescan_tx  # noqa: E402
from backend.research.x402_client import ResearchPayer, payer_address, usdc_balance  # noqa: E402
from backend.solana_utils import Rpc  # noqa: E402

RUNS = Path(__file__).resolve().parent.parent / "runs"


def preset_mandate() -> Mandate:
    body = MandateBody.model_validate(load_preset())
    now = datetime.now(timezone.utc).replace(microsecond=0)
    fa, ea = now.isoformat().replace("+00:00", "Z"), (now + timedelta(hours=24)).isoformat().replace("+00:00", "Z")
    mid = f"m_{now:%Y%m%d}_smrec"
    return Mandate(mandate_id=mid, frozen_at=fa, expires_at=ea, body=body, hash=compute_hash(mid, fa, ea, body.model_dump(mode="json")))


def score(c: dict) -> float:
    return rank_score({**(c.get("metrics") or {}), "roundtrip_loss_pct": (c.get("sell_sim") or {}).get("roundtrip_loss_pct")})


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pay", action="store_true")
    ap.add_argument("--budget", type=float, default=0.25)
    ap.add_argument("--fresh-flows", action="store_true")
    a = ap.parse_args()
    m = preset_mandate()
    lim = m.body.budget.research_max_coins_per_round
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    ledger = Ledger(budget_atomic=round(a.budget * ATOMIC), per_call_cap_atomic=round(m.body.budget.research_per_call_usdc * ATOMIC),
                    path=RUNS / f"research_spend_test_{ts}.json")
    events = []

    def emit(t, d):
        events.append((t, d))
        print(f"[event] {t}: {d.get('speak', '')}")

    flow_payer = ResearchPayer(ledger, emit=emit) if a.fresh_flows else CacheOnlyPayer()
    try:
        flows, info = await smart_money_flows(flow_payer, ttl_s=0 if a.fresh_flows else 10**9)
    finally:
        await flow_payer.close()
    print(f"smart money: {len(flows)} net-buying tokens, {info}")
    if not flows:
        raise SystemExit("no smart-money flows")
    coins = [asdict(r) for r in await run_pipeline(m, log=lambda s: print("  " + s), candidates=candidates_from(flows))]
    for c in coins:
        f = flows.get(c.get("mint"))
        if f:
            c.setdefault("metrics", {}).update({k: v for k, v in f.items() if v is not None})
            c["sources"] = sorted(set(c.get("sources") or []) | {"nansen_smart_money"})
    out = RUNS / f"m3_{ts}_smartmoney.json"
    out.write_text(redact(json.dumps({"mandate_id": m.mandate_id, "mandate_hash": m.hash, "discovery": {**info, "smart_money_tokens": len(flows)},
                                      "coins": coins}, ensure_ascii=False, indent=2)), encoding="utf-8")
    survivors = [c for c in coins if c["status"] == "pending_research"]
    top = sorted(survivors, key=score, reverse=True)[:lim]
    print(f"\nfree checks: {len(survivors)} of {len(coins)} survive; saved {out.name}")
    for c in top:
        mm = c["metrics"]
        print(f"  TOP {c['mint'][:8]}.. score={score(c):.2f} sm_24h=${mm.get('sm_netflow_24h_usd')} traders={mm.get('sm_traders')}")
    print(f"paid research for these {len(top)} only, at most ${a.budget:.2f} (ledger {ledger.path.name})")
    if not a.pay:
        return
    payer = ResearchPayer(ledger, emit=emit)
    rpc = Rpc(settings.solana_rpc_url)
    bal0 = await usdc_balance(payer_address(), BASE_USDC)
    try:
        res = await run_research(top, m, rpc, payer, ledger, JEVClient(), None, emit)
    finally:
        await rpc.close()
        await payer.close()
    bal1 = await usdc_balance(payer_address(), BASE_USDC)
    print("\n== results ==")
    for r in res:
        print(redact(f"{r.mint[:10]}.. {r.status:15} {'; '.join(r.reasons)[:160]}"))
    for e in ledger.entries:
        print(f"{e.seller:18} ${e.amount_usdc:.2f} {e.status:8} {e.coin_mint[:8]}… {basescan_tx(e.tx_hash) if e.tx_hash else '-'}")
    print(f"ledger spent ${ledger.spent_atomic / ATOMIC:.2f} / ${ledger.budget_atomic / ATOMIC:.2f}; "
          f"wallet ${bal0 / ATOMIC:.4f} -> ${bal1 / ATOMIC:.4f}")
    (RUNS / f"m4_{ts}_smartmoney.json").write_text(redact(json.dumps(
        {"results": [asdict(r) for r in res], "ledger": ledger.as_dict(), "wallet_before": bal0, "wallet_after": bal1},
        ensure_ascii=False, indent=2)), encoding="utf-8")


if __name__ == "__main__":
    asyncio.run(main())
