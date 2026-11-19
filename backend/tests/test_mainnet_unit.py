"""Mainnet real-funds mode (VT_NETWORK=mainnet): explicit opt-in, strict send target, hard per-trade ceiling, fail-closed
balance check, standard-RPC simulation for the Guard (no surfnet_* cheatcodes), Solscan links. No network access here."""
from __future__ import annotations

import asyncio
import base64
import struct
from types import SimpleNamespace as NS

import pytest
from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

import backend.config as cfg
import backend.execute.executor as ex_mod
from backend.config import USDC_MINT
from backend.execute import engine as eng
from backend.guard import simulate as sim_mod

TOKEN = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"


@pytest.fixture
def mainnet(monkeypatch):
    monkeypatch.setenv("VT_NETWORK", "mainnet")
    monkeypatch.delenv("SOLANA_RPC_URL", raising=False)
    monkeypatch.delenv("VT_MAINNET_MAX_TRADE_USD", raising=False)
    monkeypatch.delenv("VT_MAINNET_MIN_SOL", raising=False)
    yield


@pytest.fixture
def fork(monkeypatch):
    monkeypatch.delenv("VT_NETWORK", raising=False)
    yield


# ---------------------------------------------------------------- config
def test_fork_is_the_default_and_mainnet_needs_the_exact_opt_in(monkeypatch):
    monkeypatch.delenv("VT_NETWORK", raising=False)
    assert cfg.network() == "fork" and not cfg.is_mainnet()
    assert cfg.trading_rpc_url() == cfg.settings.surfpool_rpc_url
    for v in ("", "main", "mainnet-beta", "1", "true"):
        monkeypatch.setenv("VT_NETWORK", v)
        assert cfg.network() == "fork"
    monkeypatch.setenv("VT_NETWORK", " MAINNET ")
    assert cfg.is_mainnet()
    assert cfg.network_status()["real_funds"] is True


def test_mainnet_rpc_defaults_to_public_and_accepts_a_helius_key(mainnet, monkeypatch):
    assert cfg.trading_rpc_url() == cfg.MAINNET_PUBLIC_RPC
    assert cfg.network_status()["rpc"] == "public"
    monkeypatch.setenv("SOLANA_RPC_URL", "123e4567-e89b-12d3-a456-426614174000")
    assert cfg.trading_rpc_url().startswith("https://mainnet.helius-rpc.com/?api-key=")
    st = cfg.network_status()
    assert st["rpc"] == "custom" and "api-key" not in str(st)        # the URL (and its key) never reaches the UI


def test_ceiling_defaults_to_5_and_never_becomes_unlimited(mainnet, monkeypatch):
    assert cfg.mainnet_max_trade_usd() == 5.0
    for bad in ("abc", "-1", "0", "inf", "nan"):
        monkeypatch.setenv("VT_MAINNET_MAX_TRADE_USD", bad)
        assert cfg.mainnet_max_trade_usd() == 5.0, bad
    monkeypatch.setenv("VT_MAINNET_MAX_TRADE_USD", "2")
    assert cfg.mainnet_max_trade_usd() == 2.0


def test_solscan_link_only_on_mainnet(fork, monkeypatch):
    assert cfg.explorer_tx_url("SIG") is None
    monkeypatch.setenv("VT_NETWORK", "mainnet")
    assert cfg.explorer_tx_url("SIG") == "https://solscan.io/tx/SIG"
    assert cfg.explorer_tx_url(None) is None


# ---------------------------------------------------------------- send target
def test_send_target_fork_mode_is_local_only(fork):
    ex_mod.assert_send_allowed(NS(url="http://127.0.0.1:8899"))
    with pytest.raises(ex_mod.NotForkError):
        ex_mod.assert_send_allowed(NS(url=cfg.MAINNET_PUBLIC_RPC))


def test_send_target_mainnet_mode_is_the_configured_rpc_only(mainnet):
    ex_mod.assert_send_allowed(NS(url=cfg.MAINNET_PUBLIC_RPC))
    for bad in ("http://127.0.0.1:8899", "http://localhost:8899", "https://evil.example/rpc"):
        with pytest.raises(ex_mod.NotForkError):
            ex_mod.assert_send_allowed(NS(url=bad))


