"""Shadow listener (旁听通道): deterministic pre-parser, prework, and the never-commit-on-partial invariant."""
from __future__ import annotations

import asyncio
import copy

import pytest

from backend import app as appmod
from backend.mandate.speculative import SpecCache
from backend.tests.test_m6_unit import _desk_orch, client, types, wait  # noqa: F401  (fixture)
from backend.voice.shadow import ShadowListener, filter_diff, gate_speech, parse_intent, side_talk

RUN = {"frozen": True, "desk_open": [1, 2, 3], "pending_codes": ["47"], "round_running": True}


@pytest.mark.parametrize("text,kind,label", [
    ("Now I want another research round with market cap below 300,000.", "filter_change", "market cap < $300k"),
    ("market cap under three hundred thousand", "filter_change", "market cap < $300k"),
    ("under 1.5 million market cap", "filter_change", "market cap < $1.5M"),
    ("make the minimum liquidity twenty thousand dollars", "filter_change", "liquidity ≥ $20k"),
    ("at least two hundred holders", "filter_change", "holders ≥ 200"),
    ("top ten below 30 percent", "filter_change", "top-10 ≤ 30%"),
    ("stop the research", "stop_research", "stop the research"),
    ("sell everything", "sell_all", "sell all positions"),
    ("approve forty seven", "approve", "approve card 47"),
    ("coin two, five dollars", "coin_ref", "coin 2"),
    ("another round", "new_round", "a new research round"),
    ("stop loss at 20 percent", "guard_change", "a settings change"),
])
def test_pre_parser(text, kind, label):
    it = parse_intent(text, RUN)
    assert it is not None and (it.kind, it.label) == (kind, label)


def test_pre_parser_is_conservative():
    assert parse_intent("um", RUN) is None
    assert parse_intent("[System update] Card 47: coin 1", RUN) is None            # broadcasts are never the user's words
    assert parse_intent("don't approve 47", RUN) is None or parse_intent("don't approve 47", RUN).kind != "approve"
    assert parse_intent("approve 52", RUN) is None or parse_intent("approve 52", RUN).kind != "approve"   # not a pending code
    assert parse_intent("stop the research", {**RUN, "round_running": False}) is None
    assert parse_intent("coin 9", RUN) is None                                     # not on the desk
    assert filter_diff("market cap under 30") == []                                # no unit: leave it to the extractor
    assert side_talk("Hey mom, dinner ready later?") and side_talk("Honey, can you grab the door?")
    assert not side_talk("hey, market cap below 300k") and not side_talk("approve 47")


def _snapshot(c):
    s = appmod.app.state.session
    o = appmod.app.state.orch
    eng = o.engine
    return (copy.deepcopy(s.draft), s.frozen.model_dump(mode="json") if s.frozen else None, s.amending, o.round_no,
            o.mandate.mandate_id if o.mandate else None, len(eng.cards) if eng else 0, len(eng.created) if eng else 0,
            [c.status for c in eng.cards.values()] if eng else [],
            [e.type for e in appmod.app.state.bus.history if e.type != "pipeline_status"])


def test_partials_never_commit_anything(client):
    """However many partial / final words arrive, nothing changes: no mandate edit, no round, no card, no approval,
    no exit, no payment. Only the finalized tool call commits."""
    c = client
    o = _desk_orch(client)
    s = appmod.app.state.session
    s.spec = SpecCache()
    appmod.app.state.shadow = appmod._new_shadow()
    r = c.post("/desk/order", json={"utterance": "coin 1, 5 dollars", "wait": True}).json()
    code = r["cards"][0]["code"]
    before = _snapshot(c)
    words = ["another round with market", "another round with market cap below 300k", f"approve {code}", "sell all",
             "sell everything now", "stop the research", "coin 2 and coin 3, five dollars each", "stop loss at 10 percent",
             "confirm", "start research", "yes go ahead"]
    labels = set()
    for i, w in enumerate(words):
        for final in (False, True):
            out = c.post("/voice/partial", json={"text": w, "final": final, "seq": 2 * i + final}).json()
            assert out["ok"]
            labels.add((out["hearing"] or {}).get("label"))
    after = _snapshot(c)
    assert after == before
    assert o.engine.responded == [] if hasattr(o.engine, "responded") else True
    assert not appmod.app.state.bus.of("research_payment") and not appmod.app.state.bus.of("emergency_exit")
    assert {"market cap < $300k", f"approve card {code}", "sell all positions", "coin 2, 3"} <= labels   # (stop: no round running)
    c.post("/voice/partial", json={"text": "market cap below 300k", "final": True, "seq": 99})
    assert c.get("/state").json()["hearing"]["label"] == "market cap < $300k"     # the strip has something to show
    # the commit still needs the real tool call with the user's words + code
    rr = c.post("/voice/card-respond", json={"action": "approve", "code": code, "utterance": f"approve {code}", "wait": True}).json()
    assert rr["ok"] and c.get("/state").json()["hearing"] is None                  # a tool call clears the staged reading


