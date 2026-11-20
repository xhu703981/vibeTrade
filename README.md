# Vibe Trading

UBC 2026 Hackathon project. Numbers in here come from the code or from runs we actually did.

## 1. Summary

Trading meme coins means signing transactions you can't read. And if you give an AI agent a wallet, someone can trick it (say with a prompt injection hidden in token metadata) into draining it. Vibe Trading is a voice-controlled Solana meme-coin trading agent. It pays for its own research over x402 (USDC on Base), and a guard checks every transaction before it gets signed. Nothing is bought until you say "approve" plus the card's two-digit code.

Demo video: https://youtu.be/6_1EUfDedNY

We did four real mainnet swaps, links are in section 5. For example the [BONK buy](https://solscan.io/tx/61e5RnZD756DzYWKSP1oVM1FCTMddKdgwcJTnnmsYYe4tcGMbpV3vXiwnFJ5yMsXami7uDLg3gkqemaqoZ4J4CYA).

## 2. Decision loop

1. Limits, taken only from your own words. Hard rules get checked, then the mandate is frozen and hashed. `backend/mandate/dialogue.py`, `backend/mandate/schema.py`
2. Discover. Nansen smart-money net flow (paid over x402), with DexScreener as fallback. `backend/discovery/smart_money.py`, `backend/discovery/dexscreener.py`
3. Free on-chain checks: thresholds, raw mint bytes, LP state, plus a test buy and sell on the scratch fork. `backend/discovery/free_checks.py`, `backend/discovery/sell_sim.py`
4. Paid x402 research for the top few only. Cheapest seller first, and it stops at the first failure. `backend/research/pipeline.py`, `backend/research/x402_client.py`
5. Card with a two-digit code, built from a Jupiter build and a v0 transaction we assemble ourselves. `backend/execute/cards.py`, `backend/trade/jupiter.py`
6. Guard, see section 4. `backend/guard/pipeline.py`
7. Approval. "approve" plus the code, matched with plain word rules on your transcript (no LLM). `backend/execute/cards.py`, `backend/voice/verify.py`
8. Execute: re-quote, re-check, sign, send. `backend/execute/engine.py`, `backend/execute/executor.py`
9. Listener. Stop, take-profit ladder, trailing stop, max hold, liquidity drop, and the de-risk offers. `backend/execute/listener.py`

## 3. Limits

The preset is `config/default_mandate.json`, defaults are in `backend/mandate/schema.py`, and the hard caps are `HARD_RULES` in `backend/config.py`. The agent can't change any of these. Only your own words can, and raising a limit needs a spoken "confirm" while lowering one applies right away (`backend/mandate/dialogue.py`).

| Limit | Default | Enforced in |
|---|---|---|
| Total / per trade / daily max loss | $5 / $1 / $2 (hard max $500 / $100 / $500) | mandate, guard `OVER_BUDGET`, `backend/execute/engine.py` |
| Network fee cap | 0.002 SOL (hard max 0.01) | guard `FEE_CAP` |
| Research budget | $1.00 per session (hard max $5), top-up needs your approval | `backend/research/budget.py` |
| x402 sellers | `api.nansen.ai`, `x402.quickintel.io`, `pro-api.coinmarketcap.com`, pay-to, network and price pinned | `backend/config.py`, `backend/research/sellers.py` |
| Max price per call | $0.10 | `backend/research/x402_client.py` |
| Buy approval | "approve" + two-digit code, void after 3 failures, 180 s timeout | `backend/execute/cards.py` |
| Own words only | raw transcript from the browser's `UtteranceStore`, checked on the backend, echoes of the agent rejected | `frontend/src/voice/utterances.ts`, `backend/voice/verify.py` |
| Slippage / price impact | 300 bps buy, 1000 bps stop-loss sell / 2% | guard `SLIPPAGE`, `PRICE_IMPACT` |
| ATR stop if you set none | ATR(14) 15-min x 1.75 (or 1-h x 1.5), clamped to 5-25%, fallback 10% | `backend/market/volatility.py` |
| De-risk offers | +10% sell half, +20% break-even stop (offers only) | `backend/config.py` |
| Mainnet extras | $5 per-buy ceiling, 0.01 SOL fee reserve, balance re-checked | `backend/execute/engine.py` |

## 4. The guard

`check_transaction` in `backend/guard/pipeline.py`. If decoding or simulation fails for any reason, the trade is blocked.

First we decode the signed bytes and read the lookup tables from the chain (`backend/guard/simulate.py`). Then we simulate. On the fork that's `surfnet_profileTransaction`, on mainnet it's `getMultipleAccounts` plus `simulateTransaction` with sigVerify.

Then the deterministic rules in `backend/guard/rules.py`. Default deny, 25 block codes (`PROGRAM_NOT_ALLOWED`, `SIGNERS`, `FEE_CAP`, `OVER_BUDGET`, `MIN_OUT`, `SOL_OUTFLOW`, `OTHER_TOKEN_DECREASE`, `APPROVE`, `SET_AUTHORITY` and more). A BLOCK is final and JEV never gets asked.

Only if the rules allow do we build a structured view for JEV (`backend/guard/semantic.py`, called through OpenRouter in `backend/judge/jev.py`). It uses role labels, no addresses and no token names. Risk = 1 - P(safe). Risk of 0.5 or more, or any error, turns into `ask_human`. JEV can only make things stricter.

Here's a real one, card 14, Guard run `4a808a2fde084e5ca2b97dd58e3c8332`. 36 rules passed, JEV risk 0.12, allow. From `docs/evidence/normal-same-run-guard.json`:

```json
{"mandate_allows": {"side": "buy", "input": "USDC (input)", "output": "target token (output)",
  "max_input_amount": 500000, "min_output_amount": 497032963, "max_fee_lamports": 2000000},
 "simulated_effects_on_user_wallet": {"input_spent": 500000, "output_received": 512404810,
  "other_token_changes": {}, "network_fee_within_max_fee": true,
  "sol_leaving_wallet_beyond_fee_and_refundable_rent": false, "token_account_authority_or_delegate_changes": []},
 "top_level_programs": ["ComputeBudget", "ComputeBudget", "AssociatedTokenAccount", "AssociatedTokenAccount",
  "JupiterAggregatorV6", "SPL Token"],
 "inner_programs_invoked_by_aggregator": ["JupiterAggregatorV6", "SPL Token", "System", "Token-2022", "1 AMM programs"]}
```

Injection test: with `VIBE_REDTEAM=1` there's a test coin whose metadata (`config/redteam_token.json`) gets a modelled naive builder (`backend/copilot/redteam.py`) to append a USDC transfer to the attacker. The rules blocked it and JEV was never asked. That's Guard run `5baac606…`, in `docs/evidence/blocked-same-run-guard.json`.

JEV scores we actually measured. Card 14 above was 0.12. The `runs/m6_smoke_*.json` files (14 of them have a score, they're local and gitignored like the rest of `runs/`) were 0.04-0.08 on normal buys. `runs/m5_20261110_153206.json` and `runs/m5_20261110_153023.json` were 0.07-0.36, all allow. Earlier, in `runs/m5_20261110_152831.json`, normal buys got 0.64-0.75 and `ask_human`. Those were false positives from before we fixed the view. On mainnet (`docs/evidence/mainnet_trades.json`) the BONK and WIF buys both scored 0.05. A USDC to native SOL swap got 0.96 and `ask_human`, so we didn't send it.

## 5. Solana

Swaps go through Jupiter `GET /swap/v2/build`. We assemble the v0 transaction ourselves and fetch the lookup tables from the chain instead of trusting Jupiter's mapping (`backend/trade/jupiter.py`). Mints get SPL and Token-2022 checks in `backend/discovery/free_checks.py`. The output account is made with ATA `Create` / `CreateIdempotent`, and the owner has to be the user. We don't deploy any programs of our own.

The guard's top-level allowlist (`backend/guard/rules.py`):

| Program | ID |
|---|---|
| Jupiter Aggregator v6 | `JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4` |
| SPL Token | `TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA` |
| Token-2022 | `TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb` |
| Associated Token Account | `ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL` |
| Compute Budget | `ComputeBudget111111111111111111111111111111` |
| System | `11111111111111111111111111111111` |

Discovery also looks at PumpSwap `pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA` and Raydium CPMM `CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C` pools.

To be honest about the network: most of our trading was on a Surfpool fork. That's 95 swaps (46 buys, 49 sells, `runs/m5_trades_*.json`, `runs/m6_trades_*.json`) on :8899, with test buys on :8999.

Then we did four real mainnet swaps from a box wallet, `2hXDjgGJA5JKnE72RKjPdHTdqbCyMh34ae6jKmoAcxxN`, in mainnet mode. They didn't go through the voice UI. A script called the repo's Jupiter build, the full guard and `Executor.send` (`backend/execute/executor.py`). Settings were 1% slippage, a 0.02 SOL reserve and `maxAccounts=40`. All four are finalized, details in `docs/evidence/mainnet_trades.json`.

| Pair | In | Out | Guard | Slot | Solscan |
|---|---|---|---|---|---|
| USDC to BONK | 1.000000 USDC | 443,760.02 BONK | allow, JEV 0.05 | 471591328 | [61e5RnZD…](https://solscan.io/tx/61e5RnZD756DzYWKSP1oVM1FCTMddKdgwcJTnnmsYYe4tcGMbpV3vXiwnFJ5yMsXami7uDLg3gkqemaqoZ4J4CYA) |
| BONK to USDC | 443,760.02 BONK | 0.999055 USDC | allow (rules only, same as listener sells) | 471591445 | [2ZyCXccK…](https://solscan.io/tx/2ZyCXccKBgSibCwGSXx3Ld9apqZea7rwN1ucBqqkRb64MWqDuMS4sNB6bkYPtAhbEuuijJ1xNfnHL1Bqqiq4J9Hv) |
| USDC to WIF | 1.000000 USDC | 7.710649 WIF | allow, JEV 0.05 | 471592981 | [5k5yRmf2…](https://solscan.io/tx/5k5yRmf2SciFm8KYm7sr8B7x6n3FWMukkKCgsEG9PkaprNggefyS4qrdqZPc2MJjubejaBKaShcT4LjjQew4H6KJ) |
| WIF to USDC | 7.710649 WIF | 0.997953 USDC | allow (rules only) | 471593025 | [6145EpTZ…](https://solscan.io/tx/6145EpTZTSzKDUfn8uZkzyTPNMEtYqNydAeQL86pKiiTxrvDRzzntqybis9GPQNChN36xGj4s6o6PjZ8yYV4igjs) |

## 6. x402 payments

Sellers and prices per call: Nansen smart money $0.05 (discovery net flow), Nansen holders $0.05 (top-10 holder share), Quick Intel scan $0.03 (dev address, scam flags). CoinMarketCap DEX search at $0.01 is registered as a cross-check but never got paid in our runs.

Everything is USDC on Base (`eip155:8453`). The client refuses a 402 challenge if the network, asset, pay-to or price doesn't match `backend/research/sellers.py`, or if it's over $0.10. After paying, `verify_transfer` (`backend/research/x402_client.py`) wants a successful receipt with a USDC `Transfer` log from the payer to `pay_to` for the right amount.

We have 17 settled payments, $0.67 total. Two of them: Quick Intel [0x8fa7b14f…](https://basescan.org/tx/0x8fa7b14f5b12011ed0b5126bd9e975cf5eedbebf849d08d21cceecf8ba756efc) and Nansen [0x354c2cee…](https://basescan.org/tx/0x354c2cee917369b622401392e53a905a1c6ea31e5b0c7061ad06f142021c63c1). The full list is in `docs/evidence/x402_payments.md`. Rehearsal mode never pays (`docs/rehearsal.md`).

## 7. ElevenLabs

The whole interface is an ElevenLabs Agents (Conversational AI) Realtime session. Model `eleven_realtime_v1_max`, interruptions on, turn eagerness "patient", speculative turn off, no idle re-engagement (`backend/voice/agent_config.py`).

It has 14 client tools that run in the browser (`frontend/src/voice/tools.ts`): `submit_mandate_fields`, `confirm_mandate`, `respond_to_card`, `respond_to_topup`, `place_orders`, `suggest_stop`, `cancel_settings_change`, `skip_coins`, `stop_research`, `simulate_price_move`, `get_status`, `emergency_exit`, `manage_position`, `skip_turn`.

Auth is a signed URL from `GET /voice/signed-url` (`backend/app.py`), so the API key never leaves the backend. Backend events reach the agent with `sendUserMessage`, through a broadcast queue in `frontend/src/voice/broadcast.ts`. There's a wake word too. When idle it only answers to "Vibe", and side talk gets `skip_turn` (`frontend/src/voice/engagement.ts`).

The shadow listener (`frontend/src/voice/shadow.ts`, `backend/voice/shadow.py`) is a bit hacky. It grabs a spoken filter change from the transcript before the agent's tool call even arrives. Median time went from 2,827 ms to 1,746 ms (2.8 s to 1.7 s), 5 trials each (`docs/evidence/shadow_latency_20261111_164021.json`, `scripts/shadow_latency.py`).

I did the demo video narration with ElevenLabs TTS. `scripts/partials_probe.py` uses TTS as well.

## 8. Architecture

```
Browser (Vite + React)         FastAPI backend                    Outside
+----------------------+       +---------------------------+      +--------------------------------------+
| ElevenLabs agent     |<----->| mandate      orchestrator |----->| Nansen, Quick Intel (x402, Base)     |
| 14 client tools      |       | research + budget ledger  |----->| DexScreener, GeckoTerminal           |
| UtteranceStore, UI   |       | cards        guard        |----->| Jupiter, JEV via OpenRouter          |
+----------------------+       | listener     event bus    |----->| Surfpool :8899/:8999, Solana RPC     |
                               +---------------------------+      +--------------------------------------+
```

Where things live:

- `backend/app.py`, `backend/orchestrator.py`, `backend/events.py`: API, signed URL, the session loop, event bus
- `backend/mandate/`: limit schema, dialogue, hard rules
- `backend/discovery/` and `backend/market/`: smart money, DexScreener, free checks, sell test, GeckoTerminal ATR stop
- `backend/research/`: x402 client, sellers, budget ledger, cache
- `backend/trade/` and `backend/execute/`: Jupiter build and v0 assembly, then cards, executor, listener
- `backend/guard/`: decode, simulate, rules, JEV view, evidence
- `backend/judge/`: JEV and LLM calls (OpenRouter)
- `backend/voice/`, `frontend/src/voice/`: agent config, transcript checks, engagement, shadow listener, tools

## 9. Setup

You need Windows 10/11 or Linux, Python 3.11+ (we ran the tests on 3.11), Node.js 20+ and Surfpool 1.7.0.

Variables in `.env.example` are `ELEVENLABS_API_KEY`, `ELEVENLABS_AGENT_ID`, `JUPITER_API_KEY`, `SOLANA_RPC_URL`, `SURFPOOL_RPC_URL`, `SCRATCH_SURFPOOL_RPC_URL`, `LLM_API_KEY`, `JEV_API_KEY`, `TRADING_WALLET_KEYPAIR_PATH`, `RESEARCH_WALLET_KEYPAIR_PATH`, `BASE_RESEARCH_WALLET_PRIVATE_KEY`. Optional ones: `VIBE_DISCOVERY`, `VIBE_RESEARCH`, `VIBE_UI_BUTTONS`, `VIBE_REDTEAM`, `VT_NETWORK`, `VT_MAINNET_MAX_TRADE_USD`, `VT_MAINNET_MIN_SOL`. `.env`, `secrets/`, keypairs and `runs/` are gitignored. Please don't commit real keys.

```powershell
copy .env.example .env; .\scripts\dev.ps1 -Setup   # venv, npm packages, Surfpool binary
.\scripts\dev.ps1                 # rehearsal: replayed discovery, cached research, local fork, no money
.\scripts\dev.ps1 -Mode live      # paid x402 research, trading still on the fork
.\scripts\dev.ps1 -Mode mainnet   # real funds; type MAINNET to continue
.\scripts\stop_dev.ps1            # stop (.\scripts\stop_surfpool.ps1 for the forks)
```

On Linux `scripts/dev.sh` does all of it. By hand it's `scripts/start_surfpool.sh`, `python scripts/fund_wallets.py`, `python -m uvicorn backend.app:app --port 8000`, then `cd frontend && npx vite --port 5173`. More detail in `docs/run_on_your_pc.md`.

For the red-team demo set `VIBE_REDTEAM=1`, start rehearsal mode and order the extra test coin. Its card gets blocked.

Mainnet preflight is `python scripts/mainnet_check.py --sim 1` (read-only). Tests: `python -m pytest -q -m "not live"` (499 passed) and `cd frontend && npm test -- --run` (102 passed).
