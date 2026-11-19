"""M2 unit tests, no network: real transaction bytes built with solders + hand-built profile results."""
from __future__ import annotations

import base64
import struct
from datetime import datetime, timedelta, timezone

import pytest
from solders.address_lookup_table_account import AddressLookupTableAccount
from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

from backend.config import USDC_MINT
from backend.guard.rules import ATA_PROGRAM, JUPITER_V6, evaluate
from backend.guard.simulate import TOKEN_PROGRAM, decode_transaction, parse_token_account, summarize
from backend.guard.verdict import decide
from backend.mandate.dialogue import compute_hash
from backend.mandate.schema import Mandate, MandateBody
from backend.trade.builder import IntentError, build_buy_intent, build_sell_intent, min_out_from_quote

KP = Keypair()
W = str(KP.pubkey())
STRANGER = str(Keypair().pubkey())
MEME = str(Keypair().pubkey())
USDC_ATA = str(Keypair().pubkey())
MEME_ATA = str(Keypair().pubkey())
POOL = str(Keypair().pubkey())
TOK = Pubkey.from_string(str(TOKEN_PROGRAM))
SOL = 1_000_000_000


def mandate(expired=False) -> Mandate:
    d = MandateBody().model_dump(mode="json")
    d["budget"].update(total_usdc=50, per_trade_usdc=10)
    d["exec"]["max_fee_sol"] = 0.002
    d["exit"].update(stop_loss_pct=30, take_profit_ladder=[{"multiple": 2, "sell_pct": 50}])
    body = MandateBody.model_validate(d)
    now = datetime.now(timezone.utc).replace(microsecond=0) - (timedelta(hours=30) if expired else timedelta(0))
    fa, ea = now.isoformat().replace("+00:00", "Z"), (now + timedelta(hours=24)).isoformat().replace("+00:00", "Z")
    return Mandate(mandate_id="m", frozen_at=fa, expires_at=ea, body=body, hash=compute_hash("m", fa, ea, body.model_dump(mode="json")))


INTENT = build_buy_intent(mandate(), W, MEME, 5, 1_000_000)  # min_out = 970000


# ---------------- builders ----------------
def tok_bytes(mint, owner, amount, delegate=None, delegated=0, close=None) -> bytes:
    b = bytearray(165)
    b[0:32] = bytes(Pubkey.from_string(mint))
    b[32:64] = bytes(Pubkey.from_string(owner))
    struct.pack_into("<Q", b, 64, amount)
    if delegate:
        struct.pack_into("<I", b, 72, 1)
        b[76:108] = bytes(Pubkey.from_string(delegate))
    b[108] = 1
    struct.pack_into("<Q", b, 121, delegated)
    if close:
        struct.pack_into("<I", b, 129, 1)
        b[133:165] = bytes(Pubkey.from_string(close))
    return bytes(b)


def ui(lamports, owner, data=b""):
    return {"lamports": lamports, "data": [base64.b64encode(data).decode(), "base64"], "owner": owner,
            "executable": False, "rentEpoch": 0, "space": len(data)}


def upd(pre, post):
    return {"type": "writable", "accountChange": {"type": "update", "data": [pre, post]}}


def create(post):
    return {"type": "writable", "accountChange": {"type": "create", "data": post}}


def raw_tx(extra=(), cu_limit=200_000, cu_price=1000, payer=KP, signers=None) -> bytes:
    ixs = [set_compute_unit_limit(cu_limit), set_compute_unit_price(cu_price),
           Instruction(Pubkey.from_string(JUPITER_V6), b"\x01", [AccountMeta(payer.pubkey(), True, True),
                                                                  AccountMeta(Pubkey.from_string(USDC_ATA), False, True),
                                                                  AccountMeta(Pubkey.from_string(POOL), False, True)]), *extra]
    msg = MessageV0.try_compile(payer.pubkey(), ixs, [], Hash.default())
    return bytes(VersionedTransaction(msg, signers or [payer]))


