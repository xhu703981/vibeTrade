import type { BackendEvent } from "../types";
import { GuardDetails } from "../ui/GuardDetails";

// Warning row for guard blocks / sell failures / pipeline errors (latest one, dismissible).
export const ALERT_TYPES = new Set(["guard_blocked", "sell_failed", "trade_failed", "price_feed_down", "emergency_exit"]);

export function latestAlert(events: BackendEvent[], dismissed: number): BackendEvent | null {
  for (let i = events.length - 1; i >= 0; i--) {
    const e = events[i];
    if (e.id <= dismissed || e.type === "mandate_frozen") return null;   // warnings from before the new mandate are no longer shown
    if (ALERT_TYPES.has(e.type) || (e.type === "pipeline_status" && e.loud)) return e;
  }
  return null;
}

type FlowRow = { what: string; usdc?: number | null; tokens?: number | null; alert?: boolean };
type Flow = { expected: FlowRow[]; simulated: FlowRow[]; alerts: string[]; simulation_ok?: boolean };

function amount(r: FlowRow): string {
  if (r.usdc != null) return `${r.usdc < 0 ? "\u2212" : "+"}${Math.abs(r.usdc).toFixed(2)} USDC`;
  if (r.tokens != null) return `+${Math.round(r.tokens).toLocaleString("en-US")} tokens`;
  return "";
}

/** Expected flow vs simulated flow: numbers and fixed labels only (no token text, no foreign addresses). */
export function FlowCompare({ flow }: { flow: Flow }) {
  return (
    <div className="flow">
      {flow.alerts.map((a, i) => <div key={i} className="flow-alert">{a.replace(/\s*\u2192\s*/g, " to ")}</div>)}
      <div className="flow-cols">
        <div>
          <div className="flow-h">Expected</div>
          {flow.expected.map((r, i) => <div key={i} className="flow-row"><span>{r.what}</span><span>{amount(r)}</span></div>)}
        </div>
        <div>
          <div className="flow-h">Simulated{flow.simulation_ok === false ? ", from the instructions" : ""}</div>
          {flow.simulated.map((r, i) => (
            <div key={i} className={`flow-row ${r.alert ? "bad" : ""}`}><span>{r.what}</span><span>{amount(r)}</span></div>
          ))}
        </div>
      </div>
    </div>
  );
}

export function GuardAlert(props: { event: BackendEvent | null; onDismiss: (id: number) => void }) {
  const e = props.event;
  if (!e) return null;
  const reasons: string[] = Array.isArray(e.panel?.reasons) ? e.panel.reasons : [];
  return (
    <div className={`alert ${e.type === "guard_blocked" ? "guard" : ""}`} role="alert">
      <b className="alert-h">{e.type === "guard_blocked" ? "Blocked" : "Needs your look"}</b>
      <span>{e.speak}</span>
      {e.panel?.flow && (e.panel.flow as Flow).alerts?.length > 0 && <FlowCompare flow={e.panel.flow as Flow} />}
      {reasons.length > 0 && <ul>{reasons.map((r, i) => <li key={i}>{r}</li>)}</ul>}
      {e.type === "guard_blocked" && <GuardDetails key={e.panel?.guard_run_id ?? `legacy-${e.id}`} runId={e.panel?.guard_run_id} />}
      <button className="text-btn" onClick={() => props.onDismiss(e.id)}>Dismiss</button>
    </div>
  );
}