def test_executor_records_signature_and_solscan_link_on_mainnet(mainnet, monkeypatch):
    seen = {}

    async def fake_send(rpc, raw, timeout_s=30.0):
        seen["timeout"] = timeout_s
        return "5igSIG", None
    monkeypatch.setattr(ex_mod, "send_and_confirm", fake_send)
    x = ex_mod.Executor(NS(url=cfg.MAINNET_PUBLIC_RPC))
    rec = asyncio.run(x.send(b"raw", "buy", "card", USDC_MINT, 1_000_000, 1))
    assert rec.signature == "5igSIG" and rec.network == "mainnet"
    assert rec.explorer_url == "https://solscan.io/tx/5igSIG"
    assert seen["timeout"] >= 60          # waits out the blockhash on mainnet instead of the fork's 30 s


def test_executor_fork_records_have_no_explorer_link(fork, monkeypatch):
    async def fake_send(rpc, raw, timeout_s=30.0):
        return "forksig", None
    monkeypatch.setattr(ex_mod, "send_and_confirm", fake_send)
    rec = asyncio.run(ex_mod.Executor(NS(url="http://127.0.0.1:8899")).send(b"raw", "buy", "card", USDC_MINT, 1, 1))
    assert rec.network == "fork" and rec.explorer_url is None


# ---------------------------------------------------------------- ceiling + balance (fail closed)
def test_ceiling_blocks_above_5_dollars_on_mainnet_only(fork, monkeypatch):
    assert eng.mainnet_ceiling_reason(50.0) is None              # fork: the mandate alone decides
    monkeypatch.setenv("VT_NETWORK", "mainnet")
    assert eng.mainnet_ceiling_reason(5.0) is None
    assert "real-money limit" in eng.mainnet_ceiling_reason(5.01)
    assert eng.mainnet_ceiling_reason(0) is not None


class BalRpc:
    def __init__(self, lamports=None, usdc=None, fail=False):
        self.lamports, self.usdc, self.fail = lamports, usdc, fail
        self.url = cfg.MAINNET_PUBLIC_RPC

    async def call(self, method, params=None):
        if self.fail:
            raise RuntimeError("rpc down")
        if method == "getBalance":
            return {"value": self.lamports}
        if method == "getTokenAccountsByOwner":
            return {"value": [{"account": {"data": {"parsed": {"info": {"tokenAmount": {"amount": str(self.usdc)}}}}}}]}
        raise AssertionError(method)


def test_balance_check_fails_closed(mainnet):
    w = str(Keypair().pubkey())
    assert "could not be read" in asyncio.run(eng.mainnet_funds_reason(BalRpc(fail=True), w, 1.0))
    assert "SOL" in asyncio.run(eng.mainnet_funds_reason(BalRpc(lamports=1_000, usdc=10_000_000), w, 1.0))
    assert "less than" in asyncio.run(eng.mainnet_funds_reason(BalRpc(lamports=10**9, usdc=500_000), w, 1.0))
    assert asyncio.run(eng.mainnet_funds_reason(BalRpc(lamports=10**9, usdc=1_000_000), w, 1.0)) is None


def test_balance_check_is_skipped_on_the_fork(fork):
    assert asyncio.run(eng.mainnet_funds_reason(BalRpc(fail=True), "w", 1.0)) is None


# ---------------------------------------------------------------- guard simulation on mainnet
def _token_acct(mint: str, owner: str, amount: int) -> bytes:
    d = bytearray(165)
    d[0:32] = bytes(Pubkey.from_string(mint))
    d[32:64] = bytes(Pubkey.from_string(owner))
    struct.pack_into("<Q", d, 64, amount)
    d[108] = 1
    return bytes(d)


def _acc(data: bytes, owner: str, lamports: int) -> dict:
    return {"data": [base64.b64encode(data).decode(), "base64"], "owner": owner, "lamports": lamports, "executable": False}


