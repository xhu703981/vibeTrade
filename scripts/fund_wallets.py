"""Airdrop SOL and set the USDC balance of the trading wallet on the Surfpool fork. Fork only; never touches mainnet.

Usage: python scripts/fund_wallets.py [--sol 2] [--usdc 50] [--rpc http://127.0.0.1:8899]
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.config import USDC_MINT, settings  # noqa: E402
from backend.solana_utils import Rpc, load_or_create_keypair  # noqa: E402


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sol", type=float, default=2.0)
    ap.add_argument("--usdc", type=float, default=50.0)
    ap.add_argument("--rpc", default=settings.surfpool_rpc_url)
    a = ap.parse_args()
    from backend.config import is_mainnet
    if is_mainnet():
        sys.exit("refused: VT_NETWORK=mainnet; fund the mainnet trading wallet with a real transfer (see README)")
    if "127.0.0.1" not in a.rpc and "localhost" not in a.rpc:
        sys.exit("refused: fund_wallets may only point at a local Surfpool")

    kp, created = load_or_create_keypair(settings.trading_keypair_path)
    owner = kp.pubkey()
    print(f"trading wallet: {owner} ({'new temporary keypair' if created else 'existing keypair'}, path is gitignored)")
    rpc = Rpc(a.rpc)
    try:
        lamports = int(a.sol * 1e9)
        await rpc.call("surfnet_setAccount", [str(owner), {"lamports": lamports}])
        await rpc.call("surfnet_setTokenAccount", [str(owner), USDC_MINT, {"amount": int(a.usdc * 1e6)}])
        sol = await rpc.sol_lamports(owner)
        usdc = await rpc.token_amount(owner)
        print(f"SOL:  {sol / 1e9:.9f}")
        print(f"USDC: {usdc / 1e6:.6f}")
    finally:
        await rpc.close()


if __name__ == "__main__":
    asyncio.run(main())
