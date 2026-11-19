"""Listener pricing uses plain simulateTransaction (no surfnet_profileTransaction: it grew the fork without bound)."""
from __future__ import annotations

import base64
import struct

from backend.guard.simulate import wallet_token_deltas

WALLET = "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM"
MINT = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


def _tok(amount: int) -> dict:
    d = bytes(64) + struct.pack("<Q", amount) + bytes(93)
    return {"data": [base64.b64encode(d).decode(), "base64"], "owner": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"}


class FakeRpc:
    def __init__(self):
        self.calls = []

    async def call(self, method, params):
        self.calls.append(method)
        if method == "getTokenAccountsByOwner":
            mint = params[1]["mint"]
            return {"value": [{"pubkey": "TOKACC", "account": _tok(1_000)}] if mint == MINT else []}   # no USDC account yet
        if method == "simulateTransaction":
            from solders.pubkey import Pubkey
            from backend.solana_utils import ata
            usdc_ata = str(ata(Pubkey.from_string(WALLET), Pubkey.from_string(USDC)))
            addrs = params[1]["accounts"]["addresses"]
            return {"value": {"err": None, "accounts": [_tok(0) if a == "TOKACC" else _tok(2_500_000) if a == usdc_ata else None
                                                        for a in addrs]}}
        raise AssertionError(method)


async def test_wallet_token_deltas_reads_post_state_without_profiling():
    rpc = FakeRpc()
    ok, err, d = await wallet_token_deltas(rpc, b"\x01raw", WALLET, [MINT, USDC])
    assert ok and err is None
    assert d[MINT] == -1_000 and d[USDC] == 2_500_000        # the USDC account the sell creates counts from 0
    assert "surfnet_profileTransaction" not in rpc.calls


async def test_wallet_token_deltas_reports_failure():
    class Bad(FakeRpc):
        async def call(self, method, params):
            if method == "simulateTransaction":
                return {"value": {"err": {"InstructionError": [2, "Custom"]}, "accounts": None}}
            return await super().call(method, params)
    ok, err, d = await wallet_token_deltas(Bad(), b"\x01raw", WALLET, [MINT, USDC])
    assert not ok and "InstructionError" in err and d == {}


# ---------------- following mainnet: refresh the route's pools before each quote ----------------
import pytest  # noqa: E402

import backend.execute.price_source as psrc  # noqa: E402
from backend.execute.price_source import ForkPriceSource, PriceFeedDown  # noqa: E402


class _Kp:
    def pubkey(self):
        return WALLET


class _Rpc:
    def __init__(self):
        self.calls: list[tuple] = []

    async def call(self, method, params):
        self.calls.append((method, params[0] if params else None))
        if method == "surfnet_resetAccount":
            return {"value": None}
        raise AssertionError(method)


def _ps(monkeypatch, value=1_234, route=("POOL", "VAULT_A", "VAULT_B"), refresh=True):
    rpc = _Rpc()
    ps = ForkPriceSource(rpc, _Kp(), upstream_url="http://upstream", refresh=refresh)
    found = []

    async def pricing_tx(mint, amount, fresh=False):
        return b"tx-" + mint.encode()

    async def accounts(r, raw, wallet):
        found.append(raw)
        assert wallet == WALLET
        return list(route)

    async def deltas(r, raw, wallet, mints):
        r.calls.append(("simulateTransaction", raw))
        return True, None, {mints[0]: -10, mints[1]: value}
    monkeypatch.setattr(ps, "_pricing_tx", pricing_tx)
    monkeypatch.setattr(psrc, "pool_accounts_to_refresh", accounts)
    monkeypatch.setattr(psrc, "wallet_token_deltas", deltas)
    return ps, rpc, found


async def test_quote_resets_route_pools_before_simulating(monkeypatch):
    ps, rpc, found = _ps(monkeypatch)
    q = await ps.quote(MINT, 10, None)
    assert q.value_usdc == 1_234
    assert rpc.calls == [("surfnet_resetAccount", "POOL"), ("surfnet_resetAccount", "VAULT_A"),
                         ("surfnet_resetAccount", "VAULT_B"), ("simulateTransaction", b"tx-" + MINT.encode())]
    assert not any(m == "surfnet_profileTransaction" for m, _ in rpc.calls)   # memory: never profile per poll
    await ps.quote(MINT, 10, None)
    assert len(found) == 1                       # route accounts are looked up once per built pricing tx
    assert sum(m == "surfnet_resetAccount" for m, _ in rpc.calls) == 6   # but reset on every poll


async def test_frozen_coin_keeps_fork_only_pool_state(monkeypatch):
    ps, rpc, _ = _ps(monkeypatch)
    ps.freeze(MINT)
    assert not ps.follows_mainnet(MINT) and ps.follows_mainnet(USDC)
    await ps.quote(MINT, 10, None)
    assert [m for m, _ in rpc.calls] == ["simulateTransaction"]          # rehearsal whale move is not wiped
    ps.unfreeze(MINT)
    await ps.quote(MINT, 10, None)
    assert [m for m, _ in rpc.calls].count("surfnet_resetAccount") == 3


async def test_refresh_off_switch(monkeypatch):
    ps, rpc, _ = _ps(monkeypatch, refresh=False)
    await ps.quote(MINT, 10, None)
    assert [m for m, _ in rpc.calls] == ["simulateTransaction"]


async def test_refresh_failure_is_price_feed_down_never_a_price(monkeypatch):
    ps, rpc, _ = _ps(monkeypatch)

    async def boom(method, params):
        raise RuntimeError("upstream 429")
    rpc.call = boom
    with pytest.raises(PriceFeedDown):
        await ps.quote(MINT, 10, None)


async def test_empty_route_is_price_feed_down(monkeypatch):
    ps, rpc, _ = _ps(monkeypatch, route=())
    with pytest.raises(PriceFeedDown):
        await ps.quote(MINT, 10, None)
    assert not any(m == "simulateTransaction" for m, _ in rpc.calls)


async def test_price_test_freezes_under_listener_lock_and_releases_when_closed(monkeypatch):
    import asyncio
    from types import SimpleNamespace as NS
    import backend.execute.price_test as pt
    from backend.events import EventBus
    ps, _rpc, _ = _ps(monkeypatch)
    lock = asyncio.Lock()
    seen = []
    pos = NS(mint=MINT, pool="POOL", sells=[], status="open", held_tokens=10, last_ratio=1.0, tp_done=[])
    ex = NS(stop_loss_pct=20, take_profit_ladder=(NS(multiple=1.3, sell_pct=100),), poll_interval_sec=0.1)
    eng = NS(rpc=NS(url="http://127.0.0.1:8899"), m=NS(body=NS(exit=ex)), bus=EventBus(), listener=NS(ps=ps, _lock=lock))

    async def move(rpc, kp, pool, mint, factor):
        seen.append(ps.follows_mainnet(MINT))
        pos.sells.append({"trigger": "take_profit"})
        pos.status, pos.held_tokens = "closed", 0
        return {}
    monkeypatch.setattr(pt, "move_price", move)
    monkeypatch.setattr(pt, "assert_local", lambda rpc: None)
    monkeypatch.setattr(pt, "load_or_create_keypair", lambda p: (None, False))
    assert (await pt.run_price_test(eng, pos, "take_profit"))["ok"]
    assert seen == [False]                      # frozen while the whale moved the pool
    assert ps.follows_mainnet(MINT)             # position closed -> back to mainnet

    # still open after the test (e.g. a 50% take-profit): stays frozen so a snap back cannot fire a trailing stop
    pos2 = NS(mint=MINT, pool="POOL", sells=[], status="open", held_tokens=10, last_ratio=1.0, tp_done=[])

    async def move2(rpc, kp, pool, mint, factor):
        pos2.sells.append({"trigger": "take_profit"})
        return {}
    monkeypatch.setattr(pt, "move_price", move2)
    assert (await pt.run_price_test(eng, pos2, "take_profit"))["ok"]
    assert not ps.follows_mainnet(MINT)


async def test_listener_sell_refreshes_pools_unless_frozen(monkeypatch):
    """The real sell is guarded with refresh_pools=True (runs against current mainnet pool state) unless frozen."""
    from types import SimpleNamespace as NS
    import backend.execute.listener as L
    from backend.events import EventBus
    from backend.guard.verdict import Verdict
    flags = []

    async def check(rpc, raw, intent, b, jev=None, refresh_pools=False):
        flags.append(refresh_pools)
        return Verdict("block", ["stop here"]), None
    monkeypatch.setattr(L, "check_transaction", check)

    async def noop(*a, **k):
        return {}

    async def swap(*a, **k):
        return {"outAmount": "100"}
    monkeypatch.setattr(L, "sync_fork_clock", noop)
    monkeypatch.setattr(L, "build_swap", swap)
    monkeypatch.setattr(L, "fetch_alts", lambda *a: noop())
    monkeypatch.setattr(L, "build_instructions", lambda b: [])
    monkeypatch.setattr(L, "assemble_v0", lambda *a: b"raw")
    monkeypatch.setattr(L, "build_sell_intent", lambda *a, **k: NS(min_out=1))
    q = psrc.PriceQuote(10, 100, None, "fork_profile", 0)

    class PS:
        def __init__(self, follow):
            self.follow = follow

        def follows_mainnet(self, mint):
            return self.follow

        async def quote(self, *a):
            return q
    slip = NS(stop_loss_sell=1000, buy=300)
    m = NS(body=NS(exec=NS(max_slippage_bps=slip)))

    async def latest():
        return "BH"
    for follow in (True, False):
        eng = NS(rpc=NS(latest_blockhash=latest), upstream="u", wallet=WALLET, kp=None, bus=EventBus(), mandate_for=lambda p: m)
        lst = L.Listener(eng, PS(follow), fork_mode=True, clock=noop)
        pos = L.Position(MINT, "X", "POOL", 10, 10, 100, 0.0)
        assert await lst.sell(pos, 10, "stop_loss", q) is False
    assert flags == [True, False]


async def test_full_sell_closes_even_with_leftover_tokens_in_wallet(monkeypatch):
    """Tokens of the same mint left in the wallet from an earlier session are not part of the position."""
    from types import SimpleNamespace as NS
    import backend.execute.listener as L
    from backend.events import EventBus
    from backend.guard.verdict import Verdict

    async def check(*a, **k):
        return Verdict("allow", []), None

    async def noop(*a, **k):
        return {}

    async def swap(*a, **k):
        return {"outAmount": "100"}
    bal = {"tok": 2_256_436 + 1_000, "usdc": 0}

    async def token_balance(rpc, owner, mint):
        return bal["usdc"] if mint == L.USDC_MINT else bal["tok"]

    async def send(raw, *a, **k):
        bal["tok"] -= 1_000
        bal["usdc"] += 99
        return NS(error=None, signature="SIG", received=0)
    monkeypatch.setattr(L, "check_transaction", check)
    monkeypatch.setattr(L, "sync_fork_clock", noop)
    monkeypatch.setattr(L, "build_swap", swap)
    monkeypatch.setattr(L, "fetch_alts", lambda *a: noop())
    monkeypatch.setattr(L, "build_instructions", lambda b: [])
    monkeypatch.setattr(L, "assemble_v0", lambda *a: b"raw")
    monkeypatch.setattr(L, "build_sell_intent", lambda *a, **k: NS(min_out=1))
    monkeypatch.setattr(L, "token_balance", token_balance)
    q = psrc.PriceQuote(1_000, 100, None, "fork_profile", 0)

    class PS:
        def follows_mainnet(self, mint):
            return False

        async def quote(self, *a):
            return q
    slip = NS(stop_loss_sell=1000, buy=300)
    m = NS(body=NS(exec=NS(max_slippage_bps=slip)))

    async def latest():
        return "BH"
    eng = NS(rpc=NS(latest_blockhash=latest), upstream="u", wallet=WALLET, kp=None, bus=EventBus(), mandate_for=lambda p: m,
             executor=NS(send=send, save=lambda: None))
    lst = L.Listener(eng, PS(), fork_mode=True, clock=noop)
    pos = L.Position(MINT, "X", "POOL", 1_000, 1_000, 100, 0.0)
    assert await lst.sell(pos, 1_000, "stop_loss", q) is True
    assert pos.held_tokens == 0 and pos.status == "closed" and pos.realized_usdc == 99
