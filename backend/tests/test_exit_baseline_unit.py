"""Exit rules measure the price move since the fill (baseline sell quote right after the buy), not cost vs liquidation value.
Regression: a 1% stop loss fired on the first poll from the ~1.5% round-trip fee alone (GATO, 0.01 USDC, 2026-11-13)."""
from __future__ import annotations

import pytest

from backend.execute.listener import Position, _exit_speak, _sell_speak, decide_exit, money_words, pct_words
from backend.execute.price_source import PriceQuote
from backend.mandate.schema import MandateBody
from backend.tests.test_execute_unit import M, MINT, card, eng  # noqa: F401  (eng is a fixture)


def ex(sl=1.0, tp=1.01, trailing=30.0):
    d = MandateBody().model_dump(mode="json")["exit"]
    d.update(stop_loss_pct=sl, take_profit_ladder=[{"multiple": tp, "sell_pct": 100}], trailing_stop_pct=trailing)
    return MandateBody.model_validate({**MandateBody().model_dump(mode="json"), "exit": d}).exit


def her_position(**kw):
    """Xinrui's case: 0.01 USDC for 1_750_853 base units; selling at once returns 0.009844 (fees, not a price move)."""
    p = Position(MINT, "GATO", "pool", 1_750_853, 1_750_853, 10_000, 0.0, entry_liquidity=1_000)
    for k, v in kw.items():
        setattr(p, k, v)
    return p


def quote(value, amount=1_750_853):
    return PriceQuote(amount, value, 1_000, "test", 0)


def test_fee_only_quote_does_not_fire_one_percent_stop_loss():
    p = her_position()
    p.set_exit_baseline(9_844, 1_750_853)
    assert round(p.roundtrip_cost_pct, 2) == 1.56
    assert decide_exit(p, quote(9_844), 1, ex()) is None and p.last_ratio == pytest.approx(1.0)
    assert decide_exit(p, quote(9_800), 1, ex()) is None              # -0.45% price: still inside a 1% stop
    assert decide_exit(p, quote(9_740), 1, ex())[0] == "stop_loss"    # -1.06% real move
    p2 = her_position()
    p2.set_exit_baseline(9_844, 1_750_853)
    assert decide_exit(p2, quote(9_943), 1, ex())[0] == "take_profit"  # +1.0% real move


def test_old_rule_would_have_fired_on_fees():
    """Same quote, measured against the USDC spent (the old rule): 0.9844 <= 0.99 -> instant stop loss."""
    p = her_position(exit_ref_per_token=10_000 / 1_750_853)
    assert decide_exit(p, quote(9_844), 1, ex())[0] == "stop_loss"


def test_no_baseline_first_poll_sets_it_and_never_triggers():
    p = her_position()
    assert decide_exit(p, quote(9_000), 1, ex()) is None              # would be -10% vs cost: no trigger on the baseline poll
    assert p.exit_ref_per_token == pytest.approx(9_000 / 1_750_853) and p.last_ratio == 1.0 and p.last_value_usdc == 9_000
    assert p.entry_exit_value == 9_000
    assert decide_exit(p, quote(8_900), 2, ex())[0] == "stop_loss"    # -1.1% after the baseline


def test_trailing_uses_price_peak():
    p = her_position()
    p.set_exit_baseline(10_000, 1_750_853)
    assert decide_exit(p, quote(10_050), 1, ex(sl=50, tp=5, trailing=1)) is None and p.peak_ratio == pytest.approx(1.005)
    assert decide_exit(p, quote(9_940), 1, ex(sl=50, tp=5, trailing=1))[0] == "trailing_stop"


def test_add_fill_weights_the_reference():
    p = Position(MINT, "X", None, 1000, 1000, 1_000_000, 0.0)
    p.set_exit_baseline(980_000, 1000)
    p.add_fill(1000, 1_000_000, 1_000_000)                   # second buy after a +2% move
    assert p.initial_tokens == p.held_tokens == 2000 and p.entry_usdc == 2_000_000
    assert p.exit_ref_per_token == pytest.approx(990.0)
    p.add_fill(10, 10_000, None)                              # baseline unknown for a fill: re-baseline next poll
    assert p.exit_ref_per_token is None


def test_words():
    assert pct_words(0.4) == "+0.4%" and pct_words(-1.56) == "-1.6%" and pct_words(49.4) == "+49%"
    assert money_words(9_844) == "0.984 cents" and money_words(-156) == "0.016 cents" and money_words(123_000) == "12.3 cents"
    assert money_words(25_000) == "2.5 cents" and money_words(10_000) == "1 cent" and money_words(1_230_000) == "1.23 dollars" and money_words(5) == "0.0005 cents"


def test_exit_and_sell_messages_report_price_move_and_cent_pnl():
    p = her_position()
    p.set_exit_baseline(9_844, 1_750_853)
    decide_exit(p, quote(9_740), 1, ex())
    assert _exit_speak("stop_loss", p, p.held_tokens) == "Stop-loss triggered: price -1.1% since the buy, selling 100% of the position."
    p.held_tokens, p.status, p.realized_usdc = 0, "closed", 9_740
    s = _sell_speak("stop_loss", 9_740, p, -0.00026)
    assert "got back 0.974 cents" in s and "loss 0.026 cents (-2.6% including fees; the price -1.1% since the buy)" in s


async def test_engine_fill_takes_baseline_and_speaks_fee(eng):  # noqa: F811
    class PS:
        async def quote(self, mint, amount, pool):
            return PriceQuote(amount, int(amount * 1000 * 0.985), 5, "fork_profile", 0)   # 1.5% round trip
    eng.listener.ps = PS()
    c = await card(eng)
    assert (await eng.respond(c.id, "approve", f"approve {c.code}"))["ok"]
    p = eng.positions[MINT]
    assert p.entry_exit_value == 9_850_000 and p.last_ratio == 1.0 and p.entry_liquidity == 5
    ev = eng.bus.of("trade_executed")[-1]
    assert "About 2 percent of that is fees and slippage if you sold right now" in ev.speak and ev.panel["roundtrip_cost_pct"] == 1.5
    assert decide_exit(p, PriceQuote(p.held_tokens, 9_850_000, 5, "t", 0), 1, ex()) is None   # fees alone: no stop loss


async def test_engine_fill_baseline_failure_falls_back_to_first_poll(eng):  # noqa: F811
    class PS:
        async def quote(self, mint, amount, pool):
            raise RuntimeError("down")
    eng.listener.ps = PS()
    c = await card(eng)
    assert (await eng.respond(c.id, "approve", f"approve {c.code}"))["ok"]
    p = eng.positions[MINT]
    assert p.exit_ref_per_token is None and "fees" not in eng.bus.of("trade_executed")[-1].speak
    assert decide_exit(p, PriceQuote(p.held_tokens, 9_000_000, 5, "t", 0), 1, ex()) is None
