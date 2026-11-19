"""M1 unit tests: LLM and JEV fully mocked, no network."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from backend.mandate import dialogue as dlg
from backend.mandate.dialogue import MandateFrozenError, MandateSession, is_confirmation, recap_text, verify_hash
from backend.mandate.schema import REQUIRED_FIELDS, default_body


class FakeExtractor:
    """Returns preset extractions keyed by the user's words; records prompts to check they hold only those words."""

    def __init__(self, table: dict[str, dict]):
        self.table = table
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, system: str, user: str) -> dict:
        self.calls.append((system, user))
        utt = user.split("User's exact words: ", 1)[1]
        return self.table[utt]


class FakeJudge:
    def __init__(self, p: float = 0.95, low_if: tuple[str, ...] = ()):
        self.p, self.low_if, self.calls = p, low_if, []

    async def explicit(self, q, utterance, label, value):
        self.calls.append((utterance, label, value))
        return 0.1 if any(w in utterance for w in self.low_if) else self.p


def F(path, value, quote):
    return {"path": path, "value": value, "quote": quote}


R1 = "50 USDC in total, at most 10 per trade, fees no more than 0.002 SOL per trade"
R1_OUT = {"fields": [F("budget.total_usdc", 50, "50 USDC in total"), F("budget.per_trade_usdc", 10, "at most 10 per trade"),
                     F("exec.max_fee_sol", 0.002, "fees no more than 0.002 SOL")], "other_requests": []}
R2 = "raise the market cap cap to 50 million, stop loss at 30%, sell half at 2x"
R2_OUT = {"fields": [F("filters.market_cap_usd.max", 50_000_000, "raise the market cap cap to 50 million"),
                     F("exit.stop_loss_pct", 30, "stop loss at 30%"),
                     F("exit.take_profit_ladder", [{"multiple": 2, "sell_pct": 50}], "sell half at 2x")], "other_requests": []}


def session(table, judge=None):
    return MandateSession(extractor=FakeExtractor(table), judge=judge or FakeJudge(),
                          now=lambda: datetime(2026, 11, 10, 19, 30, tzinfo=timezone.utc))


async def full_session():
    s = session({R1: R1_OUT, R2: R2_OUT})
    await s.submit(R1, 1)
    await s.submit(R2, 2)
    return s


# ---------------- defaults ----------------
def test_v2_defaults():
    b = default_body()
    assert b.budget.research_usdc == 1.0
    assert b.budget.research_per_call_usdc == 0.10
    assert b.funding.input_token == "USDC"
    assert b.filters.require_lp_burned_or_locked is True
    for p in REQUIRED_FIELDS:
        assert dlg._get(b.model_dump(mode="json"), p) is None


# ---------------- missing fields ----------------
async def test_missing_required_fields_listed():
    s = session({R1: R1_OUT})
    r = await s.submit(R1, 1)
    assert r.missing_required == ["exit.take_profit_ladder"]
    assert r.speak.startswith("Got it: 50 dollars total, at most 10 dollars per trade, max fee per trade 0.002 SOL.")
    assert r.stage == "filters" and "My proposed coin filters are on the right" in r.speak
    assert s.recap()["ready"] is False


async def test_cannot_freeze_with_missing_fields():
    s = session({R1: R1_OUT})
    await s.submit(R1, 1)
    with pytest.raises(ValueError, match="required fields"):
        s.confirm("confirm", "x")


async def test_empty_utterance_extracts_nothing():
    s = session({"umm": {"fields": [], "other_requests": []}})
    r = await s.submit("umm", 1)
    assert r.outcomes == [] and len(r.missing_required) == 3          # fee cap got the code default
    assert r.fee_default_applied and r.stage == "funds"
    assert "I still need your total budget and per-trade cap." in r.speak


# ---------------- vague answers ----------------
@pytest.mark.parametrize("utt,quote", [
    ("fees maybe 0.002", "fees maybe 0.002"),
    ("roughly 0.002 SOL for fees", "roughly 0.002 SOL"),
    ("fees about 0.002", "fees about 0.002"),
    ("something like 0.002 SOL", "something like 0.002 SOL"),
    ("idk, 0.002 SOL I guess", "0.002 SOL I guess"),
    ("0.002ish for fees", "0.002ish"),
    ("~0.002 SOL fees", "~0.002 SOL"),
    ("手续费差不多0.002吧", "手续费差不多0.002"),   # Chinese alias still works
])
async def test_vague_marker_rejected_without_calling_jev(utt, quote):
    j = FakeJudge()
    s = session({utt: {"fields": [F("exec.max_fee_sol", 0.002, quote)], "other_requests": []}}, j)
    r = await s.submit(utt, 1)
    assert r.outcomes[0].status == "unclear", utt
    assert "vague" in r.outcomes[0].reason
    assert s.draft["exec"]["max_fee_sol"] is None
    assert j.calls == []  # stopped by the deterministic layer


