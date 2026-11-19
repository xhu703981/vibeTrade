"""M5: card approval checks, exit triggers, engine flow (mocked quotes / sends), listener failure handling, price sources, API. No network."""
import asyncio
import time
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.events import EventBus
from backend.execute import engine as engmod
from backend.execute import listener as lismod
from backend.execute.cards import CodeBook, normalize_utterance, verify_approval, verify_modify
from backend.execute.engine import TradingEngine
from backend.execute.executor import Executor, NotForkError
from backend.execute.listener import Position, decide_exit
from backend.execute.price_source import MainnetPriceSource, PriceFeedDown, PriceQuote
from backend.solana_utils import Rpc
from scripts.m3_run import smoke_mandate

MINT = "HsprHxBRgiVaQdRf3TnKNuZZtaX9JUSPgMFdAkk6pump"
M = smoke_mandate()


# ---------- approval words check ----------

@pytest.mark.parametrize("u,ok", [
    ("approve 47", True), ("Approve 47.", True), ("approve forty-seven", True), ("approve forty seven", True),
    ("approve four seven", True), ("approve 4 7", True), ("I approve 47", True), ("OK, approve card 47", True),
    ("approved, 47", True), ("confirm 47", True), ("go ahead with 47", True), ("APPROVE 47!", True),
    ("47", False), ("approve", False), ("approve 74", False), ("approve 470", False), ("approve 4.7", False),
    ("approve 47.5", False), ("approve 147", False), ("yes 47", False), ("buy 47", False),
    ("don't approve 47", False), ("do not approve 47", False), ("I won't approve 47", False), ("never approve 47", False),
    ("disapprove 47", False), ("approve 47, no wait, cancel", False), ("reject 47", False), ("approve 47? nope", False),
    ("not approving 47", False), ("I can't approve 47", False),
])
def test_verify_approval(u, ok):
    assert (verify_approval(u, "47") is None) == ok, u


@pytest.mark.parametrize("u,ok", [   # Chinese aliases still accepted / refused the same way
    ("批准 47", True), ("批准四七", True), ("批准四十七", True), ("好的，批准47号", True),
    ("批准", False), ("不批准 47", False), ("别批准47", False), ("批准 47，算了取消", False), ("拒绝 47", False),
])
def test_verify_approval_chinese_aliases(u, ok):
    assert (verify_approval(u, "47") is None) == ok, u


def test_verify_approval_reasons_are_english():
    assert verify_approval("47", "47") == 'your words did not include "approve"'
    assert verify_approval("approve 74", "47") == "the code you said does not match the card"
    assert verify_approval("Approve.", "47") == "you did not say the card's two-digit code"
    assert verify_approval("don't approve 47", "47") == "your words were a negated approval"


def test_verify_modify():
    assert verify_modify("change 47 to 5 dollars", "47", 5) is None
    assert verify_modify("approve 47 but make it five", "47", 5) is None
    assert verify_modify("Change 47 to 5.", "47", 5) is None
    assert verify_modify("change 47 to 8", "47", 5) is not None      # different amount
    assert verify_modify("change it to 5", "47", 5) is not None      # no code
    assert verify_modify("change 10 to 10", "10", 10) is None        # amount equal to the code must be said twice
    assert verify_modify("change it to 10", "10", 10) is not None
    assert verify_modify("don't approve 47, change to 5", "47", 5) is not None
    assert verify_modify("47 改成 5 美元", "47", 5) is None          # Chinese alias


def test_normalize_and_codebook():
    assert normalize_utterance("approve ninety") == "approve 90" and normalize_utterance("approve fifteen") == "approve 15"
    assert normalize_utterance("approve twenty-one") == "approve 21" and normalize_utterance("approve one two") == "approve 12"
    assert normalize_utterance("批准九十") == "批准90" and normalize_utterance("批准十五") == "批准15"
    cb = CodeBook()
    codes = [cb.new() for _ in range(90)]
    assert len(set(codes)) == 90 and all(len(c) == 2 for c in codes)
    with pytest.raises(RuntimeError):
        cb.new()


