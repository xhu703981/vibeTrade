# M6: Voice + meeting UI — start-up and demo

## One-command start

On a Windows PC, prefer PowerShell (no Ubuntu): see `docs/run_on_your_pc.md`. First time `.\scripts\dev.ps1 -Setup`, then `.\scripts\dev.ps1`. Open the URL the script prints. Stop with `.\scripts\stop_dev.ps1`.

On Linux / macOS, or if you still use WSL: Surfpool installed; a `.venv` built with `pip` or `uv` (`pip install -r requirements.txt`); Node 20+.
`.env` must contain `ELEVENLABS_API_KEY`, `ELEVENLABS_AGENT_ID`, `JUPITER_API_KEY`, `SOLANA_RPC_URL`, `LLM_API_KEY`, `JEV_API_KEY`
(live mode also needs `BASE_RESEARCH_WALLET_PRIVATE_KEY`).

```bash
scripts/dev.sh          # rehearsal mode: replays the last coin-discovery run + uses only already-bought research cache; spends no real money
scripts/dev.sh live     # live mode: live discovery + real-money x402 research (asks you to type yes first)
```

The script starts two Surfpool forks (8899 / 8999) -> gives the temporary fork trading wallet 2 SOL + 50 USDC -> starts the backend on `:8000` -> starts the frontend on `:5173`.
Open **http://127.0.0.1:5173** in Chrome or Edge and allow the microphone. On Windows the PowerShell start keeps running after the window is closed; stop with `.\scripts\stop_dev.ps1`. On Linux, Ctrl-C stops backend and frontend; stop Surfpool with `scripts/stop_surfpool.sh`.

You can check the whole chain without a browser: in another terminal run `python scripts/m6_smoke.py --approve` (typed channel through the Vite proxy, from mandate to fill to emergency exit).

## The screen

- Left: the agent (Vibe-Trader) avatar, with a waveform while it speaks. Right: you, with a microphone waveform while you speak.
- Main stage after research: the **selection desk**. Every coin that passed is a numbered tile (coin 1..N) with the numbers a trader compares: price and 1h / 6h / 24h change, market cap, liquidity, 24h volume, volume/liquidity (flagged "wash?" near the mandate max), age, holders, top-10 %, dev %, round-trip loss, 1h / 24h buys vs sells, LP burned/locked, mint/freeze revoked, a paid-promo badge, and short pass / caution notes. Above the tiles is a K-line chart (5m / 15m / 1h / 4h / 1d candles from GeckoTerminal through the local backend; click "Chart" on a tile to switch coins; a DexScreener embed is the fallback). Tick any number of coins, give each an amount (or one "same amount for all"), and **Create cards**: one card per coin. Coins that did not pass are in a collapsed list.
- Right column: research top-up prompt (appears when the budget runs out), confirmation cards (two-digit code, countdown, Approve / Reject buttons), positions and P&L (Emergency exit; in rehearsal mode also **Test take profit** / **Test stop loss**), the mandate settings table (click "Edit" to type a new value; it stays editable while the session runs, see step 11), candidate coins (during discovery / research).
- Bottom: research spend list (with Base transaction links), simulation results, live event log. Guard blocks, failed sells, etc. show a red alert at the top.

## Demo walkthrough (about 3 minutes) — say these lines in English

The agent keeps every turn to one or two short sentences and never reads the settings table aloud; the full settings are always on the right of the screen.

1. Click **Start voice**. The agent asks only how many dollars in total and the most per trade.
   You say: "**I want to put in 50 dollars total, and at most 10 dollars per trade.**"
   - No fee needed: the code sets the fee cap to the default 0.002 SOL and says so once. If you want your own fee cap you can say it in SOL or in dollars/cents, e.g. "**max 20 cents in fees**" (converted to SOL with the live SOL price; if no price is available, nothing is guessed and it asks again).
2. The agent points you at the **default coin filters** on the right as its proposal and asks whether to change any.
   Change one, e.g. "**Set min liquidity to 80000 dollars.**" (you stay in this step), or accept: "**Looks good.**" (also "keep these", "no changes", "that's good").