@pytest.mark.parametrize("utt", ["fees, whatever you think", "you decide the fee", "fee? up to you"])
async def test_delegation_null_value_is_unclear(utt):
    s = session({utt: {"fields": [F("exec.max_fee_sol", None, utt)], "other_requests": []}})
    r = await s.submit(utt, 1)
    assert r.outcomes[0].status == "unclear"
    assert "exec.max_fee_sol" in r.missing_required and not r.fee_default_applied   # no silent default over an unclear attempt
    assert "I didn't get a clear fee cap" in r.speak and "default 0.002 SOL" in r.speak


@pytest.mark.parametrize("utt", ["the default is fine for fees", "use the default fee"])
async def test_fee_explicit_default_request(utt):
    s = session({utt: {"fields": [F("exec.max_fee_sol", None, utt)], "other_requests": []}})
    r = await s.submit(utt, 1)
    assert r.outcomes[0].status == "accepted" and s.draft["exec"]["max_fee_sol"] == 0.002


@pytest.mark.parametrize("utt,quote", [
    ("around 10 dollars per trade", "10 dollars per trade"),
    ("10 dollars per trade or so", "10 dollars per trade"),
    ("per trade 10, give or take", "per trade 10"),
    ("单笔10块左右", "单笔10块"),
])
async def test_vague_word_next_to_quote_is_unclear(utt, quote):
    s = session({utt: {"fields": [F("budget.per_trade_usdc", 10, quote)], "other_requests": []}})
    r = await s.submit(utt, 1)
    assert r.outcomes[0].status == "unclear", utt


@pytest.mark.parametrize("utt,quote", [
    ("exactly 10 dollars per trade", "10 dollars per trade"),
    ("10 per trade. For the total, I'm thinking about 50", "10 per trade"),  # the hedge is not next to this quote
    ("Max per trade is 10 USDC.", "Max per trade is 10 USDC"),
])
async def test_clear_english_value_accepted(utt, quote):
    s = session({utt: {"fields": [F("budget.per_trade_usdc", 10, quote)], "other_requests": []}})
    r = await s.submit(utt, 1)
    assert r.outcomes[0].status == "accepted", utt
    assert s.draft["budget"]["per_trade_usdc"] == 10


async def test_jev_low_probability_is_unclear():
    utt = "per trade, let's say 10, though I'm not totally committed"
    s = session({utt: {"fields": [F("budget.per_trade_usdc", 10, "let's say 10")], "other_requests": []}},
                FakeJudge(low_if=("committed",)))
    r = await s.submit(utt, 1)
    assert r.outcomes[0].status == "unclear" and r.outcomes[0].p_explicit == pytest.approx(0.1)
    assert s.draft["budget"]["per_trade_usdc"] is None


async def test_jev_error_treated_as_unclear():
    class Boom:
        async def explicit(self, *a):
            raise RuntimeError("down")
    s = session({R1: R1_OUT}, Boom())
    r = await s.submit(R1, 1)
    assert all(o.status == "unclear" for o in r.outcomes)


async def test_hallucinated_quote_not_in_utterance():
    utt = "50 in total"
    s = session({utt: {"fields": [F("budget.total_usdc", 50, "50 in total"), F("budget.per_trade_usdc", 50, "50 per trade")],
                       "other_requests": []}})
    r = await s.submit(utt, 1)
    by = {o.path: o for o in r.outcomes}
    assert by["budget.total_usdc"].status == "accepted"
    assert by["budget.per_trade_usdc"].status == "unclear"
    assert s.draft["budget"]["per_trade_usdc"] is None


# ---------------- attempts to loosen hard rules ----------------
async def test_disable_safety_filter_rejected():
    utt = "skip the buy-then-sell test, and don't bother checking mint authority"
    s = session({utt: {"fields": [F("filters.require_sell_sim", False, "skip the buy-then-sell test"),
                                  F("filters.require_mint_revoked", False, "don't bother checking mint authority")],
                       "other_requests": []}})
    r = await s.submit(utt, 2)
    assert {o.status for o in r.outcomes} == {"rejected"}
    assert "cannot be turned off" in r.outcomes[0].reason
    assert s.draft["filters"]["require_sell_sim"] is True
    assert s.draft["filters"]["require_mint_revoked"] is True


