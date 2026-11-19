"""Preset skip-setup, 'both 1.5%' exits, and funds that must not be cleared."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.mandate.dialogue import MandateSession, is_preset_accept
from backend.mandate.preset import load_preset
from backend.mandate.schema import MandateBody
from backend.market.ohlcv import apply_live_price
from backend.tests.test_mandate import F, FUNDS, FUNDS_OUT, LOOKS_GOOD, fee_session, session


BOTH = "I want both of them to be 1.5%."
BOTH_PCT = "both of them 1.5 percent"
# extractor wrongly maps the percent onto the two budget fields (the live bug)
BOTH_AS_BUDGET = {"fields": [
    F("budget.total_usdc", 1.5, "1.5%"),
    F("budget.per_trade_usdc", 1.5, "1.5%"),
], "other_requests": []}


@pytest.mark.parametrize("utt", [
    "no, looks good",
    "looks good",
    "go ahead",
    "No.",
    "sure",
    "Yeah, sure. You look good to me.",
])
def test_preset_accept_phrases(utt):
    assert is_preset_accept(utt)


def test_preset_accept_rejects_named_changes():
    assert not is_preset_accept("I want both of them to be 1.5%.")
    assert not is_preset_accept("make the cap two cents")


def test_load_preset_values():
    body = MandateBody.model_validate(load_preset())
    assert body.budget.total_usdc == 5
    assert body.budget.per_trade_usdc == 1
    assert body.budget.daily_max_loss_usdc == 2   # the three opening limits
    assert body.exec.max_fee_sol == 0.002
    assert body.exit.stop_loss_pct is None        # per trade from volatility
    assert body.exit.take_profit_ladder == ()     # no automatic take profit; de-risking is offered instead
    assert body.filters.require_lp_burned_or_locked is True


@pytest.mark.asyncio
async def test_both_1_5_percent_sets_sl_and_tp(monkeypatch):
    s = fee_session({FUNDS: FUNDS_OUT, "looks good": LOOKS_GOOD, BOTH: BOTH_AS_BUDGET, BOTH_PCT: BOTH_AS_BUDGET})
    await s.submit(FUNDS, 1)
    await s.submit("looks good", 2)
    assert s.draft["budget"]["total_usdc"] == 50
    r = await s.submit(BOTH, 2)
    assert r.stage == "ready" and not r.missing_required, (r.stage, r.missing_required, r.speak, r.outcomes)
    assert s.draft["budget"]["total_usdc"] == 50 and s.draft["budget"]["per_trade_usdc"] == 10
    assert s.draft["exit"]["stop_loss_pct"] == 1.5
    assert s.draft["exit"]["take_profit_ladder"] == [{"multiple": 1.015, "sell_pct": 100.0}]
    r2 = await s.submit(BOTH_PCT, 2)  # already set; should stay
    assert s.draft["exit"]["stop_loss_pct"] == 1.5


@pytest.mark.asyncio
async def test_unclear_budget_extract_does_not_clear_funds():
    """Fields the user did not mention must stay; a bad extract must not blank 5 cents / 1 cent."""
    utt = "I want both of them to be 1.5%."
    s = fee_session({FUNDS: FUNDS_OUT, "looks good": LOOKS_GOOD, utt: {
        "fields": [F("budget.total_usdc", None, "both of them"), F("budget.per_trade_usdc", None, "both of them")],
        "other_requests": [],
    }})
    await s.submit(FUNDS, 1)
    await s.submit("looks good", 2)
    r = await s.submit(utt, 2)
    assert s.draft["budget"]["total_usdc"] == 50
    assert s.draft["budget"]["per_trade_usdc"] == 10
    # both-exits rewrite should still set SL/TP even when the extractor missed them
    assert s.draft["exit"]["stop_loss_pct"] == 1.5
    assert not r.missing_required


def test_prepare_then_looks_good_freezes(monkeypatch):
    import backend.app as appmod
    started = []

    class FakeOrch:
        def reset(self):  # noqa: ANN001
            return None
        async def close(self):
            return None
        def start(self, m):
            started.append(m.mandate_id)

    old = (appmod.app.state.session, appmod.app.state.orch, appmod.app.state.shadow, appmod.app.state.engine)
    appmod.app.state.orch = FakeOrch()
    try:
        with TestClient(appmod.app) as c:
            prep = c.post("/mandate/prepare").json()
            assert prep["ok"] and prep["ready"] and prep["recap"]["digest"]
            assert prep["draft"]["budget"]["total_usdc"] == 5
            assert prep["greeting"] == ("Hi, I'm Vibe-Trader. Same as last time: five dollars total, one dollar per trade, "
                                        "stop buying after a two dollar loss today?")
            r = c.post("/mandate/confirm", json={"utterance": "no, looks good", "recap_digest": prep["recap"]["digest"]})
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["frozen"] is True and started
    finally:
        (appmod.app.state.session, appmod.app.state.orch, appmod.app.state.shadow, appmod.app.state.engine) = old


def test_apply_live_price_updates_last_candle():
    candles = [[1_700_000_000, 1.0, 1.2, 0.9, 1.1, 10.0], [1_700_000_900, 1.1, 1.3, 1.0, 1.2, 8.0]]
    now = 1_700_000_900 + 60
    out = apply_live_price(candles, 1.4, now, "15m")
    assert out[-1][4] == 1.4 and out[-1][2] == 1.4
    assert out[-1][3] == 1.0  # low unchanged
    assert len(out) == 2


def test_greeting_follows_saved_settings():
    from backend.mandate.preset import greeting_text, money_spoken
    assert money_spoken(0.05) == "five cents" and money_spoken(0.01) == "one cent" and money_spoken(5) == "five dollars"
    g = greeting_text({"budget": {"total_usdc": 5, "per_trade_usdc": 1, "daily_max_loss_usdc": 2}})
    assert g == ("Hi, I'm Vibe-Trader. Same as last time: five dollars total, one dollar per trade, "
                 "stop buying after a two dollar loss today?")
    g2 = greeting_text({"budget": {"total_usdc": 0.05, "per_trade_usdc": 0.01}})
    assert "five cents total, one cent per trade?" in g2
