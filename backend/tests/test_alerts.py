"""Proactive alert reasons: move, near-exit, merge, throttle."""
from __future__ import annotations

from types import SimpleNamespace

from backend.config import ALERT_MOVE_PCT, ALERT_NEAR_EXIT_PCT
from backend.execute.alerts import merge_speak, near_exit, position_reasons, pct_plain
from backend.execute.listener import Position


def _pos(**kw):
    p = Position("Mint111111111111111111111111111111111111111", "XX", None, 1000, 1000, 1_000_000, 0)
    for k, v in kw.items():
        setattr(p, k, v)
    return p


def test_merge_and_move_alert():
    p = _pos(coin_number=1, last_ratio=1.0 + (ALERT_MOVE_PCT + 0.5) / 100, last_alert_at=0, last_alert_ratio=1.0)
    ex = SimpleNamespace(stop_loss_pct=1.5, take_profit_ladder=[], liquidity_drop_pct=80)
    rs = position_reasons(p, SimpleNamespace(liquidity=None), 100, ex)
    assert rs and "coin 1 is up" in rs[0] and "percent" in rs[0]
    speak = merge_speak(rs)
    assert speak.endswith(".") and speak[0].isupper()
    # throttled
    p.last_alert_at = 90
    assert position_reasons(p, SimpleNamespace(liquidity=None), 100, ex) == []


def test_near_stop():
    p = _pos(coin_number=2, last_ratio=1 - (1.5 - ALERT_NEAR_EXIT_PCT / 2) / 100, last_alert_at=0)
    ex = SimpleNamespace(stop_loss_pct=1.5, take_profit_ladder=[], liquidity_drop_pct=80)
    assert near_exit(p, p.last_ratio, ex) == "stop loss"
    rs = position_reasons(p, SimpleNamespace(liquidity=None), 100, ex)
    assert any("stop loss" in r for r in rs)


def test_pct_plain_no_name():
    assert "JUP" not in merge_speak(["coin 1 is up " + pct_plain(5.2)])
    assert "5.2 percent" in pct_plain(5.2) or "5.2" in pct_plain(5.2)