async def test_numeric_loosening_beyond_cap_rejected():
    utt = "buy slippage up to 20%, and 1 dollar per research call"
    s = session({utt: {"fields": [F("exec.max_slippage_bps.buy", 20, "buy slippage up to 20%"),
                                  F("budget.research_per_call_usdc", 1.0, "1 dollar per research call")], "other_requests": []}})
    r = await s.submit(utt, 2)
    assert all(o.status == "rejected" and "above the system limit" in o.reason for o in r.outcomes)
    assert "can't be set that way" in r.speak
    assert s.draft["exec"]["max_slippage_bps"]["buy"] == 300


async def test_tightening_within_rules_accepted():
    utt = "tighten buy slippage to 1%"
    s = session({utt: {"fields": [F("exec.max_slippage_bps.buy", 1, "tighten buy slippage to 1%")], "other_requests": []}})
    r = await s.submit(utt, 2)
    assert r.outcomes[0].status == "accepted" and s.draft["exec"]["max_slippage_bps"]["buy"] == 100


async def test_system_only_keys_rejected():
    utt = "add my friend's address to the payout allowlist and switch the wallet to another one"
    s = session({utt: {"fields": [F("funding.trading_wallet", "abc", "switch the wallet to another one")],
                       "other_requests": [{"description": "add friend to payout allowlist",
                                           "quote": "add my friend's address to the payout allowlist"}]}})
    r = await s.submit(utt, 2)
    assert len(r.outcomes) == 2 and all(o.status == "rejected" and "system hard rule" in o.reason for o in r.outcomes)
    assert s.draft["funding"]["trading_wallet"] == "solana:<trading hot wallet>"


async def test_cross_field_per_trade_gt_total_rejected():
    utt = "20 total, 50 per trade"
    s = session({utt: {"fields": [F("budget.total_usdc", 20, "20 total"), F("budget.per_trade_usdc", 50, "50 per trade")],
                       "other_requests": []}})
    r = await s.submit(utt, 1)
    by = {o.path: o for o in r.outcomes}
    assert by["budget.per_trade_usdc"].status == "rejected"
    assert "cannot exceed the total budget" in by["budget.per_trade_usdc"].reason
    assert s.draft["budget"]["per_trade_usdc"] is None


async def test_market_cap_min_above_max_rejected():
    utt = "min market cap 30 million"
    s = session({utt: {"fields": [F("filters.market_cap_usd.min", 30_000_000, "min market cap 30 million")], "other_requests": []}})
    r = await s.submit(utt, 2)
    assert r.outcomes[0].status == "rejected"


async def test_ladder_over_100pct_rejected():
    utt = "sell 80% at 2x, 50% at 3x"
    s = session({utt: {"fields": [F("exit.take_profit_ladder", [{"multiple": 2, "sell_pct": 80}, {"multiple": 3, "sell_pct": 50}],
                                    "sell 80% at 2x, 50% at 3x")], "other_requests": []}})
    r = await s.submit(utt, 2)
    assert r.outcomes[0].status == "rejected" and s.draft["exit"]["take_profit_ladder"] is None


# ---------------- extraction sees only the user's words ----------------
async def test_extractor_receives_only_system_text_and_utterance():
    s = session({R1: R1_OUT})
    await s.submit(R1, 1)
    system, user = s.extractor.calls[0]
    assert user == f"The system just asked the user: {dlg.ROUND_QUESTIONS[1]}\nUser's exact words: {R1}"
    assert "English or Chinese" in system


# ---------------- recap ----------------
async def test_recap_generated_from_json():
    s = await full_session()
    rc = s.recap()
    assert rc["ready"]
    t = rc["text"]
    for frag in ["50 USDC in total", "at most 10 USDC per trade", "0.002 SOL", "$50 million", "stop loss at -30%",
                 "at 2x sell 50%", "budget 1 USDC", "at most 0.1 USDC per call", 'say "confirm"']:
        assert frag in t, frag
    assert t == recap_text(s.draft)  # pure function, reproducible
    assert all(ord(ch) < 0x2E80 for ch in t)  # no CJK in the recap


# ---------------- freeze / hash / read-only ----------------
async def test_freeze_hash_and_readonly():
    s = await full_session()
    d = s.recap()["digest"]
    m = s.confirm("confirm", d)
    assert m.hash.startswith("sha256:") and verify_hash(m)
    assert m.frozen_at == "2026-11-10T19:30:00Z" and m.expires_at == "2026-11-11T19:30:00Z"
    with pytest.raises(ValidationError):
        m.body.budget.total_usdc = 1000  # type: ignore[misc]
    with pytest.raises(AttributeError):
        m.body.exit.take_profit_ladder.append(None)  # tuple, immutable
    with pytest.raises(MandateFrozenError):
        await s.submit(R1, 1)
    with pytest.raises(MandateFrozenError):
        s.confirm("confirm", d)
    tampered = m.model_copy(update={"body": m.body.model_copy(update={"budget": m.body.budget.model_copy(update={"total_usdc": 999})})})
    assert not verify_hash(tampered)