3. The agent asks for stop loss and take profit. You say: "**Stop loss at 30 percent, and sell half when it hits 2x.**"
   - Take profit can also be a percent gain: "**Stop loss 20 percent, take profit 30 percent.**" is stored as the ladder `[{multiple: 1.3, sell_pct: 100}]` (sell everything at +30%) unless you say how much to sell ("take profit 50 percent, sell half" -> 1.5x, 50%). A take-profit percent is never used as the stop loss, and a bare "30 percent" answered when only take profit is missing fills take profit.
   - Fillers such as "I guess", "um", "let's just" do not make a definite number vague ("I guess stop loss would be 20%" is accepted); real hedges such as "maybe" / "around" / "about" are still refused. Cents work for the budget too ("five cents total", "one cent per trade").
   - If part of an answer is captured, the agent says what was noted and only what is still needed.
   (Giving stop loss / take profit earlier also works; the filters then stay as shown. "Start researching" / "let's start" in the filter step just means "keep these filters and continue".)
4. The agent says the full settings are on the right and asks you to say "confirm". You say "**Confirm.**" The mandate freezes and coin discovery starts.
5. Short system updates: candidates found, how many pass the free checks, what research cost. Payments appear in the spend list (rehearsal mode shows "cache replay, no spending").
   - In live mode, when the budget runs out the agent asks "How much should I add?"; say "**Add 0.5 dollars.**" or "**Don't add any.**"
6. **Selection desk.** When research is done nothing is built yet. The agent says how many coins qualified and the limits ("2 coins qualified; they are on the selection desk as coins 1 to 2. ... Max $10 per trade, $50 left in the session."). Pick any number of coins and amounts: "**Coin 1 and coin 2, 5 dollars each.**", "**Coin 1 five dollars and coin 2 two dollars.**", "**the first one, 3 dollars**", or "**skip coin 2**". One amount for several coins needs "each". Each amount must be within the per-trade cap and the sum within what is left in the session (pending cards count). Max per trade is only a cap, the agent never picks coins or amounts itself. The desk on screen does the same with checkboxes.
7. One confirmation card per coin appears: "Card 47: coin 1, 5 dollars; Card 12: coin 2, 5 dollars. Approve each one by its code, for example 'approve 47'." You say "**Approve 47.**" and "**Approve 12.**" ("change 47 to 3 dollars" still modifies a card; rejecting a card puts the coin back on the desk.)
   To show the safety checks: say just "**Approve.**" without the code, or a wrong code; it is refused. No answer within 180 seconds counts as a reject.
8. After the fill the listener watches the price and the agent stays **silent** (no "are you still there?") until a system update arrives or you speak. Ask "**What's the status? How much did research cost?**" and the agent reports the numbers.
9. **Test take profit / stop loss (rehearsal only).** Click **Test take profit** or **Test stop loss** under Positions (or say "**simulate take profit**" / "**simulate stop loss**"). A whale wallet on the local Surfpool fork swaps into the pool until the price crosses your take-profit (or stop-loss) line; the listener then sells as usual and the agent speaks only because of that sell update. Shown and accepted only when discovery and research are in rehearsal mode and the RPC is the local fork; refused in live mode and never possible on mainnet.
10. Say "**Sell all.**" for the emergency exit.
11. **Keep going: change the rules and hunt again.** The session is a living portfolio: cash, open positions, trade history and research spend carry over; there is no reset. After a sell (or any time no card is waiting and no swap is executing) say e.g. "**I want another research round with market cap below 400,000.**" A coin-filter change gets a light confirm: the agent restates it and starts the new round ("Settings updated, version 2 ..."). A guard change (budget, per-trade cap, fees, default stop loss / take profit) is restated and needs a clear "**confirm**" (or the Confirm button). "**Research again**" runs a new round with the same settings. Open positions keep the exit rules they were bought with; pending cards are voided when you change settings; the new round replaces the desk but keeps the card and trade history. Settings changes are paused only while research is running, a top-up is waiting, or a swap is executing.

Demo without a microphone:
- **Text chat (agent)**: type to the agent; tools run as usual and the agent replies in text.
- Typing in the input box without the agent: direct mode, which routes what you type to the right endpoint for the current phase (mandate, buy amount, card, top-up, rehearsal price test). The backend checks are the same as for voice.

Chinese phrasings (e.g. "批准 47", "确认", "全部卖掉") are still accepted as aliases, but English is the primary path.

