"""M2 acceptance (live): real Jupiter /build transactions with a temp wallet on the Surfpool fork.

Needs: the main Surfpool instance at SURFPOOL_RPC_URL and JUPITER_API_KEY; skipped otherwise.
Run: .venv/bin/python -m pytest -m live -s
"""
from __future__ import annotations

import asyncio
import base64
import struct
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.system_program import TransferParams, transfer

from backend.config import USDC_MINT, settings
from backend.guard.pipeline import check_transaction
from backend.judge.jev import JEVClient
from backend.mandate.dialogue import compute_hash
from backend.mandate.schema import Mandate, MandateBody
from backend.solana_utils import TOKEN_PROGRAM, Rpc, ata, load_or_create_keypair, sync_fork_clock
from backend.trade.builder import build_buy_intent
from backend.trade.jupiter import JupiterError, assemble_v0, build_instructions, build_swap, fetch_alts

pytestmark = pytest.mark.live
BONK = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
STRANGER = Keypair().pubkey()
RESULTS: list[tuple[str, str, str]] = []


def _up() -> bool:
    try:
        return httpx.post(settings.surfpool_rpc_url, json={"jsonrpc": "2.0", "id": 1, "method": "getHealth"}, timeout=2).json().get("result") == "ok"
    except Exception:  # noqa: BLE001
        return False


if not (_up() and settings.jupiter_api_key):
    pytest.skip("needs a local Surfpool and JUPITER_API_KEY", allow_module_level=True)


def mandate() -> Mandate:
    d = MandateBody().model_dump(mode="json")
    d["budget"].update(total_usdc=50, per_trade_usdc=10)
    d["exec"]["max_fee_sol"] = 0.002
    d["exit"].update(stop_loss_pct=30, take_profit_ladder=[{"multiple": 2, "sell_pct": 50}])
    body = MandateBody.model_validate(d)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    fa, ea = now.isoformat().replace("+00:00", "Z"), (now + timedelta(hours=24)).isoformat().replace("+00:00", "Z")
    return Mandate(mandate_id="m_test", frozen_at=fa, expires_at=ea, body=body, hash=compute_hash("m_test", fa, ea, body.model_dump(mode="json")))


@pytest.fixture(scope="module")
def env():
    loop = asyncio.new_event_loop()
    kp, _ = load_or_create_keypair(settings.trading_keypair_path)
    rpc = Rpc(settings.surfpool_rpc_url)
    w = str(kp.pubkey())
    loop.run_until_complete(rpc.call("surfnet_setAccount", [w, {"lamports": 2_000_000_000}]))
    loop.run_until_complete(rpc.call("surfnet_setTokenAccount", [w, USDC_MINT, {"amount": 50_000_000}]))
    loop.run_until_complete(rpc.call("surfnet_setTokenAccount", [str(STRANGER), USDC_MINT, {"amount": 0}]))
    yield loop, kp, rpc, mandate()
    loop.run_until_complete(rpc.close())
    loop.close()
    print("\n\n==== M2 live results ====")
    for name, dec, why in RESULTS:
        print(f"{name:28} {dec:10} {why}")


async def _swap_tx(kp, rpc, usdc: float, extra: list[Instruction] = ()):
    await sync_fork_clock(rpc, settings.solana_rpc_url)  # align the fork clock with mainnet, otherwise some pools report InvalidTimestamp
    # limit route accounts when appending malicious instructions so the tx stays under 1232 bytes
    kw = {"maxAccounts": 24 if extra else 32}
    # pin standard AMMs: oracle-style makers like GoonFi/Scorch go stale on the fork and occasionally fail simulation with custom errors (unrelated to the guard)
    kw["dexes"] = "Whirlpool,Raydium CLMM,Meteora DLMM,Raydium"
    for attempt in range(3):  # Jupiter has occasional upstream errors (e.g. "Oracle price out of date"); retried in tests only, never in production
        try:
            b = await build_swap(USDC_MINT, BONK, int(usdc * 1e6), str(kp.pubkey()), 300, **kw)  # = the mandate's buy slippage cap
            break
        except JupiterError:
            if attempt == 2:
                raise
            await asyncio.sleep(1.5)
    ixs = build_instructions(b) + list(extra)
    alts = await fetch_alts(rpc, list((b.get("addressesByLookupTableAddress") or {}).keys()))
    return bytes(assemble_v0(kp, ixs, alts, await rpc.latest_blockhash())), b


