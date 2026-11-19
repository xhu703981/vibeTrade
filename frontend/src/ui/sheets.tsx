// The contents of each contextual drawer. Short plain wording; numbers come from the backend as they are.
import { useState } from "react";
import type { Api } from "../api";
import { secondsLeft } from "../panels/CardsPanel";
import { getPath } from "../panels/FilterTable";
import { DECISION_LABEL, DECISION_TONE, MINUS, TRIGGER_LABEL, big, pct, signedPct, updatedAgo, usdF } from "../panels/format";
import { FlowCompare } from "../panels/GuardAlert";
import { pnl, usd } from "../panels/PositionsPanel";
import { PriceChart, fmtPrice } from "../panels/PriceChart";
import type { AppState, BackendEvent, Card, DeskOffer, Position } from "../types";
import { useNow } from "../useBackend";
import { Sparkline } from "./Sparkline";

export { signedPct };

export const coinName = (o: { kind?: string; ticker?: string | null; symbol?: string; number?: number | null }) =>
  o.kind === "major" ? (o.ticker || o.symbol || "Major") : `Coin ${o.number}`;

/** The real picks of this round (the rehearsal red-team coin stays off this list). */
export const listedPicks = (offers: DeskOffer[]) => offers.filter((o) => !o.redteam);

/** "3 of 25 coins passed. Research cost $0.24." from this round's own numbers; the cost only when money was spent. */
export function picksSource(state: AppState): string {
  const total = state.candidates?.length ?? 0;
  const passed = (state.research ?? []).filter((r: any) => r?.status === "pass").length || listedPicks(state.desk?.offers ?? []).length;
  const spent = state.spend?.spent_usdc ?? 0;
  const parts = [total ? `${passed} of ${total} coins passed.` : ""];
  if (spent > 0) parts.push(`Research cost $${spent.toFixed(2)}.`);
  return parts.filter(Boolean).join(" ");
}

/** Today's picks as table rows: number, ticker, price, 24h, sparkline, the one reason. */
export function PicksSheet(props: { api: Api; state: AppState; current?: number | null; onOpenCoin: (n: number) => void }) {
  const desk = props.state.desk!;
  const rows = listedPicks(desk.offers);
  if (!rows.length) return <p className="empty">No coins passed. Try a wider market cap in Settings, or say it.</p>;
  return (
    <div className="picks">
      <ol className="pick-rows">
        {rows.map((o) => {
          const ch = o.metrics.price_change_h24;
          const status = o.status !== "open" ? STATUS_WORDS[o.status] ?? o.status : null;
          return (
            <li key={o.id}>
              <button className={`pick-row st-${o.status}`} onClick={() => props.onOpenCoin(o.number)} aria-current={props.current === o.number ? "true" : undefined}
                      aria-label={`Coin ${o.number}, ${o.symbol}${o.reason ? `. ${o.reason}` : ""}${o.warning ? `. ${o.warning}` : ""}`}>
                <span className="pr-no num">{o.number}</span>
                <span className="pr-sym">{o.symbol}</span>
                <span className="pr-price num">{o.metrics.price_usd != null ? `$${fmtPrice(o.metrics.price_usd)}` : "\u2013"}</span>
                <span className={`num pr-chg ${ch == null ? "" : ch >= 0 ? "up" : "down"}`}>{ch == null ? "\u2013" : signedPct(ch)}</span>
                <span className="pr-spark"><Sparkline api={props.api} pool={o.pool} width={80} height={24} dir={ch == null ? undefined : ch >= 0 ? "up" : "down"} /></span>
                <span className="pr-why">
                  <span className="pr-reason">{o.reason ?? ""}</span>
                  {(o.warning || status) && (
                    <span className="pr-sub">
                      {o.warning && <span className="pr-warn"><i aria-hidden />{o.warning}</span>}
                      {status && <span className="pr-status">{status}{o.card && o.status === "carded" ? `, card ${o.card.code}` : ""}</span>}
                    </span>
                  )}
                </span>
              </button>
            </li>
          );
        })}
      </ol>
      <p className="hint-line">Say “coin 1, one dollar” to get a card ready. Nothing is bought until you approve its code.</p>
    </div>
  );
}

/** Where the coin's numbers came from (the attribution lives here, not in the one-line reason). */
export function dataSources(o: DeskOffer): string {
  const names = new Set<string>();
  if ((o.metrics as any).sm_traders != null || (o.metrics as any).sm_netflow_24h_usd != null) names.add("Nansen smart money");
  for (const r of o.receipt ?? []) names.add(r.seller);
  const list = [...names];
  return list.length ? `Data from ${list.length > 1 ? `${list.slice(0, -1).join(", ")} and ${list[list.length - 1]}` : list[0]}.` : "";
}