def normal_states(usdc_spent=5_000_000, meme_got=1_000_000, sol_delta=-5_200, **kw):  # fee = 5000 + 1000*200000/1e6
    st = {
        W: upd(ui(2 * SOL, "11111111111111111111111111111111"), ui(2 * SOL + sol_delta, "11111111111111111111111111111111")),
        USDC_ATA: upd(ui(2_039_280, str(TOKEN_PROGRAM), tok_bytes(USDC_MINT, W, 50_000_000)),
                      ui(2_039_280, str(TOKEN_PROGRAM), tok_bytes(USDC_MINT, W, 50_000_000 - usdc_spent, **kw))),
        MEME_ATA: upd(ui(2_039_280, str(TOKEN_PROGRAM), tok_bytes(MEME, W, 0)),
                      ui(2_039_280, str(TOKEN_PROGRAM), tok_bytes(MEME, W, meme_got))),
    }
    return st


RENT = {165: 2_039_280, 137: 1_844_400}  # rent table for unit tests (simulate queries the real values on-chain)


def sim_of(raw, states, error=None, logs=()):
    tx = decode_transaction(raw, {})
    sim = summarize(tx, W, {"transactionProfile": {"accountStates": states, "computeUnitsConsumed": 1000,
                                                   "logMessages": list(logs), "errorMessage": error}})
    sim.created_rent_lamports = sum(l for _, _, n, l in sim.created_program_accounts if RENT.get(n) == l)
    return sim


def codes(sim, intent=INTENT, quote=None):
    return {f.code for f in evaluate(sim, intent, quote)}


# ---------------- decoding ----------------
def test_decode_resolves_alt_and_writability():
    alt_addr = Keypair().pubkey()
    a, b_ = Keypair().pubkey(), Keypair().pubkey()
    ix = Instruction(Pubkey.from_string(JUPITER_V6), b"\x02", [AccountMeta(KP.pubkey(), True, True), AccountMeta(a, False, True), AccountMeta(b_, False, False)])
    msg = MessageV0.try_compile(KP.pubkey(), [ix], [AddressLookupTableAccount(alt_addr, [b_, a])], Hash.default())
    raw = bytes(VersionedTransaction(msg, [KP]))
    tx = decode_transaction(raw, {str(alt_addr): [str(b_), str(a)]})
    assert tx.alt_addresses == (str(alt_addr),)
    keys = dict(zip(tx.account_keys, tx.writable))
    assert keys[str(a)] is True and keys[str(b_)] is False
    assert tx.instructions[0].accounts == (W, str(a), str(b_))


def test_decode_missing_alt_fails_closed():
    alt_addr = Keypair().pubkey()
    a = Keypair().pubkey()
    ix = Instruction(Pubkey.from_string(JUPITER_V6), b"", [AccountMeta(a, False, True)])
    raw = bytes(VersionedTransaction(MessageV0.try_compile(KP.pubkey(), [ix], [AddressLookupTableAccount(alt_addr, [a])], Hash.default()), [KP]))
    with pytest.raises(Exception):
        decode_transaction(raw, {})


def test_fee_from_compute_budget():
    tx = decode_transaction(raw_tx(cu_limit=1_400_000, cu_price=7419), {})
    assert tx.fee_lamports_max == 5000 + 10387  # measured in M0


def test_parse_token_account_fields():
    t = parse_token_account(tok_bytes(USDC_MINT, W, 123, delegate=STRANGER, delegated=5, close=STRANGER), str(TOKEN_PROGRAM))
    assert (t.mint, t.owner, t.amount, t.delegate, t.delegated_amount, t.close_authority) == (USDC_MINT, W, 123, STRANGER, 5, STRANGER)


# ---------------- rules ----------------
def test_normal_swap_passes():
    sim = sim_of(raw_tx(), normal_states())
    assert codes(sim) == set()
    assert sim.token_deltas == {USDC_MINT: -5_000_000, MEME: 1_000_000}


def test_program_not_allowlisted():
    evil = Instruction(Pubkey.from_string(STRANGER), b"\x00", [AccountMeta(KP.pubkey(), True, True)])
    assert "PROGRAM_NOT_ALLOWED" in codes(sim_of(raw_tx([evil]), normal_states()))


def test_set_authority_instruction_and_state():
    ix = Instruction(TOK, bytes([6, 2, 1]) + bytes(Pubkey.from_string(STRANGER)),
                     [AccountMeta(Pubkey.from_string(USDC_ATA), False, True), AccountMeta(KP.pubkey(), True, False)])
    st = normal_states()
    st[USDC_ATA]["accountChange"]["data"][1] = ui(2_039_280, str(TOKEN_PROGRAM), tok_bytes(USDC_MINT, STRANGER, 45_000_000))
    c = codes(sim_of(raw_tx([ix]), st))
    assert "SET_AUTHORITY" in c and "OVER_BUDGET" in c  # whole account moved away = all USDC flows out


