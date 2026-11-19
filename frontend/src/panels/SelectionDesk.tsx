// Selection desk: the main stage after research. Every coin that passed is a comparable tile (coin 1..N);
// tick any number of them, give each an amount, and create one confirmation card per coin (approval is still by code).
// Voice works the same way: "coin 1 and coin 3, 5 dollars each".
import { useMemo, useState } from "react";
import { useNow } from "../useBackend";
import type { Api } from "../api";
import type { Desk, DeskMetrics, DeskOffer } from "../types";
import { PriceChart, fmtPrice } from "./PriceChart";
import { big, pct, signedPct, updatedAgo, usdF } from "./format";

export type DeskOrder = { coin_number: number; amount_usdc: number };

// already shown as flag pills above the notes
const DUP_FLAG = /^(LP \d|mint and freeze authority revoked)/;

const STATUS: Record<string, string> = { open: "Open", building: "Getting a card ready", carded: "Card waiting", executing: "Buying", bought: "Bought", skipped: "Skipped" };

export function age(h: number | null | undefined) {
  if (h == null) return "–";
  if (h < 1) return `${Math.round(h * 60)}m`;
  if (h < 48) return `${h.toFixed(h < 10 ? 1 : 0)}h`;
  return `${(h / 24).toFixed(h < 240 ? 1 : 0)}d`;
}

function Chg(props: { v: number | null | undefined; label: string }) {
  const v = props.v;
  return (
    <span className={`chg ${v == null ? "" : v >= 0 ? "up" : "down"}`}>
      <small>{props.label}</small> {v == null ? "\u2013" : signedPct(v)}
    </span>
  );
}

function TxBar(props: { buys?: number | null; sells?: number | null }) {
  const b = props.buys ?? 0, s = props.sells ?? 0;
  if (props.buys == null && props.sells == null) return <span className="muted">–</span>;
  const tot = Math.max(1, b + s);
  return (
    <span className="txbar" title={`${b} buys / ${s} sells`}>
      <span className="b" style={{ width: `${(100 * b) / tot}%` }} />
      <span className="s" style={{ width: `${(100 * s) / tot}%` }} />
      <em>{b} / {s}</em>
    </span>
  );
}

function Flag(props: { ok: boolean | null | undefined; yes: string; no: string }) {
  if (props.ok == null) return <span className="pill">{props.yes}: unknown</span>;
  return <span className={`pill ${props.ok ? "good" : "bad"}`}>{props.ok ? props.yes : props.no}</span>;
}

function lpText(m: DeskMetrics) {
  const b = m.lp_burned_pct, l = m.lp_locked_pct;
  if (b == null && l == null) return null;
  return `LP ${b != null ? `${b.toFixed(0)}% burned` : ""}${b != null && l ? ", " : ""}${l ? `${l.toFixed(0)}% locked` : ""}`;
}