const STATUS_WORDS: Record<string, string> = { building: "Getting a card ready", carded: "Card waiting", executing: "Buying", bought: "Bought", skipped: "Skipped" };

/** Drawer title: "Coin 2" and the ticker set apart, no joining dot. */
export function CoinTitle(props: { offer: DeskOffer }) {
  return <>{coinName(props.offer)} <span className="title-sym">{props.offer.symbol}</span></>;
}

/** One coin up close: price, the K-line, and the numbers that matter as a label / number list. */
export function CoinSheet(props: { api: Api; offer: DeskOffer; capUsdc: number; buttons: boolean; position?: Position | null; onOrder: (coin: number, usd: number) => void }) {
  const o = props.offer;
  const m = o.metrics;
  const now = useNow(1000);
  const [amt, setAmt] = useState("");
  const v = Number(amt);
  const ok = amt.trim() !== "" && Number.isFinite(v) && v > 0 && v <= props.capUsdc + 1e-9;
  const ch = m.price_change_h24;
  const pos = props.position;
  const stale = updatedAgo(o.market_at, now);
  const facts: [string, string][] = [
    ["Market cap", m.market_cap != null ? `$${big(m.market_cap)}` : "\u2013"],
    ["Liquidity", m.liquidity_usd != null ? `$${big(m.liquidity_usd)}` : "\u2013"],
    ["Holders", big(m.holders)],
    ["Top 10 hold", pct(m.top10_pct, 1)],
    ["Dev holds", pct(m.dev_pct, 2)],
    ["Round-trip loss", pct(m.roundtrip_loss_pct, 1)],
  ];
  if (o.stop) facts.push(["Suggested stop", `${MINUS}${Math.abs(o.stop.stop_pct).toFixed(1)}%`]);
  return (
    <div className="coin">
      <div className="coin-price">
        <span className="cp-v num">{m.price_usd != null ? `$${fmtPrice(m.price_usd)}` : "\u2013"}</span>
        {ch != null && <span className={`cp-chg num ${ch >= 0 ? "up" : "down"}`}>{signedPct(ch)} <span className="cp-l">24h</span></span>}
        {stale && <span className="cp-ago">{stale}</span>}
      </div>
      <PriceChart api={props.api} pool={o.pool} compact label={o.symbol} height={typeof window !== "undefined" && window.innerWidth < 700 ? 200 : 260}
                  lines={pos && pos.last_ratio ? { entryRatio: pos.last_ratio, stopPct: pos.stop_loss_pct ?? null } : null} />
      {o.reason && <p className="coin-why">{o.reason}</p>}
      {o.warning && <p className="pick-worry pr-warn"><i aria-hidden />{o.warning}</p>}
      <dl className="kv">
        {facts.map(([k, val]) => <div key={k}><dt>{k}</dt><dd className="num">{val}</dd></div>)}
      </dl>
      {dataSources(o) && <p className="coin-src">{dataSources(o)}</p>}
      {o.status === "open" || o.status === "skipped" ? (
        <form className="quick-buy" onSubmit={(e) => { e.preventDefault(); if (ok) { props.onOrder(o.number, Math.round(v * 1e6) / 1e6); setAmt(""); } }}>
          <label>
            <span>$</span>
            <input type="number" inputMode="decimal" min={0} step="0.5" max={props.capUsdc} value={amt} placeholder={`Up to ${usdF(props.capUsdc).slice(1)}`}
                   onChange={(e) => setAmt(e.target.value)} aria-label={`Amount for coin ${o.number}`} />
          </label>
          <button className="primary" disabled={!props.buttons || !ok}>Get a card ready</button>
        </form>
      ) : <p className="soft">{STATUS_WORDS[o.status] ?? o.status}{o.card ? `, card ${o.card.code}` : ""}</p>}
    </div>
  );
}

