"""Short text-mode probe: fixture desk, 5-cent budget / 1-cent cap, spoken orders create a card.

  python scripts/order_probe.py
No live research / x402 spend. Uses the same FakeEngine path as the unit tests.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.tests.test_desk_order_extract import _fixture_orch, _reset_desk  # noqa: E402


PHRASES = (
    "buy coin one for 0.01 dollars",
    "coin 1, one cent",
    "buy the first one with a penny",
)


async def main() -> int:
    orch, engine = _fixture_orch(total=0.05, per_trade=0.01)
    cap, left = orch.buy_cap_usdc(), orch.remaining_usdc()
    print(f"fixture desk ready: coin 1 open, cap={cap:g} dollars, {left:g} left (no research spend)")
    ok = True
    for utt in PHRASES:
        _reset_desk(orch, engine)
        r = await orch.place_orders({1: 10}, utt, "voice", wait=True)  # wrong agent args on purpose
        cards = r.get("cards") or []
        if r.get("ok") and cards and abs(cards[0]["amount_usdc"] - 0.01) < 1e-9 and cards[0].get("code"):
            print(f"OK  {utt!r} -> card {cards[0]['code']} for {cards[0]['amount_usdc']:g} dollars")
        else:
            print(f"FAIL {utt!r} -> {r}")
            ok = False
    if ok:
        print("PROBE OK")
        return 0
    print("PROBE FAIL")
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