export function DeskTile(props: {
  offer: DeskOffer;
  picked: boolean;
  amount: string;
  focus: boolean;
  washMax?: number;
  capUsdc: number;
  onPick: (on: boolean) => void;
  onAmount: (v: string) => void;
  onFocus: () => void;
}) {
  const o = props.offer;
  const m = o.metrics;
  const now = useNow(1000);
  const open = o.status === "open" || o.status === "skipped";
  const vl = m.volume_to_liquidity;
  const wash = vl != null && props.washMax != null && vl > 0.6 * props.washMax;
  const lp = lpText(m);
  return (
    <article className={`dtile ${props.picked ? "picked" : ""} ${props.focus ? "focus" : ""} st-${o.status}`} aria-label={`Coin ${o.number}`}>
      <header>
        <span className="num">Coin {o.number}</span>
        <span className="dsym" title={o.mint}>{o.symbol || "(no symbol)"}</span>
        {o.dex && <span className="pill dim">{o.dex}</span>}
        {o.paid_promo && <span className="pill warn" title="Boosted / paid listing on DexScreener">Paid promo</span>}
        <span className="spacer" />
        <span className={`pill st st-${o.status}`}>{STATUS[o.status] ?? o.status}{o.card && o.status !== "open" ? `, card ${o.card.code}` : ""}</span>
      </header>
      {o.reason && <p className="why" data-testid="desk-reason">{o.reason}</p>}
      {o.warning && <p className="pr-warn" data-testid="desk-suspicion"><i aria-hidden />{o.warning}</p>}
      <div className="dprice">
        <b className="num">{m.price_usd != null ? `$${fmtPrice(m.price_usd)}` : "\u2013"}</b>
        <Chg v={m.price_change_h1} label="1h" /><Chg v={m.price_change_h6} label="6h" /><Chg v={m.price_change_h24} label="24h" />
        {updatedAgo(o.market_at, now) && <small className="ago" data-testid="desk-updated">{updatedAgo(o.market_at, now)}</small>}
      </div>
      <dl className="dgrid">
        <dt>Market cap</dt><dd>${big(m.market_cap)}</dd>
        <dt>Liquidity</dt><dd>${big(m.liquidity_usd)}</dd>
        <dt>Vol 24h</dt><dd>${big(m.volume_h24)}</dd>
        <dt>Vol / liq</dt><dd className={wash ? "bad" : ""}>{vl != null ? `${vl.toFixed(1)}\u00d7` : "\u2013"}{wash ? ", possible wash trading" : ""}</dd>
        <dt>Age</dt><dd>{age(m.age_hours)}</dd>
        <dt>Holders</dt><dd>{big(m.holders)}</dd>
        <dt>Top 10</dt><dd>{pct(m.top10_pct, 1)}</dd>
        <dt>Dev</dt><dd>{pct(m.dev_pct, 2)}</dd>
        <dt>Round trip</dt><dd>{pct(m.roundtrip_loss_pct, 1)}</dd>
        <dt>Impact</dt><dd>{pct(m.price_impact_pct, 2)}</dd>
        <dt>Txns 1h</dt><dd><TxBar buys={m.buys_h1} sells={m.sells_h1} /></dd>
        <dt>Txns 24h</dt><dd><TxBar buys={m.buys_h24} sells={m.sells_h24} /></dd>
        {(m as any).sm_netflow_24h_usd != null && <><dt>Smart money 24h</dt><dd className="good">+${big((m as any).sm_netflow_24h_usd)}{(m as any).sm_traders ? `, ${(m as any).sm_traders} wallets` : ""}</dd></>}
      </dl>
      <div className="flags">
        {lp && <span className="pill good">{lp}</span>}
        <Flag ok={m.mint_revoked} yes="Mint revoked" no="Mint live" />
        <Flag ok={m.freeze_revoked} yes="Freeze revoked" no="Freeze live" />
      </div>
      {(o.signals.good.length > 0 || o.signals.caution.length > 0) && (
        <ul className="sig">
          {o.signals.good.filter((s) => !DUP_FLAG.test(s)).map((s, i) => <li key={`g${i}`} className="good">{s}</li>)}
          {o.signals.caution.map((s, i) => <li key={`c${i}`} className="caution">{s}</li>)}
        </ul>
      )}
      {(o.receipt ?? []).length > 0 && (
        <p className="receipt muted small" data-testid="desk-receipt">
          Research receipt: {(o.receipt ?? []).map((r) => receiptText(r)).join("; ")}.
        </p>
      )}
      <footer>
        <label className="pick">
          <input type="checkbox" checked={props.picked} disabled={!open} onChange={(e) => props.onPick(e.target.checked)} aria-label={`Pick coin ${o.number}`} />
          Pick
        </label>
        <label className="amt">
          $
          <input type="number" min={0} step="0.5" max={props.capUsdc} inputMode="decimal" disabled={!open} value={props.amount}
                 placeholder={`Up to ${props.capUsdc}`} aria-label={`Amount for coin ${o.number}`}
                 onChange={(e) => { props.onAmount(e.target.value); if (e.target.value && !props.picked) props.onPick(true); }} />
        </label>
        <button className={`text-btn ${props.focus ? "on" : ""}`} onClick={props.onFocus} aria-pressed={props.focus}>{props.focus ? "Charted" : "Chart"}</button>
        {!o.market_live && <span className="muted small" title="Live DexScreener numbers could not be loaded">Numbers from research time</span>}
      </footer>
    </article>
  );
}

/** Example amount for the hint, within the per-trade cap: "1 cent" for a cent cap, "5 dollars" for a large one. */
export function exampleAmount(cap: number | null | undefined): string {
  const c = typeof cap === "number" && cap > 0 ? cap : 5;
  if (c >= 5) return "5 dollars";
  if (c >= 1) return `${Math.floor(c)} dollar${Math.floor(c) === 1 ? "" : "s"}`;
  const cents = Math.max(1, Math.floor(c * 100 + 1e-9));
  return `${cents} cent${cents === 1 ? "" : "s"}`;
}