def test_approve_via_inner_cpi_caught_by_state():
    """No top-level Approve (hidden inside Jupiter); the state layer still blocks it."""
    st = normal_states(delegate=STRANGER, delegated=2**64 - 1)
    assert codes(sim_of(raw_tx(), st)) == {"APPROVE"}


def test_approve_top_level_instruction():
    ix = Instruction(TOK, bytes([4]) + struct.pack("<Q", 1), [AccountMeta(Pubkey.from_string(USDC_ATA), False, True),
                                                             AccountMeta(Pubkey.from_string(STRANGER), False, False), AccountMeta(KP.pubkey(), True, False)])
    assert "APPROVE" in codes(sim_of(raw_tx([ix]), normal_states()))


def test_close_account_to_stranger():
    ix = Instruction(TOK, bytes([9]), [AccountMeta(Pubkey.from_string(MEME_ATA), False, True), AccountMeta(Pubkey.from_string(STRANGER), False, True),
                                      AccountMeta(KP.pubkey(), True, False)])
    assert "CLOSE_TO_STRANGER" in codes(sim_of(raw_tx([ix]), normal_states()))


def test_close_account_to_self_allowed():
    ix = Instruction(TOK, bytes([9]), [AccountMeta(Pubkey.from_string(MEME_ATA), False, True), AccountMeta(KP.pubkey(), True, True),
                                      AccountMeta(KP.pubkey(), True, False)])
    assert codes(sim_of(raw_tx([ix]), normal_states())) == set()


def test_ata_for_stranger_blocked():
    ix = Instruction(Pubkey.from_string(ATA_PROGRAM), bytes([1]), [AccountMeta(KP.pubkey(), True, True), AccountMeta(Pubkey.from_string(POOL), False, True),
                                                                   AccountMeta(Pubkey.from_string(STRANGER), False, False)])
    assert "ATA_OWNER" in codes(sim_of(raw_tx([ix]), normal_states()))


def test_over_budget():
    assert "OVER_BUDGET" in codes(sim_of(raw_tx(), normal_states(usdc_spent=5_000_001)))


def test_min_out():
    assert "MIN_OUT" in codes(sim_of(raw_tx(), normal_states(meme_got=969_999)))


def test_other_token_decrease():
    other_mint, other_ata = str(Keypair().pubkey()), str(Keypair().pubkey())
    st = normal_states()
    st[other_ata] = upd(ui(2_039_280, str(TOKEN_PROGRAM), tok_bytes(other_mint, W, 100)), ui(2_039_280, str(TOKEN_PROGRAM), tok_bytes(other_mint, W, 0)))
    assert "OTHER_TOKEN_DECREASE" in codes(sim_of(raw_tx(), st))


def test_sol_outflow_and_reserve():
    st = normal_states(sol_delta=-int(1.99 * SOL))
    c = codes(sim_of(raw_tx(), st))
    assert {"SOL_OUTFLOW", "SOL_RESERVE"} <= c


def test_new_ata_rent_allowed():
    st = normal_states(sol_delta=-(2_039_280 + 5000 + 200))
    st[MEME_ATA] = create(ui(2_039_280, str(TOKEN_PROGRAM), tok_bytes(MEME, W, 1_000_000)))
    sim = sim_of(raw_tx(), st)
    assert sim.created_user_token_accounts == 1 and codes(sim) == set()


def test_fee_cap():
    assert "FEE_CAP" in codes(sim_of(raw_tx(cu_limit=1_400_000, cu_price=2_000_000), normal_states()))


def test_sim_failed_blocks():
    assert codes(sim_of(raw_tx(), normal_states(), error="custom program error: 0x1771")) == {"SIM_FAILED"}


def test_foreign_fee_payer_and_extra_signer():
    other = Keypair()
    ixs = [Instruction(Pubkey.from_string(JUPITER_V6), b"\x01", [AccountMeta(other.pubkey(), True, True), AccountMeta(KP.pubkey(), True, True)])]
    raw = bytes(VersionedTransaction(MessageV0.try_compile(other.pubkey(), ixs, [], Hash.default()), [other, KP]))
    assert {"FEE_PAYER", "SIGNERS"} <= codes(sim_of(raw, normal_states()))


