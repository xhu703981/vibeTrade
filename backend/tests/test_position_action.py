"""Deterministic parse of sell / stop / take-profit voice commands."""
from __future__ import annotations

from backend.voice.position_action import parse_position_action
from backend.voice.desk_order import fold_major_tickers, parse_desk_order
from backend.voice.verify import parse_desk_order as pdo


def test_sell_half_and_all():
    a, why = parse_position_action("sell half of coin 3", [1, 3])
    assert why is None and a.kind == "sell" and a.coins == [3] and a.fraction == 0.5
    a, why = parse_position_action("sell all", [1, 2])
    assert why is None and a.kind == "sell" and a.coins == "all" and a.fraction == 1.0
    a, why = parse_position_action("sell 25% of coin 1", [1])
    assert why is None and a.fraction == 0.25 and a.coins == [1]
    a, why = parse_position_action("close everything", [1, 2])
    assert a.kind == "sell" and a.coins == "all"


def test_stop_and_tp():
    a, why = parse_position_action("move my stop to break-even", [1])
    assert why is None and a.kind == "set_stop" and a.break_even and a.coins == [1]
    a, why = parse_position_action("set stop loss on coin 2 to 3%", [1, 2])
    assert a.kind == "set_stop" and a.coins == [2] and a.stop_loss_pct == 3
    a, why = parse_position_action("take profit at 10% instead", [1])
    assert a.kind == "set_tp" and a.take_profit_pct == 10


def test_major_ticker_folds_into_desk_order():
    u = fold_major_tickers("buy one cent of JUP", {"JUP": 101})
    assert "coin 101" in u.lower()
    heard, why = pdo(u, [101], 0.01)
    assert why is None and heard == {101: 0.01}


import asyncio
import pytest

from backend.events import EventBus
from backend.execute.engine import TradingEngine
from backend.execute.listener import Position, decide_exit
from scripts.m3_run import smoke_mandate


@pytest.mark.asyncio
async def test_sell_adjust_approve_skips_buy_amount(tmp_path):
    """Sell / exit-rule cards have amount_usdc=0; approve must not run the buy-amount check."""
    m = smoke_mandate()
    e = TradingEngine(m, rpc_url="http://127.0.0.1:1", keypair_path=tmp_path / "kp.json", bus=EventBus())
    mint = "Mint111111111111111111111111111111111111111"
    pos = Position(mint, "XX", None, 1000, 1000, 1_000_000, 0, last_ratio=1.02,
                   coin_number=1, status="open")
    e.positions[mint] = pos

    async def fake_sell(c):
        pos.held_tokens = 500
        c.status = "executed"
        return {"ok": True, "status": "executed", "held_after": 500}

    e._execute_sell = fake_sell
    c = e.create_action_card("sell", mint, coin_number=1, sell_fraction=0.5, announce=False)
    r = await e.respond(c.id, "approve", f"approve {c.code}")
    assert r["ok"] and r.get("status") == "executed" and pos.held_tokens == 500

    c2 = e.create_action_card("adjust_exit", mint, coin_number=1, adjust={"break_even": True}, announce=False)
    r2 = await e.respond(c2.id, "approve", f"approve {c2.code}")
    assert r2["ok"] and r2.get("status") == "executed"
    assert pos.stop_loss_pct == 0.0
    # listener uses 0.0 as break-even (not "no stop")
    q = type("Q", (), {"amount": 1000, "value_usdc": 1_000_000, "liquidity": None})()
    pos.exit_ref_per_token = 1000.0  # value_usdc / amount, so ratio == 1.0
    pos.held_tokens = 1000
    # ratio 1.0 should trigger stop when sl is 0
    trig = decide_exit(pos, q, 10, type("E", (), {"stop_loss_pct": 1.5, "take_profit_ladder": [],
                                                   "liquidity_drop_pct": 80, "trailing_stop_pct": 50,
                                                   "max_hold_hours": 99})())
    assert trig and trig[0] == "stop_loss"


def test_majors_routes_omit_zerofi():
    from backend.config import MAJORS, MAJORS_ROUTE_DEXES, JUPITER_ROUTE_DEXES, route_dexes
    assert "ZeroFi" not in MAJORS_ROUTE_DEXES
    assert "ZeroFi" not in (route_dexes(MAJORS["JUP"]) or "")
    # memes keep the tight list; majors still use the broader AMMs
    assert "Pump.fun" in JUPITER_ROUTE_DEXES
    assert "Raydium" in MAJORS_ROUTE_DEXES and "Meteora" in MAJORS_ROUTE_DEXES