/** One plain sentence for a card: "Buy $0.41 of coin 1. Stop at −24%. Planned loss about $0.10." */
export function cardSentence(c: Card): string {
  const coin = c.ticker || (c.coin_number != null ? `coin ${c.coin_number}` : c.symbol_display || "this coin");
  if (c.kind === "sell") return `Sell ${c.sell_fraction != null ? `${Math.round(c.sell_fraction * 100)}% of ` : ""}${coin}.`;
  if (c.kind && c.kind !== "buy") return `Change the exit on ${coin}.`;
  const parts = [`Buy ${usdF(c.amount_usdc)} of ${coin}.`];
  if (c.stop_loss_pct != null) parts.push(`Stop at ${MINUS}${Math.abs(c.stop_loss_pct).toFixed(0)}%.`);
  if (c.loss_plan_usdc != null) parts.push(`Planned loss about ${usdF(c.loss_plan_usdc)}.`);
  return parts.join(" ");
}

/** Cards waiting for her yes: the code is the thing she answers, so it is the largest and the only amber. */
export function OrderSheet(props: { cards: Card[]; buttons: boolean; onApprove: (c: Card) => void; onReject: (c: Card) => void }) {
  const now = useNow(500);
  return (
    <div className="orders">
      {props.cards.map((c) => {
        const left = secondsLeft(c, now);
        const total = Math.max(1, c.expires_at - c.created_at);
        const buy = !c.kind || c.kind === "buy";
        const g = c.guard.decision;
        return (
          <article key={c.id} className="order" aria-label={`Card ${c.code}`}>
            <div className="order-top">
              <b className="order-code num">{c.code}</b>
              <span className={`verdict ${DECISION_TONE[g] ?? ""}`}>{DECISION_LABEL[g] ?? g}</span>
            </div>
            <p className="order-what">{cardSentence(c)}</p>
            {buy && c.loss_worst_usdc != null && <p className="order-note">Worst case about {usdF(c.loss_worst_usdc)} if the price gaps past the stop.</p>}
            {c.cap_note && <p className="order-note">{c.cap_note[0].toUpperCase() + c.cap_note.slice(1)}.</p>}
            {c.warning && <p className="pick-worry pr-warn"><i aria-hidden />{c.warning}</p>}
            {g !== "allow" && c.guard.reasons.length > 0 && <ul className="order-reasons">{c.guard.reasons.slice(0, 3).map((r, i) => <li key={i}>{r}</li>)}</ul>}
            <p className="say-it">Say “approve {c.code}” or “reject {c.code}”.</p>
            <div className="order-actions">
              <button className="ghost-btn" onClick={() => props.onReject(c)}>Reject</button>
              <button className="primary" disabled={!props.buttons} onClick={() => props.onApprove(c)}>Approve {c.code}</button>
            </div>
            <div className="drain" role="timer" aria-label="Time left to approve"><span style={{ width: `${(100 * left) / total}%` }} /></div>
          </article>
        );
      })}
    </div>
  );
}