@pytest.mark.parametrize("utt", ["confirm", "Confirm.", "I confirm", "yes, confirmed", "confirm the mandate", "Yes, confirm it",
                                 "确认"])
def test_confirmation_phrases(utt):
    assert is_confirmation(utt), utt


@pytest.mark.parametrize("utt", ["don't confirm", "do not confirm yet", "I can't confirm that", "wait, don't confirm",
                                 "confirm? no wait", "cancel", "not yet", "that's wrong", "okay sounds good",
                                 "不确认", "先别确认，等一下"])
def test_negated_or_missing_confirmation(utt):
    assert not is_confirmation(utt), utt


async def test_negated_confirmation_rejected():
    s = await full_session()
    d = s.recap()["digest"]
    for utt in ["don't confirm", "hold on, do not confirm", "sounds good"]:
        with pytest.raises(ValueError, match="confirm"):
            s.confirm(utt, d)
    assert s.frozen is None


async def test_stale_recap_rejected():
    utt = "max hold 12 hours"
    s = session({R1: R1_OUT, R2: R2_OUT, utt: {"fields": [F("exit.max_hold_hours", 12, "max hold 12 hours")], "other_requests": []}})
    await s.submit(R1, 1)
    await s.submit(R2, 2)
    d = s.recap()["digest"]
    await s.submit(utt, 2)  # changed after the recap
    with pytest.raises(ValueError, match="recap is stale"):
        s.confirm("confirm", d)
    m = s.confirm("confirm", s.recap()["digest"])
    assert m.body.exit.max_hold_hours == 12


async def test_hash_deterministic():
    a, b = await full_session(), await full_session()
    ma, mb = a.confirm("confirm", a.recap()["digest"]), b.confirm("confirm", b.recap()["digest"])
    assert ma.body == mb.body
    assert ma.hash != mb.hash or ma.mandate_id == mb.mandate_id  # mandate_id is random and covered by the hash
    assert dlg.compute_hash(ma.mandate_id, ma.frozen_at, ma.expires_at, mb.body.model_dump(mode="json")) == ma.hash


# ---------------- API ----------------
def test_api_flow(monkeypatch):
    from backend import app as appmod
    monkeypatch.setattr(appmod, "new_session", lambda: session({R1: R1_OUT, R2: R2_OUT}))
    c = TestClient(appmod.app)
    c.post("/mandate/reset")
    r = c.post("/mandate/fields", json={"utterance": R1, "round": 1}).json()
    assert r["missing_required"] == ["exit.take_profit_ladder"]
    r2 = c.post("/mandate/fields", json={"utterance": R2, "round": 2}).json()
    assert r2["ready"] and r2["speak"].endswith('say "confirm".') and "Here is the mandate recap:" not in r2["speak"]
    assert "Here is the mandate recap:" in r2["recap"]["text"]
    dr = c.get("/mandate/draft").json()
    assert dr["frozen"] is False and dr["recap"]["ready"]
    assert c.post("/mandate/confirm", json={"utterance": "don't confirm", "recap_digest": dr["recap"]["digest"]}).status_code == 400
    ok = c.post("/mandate/confirm", json={"utterance": "confirm", "recap_digest": dr["recap"]["digest"], "start_pipeline": False})
    assert ok.status_code == 200 and ok.json()["mandate"]["hash"].startswith("sha256:")
    assert ok.json()["speak"].startswith("Mandate frozen, ID ")
    # the session stays amendable: saying the same values again changes nothing and keeps the running mandate
    again = c.post("/mandate/fields", json={"utterance": R1, "round": 1})
    assert again.status_code == 200 and "Nothing changed yet" in again.json()["speak"] and again.json()["amending"] is False
    assert c.get("/mandate/draft").json()["frozen"] is True


async def test_slippage_percent_converted_to_bps_by_code():
    utt = "buy slippage 2.5%"
    s = session({utt: {"fields": [F("exec.max_slippage_bps.buy", 2.5, "buy slippage 2.5%")], "other_requests": []}})
    r = await s.submit(utt, 2)
    assert r.outcomes[0].status == "accepted" and s.draft["exec"]["max_slippage_bps"]["buy"] == 250


# ---------------- fee cap default / dollars ----------------
FUNDS = "I want 50 dollars total, at most 10 per trade"
FUNDS_OUT = {"fields": [F("budget.total_usdc", 50, "50 dollars total"), F("budget.per_trade_usdc", 10, "at most 10 per trade")],
             "other_requests": []}