class SimRpc:
    """A fake mainnet RPC: standard methods only; any surfnet_* call fails the test."""

    def __init__(self, kp, usdc_ata, err=None):
        self.url = cfg.MAINNET_PUBLIC_RPC
        self.w, self.ata, self.err = str(kp.pubkey()), str(usdc_ata), err
        self.calls = []

    async def call(self, method, params=None):
        self.calls.append((method, params))
        assert not method.startswith("surfnet_"), method
        if method == "getMultipleAccounts":
            return {"value": [_acc(b"", "11111111111111111111111111111111", 1_000_000_000) if k == self.w else
                              _acc(_token_acct(USDC_MINT, self.w, 5_000_000), TOKEN, 2_039_280) for k in params[0]]}
        if method == "simulateTransaction":
            post = [_acc(b"", "11111111111111111111111111111111", 999_995_000) if k == self.w else
                    _acc(_token_acct(USDC_MINT, self.w, 4_000_000), TOKEN, 2_039_280) for k in params[1]["accounts"]["addresses"]]
            return {"value": {"err": self.err, "logs": [f"Program {TOKEN} invoke [1]"], "unitsConsumed": 1234,
                              "accounts": None if self.err else post}}
        if method == "getMinimumBalanceForRentExemption":
            return 2_039_280
        raise AssertionError(method)


def _tx(kp, ata):
    ix = Instruction(Pubkey.from_string(TOKEN), b"\x03", [AccountMeta(ata, False, True), AccountMeta(kp.pubkey(), True, False)])
    msg = MessageV0.try_compile(kp.pubkey(), [ix], [], Hash.default())
    return bytes(VersionedTransaction(msg, [kp]))


def test_mainnet_simulation_builds_balance_deltas_without_cheatcodes(mainnet):
    kp, ata = Keypair(), Keypair().pubkey()
    rpc = SimRpc(kp, ata)
    s = asyncio.run(sim_mod.simulate(rpc, _tx(kp, ata), str(kp.pubkey()), refresh=["somepool"]))
    assert s.success and s.source == "simulateTransaction"
    assert s.token_deltas[USDC_MINT] == -1_000_000
    assert s.sol_delta == -5_000 and s.compute_units == 1234
    sim_call = [p for m, p in rpc.calls if m == "simulateTransaction"][0][1]
    assert sim_call["sigVerify"] is True and sim_call["replaceRecentBlockhash"] is False   # the real signed bytes
    assert not any(m.startswith("surfnet_") for m, _ in rpc.calls)


def test_mainnet_simulation_failure_is_reported_as_failure(mainnet):
    kp, ata = Keypair(), Keypair().pubkey()
    s = asyncio.run(sim_mod.simulate(SimRpc(kp, ata, err={"InstructionError": [0, "Custom"]}), _tx(kp, ata), str(kp.pubkey())))
    assert not s.success and "InstructionError" in s.error
    assert s.token_deltas.get(USDC_MINT, 0) == 0


def test_refresh_and_clock_sync_never_touch_a_real_rpc(mainnet):
    from backend.solana_utils import sync_fork_clock

    class NoCalls:
        url = cfg.MAINNET_PUBLIC_RPC

        async def call(self, method, params=None):
            raise AssertionError(method)
    asyncio.run(sim_mod.refresh_accounts(NoCalls(), ["a", "b"]))
    assert asyncio.run(sync_fork_clock(NoCalls(), cfg.MAINNET_PUBLIC_RPC))["synced"] is False


# ---------------------------------------------------------------- rehearsal-only features are off on mainnet
def test_price_tests_are_off_on_mainnet(fork, monkeypatch):
    from backend.events import EventBus
    from backend.orchestrator import Orchestrator
    o = Orchestrator(EventBus(), discovery="replay", research="cache", ui_buttons=True)
    o.engine = NS(listener=NS(fork_mode=True), rpc=NS(url="http://127.0.0.1:8899"))
    assert o.price_test_enabled() is True
    monkeypatch.setenv("VT_NETWORK", "mainnet")
    assert o.price_test_enabled() is False
    import backend.execute.price_test as pt
    with pytest.raises(RuntimeError):
        asyncio.run(pt.run_price_test(o.engine, None, "stop_loss"))


def test_engine_refuses_fork_listener_on_mainnet(mainnet):
    with pytest.raises(ValueError):
        eng.TradingEngine(NS(mandate_id="m"), fork_mode=True)
