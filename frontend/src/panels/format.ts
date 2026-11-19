// Number formats shared by every panel (spec copy rule 7): money with $ and two decimals, signed changes with one
// decimal, and a true minus sign (U+2212) everywhere.
export const MINUS = "\u2212";

/** Atomic USDC (1e6) -> "$1.25" / "−$0.40". */
export const usd = (atomic: number | null | undefined) =>
  atomic == null ? "\u2013" : `${atomic < 0 ? MINUS : ""}$${(Math.abs(atomic) / 1e6).toFixed(2)}`;
/** Dollars -> "$1.25" (d decimals). */
export const usdF = (v: number | null | undefined, d = 2) => (v == null ? "\u2013" : `${v < 0 ? MINUS : ""}$${Math.abs(v).toFixed(d)}`);
/** Dollars, signed: "+$0.03" / "−$0.01" / "$0.00". */
export const usdSigned = (v: number) => {
  const r = Math.round(v * 100) / 100;
  return r === 0 ? "$0.00" : `${r > 0 ? "+" : MINUS}$${Math.abs(r).toFixed(2)}`;
};
export const pct = (v: number | null | undefined, d = 1) => (v == null ? "\u2013" : `${v < 0 ? MINUS : ""}${Math.abs(v).toFixed(d)}%`);
/** Signed change, one decimal: "+4.2%", "−13.2%", "+172.0%". */
export const signedPct = (v: number) => `${v >= 0 ? "+" : MINUS}${Math.abs(v).toFixed(1)}%`;
export const big = (v: number | null | undefined) =>
  v == null ? "\u2013" : v >= 1e6 ? `${(v / 1e6).toFixed(2)}M` : v >= 1e3 ? `${(v / 1e3).toFixed(1)}k` : v.toFixed(0);
export const short = (s: string | null | undefined, n = 6) => (s ? `${s.slice(0, n)}\u2026${s.slice(-4)}` : "\u2013");
/** Guard verdict in words (and the colour class next to it). */
export const DECISION_LABEL: Record<string, string> = { allow: "Passed", ask_human: "Needs your look", block: "Blocked" };
export const DECISION_TONE: Record<string, string> = { allow: "up", ask_human: "amber", block: "down" };
export const TRIGGER_LABEL: Record<string, string> = {
  take_profit: "Take-profit", stop_loss: "Stop-loss", trailing_stop: "Trailing stop", max_hold: "Max hold time",
  liquidity_drop: "Liquidity drop", emergency: "Emergency exit",
};

/** Only when the data is stale (over 30 seconds old): "Updated 2 min ago". `at` is unix seconds (or ms), `now` unix seconds. */
export function updatedAgo(at: number | null | undefined, now: number): string | null {
  if (at == null || at <= 0 || !Number.isFinite(at) || !Number.isFinite(now)) return null;
  const sec = at > 1e12 ? at / 1000 : at;
  const s = Math.max(0, Math.round(now - sec));
  if (s <= 30) return null;
  if (s < 3600) return `Updated ${Math.max(1, Math.round(s / 60))} min ago`;
  return `Updated ${Math.round(s / 3600)} h ago`;
}