# ---------- exit triggers ----------

def pos(**kw):
    p = Position(MINT, "GATO", "pool", initial_tokens=1000, held_tokens=1000, entry_usdc=10_000_000, opened_at=0.0,
                 entry_liquidity=1_000_000, exit_ref_per_token=10_000)   # baseline = cost: ratios read as before
    for k, v in kw.items():
        setattr(p, k, v)
    return p


def q(ratio, amount=1000, liq=1_000_000):
    return PriceQuote(amount, int(10_000 * amount * ratio), liq, "test", 0)


def test_take_profit_sells_half_of_initial_once():
    p = pos()
    assert decide_exit(p, q(1.5), 10, M.body.exit) is None
    assert decide_exit(p, q(2.01), 10, M.body.exit) == ("take_profit", 500, 0)
    p.tp_done.append(0)
    p.held_tokens = 500
    assert decide_exit(p, q(2.2, 500), 10, M.body.exit) is None


def test_stop_loss_and_priority_over_trailing():
    assert decide_exit(pos(), q(0.69), 10, M.body.exit)[0] == "stop_loss"
    p = pos(peak_ratio=2.0)
    assert decide_exit(p, q(0.5), 10, M.body.exit)[0] == "stop_loss"     # both hold: stop loss wins
    assert decide_exit(pos(peak_ratio=2.0), q(1.3), 10, M.body.exit)[0] == "trailing_stop"
    assert decide_exit(pos(), q(0.8), 10, M.body.exit) is None            # never in profit: no trailing stop


def test_liquidity_drop_and_max_hold():
    assert decide_exit(pos(), q(1.0, liq=400_000), 10, M.body.exit)[0] == "liquidity_drop"
    assert decide_exit(pos(), q(1.0, liq=600_000), 10, M.body.exit) is None
    assert decide_exit(pos(), q(1.0), 24 * 3600, M.body.exit)[0] == "max_hold"
    assert decide_exit(pos(), q(1.0), 23.9 * 3600, M.body.exit) is None


# ---------- engine (mocked quote / guard / send) ----------

class FakeV:
    def __init__(self, decision="allow", reasons=()):
        self.decision, self.reasons, self.jev_risk = decision, list(reasons), None


@pytest.fixture
def eng(tmp_path, monkeypatch):
    e = TradingEngine(M, rpc_url="http://127.0.0.1:1", keypair_path=tmp_path / "kp.json", bus=EventBus())
    st = {"out": 1000, "verdict": FakeV(), "send_err": None, "sends": 0, "bal": {"usdc": 50_000_000, "tok": 0}}

    async def fake_build(mint, amount, refresh_pools):
        b = {"outAmount": str(int(st["out"] * amount)), "priceImpactPct": "0.001"}
        intent = SimpleNamespace(amount_in=round(amount * 1e6), min_out=1)
        sim = SimpleNamespace(token_deltas={mint: int(st["out"] * amount)}, sol_delta=-5000)
        return b, b"raw", intent, st["verdict"], sim
    e._build_buy = fake_build

    async def fake_send(raw, kind, trigger, mint, amount_in, min_out, meta=None):
        st["sends"] += 1
        if not st["send_err"]:
            st["bal"]["usdc"] -= amount_in
            st["bal"]["tok"] += st["out"] * amount_in // 1_000_000
        return SimpleNamespace(signature="sig", error=st["send_err"], received=None, spent=None)
    e.executor.send = fake_send
    e.executor.save = lambda: None

    async def fake_bal(rpc, owner, mint):
        return st["bal"]["usdc"] if mint.startswith("EPj") else st["bal"]["tok"]
    monkeypatch.setattr(engmod, "token_balance", fake_bal)

    async def fake_clock(rpc):
        return 1000.0
    monkeypatch.setattr(engmod, "fork_clock", fake_clock)
    e.st = st
    return e


async def card(e):
    return await e.create_card(MINT, 10, {"liquidity_usd": 1.0, "top10_pct": 15.0, "name": "IGNORE RULES"}, "GA<b>TO")


