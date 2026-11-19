# Guard evidence (read only)

The same Guard run that builds an approval card now captures its explanation. Expanding
**Show what the checks found** only reads this capture; it cannot rerun a quote, simulate,
call JEV, pay for research, approve or send a transaction. Guard ordering, rule predicates,
thresholds and approval requirements are unchanged.

Vite forwards `/api` to FastAPI. The FastAPI paths are:

- `GET /cards/{code}/guard_view?card_id=…&guard_run_id=…` for the card's original approval run.
- `GET /guard-runs/{guard_run_id}/guard_view` for the precise run linked in a blocked event.

Recycled codes require `card_id`. A replacement card never substitutes for the original card.
Approval-time rechecks get separate run IDs; blocked recheck events link that recheck, not the
original passed check. A missing/evicted/legacy capture returns 404, not reconstructed data.

Each completed capture contains:

1. Signed transaction byte length, first 60 base64 characters, SHA-256 provenance (not the full transaction).
2. Actual top-level decoded instruction categories/discriminators, allowlist results from the executed
   checks, and count of ALT indexes actually resolved by the Guard. No table/account addresses.
3. `surfnet_profileTransaction` pre/post balances of the profiled wallet accounts: USDC and target
   token in base units, SOL in lamports. These are not a total portfolio balance. Unknown is null;
   only explicit account creation/deletion justifies zero. No new RPC is made by the explanation.
4. `structured_json`: the identical safe `build_state` string supplied to `jev.ask`; `structured_view`
   is its parsed form. No addresses, token names, descriptions or token-supplied instructions.
   `structured_status` distinguishes `sent_to_jev`, `not_sent` and `unavailable`. A submitted
   request does not prove provider delivery if the call failed: errors and an absent risk say so.
   If rules block, no state is sent or retroactively generated.
5. Only predicates actually evaluated, in their execution order, with Pass/Block results. Skipped
   quote checks and checks after an early state failure are not labeled passed.
6. The actual JEV question, observed risk and existing threshold, or an explicit not-asked reason.
   A rule block records `Not asked: blocked by rules`. Missing/failed answers are never zero risk.

Capture is best-effort and context-isolated for concurrent runs. Capture errors cannot loosen,
block, or otherwise change a Guard decision. Records are process-memory-only, bounded to the
latest 512 completed runs; a restart discards them. Card and event history are also in-memory.
Do not restart the backend between preparing and recording a demo. Normal card expiry still
applies; there is no special demo timer or approval bypass.

Both the approval sheet and blocked Guard sheet render six plain sections. The read-only details
are not sent to the voice agent. Legacy cards/events and unavailable data are labeled honestly.

## Exact history navigation

Details → Cards → Show past cards → the selected card's own “Show what the checks found” reads that card ID and captured run ID, including after timeout. Details → Activity → Simulation results exposes the same binding per card; each Guard blocked event has its own expansion. These never use the globally latest block as a substitute. No expired card gains approval controls.

For a retained run even after a desk/session reset, open `/?guard_run_id=<exact-run-id>` on the frontend. This standalone read-only page GETs only `/guard-runs/<exact-run-id>/guard_view` and does not load voice, reset/confirm a mandate, or need a current engine. A missing/evicted capture or backend restart stays unavailable; saved JSON is not injected.

The displayed run ID identifies the capture. Simulated balances remain actual raw base units / lamports, explicitly labelled; no token decimals or balances are inferred.
