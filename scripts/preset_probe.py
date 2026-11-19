"""Text-mode probe: saved preset -> 'no, looks good' starts research; buy coin one for one cent.

Also replays the 09:52 ET transcript's mandate path on a fresh (non-preset) session.

  python scripts/preset_probe.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.events import EventBus  # noqa: E402
from backend.mandate.dialogue import MandateSession  # noqa: E402
from backend.tests.test_desk_order_extract import _fixture_orch, _reset_desk  # noqa: E402
from backend.tests.test_mandate import F, LOOKS_GOOD, fee_session  # noqa: E402
from backend.tests.test_preset_and_exits import BOTH, BOTH_AS_BUDGET  # noqa: E402


async def preset_buy() -> None:
    from fastapi.testclient import TestClient
    import backend.app as appmod

    started = []

    class FakeOrch:
        def reset(self):
            return None
        async def close(self):
            return None
        def start(self, m):
            started.append(m)

    old = (appmod.app.state.session, appmod.app.state.orch, appmod.app.state.shadow, appmod.app.state.engine)
    appmod.app.state.orch = FakeOrch()
    try:
        with TestClient(appmod.app) as c:
            prep = c.post("/mandate/prepare").json()
            assert prep["ok"] and prep["ready"], prep
            r = c.post("/mandate/confirm", json={"utterance": "no, looks good", "recap_digest": prep["recap"]["digest"]}).json()
            assert r.get("frozen") is True and started, r
            print("OK  prepare + 'no, looks good' froze the preset and started research")
    finally:
        (appmod.app.state.session, appmod.app.state.orch, appmod.app.state.shadow, appmod.app.state.engine) = old

    orch, engine = _fixture_orch(total=0.05, per_trade=0.01)
    # use the frozen mandate's caps (already 0.05 / 0.01)
    _reset_desk(orch, engine)
    r = await orch.place_orders(None, "buy coin one for one cent", "voice", wait=True)
    assert r.get("ok") and r.get("cards") and r["cards"][0]["amount_usdc"] == 0.01, r
    print(f"OK  'buy coin one for one cent' -> card {r['cards'][0]['code']} for 0.01 dollars")


async def transcript_non_preset() -> None:
    five = "Hi, I want to put five cents into the session, and then one cent at most per trade."
    five_out = {"fields": [F("budget.total_usdc", 0.05, "five cents into the session"),
                           F("budget.per_trade_usdc", 0.01, "one cent at most per trade")], "other_requests": []}
    s = fee_session({five: five_out, "Yeah, sure. You look good to me.": LOOKS_GOOD,
                     "I want both of them to be 1.5%.": BOTH_AS_BUDGET})
    r1 = await s.submit(five, 1)
    r2 = await s.submit("Yeah, sure. You look good to me.", 2)
    r3 = await s.submit("I want both of them to be 1.5%.", 2)
    assert s.draft["budget"]["total_usdc"] == 0.05 and s.draft["budget"]["per_trade_usdc"] == 0.01, s.draft["budget"]
    assert s.draft["exit"]["stop_loss_pct"] == 1.5, (r3.speak, s.draft["exit"])
    assert s.draft["exit"]["take_profit_ladder"][0]["multiple"] == 1.015
    assert r3.stage == "ready" and "I still need your total budget" not in r3.speak
    print("OK  non-preset path: funds kept, 'both of them to be 1.5%' -> SL 1.5 / TP 1.015")
    print(f"    speak: {r3.speak}")


async def main() -> int:
    await preset_buy()
    await transcript_non_preset()
    print("PROBE OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
