"""Observational Guard evidence: real serialized fixture txs/profile responses, no network or trades."""
from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from solders.address_lookup_table_account import AddressLookupTableAccount
from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

from backend.guard import evidence
from backend.guard.pipeline import check_transaction
from backend.guard.semantic import QUESTION, RISK_THRESHOLD, build_state
from backend.guard.simulate import TOKEN_PROGRAM
from backend.tests.test_guard_unit import (INTENT, JUPITER_V6, KP, MEME, MEME_ATA, STRANGER, USDC_ATA, USDC_MINT,
                                           W, normal_states, raw_tx)


class RPC:
    def __init__(self, states=None, fail=False):
        self.states = states if states is not None else normal_states()
        self.calls = []
        self.fail = fail

    async def call(self, method, params):
        self.calls.append(method)
        assert method == "surfnet_profileTransaction"            # any new side effect fails the test
        if self.fail:
            raise RuntimeError("sensitive arbitrary failure text " + STRANGER)
        return {"value": {"transactionProfile": {"accountStates": self.states, "computeUnitsConsumed": 1000,
                                                 "logMessages": [], "errorMessage": None}}}


class JEV:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    async def ask(self, state, name, question):
        self.calls.append((state, name, copy.deepcopy(question)))
        if self.fail:
            raise RuntimeError("untrusted exception " + STRANGER)
        return 0.94


@pytest.fixture(autouse=True)
def captures():
    evidence._RECORDS.clear()
    yield
    evidence._RECORDS.clear()


def run(raw=None, states=None, jev=None, fail=False):
    rpc = RPC(states, fail)
    j = jev or JEV()
    raw = raw if raw is not None else raw_tx()
    v, sim = asyncio.run(check_transaction(rpc, raw, INTENT, jev=j))
    return v, sim, evidence.get(v.guard_run_id), rpc, j


def test_capture_is_from_same_bytes_profile_and_exact_jev_call():
    raw = raw_tx()
    v, sim, view, rpc, j = run(raw)
    assert v.decision == "allow" and rpc.calls == ["surfnet_profileTransaction"]
    assert view["tx_bytes_len"] == len(raw)
    assert view["tx_base64_prefix"] == base64.b64encode(raw).decode("ascii")[:60]
    assert view["transaction_sha256"] == hashlib.sha256(raw).hexdigest()
    assert view["structured_json"] == j.calls[0][0] == build_state(sim, INTENT)
    assert view["structured_view"] == json.loads(j.calls[0][0])
    assert view["structured_status"] == "sent_to_jev"
    assert view["jev"]["question"] == j.calls[0][2] == QUESTION
    assert view["jev"]["risk"] == v.jev_risk == 1 - 0.94 and view["jev"]["threshold"] == RISK_THRESHOLD
    assert view["lookup_tables"]["accounts_resolved"] == 0
    assert [i["allowed"] for i in view["decoded"]] == [True, True, True]
    assert all(r["result"] == "pass" for r in view["rules"])
    balances = {b["asset"]: b for b in view["simulation"]["balances"]}
    assert balances["USDC"]["before"] == 50_000_000 and balances["USDC"]["after"] == 45_000_000
    assert balances["target token"]["delta"] == 1_000_000
    assert balances["SOL"]["delta"] == -5200
    blob = json.dumps(view)
    for private in (W, MEME, STRANGER, USDC_ATA, MEME_ATA, USDC_MINT, JUPITER_V6):
        assert private not in blob                              # no addresses, token metadata or arbitrary log text


def test_block_records_actual_rules_and_never_asks_jev():
    transfer = Instruction(Pubkey.from_string(TOKEN_PROGRAM), bytes([3]) + bytes(8), [])
    v, _, view, rpc, j = run(raw_tx([transfer]), normal_states(usdc_spent=10_000_000))
    assert v.decision == "block" and j.calls == []
    assert view["structured_json"] is None and view["structured_view"] is None
    assert view["structured_status"] == "not_sent"
    assert view["jev"] == {"not_asked": "blocked by rules"}
    assert view["decoded"][-1]["allowed"] is False
    blocked = [r["name"] for r in view["rules"] if r["result"] == "block"]
    assert "Input within budget" in blocked and "Instruction 3: token instruction allowlist" in blocked
    assert any(r["name"] == "Output meets minimum" for r in view["rules"])  # no stop/reorder introduced
    assert rpc.calls == ["surfnet_profileTransaction"]


def test_alt_account_count_comes_from_resolved_transaction(monkeypatch):
    from backend.guard import simulate as sm
    table, a, b = Pubkey.new_unique(), Pubkey.new_unique(), Pubkey.new_unique()
    ix = Instruction(Pubkey.from_string(JUPITER_V6), b"route", [AccountMeta(KP.pubkey(), True, True),
                     AccountMeta(a, False, True), AccountMeta(b, False, False)])
    raw = bytes(VersionedTransaction(MessageV0.try_compile(KP.pubkey(), [ix],
                 [AddressLookupTableAccount(table, [b, a])], Hash.default()), [KP]))
    fetched = []
    async def tables(rpc, addresses):
        fetched.extend(addresses)
        return {str(table): [str(b), str(a)]}
    monkeypatch.setattr(sm, "fetch_alt_tables", tables)
    _, _, view, _, _ = run(raw)
    assert fetched == [str(table)]
    assert view["lookup_tables"]["tables"] == 1 and view["lookup_tables"]["accounts_resolved"] == 2
    assert str(a) not in json.dumps(view) and str(table) not in json.dumps(view)