def test_system_transfer_to_stranger_blocked():
    from solders.system_program import TransferParams, transfer
    ix = transfer(TransferParams(from_pubkey=KP.pubkey(), to_pubkey=Pubkey.from_string(STRANGER), lamports=1))
    assert "SYSTEM_IX" in codes(sim_of(raw_tx([ix]), normal_states()))


def test_quote_checks():
    q = {"slippageBps": 1000, "otherAmountThreshold": "1", "priceImpactPct": "0.05", "inAmount": "6000000",
         "inputMint": USDC_MINT, "outputMint": STRANGER}
    assert {"SLIPPAGE", "MIN_OUT_QUOTE", "PRICE_IMPACT", "QUOTE_AMOUNT", "QUOTE_MINTS"} <= codes(sim_of(raw_tx(), normal_states()), quote=q)


# ---------------- verdict / JEV can only tighten ----------------
class J:
    def __init__(self, p_safe=None, boom=False):
        self.p_safe, self.boom, self.calls = p_safe, boom, 0

    async def ask(self, state, name, q):
        self.calls += 1
        assert "EPjFWdd5" not in state and MEME not in state  # role labels only, no addresses / names
        if self.boom:
            raise RuntimeError("down")
        return self.p_safe


async def test_block_is_final_and_skips_jev():
    j = J(p_safe=1.0)
    v = await decide(sim_of(raw_tx(), normal_states(usdc_spent=9_000_000)), INTENT, None, j)
    assert v.decision == "block" and j.calls == 0


async def test_jev_can_escalate_to_ask_human():
    v = await decide(sim_of(raw_tx(), normal_states()), INTENT, None, J(p_safe=0.1))
    assert v.decision == "ask_human" and v.jev_risk == pytest.approx(0.9)


async def test_jev_error_asks_human():
    v = await decide(sim_of(raw_tx(), normal_states()), INTENT, None, J(boom=True))
    assert v.decision == "ask_human"


async def test_jev_safe_allows():
    v = await decide(sim_of(raw_tx(), normal_states()), INTENT, None, J(p_safe=0.9))
    assert v.decision == "allow"


# ---------------- builder ----------------
def test_builder_limits():
    m = mandate()
    with pytest.raises(IntentError, match="per-trade cap"):
        build_buy_intent(m, W, MEME, 10.01, 1)
    with pytest.raises(IntentError, match="total budget"):
        build_buy_intent(m, W, MEME, 10, 1, spent_usdc=45)
    with pytest.raises(IntentError):
        build_buy_intent(m, W, USDC_MINT, 5, 1)
    with pytest.raises(IntentError, match="expired"):
        build_buy_intent(mandate(expired=True), W, MEME, 5, 1)


def test_builder_values_and_sell_after_expiry():
    i = build_buy_intent(mandate(), W, MEME, 5, 1_000_000)
    assert (i.amount_in, i.min_out, i.max_slippage_bps, i.max_fee_lamports, i.sol_reserve_lamports) == (5_000_000, 970_000, 300, 2_000_000, 50_000_000)
    s = build_sell_intent(mandate(expired=True), W, MEME, 1_000, 2_000_000, stop_loss=True)
    assert s.output_mint == USDC_MINT and s.max_slippage_bps == 1000 and s.min_out == 1_800_000
    assert min_out_from_quote(999, 300) == 969


def test_program_pda_rent_allowed_but_extra_lamports_not():
    pda = str(Keypair().pubkey())
    st = normal_states(sol_delta=-(1_844_400 + 5200))
    st[pda] = create(ui(1_844_400, "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA", b"\x00" * 137))
    assert codes(sim_of(raw_tx(), st)) == set()
    st2 = normal_states(sol_delta=-(1_844_400 + 5200 + 1_000_000))
    st2[pda] = create(ui(1_844_400 + 1_000_000, "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA", b"\x00" * 137))
    assert "SOL_OUTFLOW" in codes(sim_of(raw_tx(), st2))


def test_new_system_wallet_is_not_rent():
    newwallet = str(Keypair().pubkey())
    st = normal_states(sol_delta=-(890_880 + 5200))
    st[newwallet] = create(ui(890_880, "11111111111111111111111111111111"))
    assert "SOL_OUTFLOW" in codes(sim_of(raw_tx(), st))
