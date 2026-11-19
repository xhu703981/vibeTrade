# Vibe Trading

Voice-first Solana meme-coin co-pilot: you set hard limits by voice, it researches coins, shows them on a selection
desk, prepares one confirmation card per buy (with a two-digit code), runs every transaction through the Guard
(decode + simulation + injection checks) before anything is sent, and then watches stop loss / take profit.

- Run it on Windows: [docs/run_on_your_pc.md](docs/run_on_your_pc.md) (`.\scripts\dev.ps1 -Setup`, then `.\scripts\dev.ps1`)
- Rehearsal (replayed research, local fork, no money): [docs/rehearsal.md](docs/rehearsal.md)
- Guard evidence: [docs/guard_evidence.md](docs/guard_evidence.md)

By default every trade runs on a local **Surfpool fork** of mainnet with a temporary wallet. Nothing touches real funds
unless you start mainnet mode explicitly.

## Mainnet mode

Mainnet mode swaps on **Solana mainnet with real money** through Jupiter. Your trading wallet signs locally, the
transaction is simulated against mainnet, and only then is it sent. On-chain trades can't be undone.

### Enable

```powershell
# 1. (optional) a private RPC in .env; without it the public https://api.mainnet-beta.solana.com is used
#    SOLANA_RPC_URL=https://mainnet.helius-rpc.com/?api-key=...   (or just the Helius key)
#    JUPITER_API_KEY=...                                         (required for quotes)
# 2. check the RPC, the wallet and its balance (read-only, never sends anything):
.\.venv\Scripts\python.exe scripts\mainnet_check.py
#    optional: also quote and simulate a 1 USDC -> SOL buy through the full Guard, still never sent:
.\.venv\Scripts\python.exe scripts\mainnet_check.py --sim 1
# 3. start (asks you to type MAINNET):
.\scripts\dev.ps1 -Mode mainnet
```

`mainnet_check.py` always talks to mainnet and reads the same limits as the app. `-Mode mainnet` sets `VT_NETWORK=mainnet`, live discovery and live research
(x402 research fees from the Base research wallet, capped by your research budget; set `VIBE_RESEARCH=cache` before
starting to use cached research only). Rehearsal and `-Mode live` always force `VT_NETWORK=fork`, so mainnet is
never switched on by `.env` alone.

| Setting (env / .env) | Default | Meaning |
|---|---|---|
| `VT_NETWORK` | `fork` | `mainnet` = real funds (set by `-Mode mainnet`) |
| `SOLANA_RPC_URL` | public mainnet RPC | Mainnet RPC (full URL or a Helius key); never printed |
| `VT_MAINNET_MAX_TRADE_USD` | `5` | Hard ceiling per buy, on top of the mandate. An invalid or non-positive value falls back to 5 |
| `VT_MAINNET_MIN_SOL` | `0.01` | SOL kept for fees and token-account rent; buys are refused below it |

### What changes on mainnet, and what stays the same

- **Same safety:** the mandate's hard limits (budget, per-trade cap, daily max loss, fee cap, slippage), the
  confirmation card with its code ("approve 47"), the approval rules and the Guard are unchanged. The Guard
  simulates the exact signed bytes with mainnet `simulateTransaction` (`sigVerify` on, real blockhash) and reads the
  before/after state of every writable account. A failed simulation or decode blocks the trade.
- **Extra guards:** the hard per-trade ceiling ($5). The wallet's USDC and SOL are re-read right before each card
  and again right before each send. If the RPC or the balance can't be read, nothing is bought. The backend only
  sends to the configured mainnet RPC and refuses a local one, so mainnet and fork can't mix.
- **Off on mainnet:** the rehearsal **Test take profit / Test stop loss** controls, the red-team test coin, the
  fork wallet funding (`fund_wallets.py` refuses) and all `surfnet_*` cheatcodes on the trading RPC. The scratch
  fork (:8999) is still used, but only to test-sell research candidates with a throwaway wallet.
- **Visible:** an amber **MAINNET · REAL FUNDS** badge in the top bar, a real-funds line on each confirmation card,
  and a startup banner in `logs\backend.out` with the trading wallet's public key and the ceiling.
- **Receipts:** every buy and sell records its signature and a Solscan link (`https://solscan.io/tx/<signature>`).
  You see it on the position cards and in the activity log, as `[mainnet]` lines in `logs\backend.out`, and in
  `runs\m6_trades_<mandate>.json`.

### Fund the trading wallet

1. Get the **public** address: `scripts\mainnet_check.py` prints it (from `secrets\trading-keypair.json`, which
   is gitignored; never share that file).
2. Send it a little **SOL for fees**, about 0.02–0.05 SOL. Each swap costs well under 0.01 SOL, and a new token
   account locks about 0.002 SOL of rent, which you get back when the account is closed.
3. Send it the **USDC** you want to trade (SPL USDC `EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v`), for example $5–$10.
4. Re-run `scripts\mainnet_check.py` and check that the balance shows.

Only keep small amounts in this hot wallet. The key sits unencrypted on disk.

### Risks

- Real money, irreversible: a confirmed swap can't be reversed. Meme coins can go to zero within minutes, and a
  stop loss can fill far below its line when the price gaps or liquidity disappears.
- Slippage, price impact and fees are real. Quotes move between the card and the send. The card is voided and
  rebuilt when the re-quote deviates more than your limit.
- Simulation isn't a guarantee: state can change between the simulation and the block that includes the transaction.
- A send can time out without a confirmation (congestion). The app then reports a failure but shows the
  signature, so check Solscan before you try again.
- The public RPC is rate-limited. Use a private RPC for anything beyond a quick test.
- The research wallet (Base, x402) pays real research fees in live research mode.

## Mainnet transactions

Real mainnet trades made with this app (filled in after the trades are made):

| # | Date | Trade | Amount | Transaction |
|---|---|---|---|---|
| 1 | _TBD_ | _e.g. USDC → SOL_ | _$1_ | _https://solscan.io/tx/…_ |
| 2 | _TBD_ | _e.g. SOL → USDC_ | _$1_ | _https://solscan.io/tx/…_ |
| 3 | _TBD_ | _e.g. USDC → meme coin, then sell_ | _$1_ | _https://solscan.io/tx/…_ |
