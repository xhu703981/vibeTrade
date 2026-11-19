"""Rehearsal "Test stop loss" / "Test take profit": the test passes only when the rule under test sells.

Root causes fixed here (2026-11-15):
- a stop test that walked the price down past the trailing-stop line (far above the stop after a take-profit test or a
  run-up) sold through the trailing stop; the test still reported success because it accepted any sell;
- with a take-profit rule, the co-pilot's "sell half?" offer on the way up ended the test before the take profit sold;
- "fresh reading" was detected by the fork clock, which can repeat between polls, so an undershoot was not re-nudged;
- with no take-profit rule, a second test could never pass (the offer is made once per position) and the target was a
  bare price move while the offer counts P&L after fees.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace as NS

import pytest

import backend.execute.price_test as pt
from backend.events import EventBus
from backend.execute.listener import Position, decide_exit
from backend.execute.price_source import PriceQuote
from backend.tests.test_exit_baseline_unit import ex

MINT = "So11111111111111111111111111111111111111112"


def _pos(peak=1.0):
    p = Position(MINT, "GATO", "pool", 1000, 1000, 1_000_000, 0.0, entry_liquidity=1_000)
    p.exit_ref_per_token = 1_000_000 / 1000
    p.peak_ratio = peak
    return p


def _q(ratio):
    return PriceQuote(1000, int(1_000_000 * ratio), 1_000, "test", 0)


def test_stop_test_never_sells_through_the_trailing_stop():
    rules = ex(sl=13, tp=3.0, trailing=30)
    p = _pos(peak=1.5)                                                   # after a take-profit test
    assert decide_exit(p, _q(1.0), 1, rules)[0] == "trailing_stop"       # the old outcome on the way down
    p = _pos(peak=1.5)
    p.rehearsal = "stop_loss"
    assert decide_exit(p, _q(1.0), 1, rules) is None                     # on the way down: no other rule fires
    assert decide_exit(p, _q(0.86), 1, rules)[0] == "stop_loss"          # at the line: the stop-loss path
    assert p.mark_seq == 2


def _engine(ex_rules, cards=None):
    return NS(rpc=NS(url="http://127.0.0.1:8899"), m=NS(body=NS(exit=ex_rules)), bus=EventBus(), cards=cards if cards is not None else {})


@pytest.fixture
def fake_chain(monkeypatch):
    monkeypatch.setattr(pt, "assert_local", lambda rpc: None)
    monkeypatch.setattr(pt, "load_or_create_keypair", lambda p: (None, False))


async def test_a_sell_through_another_rule_is_not_a_pass(monkeypatch, fake_chain):
    ex_ = NS(stop_loss_pct=13, take_profit_ladder=(), poll_interval_sec=0.05)
    pos = NS(mint=MINT, pool="POOL", sells=[], status="open", last_ratio=1.2, tp_done=[], price_at=0, mark_seq=0)

    async def move(rpc, kp, pool, mint, factor):
        pos.sells.append({"trigger": "trailing_stop"})
        pos.status = "closed"
        return {}
    monkeypatch.setattr(pt, "move_price", move)
    eng = _engine(ex_)
    r = await pt.run_price_test(eng, pos, "stop_loss")
    assert not r["ok"] and r["outcome"] == "other:trailing_stop"
    assert "not the stop loss" in eng.bus.of("price_test")[-1].speak
    assert pos.rehearsal is None                                         # the gate is lifted afterwards


async def test_undershoot_is_renudged_even_when_the_fork_clock_repeats(monkeypatch, fake_chain):
    ex_ = NS(stop_loss_pct=13, take_profit_ladder=(), poll_interval_sec=5)   # long waits if a fresh mark were missed
    pos = NS(mint=MINT, pool="POOL", sells=[], status="open", last_ratio=1.0, tp_done=[], price_at=100.0, mark_seq=0)
    factors = []

    async def move(rpc, kp, pool, mint, factor):
        factors.append(factor)
        assert pos.rehearsal == "stop_loss"
        if len(factors) == 1:
            pos.last_ratio, pos.mark_seq = 0.93, 1          # undershoot; the fork clock (price_at) did not move
        else:
            pos.sells.append({"trigger": "stop_loss"})
        return {}
    monkeypatch.setattr(pt, "move_price", move)
    r = await asyncio.wait_for(pt.run_price_test(_engine(ex_), pos, "stop_loss"), 3)
    assert r["ok"] and len(factors) == 2 and factors[1] == pytest.approx(0.87 * 0.994 / 0.93)


async def test_take_profit_test_waits_for_the_take_profit_not_the_offer(monkeypatch, fake_chain):
    ex_ = NS(stop_loss_pct=13, take_profit_ladder=(NS(multiple=1.5, sell_pct=100),), poll_interval_sec=0.05)
    cards = {}
    pos = NS(mint=MINT, pool="POOL", sells=[], status="open", last_ratio=1.0, tp_done=[], price_at=0, mark_seq=0)
    n = []

    async def move(rpc, kp, pool, mint, factor):
        n.append(factor)
        if len(n) == 1:      # the co-pilot offers "sell half?" on the way up, and the reading is still short of 1.5
            cards["o"] = NS(offer="Coin 1 is up 30 percent. Want to sell half?", mint=MINT, status="pending")
            pos.last_ratio, pos.mark_seq = 1.3, 1
        else:
            pos.sells.append({"trigger": "take_profit"})
        return {}
    monkeypatch.setattr(pt, "move_price", move)
    assert (await pt.run_price_test(_engine(ex_, cards), pos, "take_profit"))["ok"] and len(n) == 2


async def test_no_ladder_take_profit_aims_at_the_offer_in_pnl_terms_and_can_run_twice(monkeypatch, fake_chain):
    ex_ = NS(stop_loss_pct=13, take_profit_ladder=(), poll_interval_sec=0.05)
    pos = _pos()
    pos.exit_ref_per_token = 0.866e6 / 1000                 # 13% round trip: +10% P&L needs a ~27% price move
    pos.last_ratio = 1.0
    target = pt.target_ratio(pos, ex_, "take_profit")
    assert target == pytest.approx(1.1e6 / 0.866e6)
    cards = {}
    pos.offers_made = ["sell_half"]                          # an earlier test already made (and she answered) the offer
    cards["old"] = NS(offer="...", mint=MINT, status="executed")

    async def move(rpc, kp, pool, mint, factor):
        assert "sell_half" not in pos.offers_made           # reset, so the offer can be made again
        cards["new"] = NS(offer="Coin 1 is up 10 percent. Want to sell half?", mint=MINT, status="pending")
        return {}
    monkeypatch.setattr(pt, "move_price", move)
    eng = _engine(ex_, cards)
    assert (await pt.run_price_test(eng, pos, "take_profit"))["ok"]
    r = await pt.run_price_test(eng, pos, "take_profit")     # the offer is waiting: say so, move nothing
    assert r["ok"] and r["steps"] == [] and "already waiting" in eng.bus.of("price_test")[-1].speak