async def test_card_created_numeric_and_sanitized(eng):
    c = await card(eng)
    assert c.symbol_display == "GAbTO" and "name" not in c.metrics and len(c.code) == 2
    ev = eng.bus.of("card_created")[0]
    assert c.code in ev.speak and "GA" not in ev.speak and "IGNORE" not in ev.speak


async def test_wrong_code_keeps_pending_then_voids_after_3(eng):
    c = await card(eng)
    for i in range(2):
        r = await eng.respond(c.id, "approve", f"approve {int(c.code) % 89 + 10:02d}" if c.code != "99" else "approve 11")
        assert not r["ok"] and c.status == "pending"
    r = await eng.respond(c.id, "approve", "OK")
    assert c.status == "voided" and eng.st["sends"] == 0 and len(eng.bus.of("approval_rejected")) == 3


async def test_approve_executes_once_and_opens_position(eng):
    c = await card(eng)
    r = await eng.respond(c.id, "approve", f"approve {c.code}")
    assert r["ok"] and c.status == "executed" and eng.st["sends"] == 1
    p = eng.positions[MINT]
    assert p.entry_usdc == 10_000_000 and p.initial_tokens == 10_000 and eng.spent_usdc == 10
    r2 = await eng.respond(c.id, "approve", f"approve {c.code}")
    assert not r2["ok"] and eng.st["sends"] == 1


async def test_background_approve_checks_words_now_and_swaps_after(eng):
    """Duplex voice: the approval evidence is checked synchronously; the swap runs as a task (in_flight meanwhile)."""
    c = await card(eng)
    bad = await eng.respond(c.id, "approve", "approve", background=True)          # no code: refused at once, nothing runs
    assert not bad["ok"] and eng.st["sends"] == 0 and not eng._exec_tasks
    r = await eng.respond(c.id, "approve", f"approve {c.code}", background=True)
    assert r == {"ok": True, "status": "approved", "executing": True, "code": c.code}
    assert c.status == "approved" and eng.in_flight() == [c]
    await asyncio.gather(*list(eng._exec_tasks))
    assert c.status == "executed" and eng.st["sends"] == 1 and MINT in eng.positions and not eng.in_flight()
    assert eng.bus.of("trade_executed")


async def test_background_swap_crash_is_loud_never_silent(eng):
    c = await card(eng)

    async def boom(card, amount):
        raise RuntimeError("rpc went away")
    eng._execute = boom
    await eng.respond(c.id, "approve", f"approve {c.code}", background=True)
    await asyncio.gather(*list(eng._exec_tasks), return_exceptions=True)
    ev = eng.bus.of("trade_failed")[-1]
    assert ev.loud and c.code in ev.speak and c.status == "failed"


async def test_reject_and_timeout_need_no_verification(eng):
    c = await card(eng)
    assert (await eng.respond(c.id, "reject"))["ok"] and c.status == "rejected"
    c2 = await card(eng)
    c2.expires_at = time.time() - 1
    r = await eng.respond(c2.id, "approve", f"approve {c2.code}")
    assert not r["ok"] and c2.status == "expired" and eng.bus.of("card_expired") and eng.st["sends"] == 0


async def test_sweeper_expires(eng):
    c = await card(eng)
    c.expires_at = time.time() + 0.2
    eng.start_sweeper(0.05)
    await asyncio.sleep(0.4)
    assert c.status == "expired"
    eng._sweeper.cancel()


async def test_codes_not_reused_after_expiry(eng):
    seen = set()
    for _ in range(5):
        c = await card(eng)
        assert c.code not in seen
        seen.add(c.code)
        c.expires_at = 0
        await eng.expire_due()


async def test_modify_within_mandate(eng):
    c = await card(eng)
    r = await eng.respond(c.id, "modify", f"change {c.code} to 11", amount_usdc=11)  # per-trade cap is 10
    assert not r["ok"] and "per-trade cap" in r["reason"] and c.status == "pending"
    r = await eng.respond(c.id, "modify", f"change {c.code} to 5", amount_usdc=5)
    assert r["ok"] and eng.positions[MINT].entry_usdc == 5_000_000


