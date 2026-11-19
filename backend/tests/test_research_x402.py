"""M4: x402 payment flow (simulated 402 seller, no money), budget ledger and top-up flow."""
import asyncio
import json
from pathlib import Path

import httpx
import pytest
from eth_account import Account
from x402.http.utils import (decode_payment_signature_header, encode_payment_required_header,
                             encode_payment_response_header)
from x402.mechanisms.evm import EthAccountSigner
from x402.schemas import PaymentRequired, SettleResponse

from backend.research.budget import BudgetExhausted, Ledger, TopupDeclined
from backend.research.sellers import BASE_NETWORK, BASE_USDC, SELLERS
from backend.research.x402_client import PaymentRefused, ResearchPayer, check_requirement
from x402.schemas import PaymentRequirements

FIX = json.loads((Path(__file__).parent / "fixtures" / "x402_challenges_20261110.json").read_text(encoding="utf-8"))
NANSEN = SELLERS["nansen_holders"]
MINT = "DtcicPCKTKeAh7TDEzTym3hXeoHAoMiBdYJhMYxZpump"


def accept(**kw):
    a = {"scheme": "exact", "network": BASE_NETWORK, "asset": BASE_USDC, "amount": "50000",
         "payTo": NANSEN.pay_to, "maxTimeoutSeconds": 300, "extra": {"name": "USD Coin", "version": "2"}}
    a.update(kw)
    return a


class FakeSeller:
    """Simulated seller: no signature -> 402; signature -> verified, then 200 + PAYMENT-RESPONSE."""

    def __init__(self, accepts, settle_ok=True, reject_paid=False):
        self.accepts, self.settle_ok, self.reject_paid = accepts, settle_ok, reject_paid
        self.unpaid = 0
        self.paid: list[dict] = []

    def __call__(self, req: httpx.Request):
        sig = req.headers.get("PAYMENT-SIGNATURE")
        pr = PaymentRequired.model_validate({"x402Version": 2, "accepts": self.accepts,
                                             "resource": {"url": str(req.url), "description": "", "mimeType": ""}})
        if not sig:
            self.unpaid += 1
            return httpx.Response(402, headers={"PAYMENT-REQUIRED": encode_payment_required_header(pr)}, json={})
        p = decode_payment_signature_header(sig)
        auth = p.payload["authorization"]
        self.paid.append(auth)
        if self.reject_paid:
            return httpx.Response(402, headers={"PAYMENT-REQUIRED": encode_payment_required_header(pr)}, json={})
        sr = SettleResponse(success=self.settle_ok, transaction="0x" + "ab" * 32, network=BASE_NETWORK,
                            payer=auth["from"], amount=auth["value"])
        return httpx.Response(200, headers={"PAYMENT-RESPONSE": encode_payment_response_header(sr)},
                              json={"data": [{"address": "x", "ownership_percentage": 0.1}], "pagination": {"is_last_page": True}})


def payer(fake, budget=1.0, cap=0.10, tmp: Path | None = None):
    led = Ledger(budget_atomic=round(budget * 1e6), per_call_cap_atomic=round(cap * 1e6), path=tmp)
    http = httpx.AsyncClient(transport=httpx.MockTransport(fake))
    rp = ResearchPayer(led, http=http, signer=EthAccountSigner(Account.create()))
    rp.reconcile_onchain = False
    return rp, led


async def test_happy_path_settles_and_logs(tmp_path):
    fake = FakeSeller([accept()])
    p, led = payer(fake, tmp=tmp_path / "spend.json")
    events = []
    p.emit = lambda t, d: events.append((t, d))
    r = await p.buy(NANSEN, MINT, "holder concentration", body={"chain": "solana", "token_address": MINT})
    assert r.paid and r.entry.status == "settled" and r.entry.tx_hash.startswith("0x")
    assert fake.paid[0]["to"].lower() == NANSEN.pay_to.lower() and fake.paid[0]["value"] == "50000"
    assert led.spent_atomic == 50_000 and led.reserved_atomic == 0
    saved = json.loads((tmp_path / "spend.json").read_text(encoding="utf-8"))
    e = saved["entries"][0]
    assert (e["seller"], e["coin_mint"], e["reason"], e["amount_atomic"]) == ("nansen_holders", MINT, "holder concentration", 50_000)
    assert events and events[0][0] == "research_payment"


async def test_cheaper_quote_pays_quoted_amount():
    fake = FakeSeller([accept(amount="25000")])  # e.g. half price for the first 100 calls
    p, led = payer(fake)
    r = await p.buy(NANSEN, MINT, "x", body={})
    assert fake.paid[0]["value"] == "25000" and led.spent_atomic == 25_000 and r.paid


@pytest.mark.parametrize("acc,word", [
    (accept(amount="60000"), "above the registered price"),
    (accept(payTo="0x000000000000000000000000000000000000dEaD"), "payTo address"),
    (accept(asset="0x0000000000000000000000000000000000000001"), "asset is not Base USDC"),
    (accept(network="eip155:1"), ""),
])
async def test_mismatched_quote_refused_without_signing(acc, word):
    fake = FakeSeller([acc])
    p, led = payer(fake)
    with pytest.raises(PaymentRefused):
        await p.buy(NANSEN, MINT, "x", body={})
    assert fake.paid == [] and led.spent_atomic == 0 and led.reserved_atomic == 0
    if word:
        assert word in check_requirement(PaymentRequirements.model_validate(acc), NANSEN, 100_000)


