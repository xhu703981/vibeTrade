# Rehearsal data (VIBE_DISCOVERY=replay, VIBE_RESEARCH=cache)

## Current recording: smart-money round (2026-11-13, 6:07 PM ET)

`scripts/smart_money_record.py --pay --budget 0.25` recorded `runs/m3_20261113_180658_smartmoney.json` (newest m3 file,
so rehearsal replays it): 25 Nansen smart-money net-buying tokens (cached net-flow response, no new charge) ->
free checks: 5 survive -> free ranking (same `rank_score` as the app) -> paid research only for the top 3:

| Mint (prefix) | Smart money 24h | Research | Paid |
|---|---|---|---|
| GexThZHg… | +$4,649, 5 wallets | pass | Quick Intel $0.03 + Nansen holders $0.05 |
| Ai66LHZG… | +$5,076, 10 wallets | pass | Quick Intel $0.03 + Nansen holders $0.05 |
| 2j5SaS7x… | +$5,006, 19 wallets | pass | Quick Intel $0.03 + Nansen holders $0.05 |

Spend: $0.24 (ledger `runs/research_spend_test_20261113_180658.json`, results `runs/m4_20261113_180658_smartmoney.json`),
research wallet $1.17 -> $0.93. The app ranks free-check survivors the same way in rehearsal and live mode and researches
only the top 3, so the desk shows exactly these coins with these receipts ("paid when recorded" + tx).

## Earlier recording (kept for reference)

Rehearsal mode never pays. It replays the newest `runs/m3_*.json` and reads research from
`runs/research_cache/` (both gitignored, local to the demo machine).

Recorded 2026-11-13 (box), file `runs/m3_20261113_163000_rehearsal.json`, cached research:

| Coin | Mint (prefix) | Research result |
|---|---|---|
| GATO | HsprHxBR… | pass (top-10 15.2%, dev 3.2%) |
| Ansemmas | HXxJdaQb… | pass (top-10 26.8%, dev 0.1%) |
| CBUCKS | DtcicPCK… | pass |
| AMC | HWya5CpQ… | fails: seller text judged risky by JEV (shows a research rejection) |

Spend to record it: $0.19 x402 (Quick Intel x3, Nansen holders x2), ledger `runs/research_spend_test_20261113_162855.json`.
To refresh: `scripts/m3_run.py`, then `scripts/m4_run.py --budget 0.20 --approve none` (hard cap; stops instead of topping up).

## Smart-money discovery (P2)

Candidates come from Nansen smart-money net flow (Solana, 24h, net buying only, market cap $100k-$20M) via x402,
with DexScreener trending/boosted lists as the fallback. Live verification: one call on 2026-11-13, $0.05,
tx https://basescan.org/tx/0x354c2cee917369b622401392e53a905a1c6ea31e5b0c7061ad06f142021c63c1 (25 tokens).
The response is cached as `runs/research_cache/nansen_smart_money_solana-netflow-24h.json`; rehearsal reads it and
annotates replay coins that appear in it. `VIBE_SMART_MONEY=0` turns the source off. Probe: `scripts/smart_money_probe.py`
(hard-capped at $0.05 by its own test ledger).