def _run(env, name, usdc_intent, usdc_tx, extra=(), jev=None, refresh=True):
    loop, kp, rpc, m = env
    raw, b = loop.run_until_complete(_swap_tx(kp, rpc, usdc_tx, extra))
    intent = build_buy_intent(m, str(kp.pubkey()), BONK, usdc_intent, int(b["outAmount"]) * usdc_intent // usdc_tx if usdc_tx else 0)
    v, sim = loop.run_until_complete(check_transaction(rpc, raw, intent, b, jev, refresh))
    RESULTS.append((name, v.decision, "; ".join(v.reasons)[:160]))
    return v, sim


def _usdc_ata(kp):
    return ata(kp.pubkey(), Pubkey.from_string(USDC_MINT))


def test_normal_swap_allowed(env):
    v, sim = _run(env, "normal 5 USDC->BONK", 5, 5, refresh=True)
    assert v.decision == "allow", v.reasons
    assert sim.token_deltas[USDC_MINT] == -5_000_000 and sim.token_deltas[BONK] > 0


def test_normal_swap_with_jev(env):
    v, _ = _run(env, "normal swap + JEV", 5, 5, jev=JEVClient())
    assert v.jev_risk is not None
    assert v.decision == "allow", v.reasons


def test_set_authority_blocked(env):
    _, kp, _, _ = env
    data = bytes([6, 2, 1]) + bytes(STRANGER)  # SetAuthority(AccountOwner -> stranger)
    ix = Instruction(TOKEN_PROGRAM, data, [AccountMeta(_usdc_ata(kp), False, True), AccountMeta(kp.pubkey(), True, False)])
    v, _ = _run(env, "SetAuthority hidden after the swap", 5, 5, [ix])
    assert v.decision == "block" and any(f.code == "SET_AUTHORITY" for f in v.findings), v.reasons


def test_approve_blocked(env):
    _, kp, _, _ = env
    data = bytes([4]) + struct.pack("<Q", 2**64 - 1)
    ix = Instruction(TOKEN_PROGRAM, data, [AccountMeta(_usdc_ata(kp), False, True), AccountMeta(STRANGER, False, False),
                                           AccountMeta(kp.pubkey(), True, False)])
    v, _ = _run(env, "unlimited Approve to a stranger", 5, 5, [ix])
    assert v.decision == "block" and any(f.code == "APPROVE" for f in v.findings), v.reasons


def test_over_budget_blocked(env):
    v, _ = _run(env, "overspend: intent 10, tx spends 20", 10, 20)
    assert v.decision == "block" and any(f.code == "OVER_BUDGET" for f in v.findings), v.reasons


def test_token_transfer_to_stranger_blocked(env):
    _, kp, _, _ = env
    data = bytes([12]) + struct.pack("<Q", 1_000_000) + bytes([6])  # TransferChecked 1 USDC
    ix = Instruction(TOKEN_PROGRAM, data, [AccountMeta(_usdc_ata(kp), False, True), AccountMeta(Pubkey.from_string(USDC_MINT), False, False),
                                           AccountMeta(ata(STRANGER, Pubkey.from_string(USDC_MINT)), False, True), AccountMeta(kp.pubkey(), True, False)])
    v, _ = _run(env, "USDC to a stranger", 5, 5, [ix])
    assert v.decision == "block" and {"TOKEN_TRANSFER", "OVER_BUDGET"} <= {f.code for f in v.findings}, v.reasons


def test_sol_transfer_to_stranger_blocked(env):
    _, kp, _, _ = env
    ix = transfer(TransferParams(from_pubkey=kp.pubkey(), to_pubkey=STRANGER, lamports=500_000_000))
    v, _ = _run(env, "SOL to a stranger", 5, 5, [ix])
    assert v.decision == "block" and {"SYSTEM_IX", "SOL_OUTFLOW"} <= {f.code for f in v.findings}, v.reasons


def test_unsigned_garbage_blocked(env):
    loop, kp, rpc, m = env
    intent = build_buy_intent(m, str(kp.pubkey()), BONK, 5, 1)
    v, _ = loop.run_until_complete(check_transaction(rpc, b"\x00garbage", intent))
    RESULTS.append(("garbage bytes", v.decision, v.reasons[0][:80]))
    assert v.decision == "block"
