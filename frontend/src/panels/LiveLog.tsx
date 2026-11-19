import type { AppState, BackendEvent } from "../types";
import { GuardDetails } from "../ui/GuardDetails";
import { DECISION_LABEL, short, usdF } from "./format";

const TYPE_LABEL: Record<string, string> = {
  mandate_frozen: "Limits set", candidates_found: "Candidates", free_check_result: "Free checks", research_payment: "Research payment",
  research_topup_request: "Top-up request", research_topup_result: "Top-up result", research_done: "Research done", card_created: "Card",
  card_expired: "Card timed out", card_rejected: "Card rejected", card_voided: "Card void", approval_rejected: "Approval refused",
  guard_blocked: "Guard blocked", trade_executed: "Filled", trade_failed: "Trade failed", exit_triggered: "Exit triggered",
  sell_failed: "Sell failed", price_feed_down: "Price feed down", price_feed_restored: "Price feed back", emergency_exit: "Emergency exit",
  voice_rejected: "Words not used", pipeline_status: "Progress", desk_ready: "Selection desk", cards_ready: "Cards ready", coins_skipped: "Skipped",
  mandate_amended: "Settings updated", round_started: "New round", amend_opened: "Changing settings", amend_cancelled: "Change cancelled",
  price_test: "Price test",
};

export function LiveLog(props: { events: BackendEvent[]; state: AppState | null }) {
  const spend = props.state?.spend;
  // sell / exit-rule cards have no buy simulation
  const sims = (props.state?.engine?.cards ?? []).filter((c) => !c.kind || c.kind === "buy").slice(-5).reverse();
  // only events with words: a bare label + signature ("Filled 5xY…") says nothing
  const log = props.events.filter((e) => e.type !== "mandate_updated" && e.type !== "session_reset" && !!(e.speak ?? "").trim()).slice(-40).reverse();
  return (
    <div className="livelog rows-sections">
      <section aria-label="Spend list">
        <h4>Research spend <span className="num">{spend ? `${usdF(spend.spent_usdc)} of ${usdF(spend.budget_usdc)}` : ""}</span>
          <span className="tag">{props.state?.modes.research === "live" ? "Real money" : "Recorded data, no spending"}</span></h4>
        <ul>
          {(spend?.entries ?? []).map((e, i) => (
            <li key={i}>
              <span>{e.seller}</span> <span className="num">{usdF(e.amount_usdc)}</span> <span className="soft">{e.reason}</span> <span className="soft">{short(e.coin_mint)}</span>{" "}
              {e.tx_hash ? <a href={`https://basescan.org/tx/${e.tx_hash}`} target="_blank" rel="noreferrer">{short(e.tx_hash, 8)}</a> : e.status}
            </li>
          ))}
          {!spend?.entries?.length && <li className="hint">No payments yet. Research pays only for coins that pass the free checks.</li>}
        </ul>
      </section>
      <section aria-label="Simulation results">
        <h4>Simulation results</h4>
        <ul>
          {sims.map((c) => (
            <li key={c.id} aria-label={`Card ${c.code} simulation`}>
              Card {c.code}: {usdF(c.amount_usdc)} buys {c.sim.token_out?.toLocaleString() ?? "\u2013"} tokens, price impact {c.sim.price_impact_pct.toFixed(2)}%,
              SOL {c.sim.sol_change_lamports != null ? (c.sim.sol_change_lamports / 1e9).toFixed(5) : "\u2013"}. Guard: {DECISION_LABEL[c.guard.decision] ?? c.guard.decision}
              {c.guard.jev_risk != null ? ` (JEV ${c.guard.jev_risk.toFixed(2)})` : ""}.
              <GuardDetails key={`${c.id}:${c.guard.guard_run_id ?? "legacy"}`} runId={c.guard.guard_run_id} cardId={c.id} code={c.code} />
            </li>
          ))}
          {!sims.length && <li className="hint">No simulations yet. Each card is simulated before you see it.</li>}
        </ul>
      </section>
      <section aria-label="Events">
        <h4>Events</h4>
        <ul className="events">
          {log.map((e) => (
            <li key={e.id} className={e.loud ? "loud" : ""} aria-label={`${TYPE_LABEL[e.type] ?? e.type} event ${e.id}`}>
              <time>{new Date(e.ts * 1000).toLocaleTimeString()}</time> <b>{TYPE_LABEL[e.type] ?? e.type}</b> {e.speak}
              {e.panel?.signature && (typeof e.panel.explorer_url === "string" && e.panel.explorer_url.startsWith("https://solscan.io/tx/")
                ? <> <a className="tx-link" href={e.panel.explorer_url} target="_blank" rel="noreferrer noopener"><code>{short(e.panel.signature, 8)}</code> Solscan</a></>
                : <code> {short(e.panel.signature, 8)}</code>)}
              {e.type === "guard_blocked" && <GuardDetails key={e.panel?.guard_run_id ?? `legacy-${e.id}`} runId={e.panel?.guard_run_id} />}
            </li>
          ))}
        </ul>
      </section>
    </div>
  );
}