def test_speculative_extraction_is_used_on_match_and_discarded_on_mismatch(client):
    c = client
    _desk_orch(client)
    s = appmod.app.state.session
    s.spec = SpecCache()
    appmod.app.state.shadow = appmod._new_shadow()
    u = "Another research round with market cap below 300,000."
    s.extractor.table[u] = {"fields": [{"path": "filters.market_cap_usd.max", "value": 300000, "quote": "market cap below 300,000"}],
                            "other_requests": [{"description": "another research round", "quote": "Another research round"}]}
    n0 = len(s.extractor.calls)
    out = c.post("/voice/partial", json={"text": u, "final": True, "seq": 1}).json()
    assert out["hearing"]["label"] == "market cap < $300k" and out["side_talk"] is False
    wait(c, lambda: len(s.extractor.calls) == n0 + 1)                              # primed in the background
    assert s.frozen is not None and s.frozen.body.filters.market_cap_usd[1] != 300000   # ... but nothing changed
    r = c.post("/mandate/fields", json={"utterance": u, "round": 2}).json()
    assert r["applied"] is True
    assert len(s.extractor.calls) == n0 + 1 and s.spec.stats["hits"] == 1         # the tool call reused it: no second LLM call
    wait(c, lambda: len(appmod.app.state.bus.of("desk_ready")) >= 2)
    # mismatch: words primed, but the tool call carries different words -> normal extraction, primed result unused
    u2, u3 = "market cap below 200k please", "market cap below 250k please"
    for x, v in ((u2, 200000), (u3, 250000)):
        s.extractor.table[x] = {"fields": [{"path": "filters.market_cap_usd.max", "value": v, "quote": x.replace(" please", "")}],
                                "other_requests": []}
    c.post("/voice/partial", json={"text": u2, "final": True, "seq": 2})
    wait(c, lambda: s.spec.stats["primed"] == 2)
    r = c.post("/mandate/fields", json={"utterance": u3, "round": 2}).json()
    assert s.spec.stats["misses"] >= 1
    assert appmod.app.state.session.draft["filters"]["market_cap_usd"][1] == 250000   # the words of the tool call win
    assert appmod.app.state.shadow.stats["mismatched"] >= 1


def test_no_priming_for_confirm_or_desk_words_or_when_blocked():
    calls = []

    class S:
        def prime_extract(self, t):
            calls.append(t)
            return True
    ctx = {"frozen": False}
    sh = ShadowListener(lambda: ctx, lambda: S())
    sh.hear("Confirm.", True, 1)
    sh.hear("Hmm, let me think.", True, 1)                         # filler segment (Realtime finalizes per phrase)
    sh.hear("50 dollars total, 10 per trade", False, 2)            # partial: never primed
    assert calls == []
    sh.hear("50 dollars total, 10 per trade", True, 3)
    assert calls == ["50 dollars total, 10 per trade"]
    ctx.update(frozen=True, desk_open=[1], amend_blocked=False)
    sh.hear("coin 1, 5 dollars", True, 4)                           # desk order: deterministic already, nothing to prime
    ctx["amend_blocked"] = True
    sh.hear("market cap below 300k", True, 5)
    assert calls == ["50 dollars total, 10 per trade"]
    assert sh.hear("older partial", False, 1)["stale"] is True      # out-of-order partials are ignored


def test_gate_speech_for_mandate_answers_not_desk_or_partials():
    setup, running = {"frozen": False}, {"frozen": True, "desk_open": [1], "pending_codes": ["47"]}
    funds = parse_intent("I want to put five cents into this session, and one cent per trade.", setup)
    assert funds and funds.kind == "mandate_answer"
    assert gate_speech(True, "I want to put five cents into this session, and one cent per trade.", funds, setup)
    assert not gate_speech(False, "I want to put five cents into this session, and one cent per trade.", funds, setup)
    looks = parse_intent("They all look good to me.", setup)
    assert looks and looks.kind == "mandate_answer"
    assert gate_speech(True, "They all look good to me.", looks, setup)
    assert gate_speech(True, "Confirm.", parse_intent("Confirm.", setup), setup)
    filt = parse_intent("market cap below 300,000", running)
    assert gate_speech(True, "market cap below 300,000", filt, running)
    assert not gate_speech(True, "coin 1, 5 dollars", parse_intent("coin 1, 5 dollars", running), running)
    assert not gate_speech(True, "um", parse_intent("um", setup), setup)
    ctx = {"frozen": False}
    sh = ShadowListener(lambda: ctx, lambda: None, speculate_extract=False)
    out = sh.hear("five cents total, one cent per trade", True, 1)
    assert out["gate_speech"] is True and out["ok"] is True


async def test_spec_cache_take_waits_for_running_call_and_expires():
    t = [0.0]
    sc = SpecCache(ttl_s=5, now=lambda: t[0])
    gate = asyncio.Event()

    async def call(a, b):
        await gate.wait()
        return {"fields": [], "other_requests": [], "who": a + b}
    assert sc.prime("s", "u", call) and not sc.prime("s", "u", call)
    t[0] = 1.0
    task = asyncio.ensure_future(sc.take("s", "u"))
    await asyncio.sleep(0)
    gate.set()
    assert (await task)["who"] == "su" and sc.stats["hits"] == 1 and sc.stats["saved_ms"] == [1000]
    assert await sc.take("s", "u") is None                          # one use only
    sc.prime("s", "v", call)
    t[0] = 10.0
    assert await sc.take("s", "v") is None and sc.stats["expired"] == 1
