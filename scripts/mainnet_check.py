"""Mainnet preflight (read-only, never sends a transaction, never spends anything).

  python scripts/mainnet_check.py            RPC reachable + really mainnet, trading wallet PUBLIC key, SOL / USDC balance
  python scripts/mainnet_check.py --sim 1    also: Jupiter quote for 1 USDC -> SOL, build + sign the transaction locally,
                                             run the full Guard on it via mainnet simulateTransaction. Nothing is sent.

Exit code 0 = ready, 2 = RPC unavailable or not mainnet (fail closed), 3 = balance unreadable, 4 = --sim failed.
Never prints the RPC URL, API keys or any private key.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.config import (MAINNET_GENESIS_HASH, USDC_MINT, WSOL_MINT, mainnet_max_trade_usd,  # noqa: E402
                            mainnet_min_sol_lamports, mainnet_rpc_url, settings)
from backend.solana_utils import Rpc, load_or_create_keypair, token_balance  # noqa: E402


def _mandate(usd: float):
    from backend.mandate.dialogue import compute_hash
    from backend.mandate.schema import Mandate, MandateBody
    d = MandateBody().model_dump(mode="json")
    d["budget"].update(total_usdc=max(usd, 1.0), per_trade_usdc=max(usd, 1.0))
    d["exec"]["max_fee_sol"] = d["exec"].get("max_fee_sol") or 0.002
    d["exit"].update(stop_loss_pct=d["exit"].get("stop_loss_pct") or 30)
    body = MandateBody.model_validate(d)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    fa, ea = now.isoformat().replace("+00:00", "Z"), (now + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    return Mandate(mandate_id="m_preflight", frozen_at=fa, expires_at=ea, body=body,
                   hash=compute_hash("m_preflight", fa, ea, body.model_dump(mode="json")))


async def simulate_buy(rpc: Rpc, kp, usd: float, mint: str) -> int:
    from backend.guard.pipeline import check_transaction
    from backend.trade.builder import build_buy_intent
    from backend.trade.jupiter import assemble_v0, build_instructions, build_swap, fetch_alts
    m = _mandate(usd)
    w = str(kp.pubkey())
    b = await build_swap(USDC_MINT, mint, round(usd * 1e6), w, m.body.exec.max_slippage_bps.buy)
    print(f"jupiter quote: {usd:g} USDC -> {int(b['outAmount'])} base units of {mint[:6]}...")
    alts = await fetch_alts(rpc, list((b.get("addressesByLookupTableAddress") or {}).keys()))
    raw = bytes(assemble_v0(kp, build_instructions(b), alts, await rpc.latest_blockhash()))
    intent = build_buy_intent(m, w, mint, usd, int(b["outAmount"]), 0.0)
    v, sim = await check_transaction(rpc, raw, intent, b, jev=None)
    print(f"guard (simulate only, NOT sent): {v.decision}")
    for r in v.reasons[:6]:
        print(f"  - {r[:160]}")
    if sim is not None:
        print(f"  simulation: success={sim.success} source={sim.source} usdc_delta={sim.token_deltas.get(USDC_MINT)} "
              f"out_delta={sim.token_deltas.get(mint)} sol_delta_lamports={sim.sol_delta}")
        if not sim.success:
            print("  (a failed simulation is expected while the wallet has no SOL / USDC yet)")
    return 0 if sim is not None else 4


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sim", type=float, default=None, metavar="USD", help="simulate (never send) a USDC buy of this size")
    ap.add_argument("--mint", default=WSOL_MINT, help="output mint for --sim (default: SOL)")
    a = ap.parse_args()
    rpc = Rpc(mainnet_rpc_url())
    try:
        try:
            genesis = await rpc.call("getGenesisHash")
        except Exception as e:  # noqa: BLE001
            print(f"FAIL: mainnet RPC unavailable ({type(e).__name__}). Set SOLANA_RPC_URL in .env.")
            return 2
        if genesis != MAINNET_GENESIS_HASH:
            print("FAIL: SOLANA_RPC_URL is not Solana mainnet-beta (genesis hash mismatch).")
            return 2
        print("rpc: Solana mainnet-beta OK")
        kp, created = load_or_create_keypair(settings.trading_keypair_path)
        w = kp.pubkey()
        print(f"trading wallet (public key): {w}{'  [new keypair just created; back up secrets/ privately]' if created else ''}")
        try:
            lamports = int((await rpc.call("getBalance", [str(w), {"commitment": "confirmed"}]))["value"])
            usdc = await token_balance(rpc, str(w), USDC_MINT)
        except Exception as e:  # noqa: BLE001
            print(f"FAIL: could not read the wallet balance ({type(e).__name__}); trading stays off.")
            return 3
        print(f"balance: {lamports / 1e9:.6f} SOL, {usdc / 1e6:.6f} USDC")
        print(f"limits: ${mainnet_max_trade_usd():g} per trade (VT_MAINNET_MAX_TRADE_USD), "
              f"{mainnet_min_sol_lamports() / 1e9:g} SOL fee reserve (VT_MAINNET_MIN_SOL)")
        if lamports < mainnet_min_sol_lamports() or usdc <= 0:
            print("NOTE: fund the wallet (a little SOL for fees + some USDC) before buying; buys are refused until then.")
        if a.sim is not None:
            if not (0 < a.sim <= mainnet_max_trade_usd()):
                print(f"FAIL: --sim must be > 0 and <= the ${mainnet_max_trade_usd():g} ceiling")
                return 4
            try:
                return await simulate_buy(rpc, kp, a.sim, a.mint)
            except Exception as e:  # noqa: BLE001
                print(f"FAIL: simulation path error ({type(e).__name__}: {str(e)[:160]})")
                return 4
        return 0
    finally:
        await rpc.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