def test_profile_failure_and_jev_failure_are_honest_and_safe():
    v, sim, view, _, j = run(fail=True)
    assert v.decision == "block" and sim is None and j.calls == []
    assert view["simulation"] is None and view["rules"] == []
    assert view["errors"] == [{"stage": "decode_or_profile", "error": "RuntimeError"}]
    assert view["jev"] == {"not_asked": "decode or simulation unavailable"}
    v, _, view, _, j = run(jev=JEV(fail=True))
    assert v.decision == "ask_human" and len(j.calls) == 1
    assert view["structured_json"] == j.calls[0][0]           # submitted state, no invented answer
    assert view["jev"]["risk"] is None and view["jev"]["status"] == "asked"
    assert "untrusted exception" not in json.dumps(view) and STRANGER not in json.dumps(view)


def test_missing_profile_state_is_not_zero_and_unrun_rules_not_passed():
    v, _, view, _, _ = run(states={})
    assert v.decision == "block"
    assert all(b["before"] is None and b["after"] is None for b in view["simulation"]["balances"])
    assert not any(r["name"] == "Output meets minimum" for r in view["rules"])
    assert any(r == {"name": "Wallet profile available", "result": "block"} for r in view["rules"])


def test_capture_failure_never_changes_a_decision(monkeypatch):
    monkeypatch.setattr(evidence, "simulation", lambda *a: 1 / 0)
    monkeypatch.setattr(evidence, "sent", lambda *a: 1 / 0)
    v, _, view, _, j = run()
    assert v.decision == "allow" and len(j.calls) == 1 and view["simulation"] is None
    monkeypatch.setattr(evidence, "begin", lambda *a: 1 / 0)
    v, _, view, _, j = run()
    assert v.decision == "allow" and v.guard_run_id is None and view is None and len(j.calls) == 1


def test_concurrent_runs_and_frozen_store_are_isolated():
    async def two():
        return await asyncio.gather(check_transaction(RPC(), raw_tx(), INTENT, jev=JEV()),
                                    check_transaction(RPC(normal_states(usdc_spent=10_000_000)), raw_tx(), INTENT, jev=JEV()))
    (a, _), (b, _) = asyncio.run(two())
    assert a.guard_run_id != b.guard_run_id and a.decision == "allow" and b.decision == "block"
    one = evidence.get(a.guard_run_id)
    one["rules"].clear()
    assert evidence.get(a.guard_run_id)["rules"]
    assert evidence.get(b.guard_run_id)["jev"] == {"not_asked": "blocked by rules"}
    assert evidence._CURRENT.get() is None


def test_get_endpoints_are_read_only_card_bound_and_never_follow_replacements(monkeypatch):
    from backend.app import app
    v, _, record, rpc, j = run()
    card = SimpleNamespace(id="old-card", code="51", guard={"guard_run_id": v.guard_run_id}, status="voided", replaced_by="new-card")
    evidence.bind(v.guard_run_id, card.id, card.code, "approval")
    other, _, _, _, _ = run()
    replacement = SimpleNamespace(id="new-card", code="52", guard={"guard_run_id": other.guard_run_id}, status="pending", replaced_by=None)
    evidence.bind(other.guard_run_id, replacement.id, replacement.code, "approval")
    engine = SimpleNamespace(cards={card.id: card, replacement.id: replacement})
    # State stores values in _state, not __dict__; patch its mapping to restore cleanly across tests.
    monkeypatch.setitem(app.state._state, "engine", engine)
    before_calls, before_jev = rpc.calls[:], j.calls[:]
    frozen = copy.deepcopy(evidence._RECORDS)
    client = TestClient(app)
    for _ in range(3):
        result = client.get("/cards/51/guard_view?card_id=old-card&guard_run_id=" + v.guard_run_id)
        assert result.status_code == 200 and result.json()["guard_run_id"] == v.guard_run_id
        assert result.json()["replaced"] is True
        assert client.get(f"/guard-runs/{v.guard_run_id}/guard_view").status_code == 200
    assert evidence._RECORDS == frozen and rpc.calls == before_calls and j.calls == before_jev
    assert client.get("/cards/51/guard_view?card_id=new-card").status_code == 404
    assert client.get("/cards/51/guard_view?guard_run_id=" + other.guard_run_id).status_code == 409
    # A recycled code must not silently select the latest/replacement card.
    replacement.code = "51"
    assert client.get("/cards/51/guard_view").status_code == 409
    assert client.get("/cards/51/guard_view?card_id=old-card").status_code == 200
    card.guard = {}
    assert client.get("/cards/51/guard_view?card_id=old-card").status_code == 404
    assert client.get("/guard-runs/missing/guard_view").status_code == 404
    card.guard = {"guard_run_id": other.guard_run_id}
    assert client.get("/cards/51/guard_view?card_id=old-card").status_code == 409  # foreign capture


@pytest.mark.parametrize("spent,probability,expected", [(5_000_000, 0.94, "allow"), (5_000_000, 0.49, "ask_human"),
                                                        (10_000_000, 0.94, "block")])
def test_instrumentation_never_changes_verdict_or_findings(monkeypatch, spent, probability, expected):
    class Judge(JEV):
        async def ask(self, state, name, question):
            self.calls.append((state, name, question))
            return probability
    raw = raw_tx()
    v, _, _, _, _ = run(raw, normal_states(usdc_spent=spent), jev=Judge())
    monkeypatch.setattr(evidence, "begin", lambda raw: None)
    unrecorded, _, _, _, _ = run(raw, normal_states(usdc_spent=spent), jev=Judge())
    assert v.decision == unrecorded.decision == expected
    assert (v.findings, v.reasons, v.jev_risk) == (unrecorded.findings, unrecorded.reasons, unrecorded.jev_risk)
