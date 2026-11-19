"""P1 co-pilot: daily max loss, risk-tiered confirmation, de-risking offers, risk sizing, desk reasons, JEV suspicion."""
import asyncio
import time
from types import SimpleNamespace

import pytest

from backend.copilot.desk import (desk_reason, desk_reasons, parse_risk, spoken, warning_text, rank_score, size_from_risk, suspicion,
                                  suspicion_questions)
from backend.events import EventBus
from backend.execute.cards import Card, CodeBook, verify_derisk_yes
from backend.execute.engine import TradingEngine, is_derisk
from backend.execute.listener import Position


class E:
    """Just enough of TradingEngine to run its real methods without a wallet or RPC."""
    buy_block_reason = TradingEngine.buy_block_reason
    loss_today_usdc = TradingEngine.loss_today_usdc
    respond = TradingEngine.respond
    _expire_if_due = TradingEngine._expire_if_due
    create_action_card = TradingEngine.create_action_card
    _action_card_speak = TradingEngine._action_card_speak
    derisk_offer = TradingEngine.derisk_offer

    def __init__(self, daily=2.0):
        self.m = SimpleNamespace(mandate_id="m1", body=SimpleNamespace(
            budget=SimpleNamespace(daily_max_loss_usdc=daily), exec=SimpleNamespace(approval_timeout_sec=180)))
        self.positions, self.cards, self.bus, self.codes = {}, {}, EventBus(), CodeBook()
        self.ui_button_enabled, self._exec_tasks = False, set()
        self.executed = []
        self.wallet, self.spent_usdc = "W", 0.0


    async def _execute(self, c, amount):
        self.executed.append(c.code)
        c.status = "executed"
        return {"ok": True, "status": "executed"}


@pytest.fixture(autouse=True)
def _no_intent_check(monkeypatch):
    import backend.execute.engine as eng
    monkeypatch.setattr(eng, "build_buy_intent", lambda *a, **k: None)   # mandate amount limits are tested elsewhere


def pos(entry=1.0, ratio=1.0, held=1000, realized=0.0, mint="M1", n=1):
    p = Position(mint, "", None, 1000, held, int(entry * 1e6), 0.0, coin_number=n)
    p.exit_ref_per_token = entry * 1e6 / 1000
    p.last_ratio = ratio
    p.realized_usdc = int(realized * 1e6)
    return p


def buy_card(code="47"):
    now = time.time()
    return Card(id="c" + code, code=code, mint="M2", symbol_display="", amount_usdc=1.0, expected_out=1, metrics={}, sim={},
                guard={"decision": "allow"}, created_at=now, expires_at=now + 60, coin_number=2)


def test_daily_max_loss_counts_realized_and_unrealized():
    e = E(daily=2.0)
    e.positions["M1"] = pos(entry=1.0, ratio=0.5)                       # -0.50 unrealized
    e.positions["M3"] = pos(entry=1.0, held=0, realized=0.0, mint="M3")  # fully stopped out at zero: -1.00
    e.positions["M3"].status = "closed"
    assert round(e.loss_today_usdc(), 2) == 1.5 and e.buy_block_reason() is None
    e.positions["M1"].last_ratio = 0.0                                   # -1.00 unrealized now
    assert round(e.loss_today_usdc(), 2) == 2.0
    assert "buying is off for today" in e.buy_block_reason()


def test_buy_blocked_at_the_limit_but_selling_still_allowed():
    e = E(daily=2.0)
    e.positions["M1"] = pos(entry=2.0, ratio=0.0)                         # lost the whole $2 today
    b = buy_card("47")
    e.cards[b.id] = b
    r = asyncio.run(e.respond(b.id, "approve", utterance="approve 47"))
    assert r["ok"] is False and b.status == "voided" and "daily max loss" in r["reason"] and e.executed == []
    e.positions["M1"].last_ratio = 0.5
    e.positions["M1"].status = "open"
    s = e.create_action_card("sell", "M1", coin_number=1, sell_fraction=0.5, announce=False)
    r = asyncio.run(e.respond(s.id, "approve", utterance="yes"))
    assert r["ok"] and e.executed == [s.code]                             # de-risking is never blocked


def test_derisk_yes_but_buys_need_the_code():
    assert verify_derisk_yes("yes", "47") is None
    assert verify_derisk_yes("yeah, sell half", "47") is None
    assert verify_derisk_yes("approve 47", "47") is None
    assert verify_derisk_yes("no", "47") and verify_derisk_yes("yes... wait, don't", "47")
    assert verify_derisk_yes("yes 12", "47")                              # a number that is not this card's code
    e = E(daily=None)
    b = buy_card("47")
    e.cards[b.id] = b
    r = asyncio.run(e.respond(b.id, "approve", utterance="yes"))
    assert r["ok"] is False and e.executed == []                          # a buy needs "approve" + its code
    r = asyncio.run(e.respond(b.id, "approve", utterance="approve 47"))
    assert r["ok"] and e.executed == ["47"]