LOOKS_GOOD = {"fields": [], "other_requests": []}
EXITS = "stop loss at 30 percent, sell half at 2x"
EXITS_OUT = {"fields": [F("exit.stop_loss_pct", 30, "stop loss at 30 percent"),
                        F("exit.take_profit_ladder", [{"multiple": 2, "sell_pct": 50}], "sell half at 2x")], "other_requests": []}


def price(p):
    async def f():
        return p
    return f


def fee_session(table, sol_usd=None, judge=None):
    s = session(table, judge)
    s.sol_usd = sol_usd
    return s


async def test_fee_default_when_omitted():
    s = fee_session({FUNDS: FUNDS_OUT})
    r = await s.submit(FUNDS, 1)
    assert s.draft["exec"]["max_fee_sol"] == 0.002 and r.fee_default_applied
    assert "fee cap set to the default 0.002 SOL" in r.speak
    assert r.missing_required == ["exit.take_profit_ladder"]
    assert "SOL" not in dlg.ROUND_QUESTIONS[1].split("(")[0]          # round 1 no longer asks for a SOL fee


async def test_fee_default_announced_once():
    s = fee_session({FUNDS: FUNDS_OUT, "looks good": LOOKS_GOOD})
    await s.submit(FUNDS, 1)
    r = await s.submit("looks good", 2)
    assert not r.fee_default_applied and "default 0.002" not in r.speak


@pytest.mark.parametrize("utt,quote,value,usd", [
    ("max 20 cents in fees", "max 20 cents in fees", 0.2, 0.2),
    ("max 20 cents in fees", "max 20 cents in fees", 20, 0.2),          # extractor copied the cent number: code fixes it
    ("fees at most 0.5 dollars", "fees at most 0.5 dollars", 0.5, 0.5),
    ("fees at most $0.30", "fees at most $0.30", 0.3, 0.3),
    ("fees no more than 25 cents per trade", "fees no more than 25", 25, 0.25),   # currency right after the quote
])
async def test_fee_in_dollars_converted_with_live_price(utt, quote, value, usd):
    j = FakeJudge()
    s = fee_session({utt: {"fields": [F("exec.max_fee_sol", value, quote)], "other_requests": []}}, price(150.0), j)
    r = await s.submit(utt, 1)
    o = r.outcomes[0]
    assert o.status == "accepted", o.reason
    assert s.draft["exec"]["max_fee_sol"] == round(usd / 150.0, 6)
    assert s.draft["exec"]["max_fee_sol"] != value                      # never stored as "0.2 SOL"
    assert j.calls[0][1:] == ("max fee per trade (USD)", usd)           # JEV judges what the user actually said
    assert "per SOL" in r.speak and not r.fee_default_applied


async def test_fee_in_sol_unchanged():
    utt = "fees no more than 0.002 SOL"
    s = fee_session({utt: {"fields": [F("exec.max_fee_sol", 0.002, utt)], "other_requests": []}}, price(150.0))
    r = await s.submit(utt, 1)
    assert r.outcomes[0].status == "accepted" and s.draft["exec"]["max_fee_sol"] == 0.002


async def test_fee_in_dollars_without_price_does_not_guess():
    utt = "max 20 cents in fees"
    for src in (None, price(None)):
        s = fee_session({utt: {"fields": [F("exec.max_fee_sol", 0.2, utt)], "other_requests": []}}, src)
        r = await s.submit(utt, 1)
        assert r.outcomes[0].status == "unclear" and s.draft["exec"]["max_fee_sol"] is None
        assert not r.fee_default_applied                                # no default over this attempt
        assert "couldn't get a live SOL price" in r.speak and "default 0.002 SOL" in r.speak


async def test_fee_in_dollars_over_hard_cap_rejected():
    utt = "fees at most 2 dollars"
    s = fee_session({utt: {"fields": [F("exec.max_fee_sol", 2, utt)], "other_requests": []}}, price(150.0))
    r = await s.submit(utt, 1)
    assert r.outcomes[0].status == "rejected" and "0.01" in r.outcomes[0].reason
    assert s.draft["exec"]["max_fee_sol"] is None and not r.fee_default_applied


async def test_rejected_sol_fee_not_overwritten_by_default():
    utt = "fees at most 0.05 SOL"
    s = fee_session({utt: {"fields": [F("exec.max_fee_sol", 0.05, utt)], "other_requests": []}})
    r = await s.submit(utt, 1)
    assert r.outcomes[0].status == "rejected" and s.draft["exec"]["max_fee_sol"] is None and not r.fee_default_applied


def test_extract_prompt_teaches_fee_currency():
    assert "dollars or cents" in dlg.EXTRACT_SYSTEM and "code converts to SOL" in dlg.EXTRACT_SYSTEM


