import type { Position } from "../types";
import { useNow } from "../useBackend";
import { MINUS, TRIGGER_LABEL, short, usd as usd2, updatedAgo } from "./format";

// cent-sized trades: $0.0098 / -$0.0002 instead of $0.01 / $0.00 (whole cents and >= $1 keep two decimals)
export const usd = (atomic: number | null | undefined) => {
  if (atomic == null) return usd2(atomic);
  const a = Math.abs(atomic);
  return a > 0 && a < 1e6 && a % 1e4 !== 0 ? `${atomic < 0 ? MINUS : ""}$${(a / 1e6).toFixed(4)}` : usd2(atomic);
};
// price move since the fill: 1.004x / 0.984x near 1, 2.24x further away
const mult = (r: number) => r.toFixed(Math.abs(r - 1) < 0.1 ? 3 : 2) + "\u00d7";

export function pnl(p: Position): { value: number | null; realized: number; markUsdc: number | null } {
  const realized = p.realized_usdc;
  // last_value_usdc: what the held tokens sell for now (fees included). last_ratio is the price move since the fill, not a P&L multiple.
  // the backend's mark (fees included) is the one the agent speaks from; older payloads fall back to the last sell quote
  const markUsdc = p.held_tokens <= 0 ? 0 : p.mark_usdc != null ? p.mark_usdc : p.last_value_usdc != null ? p.last_value_usdc
    : p.last_ratio != null && p.initial_tokens ? (p.entry_usdc * p.last_ratio * p.held_tokens) / p.initial_tokens : null;
  const value = markUsdc == null && p.held_tokens > 0 ? null : realized + (markUsdc ?? 0) - p.entry_usdc;
  return { value, realized, markUsdc };
}

export function PositionsPanel(props: {
  positions: Position[];
  onEmergency: () => void;
  canExit: boolean;
  testEnabled?: boolean;
  onTest?: (kind: "take_profit" | "stop_loss") => void;
}) {
  const now = useNow(1000);
  const canTest = !!props.testEnabled && props.positions.some((p) => p.status === "open" && p.held_tokens > 0);
  return (
    <section className="panel" aria-label="Positions">
      <h3>Positions and P&amp;L</h3>
      {props.positions.length === 0 && <p className="hint">No positions yet. Approve a card to open one.</p>}
      {props.positions.map((p) => {
        const { value, markUsdc } = pnl(p);
        return (
          <div key={p.mint} className={`pos row-item s-${p.status}`}>
            <div className="row">
              <b>{p.ticker || (p.coin_number != null ? `Coin ${p.coin_number}` : p.symbol_display) || short(p.mint)}</b>
              <span>Bought {usd(p.entry_usdc)}</span>
              <span>Left {p.initial_tokens ? Math.round((100 * p.held_tokens) / p.initial_tokens) : 0}%</span>
              <span>Price {p.last_ratio != null ? mult(p.last_ratio) : "–"}</span>
              <span>Value {markUsdc == null ? "–" : usd(markUsdc)}</span>
              {updatedAgo(p.price_at, now) && <span className="ago" data-testid="pos-updated">{updatedAgo(p.price_at, now)}</span>}
              <span className={value == null ? "" : value > 0 ? "up" : value < 0 ? "down" : ""}>P&amp;L {value == null ? "\u2013" : `${value > 0 ? "+" : ""}${usd(value)}`}</span>
              {p.feed_down && <span className="tag bad">Price feed down</span>}
              {p.status !== "open" && <span className="tag">{p.status === "closed" ? "Closed" : p.status === "stuck" ? "Cannot sell" : p.status}</span>}
            </div>
            {p.sells.map((s, i) => (
              <div key={i} className="sell">
                {TRIGGER_LABEL[s.trigger] ?? s.trigger}: sold at {s.ratio != null ? mult(s.ratio) : "\u2013"}, got back {usd(s.usdc)}. <code>{short(s.sig, 8)}</code>
              </div>
            ))}
          </div>
        );
      })}
      <button className="danger-btn" disabled={!props.canExit} onClick={props.onEmergency}>Emergency exit</button>
      {props.testEnabled && (
        <span className="tests" aria-label="Rehearsal price tests">
          <button disabled={!canTest} onClick={() => props.onTest?.("take_profit")} title="Rehearsal only: a whale on the local fork pushes the price past the take-profit line">
            Test take profit
          </button>
          <button disabled={!canTest} onClick={() => props.onTest?.("stop_loss")} title="Rehearsal only: a whale on the local fork pushes the price below the stop-loss line">
            Test stop loss
          </button>
        </span>
      )}
    </section>
  );
}