def test_which_cards_count_as_derisking():
    p = SimpleNamespace(stop_loss_pct=12.0)
    assert is_derisk("sell", None, p) and is_derisk("adjust_exit", {"break_even": True}, p)
    assert is_derisk("adjust_exit", {"stop_loss_pct": 6}, p)              # tighter stop
    assert not is_derisk("adjust_exit", {"stop_loss_pct": 20}, p)         # wider stop adds risk: needs the code
    assert not is_derisk("adjust_exit", {"take_profit_pct": 30}, p)


def test_offers_only_derisking_once_per_level():
    e = E(daily=None)
    p = pos(ratio=1.05)
    e.positions["M1"] = p
    assert e.derisk_offer(p) is None                                      # +5%: stays silent
    p.last_ratio = 1.12
    c = e.derisk_offer(p)
    assert c.kind == "sell" and c.sell_fraction == 0.5 and c.derisk
    ev = e.bus.of("derisk_offer")[-1]
    assert ev.speak == "Coin 1 is up 12 percent. Want to sell half?" and "buy" not in ev.speak.lower()
    assert e.derisk_offer(p) is None                                      # one open offer at a time
    c.status = "executed"
    assert e.derisk_offer(p) is None                                      # never the same offer twice
    p.last_ratio = 1.22
    c2 = e.derisk_offer(p)
    assert c2.kind == "adjust_exit" and c2.adjust == {"break_even": True}
    assert "break-even" in e.bus.of("derisk_offer")[-1].speak


def test_risk_sizing():
    assert parse_risk("I'm willing to lose one dollar on this") == (1.0, None)
    assert parse_risk("Vibe, reject 35. I'm willing to lose ten cents on coin 1.") == (0.1, 1)   # a card code is not the risk
    assert parse_risk("risk ten cents on coin 2") == (0.1, 2)
    assert parse_risk("coin 2, 1 dollar") == (None, None)                  # an ordinary order is not a risk order
    assert size_from_risk(0.1, 12, per_trade=1.0, left=5.0) == (0.83, None)
    assert size_from_risk(1.0, 10, per_trade=1.0, left=5.0) == (1.0, "per-trade cap")
    assert size_from_risk(1.0, 10, per_trade=5.0, left=0.4) == (0.4, "money left today")


def test_desk_reason_and_rank_are_numbers_only():
    m = {"buys_h1": 52, "sells_h1": 20, "price_change_h1": 4.0, "top10_pct": 18.0, "volume_to_liquidity": 2.0}
    assert desk_reason(m) == "52 buys vs 20 sells in the last hour"
    assert rank_score(m) > rank_score({**m, "price_change_h24": 300.0})   # chasing a pump ranks lower
    assert desk_reason({}) == "Passed every safety check"


def test_desk_reasons_each_carry_a_number_of_their_own():
    # the recorded smart-money round: three coins with near-identical net buying
    ms = [{"sm_netflow_24h_usd": 4649.1, "sm_traders": 5}, {"sm_netflow_24h_usd": 5076.4, "sm_traders": 10},
          {"sm_netflow_24h_usd": 5006.4, "sm_traders": 19}]
    assert desk_reasons(ms) == ["5 smart wallets bought $4.6k today", "10 smart wallets bought $5.1k today",
                                "19 smart wallets bought $5k today"]
    # same wallets and same money: the second coin falls back to a number that is its own
    twin = {"sm_netflow_24h_usd": 5000, "sm_traders": 5}
    assert desk_reasons([twin, {**twin, "holders": 900}]) == ["5 smart wallets bought $5k today", "900 holders"]
    assert desk_reason({"volume_h1": 30_000, "volume_h24": 240_000}) == "Volume up 3\u00d7 in the last hour"
    assert all("Nansen" not in r for r in desk_reasons(ms))          # the source lives in the coin's details