export function validateOrders(picked: number[], amounts: Record<number, string>, shared: string, cap: number, left: number):
  { orders: DeskOrder[]; error: string | null } {
  const orders: DeskOrder[] = [];
  for (const n of picked) {
    const raw = (amounts[n] ?? "").trim() || shared.trim();
    const v = Number(raw);
    if (!raw || !Number.isFinite(v) || v <= 0) return { orders: [], error: `Give coin ${n} an amount (or fill "same amount for all").` };
    if (v > cap + 1e-9) return { orders: [], error: `Coin ${n}: ${v} dollars is above the per-trade cap of ${cap}.` };
    orders.push({ coin_number: n, amount_usdc: Math.round(v * 1e6) / 1e6 });
  }
  const sum = orders.reduce((a, o) => a + o.amount_usdc, 0);
  if (sum > left + 1e-9) return { orders: [], error: `Total ${sum} dollars is more than the ${left} dollars left in the session.` };
  return { orders, error: null };
}

export function SelectionDesk(props: {
  api: Api;
  desk: Desk;
  failed: { mint: string; symbol: string; reasons: string[] }[];
  buttonsEnabled: boolean;
  washMax?: number;
  busy?: string | null;
  onOrder: (orders: DeskOrder[]) => Promise<void> | void;
  onSkip: (numbers: number[]) => Promise<void> | void;
  onRefresh: () => Promise<void> | void;
}) {
  const { desk } = props;
  const [picked, setPicked] = useState<number[]>([]);
  const [amounts, setAmounts] = useState<Record<number, string>>({});
  const [shared, setShared] = useState("");
  const [focus, setFocus] = useState<number | null>(null);
  const [showFailed, setShowFailed] = useState(false);
  const majors = desk.majors ?? [];
  const allOffers = [...desk.offers, ...majors];
  const openNums = allOffers.filter((o) => o.status === "open" || o.status === "skipped").map((o) => o.number);
  const live = picked.filter((n) => openNums.includes(n));
  const { orders, error } = useMemo(() => validateOrders(live, amounts, shared, desk.cap_usdc, desk.remaining_usdc),
                                    [live.join(","), amounts, shared, desk.cap_usdc, desk.remaining_usdc]);
  const total = orders.reduce((a, o) => a + o.amount_usdc, 0);
  const focused = allOffers.find((o) => o.number === (focus ?? allOffers[0]?.number));
  const pick = (n: number, on: boolean) => setPicked((xs) => (on ? [...new Set([...xs, n])] : xs.filter((x) => x !== n)));
  const amt = exampleAmount(desk.cap_usdc);
  const example = live.length > 1 ? `coin ${live[0]} and coin ${live[1]}, ${amt} each` : `coin ${live[0] ?? openNums[0] ?? 1}, ${amt}`;

  return (
    <section className="desk" aria-label="Selection desk">
      <header className="desk-head">
        <div>
          <h2>Coins <span className="soft">Round {desk.round}</span></h2>
          <p className="muted">{desk.offers.length} recommended meme coin{desk.offers.length === 1 ? "" : "s"}{majors.length ? `, ${majors.length} major${majors.length === 1 ? "" : "s"} listed` : ""}. Ask where the stop should be, or say how much you're willing to lose on one. Each buy is a card you approve by its code.</p>
        </div>
        <div className="budget">
          <div><small>Per trade max</small><b className="num">{usdF(desk.cap_usdc)}</b></div>
          <div><small>Left in session</small><b className="num">{usdF(desk.remaining_usdc)}</b></div>
          <button className="text-btn" onClick={() => props.onRefresh()} title="Reload live DexScreener numbers">Refresh numbers</button>
        </div>
      </header>

      {desk.offers.length === 0 && (
        <p className="desk-empty">No meme coin passed this round. You can still buy a major by ticker (say “buy one cent of JUP”), or change a filter and run another round.</p>
      )}

      {focused && (
        <PriceChart api={props.api} pool={focused.pool} label={focused.kind === "major" ? `${focused.ticker || focused.symbol}` : `Coin ${focused.number} ${focused.symbol}`} height={280} />
      )}

      <div className="dtiles">
        {desk.offers.map((o) => (
          <DeskTile key={o.id} offer={o} picked={live.includes(o.number)} amount={amounts[o.number] ?? ""} capUsdc={desk.cap_usdc}
                    focus={focused?.number === o.number} washMax={props.washMax}
                    onPick={(on) => pick(o.number, on)} onAmount={(v) => setAmounts((a) => ({ ...a, [o.number]: v }))}
                    onFocus={() => setFocus(o.number)} />
        ))}
      </div>

      {majors.length > 0 && (
        <div className="majors">
          <h3>Majors</h3>
          <p className="muted">SOL-ecosystem majors, long only. They skip meme filters; the execution guard still applies. Say the ticker, for example “buy one cent of JUP”.</p>
          <div className="dtiles">
            {majors.map((o) => (
              <DeskTile key={o.id} offer={o} picked={live.includes(o.number)} amount={amounts[o.number] ?? ""} capUsdc={desk.cap_usdc}
                        focus={focused?.number === o.number} washMax={props.washMax}
                        onPick={(on) => pick(o.number, on)} onAmount={(v) => setAmounts((a) => ({ ...a, [o.number]: v }))}
                        onFocus={() => setFocus(o.number)} />
            ))}
          </div>
        </div>
      )}

      {(desk.offers.length > 0 || majors.length > 0) && <>
      <div className="orderbar" aria-label="Order summary">
        <label className="amt">Same amount for all picked $
          <input type="number" min={0} step="0.5" value={shared} onChange={(e) => setShared(e.target.value)} aria-label="Same amount for all picked" placeholder={`Up to ${desk.cap_usdc}`} />
        </label>
        <span className="summary">
          {live.length === 0 ? "Nothing picked yet." :
            <>Picked {live.map((n) => `coin ${n}`).join(", ")}{orders.length ? <>. Total <b className="num">{usdF(total)}</b> of {usdF(desk.remaining_usdc)} left.</> : null}</>}
        </span>
        {error && live.length > 0 && <span className="err small" role="status">{error}</span>}
        <span className="spacer" />
        <button className="ghost-btn" disabled={live.length === 0 || !!props.busy} onClick={() => { props.onSkip(live); setPicked([]); }}>Skip picked</button>
        <button className="primary" disabled={!props.buttonsEnabled || live.length === 0 || !!error || !!props.busy}
                title={props.buttonsEnabled ? "" : "On-screen order buttons are off (VIBE_UI_BUTTONS=1); say it instead"}
                onClick={async () => { await props.onOrder(orders); setPicked([]); setAmounts({}); }}>
          Create {orders.length || ""} card{orders.length === 1 ? "" : "s"}
        </button>
      </div>
      <p className="say">Or say: “{example}”. Then approve each card by its code.</p>
      </>}

      {props.failed.length > 0 && (
        <div className="failed">
          <button className="text-btn" onClick={() => setShowFailed(!showFailed)} aria-expanded={showFailed}>
            {showFailed ? "Hide" : "Show"} {props.failed.length} coin{props.failed.length === 1 ? "" : "s"} that did not pass
          </button>
          {showFailed && (
            <ul>
              {props.failed.map((f) => (
                <li key={f.mint}><b>{f.symbol || f.mint.slice(0, 6)}</b> <span className="muted">{f.reasons.slice(0, 3).join("; ") || "did not pass"}</span></li>
              ))}
            </ul>
          )}
        </div>
      )}
    </section>
  );
}

