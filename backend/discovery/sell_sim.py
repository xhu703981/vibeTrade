"""Buy-then-sell simulation: on the SCRATCH Surfpool (8999) really buy the per_trade amount, sell it all back to USDC, measure round-trip loss.

Per coin: resetNetwork -> align clock with mainnet -> fund a temp wallet with SOL/USDC -> /build buy -> guard -> send
        -> /build sell all -> guard -> send -> compute loss. Only touches the scratch instance, never the main fork.
Routing is limited to JUPITER_ROUTE_DEXES (PumpSwap / Raydium CPMM), matching the LP check.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

from backend.config import JUPITER_ROUTE_DEXES, USDC_MINT, settings
from backend.guard.pipeline import check_transaction
from backend.mandate.schema import Mandate
from backend.solana_utils import Rpc, load_or_create_keypair, send_and_confirm, sync_fork_clock, token_balance
from backend.trade.builder import build_buy_intent, build_sell_intent
from backend.trade.jupiter import JupiterError, assemble_v0, build_instructions, build_swap, fetch_alts

SCRATCH_KEYPAIR = settings.trading_keypair_path.parent / "scratch-keypair.json"


@dataclass
class SellSimResult:
    mint: str
    ok: bool
    reasons: list[str] = field(default_factory=list)
    usdc_in: int = 0
    usdc_out: int = 0
    tokens_bought: int = 0
    roundtrip_loss_pct: float | None = None
    buy_guard: str | None = None
    sell_guard: str | None = None
    buy_route: list[str] = field(default_factory=list)
    buy_sig: str | None = None
    sell_sig: str | None = None

    def as_dict(self) -> dict:
        return asdict(self)


async def _tx(rpc, kp, b: dict) -> bytes:
    alts = await fetch_alts(rpc, list((b.get("addressesByLookupTableAddress") or {}).keys()))
    return bytes(assemble_v0(kp, build_instructions(b), alts, await rpc.latest_blockhash()))


async def sell_sim(mint: str, m: Mandate, rpc_url: str | None = None, upstream_url: str | None = None) -> SellSimResult:
    rpc = Rpc(rpc_url or settings.scratch_surfpool_rpc_url)
    kp, _ = load_or_create_keypair(SCRATCH_KEYPAIR)
    w = str(kp.pubkey())
    per_trade = m.body.budget.per_trade_usdc
    amount = round(per_trade * 1e6)
    res = SellSimResult(mint=mint, ok=False, usdc_in=amount)
    try:
        await rpc.call("surfnet_resetNetwork")
        await sync_fork_clock(rpc, upstream_url or settings.solana_rpc_url)
        await rpc.call("surfnet_setAccount", [w, {"lamports": 1_000_000_000}])
        await rpc.call("surfnet_setTokenAccount", [w, USDC_MINT, {"amount": amount}])

        # ---- buy ----
        slip = m.body.exec.max_slippage_bps.buy
        b = await build_swap(USDC_MINT, mint, amount, w, slip, dexes=JUPITER_ROUTE_DEXES)
        res.buy_route = [s["swapInfo"]["label"] for s in b.get("routePlan", [])]
        raw = await _tx(rpc, kp, b)
        intent = build_buy_intent(m, w, mint, per_trade, int(b["outAmount"]))
        v, _ = await check_transaction(rpc, raw, intent, b)
        res.buy_guard = v.decision
        if v.decision == "block":
            res.reasons.append("buy blocked by guard: " + "; ".join(v.reasons)[:200])
            return res
        sig, err = await send_and_confirm(rpc, raw)
        res.buy_sig = sig
        if err:
            res.reasons.append(f"buy failed: {err[:150]}")
            return res
        got = await token_balance(rpc, w, mint)
        res.tokens_bought = got
        if got <= 0:
            res.reasons.append("zero balance after the buy")
            return res

        # ---- sell everything back to USDC ----
        usdc_before = await token_balance(rpc, w, USDC_MINT)
        sb = await build_swap(mint, USDC_MINT, got, w, m.body.exec.max_slippage_bps.stop_loss_sell, dexes=JUPITER_ROUTE_DEXES)
        raw = await _tx(rpc, kp, sb)
        sintent = build_sell_intent(m, w, mint, got, int(sb["outAmount"]), stop_loss=True)
        v, _ = await check_transaction(rpc, raw, sintent, sb)
        res.sell_guard = v.decision
        if v.decision == "block":
            res.reasons.append("sell blocked by guard: " + "; ".join(v.reasons)[:200])
            return res
        sig, err = await send_and_confirm(rpc, raw)
        res.sell_sig = sig
        if err:
            res.reasons.append(f"sell failed (may be unsellable): {err[:150]}")
            return res
        res.usdc_out = await token_balance(rpc, w, USDC_MINT) - usdc_before
        res.roundtrip_loss_pct = round((amount - res.usdc_out) / amount * 100, 3)
        lim = m.body.filters.max_roundtrip_loss_pct
        if res.roundtrip_loss_pct > lim:
            res.reasons.append(f"round-trip loss {res.roundtrip_loss_pct:.2f}% above {lim:g}%")
        else:
            res.ok = True
        return res
    except JupiterError as e:
        res.reasons.append(f"Jupiter could not quote (PumpSwap/Raydium CPMM routes only): {str(e)[:150]}")
        return res
    except Exception as e:  # noqa: BLE001
        res.reasons.append(f"simulation error: {type(e).__name__}: {str(e)[:150]}")
        return res
    finally:
        await rpc.close()