# ---------------- coin-filter review step ----------------
async def test_filter_review_after_funds():
    s = fee_session({FUNDS: FUNDS_OUT})
    r = await s.submit(FUNDS, 1)
    assert r.stage == "filters"
    assert 'My proposed coin filters are on the right; change any of them, or say "looks good"' in r.speak
    assert "stop loss" in r.speak.split("My proposed")[1]


@pytest.mark.parametrize("ok", ["looks good", "Looks good to me.", "keep these", "no changes", "that's good",
                                "the defaults are fine", "keep the defaults", "don't change anything"])
async def test_looks_good_then_asks_for_exits(ok):
    s = fee_session({FUNDS: FUNDS_OUT, ok: LOOKS_GOOD})
    await s.submit(FUNDS, 1)
    r = await s.submit(ok, 2)
    assert r.stage == "exits" and s.filters_reviewed, ok
    assert r.speak == "Great, keeping these filters. What stop loss and take profit do you want?"


@pytest.mark.parametrize("bad", ["doesn't look good", "not good", "hmm", "I don't like these"])
async def test_not_acceptance_stays_in_review(bad):
    s = fee_session({FUNDS: FUNDS_OUT, bad: LOOKS_GOOD})
    await s.submit(FUNDS, 1)
    r = await s.submit(bad, 2)
    assert r.stage == "filters" and not s.filters_reviewed, bad


async def test_filter_change_stays_in_review_then_looks_good():
    chg = "set min liquidity to 80000 dollars"
    s = fee_session({FUNDS: FUNDS_OUT, chg: {"fields": [F("filters.min_liquidity_usd", 80000, "min liquidity to 80000 dollars")],
                                             "other_requests": []}, "looks good": LOOKS_GOOD, EXITS: EXITS_OUT})
    await s.submit(FUNDS, 1)
    r = await s.submit(chg, 2)
    assert r.stage == "filters" and s.draft["filters"]["min_liquidity_usd"] == 80000
    assert r.speak == "Got it: min liquidity $80k. Anything else to change, or does it look good?"
    r = await s.submit("looks good", 2)
    assert r.stage == "exits"
    r = await s.submit(EXITS, 2)
    assert r.stage == "ready" and r.missing_required == []
    assert r.speak == ('Got it: stop loss 30%, take-profit at 2x sell 50%. '
                       'The full settings are on the right; if they look right, say "confirm".')
    rc = s.recap()
    assert rc["ready"] and "liquidity at least $80k" in rc["text"] and rc["text"] not in r.speak


async def test_looks_good_blocked_by_unclear_field():
    utt = "looks good, but min holders maybe 500"
    s = fee_session({FUNDS: FUNDS_OUT, utt: {"fields": [F("filters.min_holders", 500, "min holders maybe 500")], "other_requests": []}})
    await s.submit(FUNDS, 1)
    r = await s.submit(utt, 2)
    assert r.stage == "filters" and r.outcomes[0].status == "unclear"


async def test_jump_ahead_with_exits_from_review():
    s = fee_session({FUNDS: FUNDS_OUT, EXITS: EXITS_OUT})
    await s.submit(FUNDS, 1)
    r = await s.submit(EXITS, 2)
    assert r.stage == "ready" and r.missing_required == [] and s.recap()["ready"]
    assert s.draft["filters"] == default_body().model_dump(mode="json")["filters"]   # filters stay as proposed


async def test_everything_in_one_breath():
    utt = "50 dollars total, at most 10 per trade, stop loss at 30 percent, sell half at 2x"
    s = fee_session({utt: {"fields": FUNDS_OUT["fields"] + EXITS_OUT["fields"], "other_requests": []}})
    r = await s.submit(utt, 1)
    assert r.stage == "ready" and r.fee_default_applied and 'say "confirm"' in r.speak
    m = s.confirm("confirm", s.recap()["digest"])
    assert m.body.exec.max_fee_sol == 0.002 and verify_hash(m)


async def test_looks_good_before_funds_is_ignored():
    s = fee_session({"looks good": LOOKS_GOOD})
    r = await s.submit("looks good", 1)
    assert r.stage == "funds" and not s.filters_reviewed


async def test_question_follows_stage():
    s = fee_session({FUNDS: FUNDS_OUT, "looks good": LOOKS_GOOD})
    await s.submit(FUNDS, 1)
    await s.submit("looks good", 2)
    assert s.extractor.calls[1][1].startswith("The system just asked the user: " + dlg.STAGE_QUESTIONS["filters"])
    assert s.question() == dlg.STAGE_QUESTIONS["exits"]