/** Open positions as rows: the P&L number first, then what went in, what it is worth and the stop. */
export function PositionSheet(props: { positions: Position[] }) {
  const now = useNow(1000);
  if (!props.positions.length) return <p className="empty">No positions yet. Approve a card to open one.</p>;
  return (
    <ul className="rows positions">
      {props.positions.map((p) => {
        const { value, markUsdc } = pnl(p);
        const name = p.ticker || (p.coin_number != null ? `Coin ${p.coin_number}` : p.symbol_display);
        const ago = updatedAgo(p.price_at, now);
        return (
          <li key={p.mint} className={`position ${p.status}`}>
            <div className="pos-top">
              <span className="pos-name">{name}</span>
              <span className={`pos-pnl num ${value == null ? "" : value > 0 ? "up" : value < 0 ? "down" : ""}`}>{value == null ? "\u2013" : `${value > 0 ? "+" : ""}${usd(value)}`}</span>
            </div>
            <dl className="kv kv-inline">
              <div><dt>In</dt><dd className="num">{usd(p.entry_usdc)}</dd></div>
              <div><dt>Now</dt><dd className="num">{markUsdc == null ? "\u2013" : usd(markUsdc)}</dd></div>
              {p.stop_loss_pct != null && p.status === "open" && <div><dt>Stop</dt><dd className="num">{MINUS}{Math.abs(p.stop_loss_pct).toFixed(0)}%</dd></div>}
            </dl>
            {p.status !== "open" && <p className="soft small">{p.status === "closed" ? "Closed." : p.status === "stuck" ? "Can't sell right now." : p.status}</p>}
            {p.feed_down && <p className="down small">Price feed down. The stop can't fire until it is back.</p>}
            {ago && <p className="soft small">{ago}</p>}
            {p.sells.slice(-2).map((s, i) => <p key={i} className="soft small">{TRIGGER_LABEL[s.trigger] ?? s.trigger}: got back {usd(s.usdc)}.</p>)}
          </li>
        );
      })}
    </ul>
  );
}

/** The block in one plain sentence (after "Blocked:"). A transfer to someone else's wallet gets the fixed wording. */
export function guardSentence(e: BackendEvent): string {
  const flow = e.panel?.flow;
  const rows: { what?: string; alert?: boolean }[] = Array.isArray(flow?.simulated) ? flow.simulated : [];
  const foreign = rows.some((r) => r.alert && /unknown wallet|not yours/i.test(r.what ?? ""))
    || (Array.isArray(flow?.alerts) && flow.alerts.some((a: string) => /unknown wallet/i.test(a)));
  if (foreign) return "this trade would also send USDC to a wallet that isn't yours. Nothing was sent.";
  const s = (e.speak || "").trim();
  if (!s) return "this trade was stopped before it was sent. Nothing was sent.";
  return /nothing was sent/i.test(s) ? s : `${s} Nothing was sent.`;
}

/** A blocked transaction, legible: what should have moved vs what would have. */
export function GuardSheet(props: { event: BackendEvent }) {
  const e = props.event;
  const flow = e.panel?.flow;
  const reasons: string[] = Array.isArray(e.panel?.reasons) ? e.panel.reasons : [];
  const [why, setWhy] = useState(false);
  return (
    <div className="guard">
      <p className="guard-what"><span className="verdict down">Blocked:</span> {guardSentence(e)}</p>
      {flow && <FlowCompare flow={flow} />}
      {reasons.length > 0 && (
        <>
          <button className="text-btn" onClick={() => setWhy(!why)} aria-expanded={why}>{why ? "Hide the checks" : "Show what the checks found"}</button>
          {why && <ul className="order-reasons">{reasons.map((r, i) => <li key={i}>{r}</li>)}</ul>}
        </>
      )}
    </div>
  );
}

const LIMIT_LABEL: Record<string, string> = {
  "budget.total_usdc": "Total budget", "budget.per_trade_usdc": "Per trade", "budget.daily_max_loss_usdc": "Daily loss limit",
};

/** A settings change waiting for her word. Raising a limit needs "confirm". */
export function LimitsSheet(props: { state: AppState; onConfirm: () => void; onCancel: () => void }) {
  const ms = props.state.mandate_session;
  const before = ms.active?.body ?? ms.mandate?.body;
  const after = ms.draft;
  const changes = ms.amend_changes ?? [];
  return (
    <div className="limits">
      <p className="awaiting">Raising a limit loosens a safety rail. Say “confirm” to apply it.</p>
      <table className="changes">
        <thead><tr><th scope="col">Limit</th><th scope="col" className="r">Now</th><th scope="col" className="r">New</th></tr></thead>
        <tbody>
          {changes.map((p) => (
            <tr key={p}>
              <th scope="row">{LIMIT_LABEL[p] ?? sentence(p.split(".").pop()!.replace(/_/g, " "))}</th>
              <td className="r num soft">{fmtVal(getPath(before, p), p)}</td>
              <td className="r num new">{fmtVal(getPath(after, p), p)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="order-actions">
        <button className="ghost-btn" onClick={props.onCancel}>Keep it as is</button>
        <button className="primary" onClick={props.onConfirm}>Confirm</button>
      </div>
    </div>
  );
}

const sentence = (t: string) => t.charAt(0).toUpperCase() + t.slice(1);

export function fmtVal(v: any, path = ""): string {
  if (v == null) return "None";
  if (typeof v === "number") return /usdc|_usd$/.test(path) ? usdF(v) : /_pct$/.test(path) ? `${v}%` : `${v}`;
  return Array.isArray(v) ? v.join("\u2013") : String(v);
}

export function TopupSheet(props: { topup: NonNullable<AppState["topup"]>; buttons: boolean; onApprove: (usd: number) => void; onReject: () => void }) {
  const t = props.topup;
  return (
    <div className="limits">
      <p className="awaiting">Research used its budget: {usdF(t.spent_usdc)} of {usdF(t.budget_usdc)}. Add {usdF(t.suggest_usdc)} to finish this round?</p>
      <div className="order-actions">
        <button className="ghost-btn" onClick={props.onReject}>No, stop here</button>
        <button className="primary" disabled={!props.buttons} onClick={() => props.onApprove(t.suggest_usdc)}>Add {usdF(t.suggest_usdc)}</button>
      </div>
    </div>
  );
}