async def test_picks_registered_option_among_many():
    bad = accept(payTo="0x000000000000000000000000000000000000dEaD", amount="1")
    fake = FakeSeller([bad, accept(network="eip155:1"), accept()])
    p, _ = payer(fake)
    r = await p.buy(NANSEN, MINT, "x", body={})
    assert r.paid and fake.paid[0]["to"].lower() == NANSEN.pay_to.lower()


async def test_per_call_cap_below_registered_price_refuses():
    fake = FakeSeller([accept()])
    p, _ = payer(fake, cap=0.04)
    with pytest.raises((PaymentRefused, BudgetExhausted)):
        await p.buy(NANSEN, MINT, "x", body={})
    assert fake.paid == []


async def test_budget_precheck_stops_before_request():
    fake = FakeSeller([accept()])
    p, led = payer(fake, budget=0.06)
    await p.buy(NANSEN, MINT, "x", body={})
    with pytest.raises(BudgetExhausted):
        await p.buy(NANSEN, MINT, "y", body={})
    assert fake.unpaid == 1 and len(fake.paid) == 1 and led.spent_atomic == 50_000


async def test_budget_checked_in_hook_too():
    fake = FakeSeller([accept()])
    p, led = payer(fake, budget=0.10)
    orig = led.can_afford
    led.can_afford = lambda a: True       # bypass the pre-check, rely on the hook only
    led.reserved_atomic = 60_000          # another concurrent purchase already reserved
    with pytest.raises(BudgetExhausted):
        await p.buy(NANSEN, MINT, "x", body={})
    assert fake.paid == []
    led.can_afford = orig


async def test_seller_rejects_payment_records_failed_zero():
    fake = FakeSeller([accept()], reject_paid=True)
    p, led = payer(fake)
    r = await p.buy(NANSEN, MINT, "x", body={})
    assert not r.paid and r.entry.status == "failed" and led.spent_atomic == 0 and led.reserved_atomic == 0


async def test_no_settlement_header_counts_as_spent():
    fake = FakeSeller([accept()], settle_ok=False)
    p, led = payer(fake)
    r = await p.buy(NANSEN, MINT, "x", body={})
    assert r.entry.status == "unknown" and led.spent_atomic == 50_000


def test_live_challenges_match_registry():
    for key, pr in FIX.items():
        s = SELLERS[key]
        ok = [a for a in pr["accepts"] if check_requirement(PaymentRequirements.model_validate(a), s, 100_000) is None]
        assert len(ok) == 1, key
        assert int(ok[0]["amount"]) == s.unit_price_atomic


# ---------- budget top-ups ----------

def led(budget=0.1, tmp=None):
    return Ledger(budget_atomic=round(budget * 1e6), per_call_cap_atomic=100_000, path=tmp)


async def test_topup_approved_raises_budget(tmp_path):
    l = led(tmp=tmp_path / "s.json")
    ev = []

    async def approve(req):
        assert "How much should I add?" in req["speak"]
        return 0.5
    got = await l.request_topup(50_000, approve, emit=lambda t, d: ev.append(t))
    assert got == 500_000 and l.budget_atomic == 600_000
    assert ev == ["research_topup_request", "research_topup_result"]
    assert json.loads((tmp_path / "s.json").read_text(encoding="utf-8"))["topups"][0]["outcome"] == "approved"


@pytest.mark.parametrize("approver,outcome", [
    (None, "rejected"),
    ("reject", "rejected"),
    ("slow", "timeout"),
    ("huge", "over_hard_cap"),
])
async def test_topup_declined_paths(approver, outcome):
    l = led()

    async def reject(_):
        return 0

    async def slow(_):
        await asyncio.sleep(5)
        return 1.0

    async def huge(_):
        return 10.0
    fn = {"reject": reject, "slow": slow, "huge": huge}.get(approver)
    with pytest.raises(TopupDeclined):
        await l.request_topup(50_000, fn, timeout_s=0.05)
    assert l.budget_atomic == 100_000 and l.topups[-1].outcome == outcome


def test_ledger_roundtrip(tmp_path):
    from backend.research.budget import SpendEntry
    l = led(tmp=tmp_path / "s.json")
    l.record(SpendEntry(1.0, "nansen_holders", "u", 50_000, MINT, "r", "settled", "0x1"), 0)

    class M:
        class body:
            class budget:
                research_usdc, research_per_call_usdc = 1.0, 0.1
    l2 = Ledger.from_mandate(M, tmp_path / "s.json")
    assert l2.spent_atomic == 50_000 and l2.budget_atomic == 100_000 and l2.entries[0].tx_hash == "0x1"


async def test_unknown_resolved_onchain_counts_as_paid(monkeypatch):
    from backend.research import x402_client as xc
    fake = FakeSeller([accept(amount="30000", payTo=SELLERS["quickintel_scan"].pay_to)], settle_ok=False)
    p, led = payer(fake)
    p.reconcile_onchain = True
    seen = {}

    async def fake_find(payer_, pay_to, amount, asset, exclude=None, **_):
        seen["args"] = (pay_to, amount, set(exclude or ()))
        return "0x" + "cd" * 32
    monkeypatch.setattr(xc, "find_transfer", fake_find)
    ev = []
    p.emit = lambda t, d: ev.append(d)
    r = await p.buy(SELLERS["quickintel_scan"], MINT, "scan", body={})
    assert r.paid and r.entry.status == "settled" and r.entry.verified_onchain and r.entry.tx_hash == "0x" + "cd" * 32
    assert seen["args"][:2] == (SELLERS["quickintel_scan"].pay_to, 30000) and ev[0]["status"] == "settled"