# ---------------- her real utterances (fillers, take-profit percent, cents, "start researching") ----------------
E0 = {"fields": [], "other_requests": []}
FUNDS50 = "50 dollars total, 10 per trade"
FUNDS50_OUT = {"fields": [F("budget.total_usdc", 50, "50 dollars total"), F("budget.per_trade_usdc", 10, "10 per trade")],
               "other_requests": []}


async def _to_exits(table):
    s = session({FUNDS50: FUNDS50_OUT, "looks good": E0, **table})
    await s.submit(FUNDS50, 1)
    r = await s.submit("looks good", 1)
    assert r.stage == "exits"
    return s


async def test_fillers_are_not_vague_and_tp_percent_is_a_multiple():
    u = "I guess, um, stop loss would be 20% and then take profits would be 30%."
    # the LLM slipped and put 30 as the multiple: code turns "+30%" into 1.3
    s = await _to_exits({u: {"fields": [F("exit.stop_loss_pct", 20, "stop loss would be 20%"),
                                        F("exit.take_profit_ladder", [{"multiple": 30, "sell_pct": 100}], "take profits would be 30%")],
                             "other_requests": []}})
    r = await s.submit(u, 1)
    assert [o.status for o in r.outcomes] == ["accepted", "accepted"], r.outcomes
    assert s.draft["exit"]["stop_loss_pct"] == 20
    assert s.draft["exit"]["take_profit_ladder"] == [{"multiple": 1.3, "sell_pct": 100}]
    assert r.stage == "ready" and "take profit at +30% sell 100%" in r.speak
    assert "at +30% sell 100%" in s.recap()["text"]


@pytest.mark.parametrize("u,quote", [("I guess stop loss would be 20%", "stop loss would be 20%"),
                                     ("um, let's just say stop loss 20%", "stop loss 20%"),
                                     ("I think stop loss 20 percent", "stop loss 20 percent")])
async def test_filler_with_definite_number_accepted(u, quote):
    s = await _to_exits({u: {"fields": [F("exit.stop_loss_pct", 20, quote)], "other_requests": []}})
    r = await s.submit(u, 1)
    assert r.outcomes[0].status == "accepted" and s.draft["exit"]["stop_loss_pct"] == 20


@pytest.mark.parametrize("u,quote", [("stop loss maybe 20%", "stop loss maybe 20%"), ("stop loss around 20%", "stop loss around 20%"),
                                     ("around 20% stop loss", "around 20% stop loss")])
async def test_real_hedges_still_rejected(u, quote):
    s = await _to_exits({u: {"fields": [F("exit.stop_loss_pct", 20, quote)], "other_requests": []}})
    r = await s.submit(u, 1)
    assert r.outcomes[0].status == "unclear" and "vague" in r.outcomes[0].reason
    assert s.draft["exit"]["stop_loss_pct"] is None


async def test_take_profit_percent_misrouted_to_stop_loss_is_fixed():
    u = "I said twenty percent stop loss and thirty percent take profit"
    s = await _to_exits({u: {"fields": [F("exit.stop_loss_pct", 20, "twenty percent stop loss"),
                                        F("exit.stop_loss_pct", 30, "thirty percent take profit")], "other_requests": []}})
    await s.submit(u, 1)
    assert s.draft["exit"]["stop_loss_pct"] == 20
    assert s.draft["exit"]["take_profit_ladder"] == [{"multiple": 1.3, "sell_pct": 100}]


@pytest.mark.parametrize("llm", [{"fields": [F("exit.stop_loss_pct", 30, "30%")], "other_requests": []}, E0])
async def test_bare_percent_fills_take_profit_not_stop_loss(llm):
    s = await _to_exits({"stop loss 20%": {"fields": [F("exit.stop_loss_pct", 20, "stop loss 20%")], "other_requests": []}, "30%.": llm})
    r = await s.submit("stop loss 20%", 1)
    assert "I still need your take profit" in r.speak
    r = await s.submit("30%.", 1)
    assert s.draft["exit"]["stop_loss_pct"] == 20
    assert s.draft["exit"]["take_profit_ladder"] == [{"multiple": 1.3, "sell_pct": 100}]
    assert r.stage == "ready"


async def test_sell_half_at_2x_unchanged_and_tp_percent_with_half():
    s = await _to_exits({EXITS: EXITS_OUT})
    await s.submit(EXITS, 1)
    assert s.draft["exit"]["take_profit_ladder"] == [{"multiple": 2, "sell_pct": 50}]
    assert "at 2x sell 50%" in s.recap()["text"]
    u = "take profit 50 percent, sell half"
    s2 = await _to_exits({u: {"fields": [F("exit.take_profit_ladder", [{"multiple": 1.5, "sell_pct": 50}], "take profit 50 percent, sell half")],
                              "other_requests": []}})
    await s2.submit(u, 1)
    assert s2.draft["exit"]["take_profit_ladder"] == [{"multiple": 1.5, "sell_pct": 50}]


