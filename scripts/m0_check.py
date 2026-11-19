"""M0 acceptance: Surfpool fork balance -> Jupiter /swap/v2/build -> assemble v0 with a Surfpool blockhash -> sign -> send to Surfpool -> confirm -> balance diff.

Usage: python scripts/m0_check.py [--usdc 5] [--slippage-bps 100]
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.config import USDC_MINT, WSOL_MINT, settings  # noqa: E402
from backend.solana_utils import Rpc, load_or_create_keypair, sync_fork_clock  # noqa: E402
from backend.trade.jupiter import assemble_v0, build_instructions, build_swap, fetch_alts  # noqa: E402


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--usdc", type=float, default=5.0)
    ap.add_argument("--slippage-bps", type=int, default=100)
    a = ap.parse_args()

    print("== environment ==")
    for k, v in settings.status().items():
        print(f"  {k}: {'set' if v else 'not set'}")
    print(f"  SURFPOOL_RPC_URL: {settings.surfpool_rpc_url}")

    kp, _ = load_or_create_keypair(settings.trading_keypair_path)
    owner = kp.pubkey()
    rpc = Rpc(settings.surfpool_rpc_url)
    try:
        ver = await rpc.call("getVersion")
        print(f"  Surfpool: {ver.get('surfnet-version', ver.get('solana-core'))}")

        print("\n== wallet balance (fork) ==")
        sol0, usdc0 = await rpc.sol_lamports(owner), await rpc.token_amount(owner)
        print(f"  address: {owner}\n  SOL:  {sol0 / 1e9:.9f}\n  USDC: {usdc0 / 1e6:.6f}")
        amount = int(a.usdc * 1e6)
        if usdc0 < amount or sol0 < 10_000_000:
            print("  balance too low; run scripts/fund_wallets.py first")
            return 1

        if settings.solana_rpc_url:
            await sync_fork_clock(rpc, settings.solana_rpc_url)  # align the fork clock with mainnet
        print(f"\n== Jupiter /swap/v2/build: {a.usdc} USDC -> SOL ==")
        b = await build_swap(USDC_MINT, WSOL_MINT, amount, str(owner), a.slippage_bps)
        route = " -> ".join(f"{s['swapInfo']['label']}({s['percent']}%)" for s in b.get("routePlan", []))
        print(f"  outAmount: {int(b['outAmount']) / 1e9:.9f} SOL  min: {int(b['otherAmountThreshold']) / 1e9:.9f}")
        print(f"  priceImpact: {float(b.get('priceImpactPct') or 0) * 100:.4f}%  route: {route}")
        alt_addrs = list((b.get("addressesByLookupTableAddress") or {}).keys())
        print(f"  ALTs: {len(alt_addrs)}")

        print("\n== assemble + sign + send to Surfpool ==")
        ixs = build_instructions(b)
        alts = await fetch_alts(rpc, alt_addrs)
        bh = await rpc.latest_blockhash()
        tx = assemble_v0(kp, ixs, alts, bh)
        raw = bytes(tx)
        print(f"  instructions: {len(ixs)}  tx size: {len(raw)} bytes  blockhash: Surfpool's own")
        sig = await rpc.call("sendTransaction", [base64.b64encode(raw).decode(), {"encoding": "base64", "skipPreflight": False, "preflightCommitment": "confirmed"}])
        print(f"  signature: {sig}")
        status = None
        for _ in range(60):
            st = (await rpc.call("getSignatureStatuses", [[sig]]))["value"][0]
            if st and st.get("confirmationStatus") in ("confirmed", "finalized"):
                status = st
                break
            time.sleep(0.5)
        if not status:
            print("  not confirmed (timeout)")
            return 1
        if status.get("err"):
            print(f"  transaction failed: {status['err']}")
            return 1
        print(f"  confirmed at slot {status['slot']}")

        print("\n== balance changes ==")
        sol1, usdc1 = await rpc.sol_lamports(owner), await rpc.token_amount(owner)
        print(f"  SOL:  {sol0 / 1e9:.9f} -> {sol1 / 1e9:.9f}  (Δ {(sol1 - sol0) / 1e9:+.9f}, incl. fees/rent)")
        print(f"  USDC: {usdc0 / 1e6:.6f} -> {usdc1 / 1e6:.6f}  (Δ {(usdc1 - usdc0) / 1e6:+.6f})")
        ok = usdc1 == usdc0 - amount and sol1 - sol0 >= int(b["otherAmountThreshold"]) - 10_000_000
        print(f"\nM0 result: {'PASS' if ok else 'CHECK'} (/build transaction landed on Surfpool)")
        return 0 if ok else 1
    finally:
        await rpc.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