def test_warnings_only_with_a_concrete_number_and_spoken_like_the_screen():
    m = {"top10_pct": 36.2, "price_change_h1": 2.5, "buys_h1": 268, "sells_h1": 210, "volume_to_liquidity": 18.0}
    assert warning_text("dump", m) == "Top 10 wallets hold 36%, near your 40% limit"
    assert warning_text("wash", m) == "Traded 18\u00d7 its liquidity in 24 hours, near your 20\u00d7 limit"
    assert warning_text("dump", {}) is None and warning_text(None, m) is None
    assert warning_text("dump", {"top10_pct": 18.4, "dev_pct": 0.0}) is None        # 18% is not a worry on its own
    assert warning_text("pump", {"price_change_h1": -4.4, "price_change_h24": 12}) is None
    assert warning_text("wash", {"volume_to_liquidity": 2.4}) is None
    assert spoken("5 smart wallets bought $4.6k today") == "5 smart wallets bought about 4.6 thousand dollars today"
    assert spoken("Volume up 3\u00d7 in the last hour") == "Volume up 3 times in the last hour"
    assert spoken("Top 10 wallets hold 28%") == "Top 10 wallets hold 28 percent"
    assert spoken("Top 10 wallets hold 36%, near your 40% limit") == "Top 10 wallets hold 36 percent, near your 40 percent limit"


def test_limit_warnings_follow_the_mandate_limit_not_a_fixed_number():
    """Regression: the mandate allowed top-10 holders up to 40%, the coin had 28%, yet the pick row showed an amber
    'Top 10 wallets hold 28%' and the agent called it a concentration worry. A limit-based figure is a concern only
    at 87.5% of the mandate's own limit or above (35% of 40%)."""
    from backend.copilot.desk import NEAR_LIMIT, holder_note, near_limit, vs_limit
    from backend.mandate.schema import Filters
    assert NEAR_LIMIT == 0.875
    assert warning_text("dump", {"top10_pct": 27.7}) is None                      # 28% under a 40% limit: no warning
    assert warning_text("dump", {"top10_pct": 34.9}) is None
    assert warning_text("dump", {"top10_pct": 35.0}) == "Top 10 wallets hold 35%, near your 40% limit"
    assert warning_text("dump", {"top10_pct": 41.0}) == "Top 10 wallets hold 41%, over your 40% limit"
    # derived from the mandate: a 30% limit makes 28% near it; a 60% limit makes 50% fine
    tight, loose = Filters(max_top10_pct=30), Filters(max_top10_pct=60)
    assert warning_text("dump", {"top10_pct": 27.7}, tight) == "Top 10 wallets hold 28%, near your 30% limit"
    assert warning_text("dump", {"top10_pct": 50.0}, loose) is None
    # the other limit-based figures follow the same rule
    assert warning_text("dump", {"top10_pct": 10, "dev_pct": 3.0}) is None             # dev limit 5%: 3% is fine
    assert warning_text("dump", {"top10_pct": 10, "dev_pct": 4.5}) == "Dev wallet holds 4.5%, near your 5% limit"
    assert warning_text("wash", {"volume_to_liquidity": 6.0}) is None                   # turnover limit 20x
    assert near_limit(35, 40) and not near_limit(34.9, 40) and not near_limit(None, 40)
    assert vs_limit(28, 40) == "under your 40% limit"
    # a concentration reason always says what it is compared to
    assert desk_reason({"top10_pct": 18.0}) == "Top 10 wallets hold 18%, under your 40% limit"
    assert "only" not in desk_reason({"top10_pct": 18.0})
    # the agent's comparison line: under the limit, and where it sits among this round's picks
    picks = [{"top10_pct": 27.7}, {"top10_pct": 12.0}, {"top10_pct": 20.1}]
    assert holder_note(picks[0], picks) == "Top 10 wallets hold 28%, under your 40% limit, highest of the three picks"
    assert holder_note(picks[1], picks) == "Top 10 wallets hold 12%, under your 40% limit, lowest of the three picks"
    assert holder_note(picks[2], picks) == "Top 10 wallets hold 20%, under your 40% limit"
    assert holder_note({}, picks) is None
    for text in (warning_text("dump", {"top10_pct": 38}), holder_note(picks[0], picks)):
        assert "worry" not in text.lower() and "concentration" not in text.lower()


def test_jev_suspicion_asks_specific_numeric_questions_and_shows_the_worst():
    m = {"price_change_h1": 40.0, "price_change_h24": 90.0, "buys_h1": 300, "sells_h1": 20, "liquidity_usd": 60000,
         "top10_pct": 30.0, "dev_pct": 1.0, "holders": 900, "market_cap": 500000,
         "volume_to_liquidity": 9.0, "buys_h24": 2000, "sells_h24": 1900, "volume_h24": 540000}
    qs = suspicion_questions(m)
    assert [q[0] for q in qs] == ["pump", "dump", "wash"]
    for _k, _label, state, q in qs:
        assert "{" in state and q["type"] == "noul" and "thesis" not in q["instructions"]

    class J:
        async def ask(self, state, name, q):
            return {"pump": 0.72, "dump": 0.3, "wash": 0.4}[name]
    s = asyncio.run(suspicion(J(), m))
    assert s["key"] == "pump" and s["score"] == 0.72 and s["label"] == "the last hour looks like a coordinated pump"

    class Down:
        async def ask(self, *a):
            raise RuntimeError("down")
    assert asyncio.run(suspicion(Down(), m)) is None