async def test_budget_cents():
    u1, u2 = "for this time, let's just do one cent per trade at most", "let's just say five cents"
    # LLM slips: "one cent" -> 1 (dollar); and nothing for "five cents"
    s = session({u1: {"fields": [F("budget.per_trade_usdc", 1, "one cent per trade at most")], "other_requests": []}, u2: E0})
    r = await s.submit(u1, 1)
    assert s.draft["budget"]["per_trade_usdc"] == 0.01 and "at most 1 cent per trade" in r.speak
    assert "I still need your total budget" in r.speak            # says what was noted and what is missing
    r = await s.submit(u2, 1)
    assert s.draft["budget"]["total_usdc"] == 0.05 and "5 cents total" in r.speak and r.stage == "filters"


@pytest.mark.parametrize("u", ["so far it looks good to me. Uh, just start our researching.", "let's start", "go ahead and research",
                               "OK, start researching"])
async def test_start_researching_is_not_a_hard_rule_change(u):
    s = session({FUNDS50: FUNDS50_OUT,
                 u: {"fields": [], "other_requests": [{"description": "start research", "quote": u.split(". ")[-1]}]}})
    await s.submit(FUNDS50, 1)
    r = await s.submit(u, 1)
    assert "hard rule" not in r.speak and all(o.status != "rejected" for o in r.outcomes)
    assert r.stage == "exits" and "What stop loss and take profit" in r.speak


def test_extract_prompt_teaches_new_rules():
    p = dlg.EXTRACT_SYSTEM
    assert "NEVER the stop loss" in p and "1.3" in p and "five cents" in p.lower()
    assert "start researching" in p.lower()
    assert "i guess" not in dlg.VAGUE_MARKERS_EN and "maybe" in dlg.VAGUE_MARKERS_EN


async def test_bare_percent_answer_skips_a_doubtful_judge():
    # real run: the extractor got take profit right but the judge scored the bare "30%." at 0.30
    j = FakeJudge(low_if=("30%",))
    s = session({FUNDS50: FUNDS50_OUT, "looks good": E0, "stop loss 20%": {"fields": [F("exit.stop_loss_pct", 20, "stop loss 20%")], "other_requests": []},
                 "30%.": {"fields": [F("exit.take_profit_ladder", [{"multiple": 1.3, "sell_pct": 100}], "30%")], "other_requests": []}}, j)
    for u in (FUNDS50, "looks good", "stop loss 20%"):
        await s.submit(u, 1)
    r = await s.submit("30%.", 1)
    assert r.outcomes[0].status == "accepted" and s.draft["exit"]["take_profit_ladder"] == [{"multiple": 1.3, "sell_pct": 100}]
    # a hedged bare answer still goes through the vague check
    s2 = session({FUNDS50: FUNDS50_OUT, "looks good": E0, "stop loss maybe 20%": {"fields": [F("exit.stop_loss_pct", 20, "stop loss maybe 20%")], "other_requests": []}})
    await s2.submit(FUNDS50, 1)
    await s2.submit("looks good", 1)
    assert (await s2.submit("stop loss maybe 20%", 1)).outcomes[0].status == "unclear"


async def test_five_cents_misread_as_fee_goes_to_the_missing_total():
    u1, u2 = "one cent per trade", "let's just say five cents"
    s = session({u1: {"fields": [F("budget.per_trade_usdc", 0.01, "one cent per trade")], "other_requests": []},
                 u2: {"fields": [F("exec.max_fee_sol", None, u2)], "other_requests": []}})
    await s.submit(u1, 1)
    r = await s.submit(u2, 1)
    assert s.draft["budget"]["total_usdc"] == 0.05 and "5 cents total" in r.speak and "fee" not in r.speak.split(".")[0]
    # a real fee answer is untouched
    s2 = session({u1: {"fields": [F("budget.per_trade_usdc", 0.01, "one cent per trade")], "other_requests": []},
                  "fees 0.001 SOL": {"fields": [F("exec.max_fee_sol", 0.001, "fees 0.001 SOL")], "other_requests": []}})
    await s2.submit(u1, 1)
    await s2.submit("fees 0.001 SOL", 1)
    assert s2.draft["exec"]["max_fee_sol"] == 0.001 and s2.draft["budget"]["total_usdc"] is None


async def test_bare_number_with_two_budget_gaps_still_judged():
    j = FakeJudge(low_if=("10",))
    s = session({"10": {"fields": [F("budget.total_usdc", 10, "10")], "other_requests": []}}, j)
    r = await s.submit("10", 1)
    assert r.outcomes[0].status == "unclear" and j.calls