## Safety design (the voice-related parts)

- The API key stays on the backend: the browser calls `GET /api/voice/signed-url` and the backend exchanges the key for a one-time signed URL. Agent authentication is on, so nothing connects without a signed URL.
- All tools are client tools that run in the browser and call the local backend directly; no webhooks or tunnels.
- For state-changing tools (fill mandate, confirm, approve card, top up, emergency exit), the user's own words are attached separately by the frontend from the speech transcript. The agent cannot pass those words, and the backend only trusts them:
  - Approve card: the words must contain "approve" (or "confirm" / "go ahead") and this card's two-digit code, with no negation ("don't approve" does not count);
  - Top-up: the words must agree to add and say an amount;
  - Desk orders: the words must name the coin numbers and the amounts ("each" for one amount over several coins); the agent's numbers must match them; it cannot pick coins or amounts itself. Token names never reach the agent (coin numbers and card codes only), and chart data is screen-only;
  - Emergency exit: the words must say "sell all" / "emergency exit" (or similar) with no negation.
  - Each utterance can approve only once and expires after 90 seconds.
- Backend events reach the agent through `sendUserMessage("[System update] ...")` (urgent ones use `[URGENT update]`). This text is never treated as the user's words:
  - the frontend never records marked text as an utterance;
  - the backend rejects any utterance that carries a marker or repeats a recent broadcast (`voice_rejected`).
- The agent only gets numbers, codes, and text we wrote ourselves; token names and third-party text are never given to it. Token names on screen keep only letters and digits, and reason text is sanitized.

## ElevenLabs agent configuration

Configure with `python scripts/configure_elevenlabs_agent.py` (add `--dry-run` to see the changes without writing). It is safe to run again. Backups of the previous config go to `secrets/elevenlabs_agent_backup_*.json` (gitignored).
It sets: system prompt (English, concise, funds -> filter review -> exits -> confirm, duplex rules), first message (English, asks only total and per-trade), language (`en`), 10 client tools (all `immediate` + expects a response, interruptions allowed, pre-tool speech off; new: `stop_research`), client_events (adds `client_tool_call`, `interruption`, `vad_score`), max call duration (1800 seconds), authentication on, turn timing so the agent never re-engages on its own while waiting (`turn.turn_timeout` = first accepted of -1 / 300 / 120 / 60 / 30 seconds, soft timeout off, silence hang-up stays off), **patient turn eagerness**, and the **voice mode: Eleven Realtime v1 Max** (`conversation_config.realtime_model = "eleven_realtime_v1_max"`).
Stored but ignored in Realtime mode: LLM, TTS model, ASR (kept so Custom mode comes back exactly). Voice unchanged.
Revert to Custom mode: `python scripts/configure_elevenlabs_agent.py --custom` (sets `realtime_model` to null); every run backs up first.

## Duplex mode (talk while it works)

- Voice runs on Eleven Realtime v1 Max (speech-to-speech-grade latency, reasons before multi-step tool use). The browser side is unchanged: same signed URL + `ConversationProvider`; Realtime is an agent setting.
- Turn-taking: interruptions on (talk over the agent and it stops), eagerness `patient` for the whole call (per-stage eagerness needs ElevenLabs workflows; not worth it for one flow), no turn timeout, no soft-timeout fillers.
- Long work never holds a turn. The tools return in well under a second and the outcome arrives later as a `[System update]` / `[URGENT update]`:
  - `place_orders` -> `status: building_cards` (desk tiles show "Building card…"); codes arrive with `cards_ready`.
  - `respond_to_card` approve -> `status: approved, executing: true` after the words + code are checked; the fill arrives as `trade_executed` (a crash is announced as an URGENT update, never silent).
  - `emergency_exit` -> `status: selling`; each fill arrives as an update.
  - Research already ran in the background.
- During research you can change filters ("make it market cap below 300,000"): applying the change stops the old round (nothing from it goes on the desk) and starts the new one. "Stop the research" (or the **Stop research** button under the controls) stops it. In live research the payment in flight finishes (so the ledger stays exact) and no new payment starts. Only an executing trade or a running emergency exit blocks a settings change.
- The browser holds system updates while you are speaking (VAD score >= 0.5 within 1.2 s, or a final transcript within 1.5 s); urgent ones wait at most 4 s.
- A "Working on: ..." strip shows background work (`working_on` from `/state` and `get_status`).
- Safety is unchanged: every approval is still checked against your own words and the two-digit code on the backend, before anything runs in the background.