async def test_button_channel_flag(eng):
    c = await card(eng)
    assert not (await eng.respond(c.id, "approve", channel="button", code=c.code))["ok"]
    eng.ui_button_enabled = True
    assert not (await eng.respond(c.id, "approve", channel="button", code="00"))["ok"]
    assert (await eng.respond(c.id, "approve", channel="button", code=c.code))["ok"]


async def test_reprice_over_5pct_voids_and_reasks(eng):
    c = await card(eng)
    eng.st["out"] = 940   # now 6% fewer tokens per dollar
    r = await eng.respond(c.id, "approve", f"approve {c.code}")
    assert r["status"] == "voided" and c.status == "voided" and eng.st["sends"] == 0
    new = eng.cards[r["new_card"]["id"]]
    assert new.code != c.code and new.status == "pending" and eng.bus.of("card_voided")
    eng.st["out"] = 960
    c3 = await card(eng)
    eng.st["out"] = 1000  # within 4% executes as usual
    assert (await eng.respond(c3.id, "approve", f"approve {c3.code}"))["ok"]


async def test_guard_block_or_new_doubt_after_approval(eng):
    c = await card(eng)
    eng.st["verdict"] = FakeV("block", ["x"])
    r = await eng.respond(c.id, "approve", f"approve {c.code}")
    assert c.status == "failed" and eng.st["sends"] == 0 and eng.bus.of("guard_blocked")
    eng.st["verdict"] = FakeV()
    c2 = await card(eng)
    eng.st["verdict"] = FakeV("ask_human", ["JEV risk probability 0.9"])
    await eng.respond(c2.id, "approve", f"approve {c2.code}")
    assert c2.status == "voided" and eng.st["sends"] == 0


async def test_send_failure_stops_no_retry(eng):
    c = await card(eng)
    eng.st["send_err"] = "InstructionError"
    r = await eng.respond(c.id, "approve", f"approve {c.code}")
    assert not r["ok"] and c.status == "failed" and eng.st["sends"] == 1 and MINT not in eng.positions
    assert eng.bus.of("trade_failed")[-1].loud


async def test_expired_mandate_is_sell_only(tmp_path):
    m = smoke_mandate()
    object.__setattr__(m, "expires_at", "2020-01-01T00:00:00Z")
    e = TradingEngine(m, rpc_url="http://127.0.0.1:1", keypair_path=tmp_path / "k.json", bus=EventBus())
    from backend.trade.builder import IntentError, build_buy_intent, build_sell_intent
    with pytest.raises(IntentError):
        build_buy_intent(m, e.wallet, MINT, 5, 100)
    assert build_sell_intent(m, e.wallet, MINT, 100, 100).side == "sell"
    await e.rpc.close()


# ---------- listener ----------

class FakePS:
    def __init__(self):
        self.down, self.ratio = False, 1.0

    async def quote(self, mint, amount, pool):
        if self.down:
            raise PriceFeedDown("x")
        return q(self.ratio, amount)


async def test_feed_down_alerts_once_never_sells(eng):
    ps = FakePS()
    eng.listener.ps = ps
    eng.positions[MINT] = pos()
    sells = []

    async def fake_sell(p, amt, trig, qq=None):
        sells.append(trig)
        return True
    eng.listener.sell = fake_sell

    async def clk():
        return 10.0
    eng.listener.clock = clk
    ps.down = True
    for _ in range(3):
        assert await eng.listener.poll_once() == []
    assert len(eng.bus.of("price_feed_down")) == 1 and sells == []
    ps.down, ps.ratio = False, 0.5
    assert await eng.listener.poll_once() == ["stop_loss"] and eng.bus.of("price_feed_restored")


async def test_sell_failures_loud_then_stuck(eng):
    p = pos()
    eng.positions[MINT] = p
    for i in range(3):
        assert eng.listener._sell_failed(p, "stop_loss", "x") is False
    assert p.status == "stuck" and all(e.loud for e in eng.bus.of("sell_failed")) and len(eng.bus.of("sell_failed")) == 3


