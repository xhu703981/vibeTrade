"""One live Nansen smart-money net-flow call ($0.05, hard-capped by a $0.05 test ledger); caches the response for rehearsal.

Prints only counts, numbers and the payment receipt (no token names, no secrets)."""
import asyncio
import sys
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.discovery.smart_money import smart_money_flows
from backend.research.budget import ATOMIC, Ledger
from backend.research.sellers import BASE_USDC, basescan_tx
from backend.research.x402_client import ResearchPayer, payer_address, usdc_balance

RUNS = Path(__file__).resolve().parents[1] / "runs"


async def main() -> None:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    ledger = Ledger(budget_atomic=round(0.05 * ATOMIC), per_call_cap_atomic=round(0.05 * ATOMIC),
                    path=RUNS / f"research_spend_test_smartmoney_{ts}.json")
    payer = ResearchPayer(ledger, emit=lambda t, d: print(f"[event] {t}: {d.get('speak', '')}"))
    b0 = await usdc_balance(payer_address(), BASE_USDC)
    try:
        flows, info = await smart_money_flows(payer)
    finally:
        await payer.close()
    b1 = await usdc_balance(payer_address(), BASE_USDC)
    print(f"tokens with smart-money net buying (24h): {len(flows)}")
    for m, f in list(flows.items())[:10]:
        print(f"  {m[:8]}.. net 24h ${f['sm_netflow_24h_usd']:,.0f}  traders {f['sm_traders']}")
    print({k: v for k, v in info.items() if k != "tx_hash"}, basescan_tx(info["tx_hash"]) if info.get("tx_hash") else "")
    print(f"wallet ${b0 / ATOMIC:.4f} -> ${b1 / ATOMIC:.4f}; ledger spent ${ledger.spent_atomic / ATOMIC:.2f}")


asyncio.run(main())