## Shadow listener (旁听通道)

- The browser streams her words to `POST /voice/partial` (`{text, final, seq}`): tentative transcripts throttled to one POST per 200 ms, finalized transcripts at once. Our own `[System update]` lines are never sent. The words are not logged.
- What Eleven Realtime v1 Max actually emits (measured with `scripts/partials_probe.py`, TTS speech streamed into a live session): **no `tentative_user_transcript` events** even with the client event enabled. Instead it finalizes one `user_transcript` per phrase, about 0.4 s after the phrase ends and about 1.1 s before the agent's tool call, plus `vad_score` about every 80 ms. So with Realtime the shadow listener works on per-phrase finals + VAD; partials are used automatically if a mode emits them.
- The backend (`backend/voice/shadow.py`) reads the words with a deterministic, regex-first pre-parser: filter changes (market cap, liquidity, holders, top-10 share, token age), "stop the research", "sell all", "approve NN" (pending codes only), "coin N" (open desk coins), a new round, a guard change.
- **Nothing commits on these words**: no mandate or filter change, no round, no paid research, no card, no swap, no approval. Commits only happen through the finalized tool calls, checked against the raw words as before. Tests cover this (`backend/tests/test_shadow_unit.py`).
- Prework (free and reversible): the reading is shown as "Hearing: market cap < $300k…" next to "Listening…" (it disappears when a tool call commits or replaces it, or after 8 s). Free GeckoTerminal candles are pre-fetched for desk coins she names. For finalized words that will go to the extractor, the mandate extractor is **primed** with the exact prompt the tool call will use. This is a pure cache: different words or a different stage simply miss. It spends one small LLM call (no x402, no ElevenLabs), usually needed a second later anyway. Turn it off with `VIBE_SPECULATE_EXTRACT=0`.
- Measured in rehearsal (`scripts/shadow_latency.py`, real LLM, 5 trials each): a filter change went from end of speech to applied in a median of 2827 ms without the shadow listener and 1746 ms with it (the tool call itself took 1275 ms vs 194 ms).
- Side talk: a cheap heuristic (talking to "mom", "honey", "hold on, guys", ... with no trading words) sends the agent an on-screen note, and the prompt tells it to stay silent and call no tool for words meant for someone else.

## Surfpool fork memory

- Cause of the ~15 GB fork: every listener poll (every 5 s while a position is open) priced the position with `surfnet_profileTransaction`. On Surfpool 1.7 each profile call keeps several MB on the fork (measured 3-14 MB per call, not capped by `--max-profiles`). Holding a position grew the fork by about 400 MB a minute (800 MB in 2 minutes measured), so a long session reached 15 GB. `/state` polling, `sync_fork_clock` and account reads add almost nothing (measured).
- Fix: listener pricing now uses plain `simulateTransaction` with the post-state of the wallet's token accounts (`wallet_token_deltas`); measured flat (+3 MB in 2 minutes holding a position). The guard still profiles each transaction it judges (one per card or trade).
- The forks also start with `--disable-instruction-profiling --max-profiles 16` (override with `SURFPOOL_MEM_FLAGS=""`), and `dev.sh` logs fork memory to `logs/fork_memory.log` every minute and warns once above `SURFPOOL_RSS_WARN_MB` (default 6000). It never kills the fork, because the fork holds the positions. After a long session, sell, then `scripts/stop_surfpool.sh` and start again.

## Appendix: real-agent text probe (scripts/m6_agent_probe.py)

The script connects to the **real** Vibe-Trader agent in text mode (no microphone) and plays the browser frontend:
the agent's client tools call the local backend, the user's words are attached by the script, and backend events are forwarded to the agent as "[System update]".
Flow: funds (no fee) -> "Looks good." on the proposed filters -> stop loss / take profit -> confirm -> updates -> just "Approve." (the agent asks for the code) -> "Approve NN." fills -> status -> "Sell all.".
Results are saved to runs/m6_agent_probe_*.json. Start `scripts/dev.sh` first. It uses a little ElevenLabs credit; trading happens only on the fork.
