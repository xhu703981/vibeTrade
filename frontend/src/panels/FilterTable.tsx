// Mandate settings table: shows the draft; "Edit" + a new value -> builds a user sentence (typed channel) that goes through the same /mandate/fields extraction + checks as voice.
import { useState } from "react";

export type FieldRow = { path: string; label: string; unit?: string };

export const FILTER_ROWS: FieldRow[] = [
  { path: "budget.total_usdc", label: "total budget", unit: "dollars" },
  { path: "budget.per_trade_usdc", label: "per-trade cap", unit: "dollars" },
  { path: "budget.daily_max_loss_usdc", label: "daily max loss", unit: "dollars" },
  { path: "exec.max_fee_sol", label: "max fee per trade", unit: "SOL" },
  { path: "exit.stop_loss_pct", label: "stop loss", unit: "%" },
  { path: "exit.take_profit_ladder", label: "take-profit ladder" },
  { path: "filters.min_liquidity_usd", label: "min liquidity", unit: "dollars" },
  { path: "filters.market_cap_usd", label: "market cap range", unit: "dollars" },
  { path: "filters.token_age_hours_min", label: "min token age", unit: "hours" },
  { path: "filters.max_top10_pct", label: "max top-10 holder share", unit: "%" },
  { path: "filters.max_dev_pct", label: "max dev holding", unit: "%" },
  { path: "filters.min_holders", label: "min holders", unit: "holders" },
  { path: "filters.max_roundtrip_loss_pct", label: "max round-trip loss", unit: "%" },
  { path: "exit.trailing_stop_pct", label: "trailing stop", unit: "%" },
  { path: "exit.max_hold_hours", label: "max hold time", unit: "hours" },
  { path: "budget.research_usdc", label: "research budget", unit: "dollars" },
];

export function getPath(d: Record<string, any> | undefined, path: string): any {
  return path.split(".").reduce((o: any, k) => (o == null ? undefined : o[k]), d);
}

export function show(v: any): string {
  if (v == null) return "(not set)";
  if (Array.isArray(v) && v.length && typeof v[0] === "object") return v.map((x: any) => `at ${x.multiple}x sell ${x.sell_pct}%`).join(", ");
  if (Array.isArray(v)) return v.map((x) => Number(x).toLocaleString()).join(" – ");
  return typeof v === "number" ? v.toLocaleString() : String(v);
}

export function editUtterance(row: FieldRow, value: string): string {
  const v = value.trim();
  const unit = !row.unit || /[a-zA-Z%$]/.test(v) ? "" : row.unit === "%" ? "%" : " " + row.unit;
  return `Set ${row.label} to ${v}${unit}`;
}

export const GUARD_PREFIXES = ["budget.", "exec.", "exit."];

export function FilterTable(props: {
  draft?: Record<string, any>;
  frozen: boolean;
  missing: string[];
  stage?: string;
  onEdit: (utterance: string) => Promise<void> | void;
  // living session: after the mandate is running, settings stay changeable between trades
  amendAllowed?: boolean;
  blockedReason?: string | null;
  amending?: boolean;
  amendKind?: "none" | "filters" | "guards";
  amendChanges?: string[];
  version?: number;
  onCancelAmend?: () => void;
  onApplyAmend?: (kind: "filters" | "guards" | "none") => void;
}) {
  const [editing, setEditing] = useState<string | null>(null);
  const [val, setVal] = useState("");
  const editable = !props.frozen || !!props.amendAllowed || !!props.amending;
  const changed = new Set(props.amendChanges ?? []);
  const kind = props.amendKind ?? "none";
  return (
    <section className={`panel ${props.amending ? "amending" : ""}`} aria-label="Mandate settings">
      <h3>
        Settings{" "}
        {props.amending ? <span className="tag warn">Changing, version {(props.version ?? 1) + 1}</span>
          : props.frozen ? <span className="tag ok">{props.version ? `Active, version ${props.version}` : "Active"}</span> : <span className="tag">Draft</span>}
      </h3>
      {props.frozen && !props.amending && (
        <p className="hint">
          {props.amendAllowed
            ? "Running. You can change any row between trades (or just say it); open positions keep the exit rules they were bought with."
            : `Changes are paused right now${props.blockedReason ? `: ${props.blockedReason}` : ""}.`}
        </p>
      )}
      {props.amending && (
        <div className="amendbar">
          <p className="hint">
            {kind === "guards" ? "This changes a guard rule (budget, fees or exits). Confirm it to apply and start a new research round."
              : kind === "filters" ? "Coin filters changed. Start a new research round with them."
              : "Nothing changed yet."}
            {" "}Pending cards were voided; open positions keep their own exit rules.
          </p>
          <div className="row">
            <button className="ghost-btn" onClick={() => props.onCancelAmend?.()}>Cancel changes</button>
            {kind === "guards"
              ? <button className="primary" onClick={() => props.onApplyAmend?.("guards")}>Confirm and start research</button>
              : <button className="primary" disabled={kind === "none"} onClick={() => props.onApplyAmend?.(kind)}>Start research</button>}
          </div>
        </div>
      )}
      {props.frozen && !props.amending && props.amendAllowed && (
        <div className="row"><button className="ghost-btn" onClick={() => props.onApplyAmend?.("none")}>Run another research round</button></div>
      )}
      {!props.frozen && !props.amending && (
        <p className="hint proposal">
          {props.stage === "filters"
            ? "These default coin filters are my proposal. Edit any row, or say “looks good”."
            : "Default coin filters (my proposal); you can edit any row."}
        </p>
      )}
      <table className="grid">
        <tbody>
          {FILTER_ROWS.map((r) => {
            const v = getPath(props.draft, r.path);
            const missing = props.missing.includes(r.path);
            return (
              <tr key={r.path} className={`${missing ? "missing" : ""} ${[...changed].some((c) => c === r.path || c.startsWith(r.path + ".")) ? "changed" : ""}`}>
                <td>{r.label.charAt(0).toUpperCase() + r.label.slice(1)}</td>
                <td>
                  {editing === r.path ? (
                    <form
                      onSubmit={async (e) => {
                        e.preventDefault();
                        if (val.trim()) await props.onEdit(editUtterance(r, val));
                        setEditing(null);
                      }}
                    >
                      <input aria-label={`New ${r.label}`} autoFocus value={val} onChange={(e) => setVal(e.target.value)} />
                    </form>
                  ) : (
                    <span>
                      {show(v)} {v != null && r.unit && !Array.isArray(v) ? r.unit : ""}
                    </span>
                  )}
                </td>
                <td>
                  {editable && editing !== r.path && (
                    <button className="text-btn" onClick={() => { setEditing(r.path); setVal(""); }} aria-label={`Edit ${r.label}`}>
                      Edit
                    </button>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      {editable && <p className="hint">Edits are sent to the backend as something you said and checked the same way as voice; nothing is written directly.</p>}
    </section>
  );
}
