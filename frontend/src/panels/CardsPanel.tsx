import { useState } from "react";
import { GuardDetails } from "../ui/GuardDetails";
import type { Card } from "../types";
import { useNow } from "../useBackend";
import { DECISION_LABEL, DECISION_TONE, MINUS, big, pct, usdF } from "./format";

export function secondsLeft(c: Card, now: number) {
  return Math.max(0, Math.round(c.expires_at - now));
}

export function CardView(props: {
  card: Card;
  now: number;
  buttonsEnabled: boolean;
  onApprove: (c: Card) => void;
  onReject: (c: Card) => void;
}) {
  const { card: c, now } = props;
  const left = secondsLeft(c, now);
  const total = Math.max(1, c.expires_at - c.created_at);
  const pending = c.status === "pending" && left > 0;
  const g = c.guard.decision;
  return (
    <article className={`card ${pending ? "pending" : "done"} guard-${g}`} aria-label={`Card ${c.code}`}>
      <header>
        <span className="code num" title="Approval code">{c.code}</span>
        {c.kind && c.kind !== "buy" && <span className="tag">{c.kind === "sell" ? "Sell" : "Exit change"}</span>}
        {c.ticker && <span className="tag">{c.ticker}</span>}
        {c.coin_number != null && <span className="tag">Coin {c.coin_number}</span>}
        <span className="sym">{c.symbol_display || "No symbol"}</span>
        {(!c.kind || c.kind === "buy") && <span className="amt num">Buy {usdF(c.amount_usdc)}</span>}
        <span className={`verdict ${DECISION_TONE[g] ?? ""}`}>{DECISION_LABEL[g] ?? g}</span>
      </header>
      <dl className="metrics">
        <dt>Liquidity</dt><dd>${big(c.metrics.liquidity_usd)}</dd>
        <dt>Market cap</dt><dd>${big(c.metrics.market_cap)}</dd>
        <dt>Top 10</dt><dd>{pct(c.metrics.top10_pct, 0)}</dd>
        <dt>Dev</dt><dd>{pct(c.metrics.dev_pct, 2)}</dd>
        <dt>Holders</dt><dd>{big(c.metrics.holders)}</dd>
        <dt>Round-trip loss</dt><dd>{pct(c.metrics.roundtrip_loss_pct)}</dd>
        <dt>Sim price impact</dt><dd>{pct(c.sim.price_impact_pct, 2)}</dd>
        <dt>Sim tokens out</dt><dd>{big(c.sim.token_out)}</dd>
      </dl>
      {c.stop_loss_pct != null && (!c.kind || c.kind === "buy") && (
        <p className="small stopline">Stop at {MINUS}{Math.abs(c.stop_loss_pct).toFixed(1)}%, {stopBasis(c)}{c.risk_usdc != null ? ", sized from your risk" : ""}.</p>
      )}
      {c.loss_plan_usdc != null && c.loss_worst_usdc != null && (
        <p className="small lossline">Planned loss about {usdF(c.loss_plan_usdc)}. Worst case about {usdF(c.loss_worst_usdc)} if the price gaps past the stop.</p>
      )}
      {c.cap_note && <p className="small capnote">{c.cap_note[0].toUpperCase() + c.cap_note.slice(1)}</p>}
      {c.warning && <p className="small pr-warn"><i aria-hidden />{c.warning}</p>}
      {c.guard.reasons.length > 0 && (
        <ul className="reasons">{c.guard.reasons.map((r, i) => <li key={i}>{r}</li>)}</ul>
      )}
      <GuardDetails key={`${c.id}:${c.guard.guard_run_id ?? "legacy"}`} runId={c.guard.guard_run_id} cardId={c.id} code={c.code} />
      {pending ? (
        <footer>
          <div className="drain" role="timer" aria-label="Time left to approve"><span style={{ width: `${(100 * left) / total}%` }} /></div>
          <p className="say">Say "approve {c.code}" or "reject {c.code}"</p>
          <button className="ghost-btn" onClick={() => props.onReject(c)}>Reject</button>
          <button className="primary" disabled={!props.buttonsEnabled} title={props.buttonsEnabled ? "" : "On-screen button channel is off (VIBE_UI_BUTTONS=1)"}
                  onClick={() => props.onApprove(c)}>
            Approve {c.code}
          </button>
        </footer>
      ) : (
        <footer><span className={`status s-${c.status}`}>{cardDone(c)}</span></footer>
      )}
    </article>
  );
}

export function stopBasis(c: Card): string {
  if (c.stop_basis === "atr") return `set from volatility${c.atr_pct != null ? ` (ATR ${c.atr_pct}%)` : ""}`;
  if (c.stop_basis === "you") return "your number";
  if (c.stop_basis === "mandate") return "your default";
  return "default (not enough candles)";
}

/** One word per outcome, the same word as the action: "Card 47 filled", "Card 47 rejected". */
const STATUS_WORD: Record<string, string> = {
  pending: "timed out", approved: "approved, buying now", rejected: "rejected", expired: "timed out, nothing was bought",
  voided: "void", executed: "filled", failed: "failed, nothing was bought",
};
export const cardDone = (c: Card) => `Card ${c.code} ${STATUS_WORD[c.status] ?? c.status}`;

export function CardsPanel(props: {
  cards: Card[];
  buttonsEnabled: boolean;
  onApprove: (c: Card) => void;
  onReject: (c: Card) => void;
}) {
  const now = useNow();
  const [showOld, setShowOld] = useState(false);
  const pending = props.cards.filter((c) => c.status === "pending");
  const old = props.cards.filter((c) => c.status !== "pending").slice(-4).reverse();
  return (
    <section className="panel" aria-label="Confirmation cards">
      <h3>Confirmation cards {pending.length > 0 && <span className="tag">{pending.length} waiting</span>}</h3>
      {pending.length === 0 && <p className="hint">No cards waiting. Say a coin and an amount to get one ready.</p>}
      {pending.map((c) => <CardView key={c.id} card={c} now={now} {...props} />)}
      {old.length > 0 && (
        <button className="text-btn" onClick={() => setShowOld(!showOld)}>{showOld ? "Hide past cards" : `Show ${old.length} past card${old.length === 1 ? "" : "s"}`}</button>
      )}
      {showOld && old.map((c) => <CardView key={c.id} card={c} now={now} {...props} />)}
    </section>
  );
}
