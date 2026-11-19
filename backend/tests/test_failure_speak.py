"""Card/trade failures must produce a loud, calm speak line (no token names)."""
from __future__ import annotations

import time

import pytest

from backend.execute.failure_speak import (
    blocked_buy_speak, blocked_sell_speak, expired_card_speak, reprice_void_speak,
    sell_failed_speak, trade_failed_speak, voice_reason,
)
from backend.tests.test_execute_unit import FakeV, card, eng  # noqa: F401


def test_voice_reason_percent_and_strips_mint():
    mint = "So11111111111111111111111111111111111111112"
    got = voice_reason(f"price impact 2.14% exceeds the cap 2.0% for {mint}")
    assert got == "price impact 2.1 percent is over your 2 percent cap for that token"
    assert mint not in got
    assert "%" not in got


def test_blocked_buy_resim_matches_example():
    s = blocked_buy_speak("68", 1, ["price impact 2.14% exceeds the cap 2.0%"], resim_block=True)
    assert s == ("Card 68 for coin 1 was blocked: price impact 2.1 percent is over your 2 percent cap. "
                 "Nothing was bought. You can try a smaller amount.")
    assert "JUP" not in s and "BONK" not in s


def test_other_failure_lines():
    assert "timed out" in expired_card_speak("42", 3)
    rp = reprice_void_speak("11", None, 6.2, 5.0)
    assert "Card 11 is void" in rp and "6.2 percent" in rp and "5 percent" in rp
    tf = trade_failed_speak("09", 2, "insufficient USDC balance", stage="execute")
    assert "Card 09 for coin 2" in tf and "insufficient" in tf and "Nothing was bought" in tf
    assert "smaller amount" in tf
    q = trade_failed_speak("07", None, "quote expired", stage="requote")
    assert "expired or failed re-quote" in q and "Card 07" in q
    sl = blocked_sell_speak("stop_loss", ["price impact 3.00% exceeds the cap 2.0%"])
    assert sl.startswith("Stop-loss sell was blocked") and "Nothing was sold" in sl
    sf = sell_failed_speak("stop_loss", 3, "send failed: InstructionError", stuck=True)
    assert "Warning" not in sf and "attempt 3" in sf and "stopped" in sf.lower()


@pytest.mark.asyncio
async def test_resim_guard_block_is_loud_with_reason(eng):
    c = await card(eng)
    c.coin_number = 1
    eng.st["verdict"] = FakeV("block", ["price impact 2.14% exceeds the cap 2.0%"])
    r = await eng.respond(c.id, "approve", f"approve {c.code}")
    assert not r["ok"] and c.status == "failed" and eng.st["sends"] == 0
    ev = eng.bus.of("guard_blocked")[-1]
    assert ev.loud
    assert ev.speak == ("Card " + c.code + " for coin 1 was blocked: price impact 2.1 percent is over your 2 percent cap. "
                        "Nothing was bought. You can try a smaller amount.")
    assert ev.panel.get("reasons")[0].startswith("price impact")


@pytest.mark.asyncio
async def test_expired_requote_and_reprice_are_loud(eng):
    c = await card(eng)
    ev = eng.bus.of("card_created")[-1]
    # expire
    c.expires_at = time.time() - 1
    await eng.respond(c.id, "approve", f"approve {c.code}")
    exp = eng.bus.of("card_expired")[-1]
    assert exp.loud and c.code in exp.speak and "timed out" in exp.speak

    c2 = await card(eng)
    eng.st["out"] = 940  # ~6% move, over the 5% cap
    r = await eng.respond(c2.id, "approve", f"approve {c2.code}")
    assert r["status"] == "voided"
    ev = eng.bus.of("card_voided")[-1]
    assert ev.loud and c2.code in ev.speak and "void" in ev.speak and "percent" in ev.speak
