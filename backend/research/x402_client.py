"""x402 payment client (the official x402 library parses the 402 and signs; this module decides whether to pay).

Each purchase creates a fresh x402Client that registers only the Base (eip155:8453) exact EVM scheme, bound to this seller's:
  1. policy: keep only quotes whose network/asset/payTo exactly match the registry;
  2. spend_controls: per-call cap = the mandate's research_per_call_usdc;
  3. on_before_payment_creation hook: quote <= registered price <= per-call cap, payTo/network/asset/scheme match,
     spent + reserved + this <= research budget (reserve only if it passes), otherwise AbortResult.
The library's auto-retry transport is not used (it signs again on recovery); we pay once and retry once.
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any

import httpx

from backend.config import RESEARCH_SELLER_ALLOWLIST, redact, settings
from backend.research.budget import BudgetExhausted, Ledger, SpendEntry
from backend.research.sellers import Seller

MAX_BODY = 2_000_000
BASE_RPCS = ("https://base-rpc.publicnode.com", "https://mainnet.base.org", "https://1rpc.io/base")  # public nodes, tried in order
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


class PaymentRefused(RuntimeError):
    """We refused to pay (quote mismatch, seller not allowlisted, ...). Nothing signed, nothing spent."""


@dataclass
class Purchase:
    seller: str
    status_code: int
    data: Any                    # seller JSON (untrusted; parsers keep structured fields only)
    entry: SpendEntry | None     # only present if paid
    paid: bool


def check_requirement(req, seller: Seller, cap_atomic: int) -> str | None:
    """Return a refusal reason; None = OK to pay."""
    try:
        amount = int(req.amount)
    except (TypeError, ValueError):
        return "quoted amount is not an integer"
    if req.scheme != "exact":
        return f"only the exact scheme is accepted, quote uses {req.scheme}"
    if req.network != seller.network:
        return f"network mismatch: {req.network}"
    if (req.asset or "").lower() != seller.asset.lower():
        return "asset is not Base USDC"
    if (req.pay_to or "").lower() != seller.pay_to.lower():
        return "payTo address does not match the registry"
    if amount <= 0:
        return "invalid quoted amount"
    if amount > seller.unit_price_atomic:
        return f"quote ${amount / 1e6:.4f} is above the registered price ${seller.unit_price_usdc:.4f}"
    if seller.unit_price_atomic > cap_atomic:
        return f"registered price ${seller.unit_price_usdc:.4f} exceeds the per-call cap ${cap_atomic / 1e6:.2f}"
    return None


def _signer():
    from eth_account import Account
    from x402.mechanisms.evm import EthAccountSigner

    key = settings.base_research_private_key
    if not key:
        raise PaymentRefused("BASE_RESEARCH_WALLET_PRIVATE_KEY is not configured")
    return EthAccountSigner(Account.from_key(key))


def payer_address() -> str:
    from eth_account import Account

    key = settings.base_research_private_key
    return Account.from_key(key).address if key else ""


class ResearchPayer:
    def __init__(self, ledger: Ledger, http: httpx.AsyncClient | None = None, signer=None, emit=None):
        self.ledger = ledger
        self._http = http or httpx.AsyncClient(timeout=60, headers={"User-Agent": "vibe-trading/0.1"})
        self._signer = signer
        self.emit = emit or (lambda _t, _p: None)

    reconcile_onchain = True

    async def close(self):
        await self._http.aclose()

    async def _reconcile(self, e: SpendEntry, seller: Seller) -> None:
        """settled -> verify the transfer on Base; unknown (no receipt from seller) -> look for payer->payTo+amount."""
        try:
            if e.status == "settled" and e.tx_hash:
                e.verified_onchain = await verify_transfer(e.tx_hash, e.payer or payer_address(), seller.pay_to,
                                                           e.amount_atomic, seller.asset)
            elif e.status == "unknown":
                known = {x.tx_hash for x in self.ledger.entries if x.tx_hash}
                for _ in range(3):
                    h = await find_transfer(e.payer or payer_address(), seller.pay_to, e.amount_atomic, seller.asset, exclude=known)
                    if h:
                        e.status, e.tx_hash, e.verified_onchain = "settled", h, True
                        return
                    await asyncio.sleep(3)
        except Exception:  # noqa: BLE001  a failed check does not change the ledger (unknown already counts as spent)
            e.verified_onchain = None

    def _client(self, seller: Seller, state: dict):
        from x402 import AbortResult, x402Client
        from x402.mechanisms.evm.exact import ExactEvmScheme

        cap = self.ledger.per_call_cap_atomic
        client = x402Client()
        client.register(seller.network, ExactEvmScheme(self._signer or _signer()))
        client.set_spend_controls({"max_amount_per_payment": f"${cap / 1e6:.6f}"})
        client.register_policy(lambda _v, reqs: [r for r in reqs if check_requirement(r, seller, cap) is None])

        def before(ctx):
            req = ctx.selected_requirements
            why = check_requirement(req, seller, cap)
            if why:
                state["abort"] = why
                return AbortResult(reason="seller_mismatch", message=why)
            amount = int(req.amount)
            try:
                self.ledger.reserve(amount)
            except BudgetExhausted as e:
                state["abort"] = "budget"
                state["budget_exc"] = e
                return AbortResult(reason="budget_exhausted", message=str(e))
            state["reserved"] = amount
            return None

        client.on_before_payment_creation(before)
        return client

    async def _send(self, seller: Seller, body: dict | None, params: dict | None, headers: dict | None = None):
        return await self._http.request(seller.method, seller.url, json=body if seller.method == "POST" else None,
                                        params=params, headers=headers or {})

    async def buy(self, seller: Seller, coin_mint: str, reason: str, body: dict | None = None,
                  params: dict | None = None) -> Purchase:
        from x402 import PaymentAbortedError  # noqa: F401  (ensures this library version has the exception)
        from x402.http import x402HTTPClient

        if seller.host not in RESEARCH_SELLER_ALLOWLIST:
            raise PaymentRefused(f"seller {seller.host} is not allowlisted")
        if not self.ledger.can_afford(seller.unit_price_atomic):  # pre-check at the registered price; if short, send no request
            raise BudgetExhausted(seller.unit_price_atomic, self.ledger.remaining_atomic)
        extra_h = {"X-Payer-Address": payer_address()} if seller.key.startswith("nansen") else {}
        r = await self._send(seller, body, params, extra_h)
        if r.status_code != 402:
            return Purchase(seller.key, r.status_code, _json(r), None, False)

        state: dict = {}
        client = self._client(seller, state)
        http = x402HTTPClient(client)
        try:
            body_json = json.loads(r.content) if r.content else None
        except ValueError:
            body_json = None
        pr = http.get_payment_required_response(r.headers.get, body_json)
        try:
            payload = await client.create_payment_payload(pr)
        except Exception as e:  # noqa: BLE001  refused / no matching quote: nothing signed
            if state.get("budget_exc"):
                raise state["budget_exc"] from None
            raise PaymentRefused(redact(state.get("abort") or f"no acceptable quote: {type(e).__name__}: {e}")[:300]) from None
        reserved = state.get("reserved", 0)
        paid_headers = {**extra_h, **http.encode_payment_signature_header(payload)}
        try:
            r2 = await self._send(seller, body, params, paid_headers)
        except httpx.HTTPError as e:
            # signed but settlement unknown: record as unknown (counts as spent), err on spending less
            self.ledger.record(SpendEntry(time.time(), seller.key, seller.url, reserved, coin_mint, reason, "unknown",
                                          network=seller.network, payer=payer_address(), pay_to=seller.pay_to), reserved)
            raise PaymentRefused(f"request failed after payment (outcome unknown, recorded as spent): {type(e).__name__}") from None
        settle = None
        try:
            settle = http.get_payment_settle_response(r2.headers.get)
        except Exception:  # noqa: BLE001
            settle = None
        if settle is not None and settle.success and settle.transaction:
            status = "settled"
        elif r2.status_code == 402:
            status = "failed"          # seller did not collect (e.g. signature check failed); no money moved
        else:
            status = "unknown"         # got a response but no settlement receipt
        entry = SpendEntry(time.time(), seller.key, seller.url, int(settle.amount) if settle and settle.amount else reserved,
                           coin_mint, reason, status, tx_hash=settle.transaction if settle else None,
                           network=settle.network if settle else seller.network,
                           payer=settle.payer if settle else payer_address(), pay_to=seller.pay_to)
        if status == "failed":
            entry.amount_atomic = 0
        if status in ("settled", "unknown") and self.reconcile_onchain:
            await self._reconcile(entry, seller)
        self.ledger.record(entry, reserved)
        status = entry.status  # on-chain verification may turn unknown into settled
        self.emit("research_payment", {"seller": seller.name, "amount_usdc": entry.amount_usdc, "status": status,
                                       "tx_hash": entry.tx_hash, "coin_mint": coin_mint, "reason": reason,
                                       "speak": f"Paid {seller.name} {entry.amount_usdc:.2f} dollars for {reason}." if status == "settled"
                                       else f"Payment to {seller.name} did not go through ({status})."})
        return Purchase(seller.key, r2.status_code, _json(r2) if r2.status_code < 400 else None, entry, status == "settled")


def _json(r: httpx.Response):
    if len(r.content) > MAX_BODY:
        return None
    try:
        return r.json()
    except ValueError:
        return None


# ---------- Base on-chain verification ----------

async def _base_rpc(method: str, params: list, http: httpx.AsyncClient | None = None):
    c = http or httpx.AsyncClient(timeout=20)
    last = "no rpc"
    try:
        for url in BASE_RPCS:
            try:
                r = await c.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
                if r.status_code == 200 and "result" in r.json():
                    return r.json()["result"]
                last = f"HTTP {r.status_code}"
            except (httpx.HTTPError, ValueError) as e:
                last = type(e).__name__
        raise RuntimeError(f"no Base RPC is available: {last}")
    finally:
        if http is None:
            await c.aclose()


async def find_transfer(payer: str, pay_to: str, amount_atomic: int, asset: str, lookback_blocks: int = 3000,
                        exclude: set[str] | None = None) -> str | None:
    """Without a settlement receipt, find the USDC transfer (payer -> pay_to, amount) on Base."""
    bn = int(await _base_rpc("eth_blockNumber", []), 16)
    pad = lambda a: "0x" + a.lower().removeprefix("0x").rjust(64, "0")  # noqa: E731
    logs = await _base_rpc("eth_getLogs", [{"address": asset, "fromBlock": hex(bn - lookback_blocks), "toBlock": "latest",
                                            "topics": [TRANSFER_TOPIC, pad(payer), pad(pay_to)]}])
    hits = [lg for lg in logs or [] if int(lg.get("data", "0x0"), 16) == amount_atomic
            and lg["transactionHash"] not in (exclude or set())]
    return hits[-1]["transactionHash"] if hits else None


async def verify_transfer(tx_hash: str, payer: str, pay_to: str, amount_atomic: int, asset: str) -> bool:
    """Verify on Base: the tx succeeded and contains a USDC Transfer(payer -> pay_to, amount)."""
    rec = await _base_rpc("eth_getTransactionReceipt", [tx_hash])
    if not rec or rec.get("status") != "0x1":
        return False
    pad = lambda a: "0x" + a.lower().removeprefix("0x").rjust(64, "0")  # noqa: E731
    for lg in rec.get("logs", []):
        t = lg.get("topics", [])
        if (lg.get("address", "").lower() == asset.lower() and len(t) == 3 and t[0] == TRANSFER_TOPIC
                and t[1] == pad(payer) and t[2] == pad(pay_to) and int(lg.get("data", "0x0"), 16) == amount_atomic):
            return True
    return False


async def usdc_balance(address: str, asset: str) -> int:
    data = "0x70a08231" + address.lower().removeprefix("0x").rjust(64, "0")
    res = await _base_rpc("eth_call", [{"to": asset, "data": data}, "latest"])
    return int(res, 16)
