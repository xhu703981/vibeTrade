"""Desk-order extraction: spoken forms, LLM fallback, agent-arg mismatch ignored, fixture-desk probe."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from backend.events import EventBus
from backend.mandate.dialogue import compute_hash
from backend.mandate.schema import Mandate, MandateBody
from backend.orchestrator import Orchestrator
from backend.tests.test_m6_unit import FakeEngine, MINT, POOL1
from backend.voice import desk_order as desk_mod
from backend.voice.desk_order import _orders_from_llm, resolve_desk_order


def _tiny_mandate(total=0.05, per_trade=0.01) -> Mandate:
    d = MandateBody().model_dump(mode="json")
    d["budget"].update(total_usdc=total, per_trade_usdc=per_trade)
    d["exec"]["max_fee_sol"] = 0.002
    d["exit"].update(stop_loss_pct=30, take_profit_ladder=[{"multiple": 2, "sell_pct": 50}])
    d["filters"]["market_cap_usd"] = [100_000, 50_000_000]
    body = MandateBody.model_validate(d)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    fa = now.isoformat().replace("+00:00", "Z")
    ea = (now + timedelta(hours=24)).isoformat().replace("+00:00", "Z")
    mid = "m_orderfix_probe"
    return Mandate(mandate_id=mid, frozen_at=fa, expires_at=ea, body=body,
                   hash=compute_hash(mid, fa, ea, body.model_dump(mode="json")))


def _fixture_orch(total=0.05, per_trade=0.01) -> tuple[Orchestrator, FakeEngine]:
    bus = EventBus()
    m = _tiny_mandate(total, per_trade)
    engine = FakeEngine(bus, m)
    orch = Orchestrator(bus, discovery="replay", research="cache", ui_buttons=True,
                        engine_factory=lambda _m: engine, discover_fn=lambda _m: None,
                        research_fn=None, payer_factory=lambda led: None, jev_factory=lambda: None)
    orch.mandate = m
    orch.engine = engine
    orch.phase = "selecting"
    orch.round_no = 1
    orch.offers = [{"id": "r1c1", "number": 1, "round": 1, "mint": MINT, "symbol": "TEST", "pool": POOL1,
                    "dex": "pumpswap", "paid_promo": False, "metrics": {}, "status": "open", "card_ids": [],
                    "market_live": False, "market_at": None}]
    return orch, engine


def _reset_desk(orch: Orchestrator, engine: FakeEngine) -> None:
    engine.cards.clear()
    engine.created.clear()
    engine.spent_usdc = 0.0
    orch._building.clear()
    for o in orch.offers:
        o["status"] = "open"
        o["card_ids"] = []
        o.pop("building", None)


@pytest.mark.parametrize("utt", [
    "buy coin one for 0.01 dollars",
    "coin 1, one cent",
    "buy the first one with a penny",
    "I said zero point zero one dollars for coin one.",
    "Coin one, zero point zero one dollars.",
])
@pytest.mark.asyncio
async def test_resolve_spoken_orders_without_llm(utt, monkeypatch):
    async def boom(*_a, **_k):
        raise AssertionError("LLM should not be called when the deterministic parse works")
    monkeypatch.setattr(desk_mod, "extract_desk_order_llm", boom)
    got, why = await resolve_desk_order(utt, [1, 2, 3], 0.01)
    assert why is None and got == {1: 0.01}, (utt, got, why)


@pytest.mark.asyncio
async def test_sure_alone_does_not_invent_an_order(monkeypatch):
    async def boom(*_a, **_k):
        raise AssertionError("bare 'sure' must not go to the LLM")
    monkeypatch.setattr(desk_mod, "extract_desk_order_llm", boom)
    got, why = await resolve_desk_order("Uh, sure.", [1], 0.01)
    assert got is None and why


@pytest.mark.asyncio
async def test_llm_fallback_when_deterministic_parse_fails(monkeypatch):
    async def fake(utt, open_numbers, cap, fallback_why=None):
        assert "coin one" in utt and "dollars" in utt
        return {1: 0.01}, None
    monkeypatch.setattr(desk_mod, "extract_desk_order_llm", fake)
    got, why = await resolve_desk_order("please put the unusual wording dollars on coin one", [1], 0.01)
    assert why is None and got == {1: 0.01}


def test_llm_json_is_validated():
    assert _orders_from_llm({"orders": [{"coin_number": 1, "amount_usdc": 0.01}]}, None) == ({1: 0.01}, None)
    got, why = _orders_from_llm({"orders": [], "reason": "no amount said"}, "fallback")
    assert got is None and why == "no amount said"
    got, why = _orders_from_llm({"orders": [{"coin_number": 1, "amount_usdc": 0}]}, "fallback")
    assert got is None and why == "fallback"


@pytest.mark.asyncio
async def test_fixture_desk_creates_cards_for_spoken_orders():
    orch, engine = _fixture_orch()
    phrases = (
        "buy coin one for 0.01 dollars",
        "coin 1, one cent",
        "buy the first one with a penny",
    )
    for utt in phrases:
        _reset_desk(orch, engine)
        # agent args deliberately wrong (the live bug): should be ignored
        r = await orch.place_orders({1: 10}, utt, "voice", wait=True)
        assert r["ok"] and r.get("cards"), (utt, r)
        assert r["cards"][0]["amount_usdc"] == 0.01, (utt, r)
        assert r["cards"][0]["code"]
    # over the per-trade cap is still a single clear reason
    _reset_desk(orch, engine)
    r = await orch.place_orders(None, "coin 1, 5 dollars", "voice", wait=True)
    assert r["ok"] is False and "per-trade cap" in r["reason"]
    # card approval still requires the code
    _reset_desk(orch, engine)
    r = await orch.place_orders(None, "buy coin one for 0.01 dollars", "voice", wait=True)
    code = r["cards"][0]["code"]
    card_id = next(iter(engine.cards))
    bad = await engine.respond(card_id, "approve", utterance="approve", channel="voice", code=None)
    assert bad["ok"] is False
    good = await engine.respond(card_id, "approve", utterance=f"approve {code}", channel="voice", code=code)
    assert good["ok"] is True