/** Coins that did not pass this round (free checks or research), for the desk's collapsed section. */
export function failedCoins(candidates: any[], research: any[]): { mint: string; symbol: string; reasons: string[] }[] {
  const res = new Map(research.map((r) => [r.mint, r]));
  const out: { mint: string; symbol: string; reasons: string[] }[] = [];
  for (const c of candidates) {
    const r = res.get(c.mint);
    const failed = r ? r.status !== "pass" : String(c.status ?? "").startsWith("fail");
    if (failed) out.push({ mint: c.mint, symbol: c.symbol ?? "", reasons: [...(r?.reasons ?? []), ...(c.reasons ?? [])].map(String) });
  }
  return out;
}

/** "Quick Intel $0.03 (tx 0x354c…63c1)"; cached data shows what was paid when it was recorded. */
export function receiptText(r: { seller: string; amount_usdc?: number | null; cached: boolean; tx_hash?: string | null; recorded_usdc?: number | null }): string {
  const tx = r.tx_hash ? ` tx ${r.tx_hash.slice(0, 6)}\u2026${r.tx_hash.slice(-4)}` : "";
  if (r.cached) return r.recorded_usdc != null ? `${r.seller} $${r.recorded_usdc.toFixed(2)} (paid when recorded${tx ? "," + tx : ""})` : `${r.seller} (cached)`;
  return `${r.seller} $${(r.amount_usdc ?? 0).toFixed(2)}${tx ? " (" + tx.trim() + ")" : ""}`;
}