async def test_sell_guard_block_emits_sell_failed(eng, monkeypatch):
    eng.positions[MINT] = p = pos()
    eng.listener.ps = FakePS()

    async def noop(*a, **k):
        return {}
    monkeypatch.setattr(lismod, "sync_fork_clock", noop)

    async def fake_build(*a, **k):
        return {"outAmount": "10000000", "addressesByLookupTableAddress": {}}
    monkeypatch.setattr(lismod, "build_swap", fake_build)

    async def fake_alts(*a, **k):
        return []
    monkeypatch.setattr(lismod, "fetch_alts", fake_alts)
    monkeypatch.setattr(lismod, "build_instructions", lambda b: [])
    monkeypatch.setattr(lismod, "assemble_v0", lambda *a: b"tx")

    async def bh():
        return "11111111111111111111111111111111"
    eng.rpc.latest_blockhash = bh
    seen = {}

    async def fake_check(rpc, raw, intent, quote, jev=None, refresh_pools=True):
        seen.update(jev=jev, refresh=refresh_pools, src=intent.expected_source, min_out=intent.min_out)
        return FakeV("block", ["MIN_OUT"]), None
    monkeypatch.setattr(lismod, "check_transaction", fake_check)
    ok = await eng.listener.sell(p, 1000, "stop_loss")
    assert ok is False and eng.bus.of("sell_failed") and eng.bus.of("guard_blocked")
    assert seen["jev"] is None and seen["refresh"] is False and seen["src"] == "fork_profile"
    assert seen["min_out"] == 9_000_000  # fork executable value 10 USDC x (1 - 1000 bps)


# ---------- executor / price sources ----------

def test_executor_refuses_remote_rpc():
    with pytest.raises(NotForkError):
        Executor(Rpc("https://mainnet.helius-rpc.com/?api-key=x"))


async def test_mainnet_price_source():
    with pytest.raises(PriceFeedDown):
        await MainnetPriceSource({MINT: 6}, api_key="").quote(MINT, 10**6, None)

    def h(req):
        assert req.headers["x-api-key"] == "k"
        return httpx.Response(200, json={MINT: {"usdPrice": 0.01, "decimals": 6}})
    ps = MainnetPriceSource({MINT: 6}, api_key="k", client=httpx.AsyncClient(transport=httpx.MockTransport(h)))
    assert (await ps.quote(MINT, 5 * 10**6, None)).value_usdc == 50_000
    bad = MainnetPriceSource({MINT: 6}, api_key="k", client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500))))
    with pytest.raises(PriceFeedDown):
        await bad.quote(MINT, 1, None)


# ---------- events / API ----------

def test_event_bus_rejects_unknown():
    with pytest.raises(ValueError):
        EventBus().emit("whatever", "x")


def test_api_endpoints():
    from backend.app import app
    c = TestClient(app)
    app.state.engine = None
    assert c.get("/status").status_code == 409
    calls = []

    class E:
        async def respond(self, card_id, action, utterance=None, amount_usdc=None, channel="voice", code=None, background=False):
            calls.append((card_id, action, utterance, amount_usdc, channel, code, background))
            return {"ok": True}

        def status(self):
            return {"positions": []}

        async def emergency_exit(self):
            return [{"ok": True}]
    app.state.engine = E()
    assert c.post("/cards/abc/respond", json={"action": "approve", "utterance": "approve 47"}).json() == {"ok": True}
    assert calls[-1] == ("abc", "approve", "approve 47", None, "voice", None, True)      # duplex default: swap runs in the background
    c.post("/cards/abc/respond", json={"action": "approve", "utterance": "approve 47", "wait": True})
    assert calls[-1][-1] is False
    assert c.post("/cards/abc/respond", json={"action": "modify", "utterance": "x"}).status_code == 400
    assert c.post("/cards/abc/respond", json={"action": "approve", "channel": "button", "code": "4a"}).status_code == 422
    st = c.get("/status").json()
    assert st["positions"] == [] and "orchestrator" in st
    assert c.post("/emergency-exit").json() == {"ok": True, "results": [{"ok": True}]}
    app.state.engine = None