def test_price_test_aims_just_past_the_trigger():
    from backend.execute.price_test import nudge_factor
    # stop at 11% from a flat price: aim at about -11.5%, not -24%
    f = nudge_factor(1.0, 0.89, "stop_loss")
    assert 0.88 <= f <= 0.89
    # already close: only a small nudge
    assert 0.99 <= nudge_factor(0.892, 0.89, "stop_loss") < 1
    # take profit / offer at +10%: aim at about +10.6%
    assert 1.10 <= nudge_factor(1.0, 1.10, "take_profit") <= 1.11


def test_card_loss_plan_and_worst_case():
    from backend.copilot.desk import loss_bounds
    plan, worst = loss_bounds(0.41, 24, 1000, gap_pct=3)
    assert round(plan, 2) == 0.10 and round(worst, 2) == 0.14     # 41c x 24% ~ 10c; gap 3% + 10% slippage ~ 14c
    plan, worst = loss_bounds(1.0, 11, 1000)
    assert plan == 0.11 and 0.22 < worst < 0.23


def test_capped_risk_size_note():
    from backend.copilot.desk import CAP_WORDS, size_from_risk
    amt, note = size_from_risk(1.0, 10, per_trade=1.0, left=5.0)       # 1 / 10% = 10 dollars -> capped at 1
    assert amt == 1.0 and CAP_WORDS[note] == "capped at the per-trade limit"


def test_desk_announcement_reads_the_on_screen_lines():
    from backend.orchestrator import Orchestrator

    class Bus:
        def __init__(self):
            self.got = []

        def emit(self, t, speak, panel=None, loud=False):
            self.got.append((t, speak))

    o = Orchestrator.__new__(Orchestrator)
    o.bus, o.mandate, o.engine, o.round_no, o._building, o.focus_coin = Bus(), None, None, 1, {}, None
    offers = [{"number": 1, "reason": "5 smart wallets bought $4.6k today", "warning": "Top 10 wallets hold 36%, near your 40% limit",
               "suspicion": {"key": "dump", "label": "a few wallets could dump it", "score": 0.74}},
              {"number": 2, "reason": "10 smart wallets bought $5.1k today", "warning": None,
               "suspicion": {"key": "dump", "label": "a few wallets could dump it", "score": 0.9}}]
    o._announce_desk(offers)
    speak = o.bus.got[-1][1]
    assert "Coin 1: 5 smart wallets bought about 4.6 thousand dollars today." in speak
    # the figure and the limit it is near, never a label such as "one worry" or "concentration worry"
    assert "Top 10 wallets hold 36 percent, near your 40 percent limit. Still want it on the list?" in speak
    assert speak.count("Still want it") == 1 and "Nansen" not in speak      # no figure near a limit, no question for coin 2
    assert "worry" not in speak.lower() and "concentration" not in speak.lower()


def test_spoken_up_percent_is_the_same_pnl_as_the_top_bar():
    """Regression: 'Coin 1 is up 12 percent. Want to sell half?' while Today showed -$0.03. The price had risen 12% from the
    post-fill sell value, but fees and slippage on the way in (13% round trip on a $1 buy) meant selling still lost money."""
    e = E(daily=None)
    p = pos(entry=1.0, ratio=1.12)
    p.exit_ref_per_token = 0.866e6 / 1000                 # selling right after the fill would have returned $0.866
    e.positions["M1"] = p
    assert p.mark_usdc() == 969_920 and p.pnl_usdc() == -30_080          # the top bar's Today: -$0.03
    assert e.derisk_offer(p) is None                                      # not "up 12 percent": it is down 3 percent
    assert e.loss_today_usdc() == pytest.approx(0.03008)                 # the daily loss limit counts the same mark
    from dataclasses import asdict
    st = {**asdict(p), "mark_usdc": p.mark_usdc(), "pnl_usdc": p.pnl_usdc()}   # what TradingEngine.status() sends the UI
    import inspect
    assert '"pnl_usdc": p.pnl_usdc()' in inspect.getsource(TradingEngine.status) and st["pnl_usdc"] == -30_080
    p.last_ratio = 1.30                                                   # now really up: $1.1258 back for $1.00
    c = e.derisk_offer(p)
    ev = e.bus.of("derisk_offer")[-1]
    assert c is not None and ev.speak == "Coin 1 is up 13 percent. Want to sell half?"
    assert ev.panel["up_pct"] == round(p.pnl_pct(), 1) == 12.6
