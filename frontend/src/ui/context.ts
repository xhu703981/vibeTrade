// Which contextual sheet the screen shows. Pure functions (unit-tested): the voice screen stays empty until something
// needs her eyes, then one sheet slides in; everything still relevant stays reachable as a small pill.
import type { AppState, BackendEvent, Card, DeskOffer } from "../types";

export type SheetKind = "limits" | "topup" | "guard" | "order" | "position" | "coin" | "picks";

/** key = what the sheet shows ("coin:2"); trigger = the moment that surfaced it on its own (absent = only reachable by tap). */
export type SheetRef = { kind: SheetKind; key: string; trigger?: string; label: string; coin?: number; eventId?: number };

export type Line = { who: "user" | "agent" | "system"; text: string; at: number };

/** Higher first: something that waits for her answer beats something that is only nice to see. */
export const PRIORITY: SheetKind[] = ["limits", "topup", "guard", "order", "position", "coin", "picks"];

/** Kinds that stay open until resolved; the others fold back into a pill once they go stale. */
export const STICKY: Set<SheetKind> = new Set(["limits", "topup", "order", "picks"]);

export const FRESH = { guard: 180, position: 60, coin: 45 };   // seconds

export function deskOffers(state: AppState | null): DeskOffer[] {
  const d = state?.desk;
  return d ? [...d.offers, ...(d.majors ?? [])] : [];
}

export function pendingCards(state: AppState | null): Card[] {
  return (state?.engine?.cards ?? []).filter((c) => c.status === "pending");
}

/** Coin numbers mentioned in a line ("coin 2", "coins 1 and 3" -> [1, 3]). */
export function coinMentions(text: string): number[] {
  const out = new Set<number>();
  for (const m of text.matchAll(/\bcoins?\s+((?:\d{1,2}(?:\s*(?:,|and|&)\s*)?)+)/gi)) {
    for (const n of m[1].match(/\d{1,2}/g) ?? []) out.add(Number(n));
  }
  return [...out];
}

/** The coin the conversation is about right now: the newest spoken line (hers or the agent's) names exactly one coin on the desk. */
export function focusCoin(lines: Line[], offers: DeskOffer[], now: number): { coin: number; at: number } | null {
  for (let i = lines.length - 1; i >= 0; i--) {
    const l = lines[i];
    if (l.who === "system") continue;
    if (now - l.at / 1000 > FRESH.coin) return null;
    if (/\bcard\s+\d{2}\b|\bcode\s+\d{2}\b/i.test(l.text)) return null;     // a card is on screen for that
    const ns = coinMentions(l.text).filter((n) => offers.some((o) => o.number === n));
    return ns.length === 1 ? { coin: ns[0], at: l.at } : null;
  }
  return null;
}

export function lastGuard(events: BackendEvent[]): BackendEvent | null {
  for (let i = events.length - 1; i >= 0; i--) {
    const e = events[i];
    if (e.type === "mandate_frozen" || e.type === "session_reset") return null;
    if (e.type === "guard_blocked") return e;
  }
  return null;
}

export function lastFill(events: BackendEvent[]): BackendEvent | null {
  for (let i = events.length - 1; i >= 0; i--) {
    const e = events[i];
    if (e.type === "session_reset") return null;
    if (e.type === "trade_executed" || e.type === "exit_triggered") return e;
  }
  return null;
}

/** Every sheet that has something to show right now, by priority. `now` is unix seconds. */
export function availableSheets(state: AppState | null, events: BackendEvent[], lines: Line[], now: number): SheetRef[] {
  const out: SheetRef[] = [];
  const ms = state?.mandate_session;
  const changes = ms?.amending ? ms.amend_changes ?? [] : [];
  if (changes.length && ms?.amend_kind && ms.amend_kind !== "none") {
    out.push({ kind: "limits", key: "limits", trigger: `limits:${changes.join("|")}`, label: "Settings change" });
  }
  if (state?.topup) out.push({ kind: "topup", key: "topup", trigger: `topup:${state.topup.expires_at}`, label: "Research top-up" });
  const g = lastGuard(events);
  if (g && now - g.ts < 15 * 60) {
    out.push({ kind: "guard", key: `guard:${g.id}`, trigger: now - g.ts <= FRESH.guard ? `guard:${g.id}` : undefined, label: "Blocked", eventId: g.id });
  }
  const pend = pendingCards(state);
  if (pend.length) {
    out.push({ kind: "order", key: "order", trigger: `order:${pend.map((c) => c.id).join("|")}`,
               label: pend.length === 1 ? `Card ${pend[0].code}` : `${pend.length} cards` });
  }
  const positions = state?.engine?.positions ?? [];
  if (positions.length) {
    const f = lastFill(events);
    out.push({ kind: "position", key: "position", trigger: f && now - f.ts <= FRESH.position ? `fill:${f.id}` : undefined, label: "Positions" });
  }
  const offers = deskOffers(state);
  const focus = focusCoin(lines, offers, now);
  for (const o of offers) {
    const hot = focus?.coin === o.number;
    out.push({ kind: "coin", key: `coin:${o.number}`, trigger: hot ? `coin:${o.number}@${focus!.at}` : undefined,
               label: o.kind === "major" ? (o.ticker || o.symbol) : `Coin ${o.number}`, coin: o.number });
  }
  if (state?.desk && state.desk.offers.length) {
    const phase = state.phase;
    const choosing = state.desk.offers.some((o) => o.status === "open") && (phase === "selecting" || phase === "idle" || phase === "monitoring");
    out.push({ kind: "picks", key: "picks", trigger: choosing ? `picks:${state.desk.round}` : undefined, label: "Today's picks" });
  }
  return out.sort((a, b) => PRIORITY.indexOf(a.kind) - PRIORITY.indexOf(b.kind));
}

export type SheetMemory = { current: string | null; manual: boolean; fromTrigger: string | null; seen: string[]; closed: string[] };

export const EMPTY_MEMORY: SheetMemory = { current: null, manual: false, fromTrigger: null, seen: [], closed: [] };

/** One step of the sheet state machine: a new trigger takes the screen (unless she closed it); a stale non-sticky
 *  auto sheet folds away; a sheet whose content is gone closes. */
export function nextSheet(mem: SheetMemory, avail: SheetRef[]): SheetMemory {
  let { current, manual, fromTrigger } = mem;
  const seen = [...mem.seen];
  const fresh = avail.filter((a) => a.trigger && !seen.includes(a.trigger) && !mem.closed.includes(a.trigger));
  if (fresh.length) {
    const top = fresh[0];
    // a lower-priority novelty never covers a sheet that waits for her answer
    const cur = avail.find((a) => a.key === current);
    // ...and never covers a higher-priority sheet that is still live (a fill showing, a fresh block)
    const live = cur && ((STICKY.has(cur.kind) && cur.kind !== "picks") || (!!cur.trigger && cur.trigger === fromTrigger));
    const blocks = live && PRIORITY.indexOf(cur!.kind) < PRIORITY.indexOf(top.kind);
    for (const f of fresh) seen.push(f.trigger!);
    if (!blocks) { current = top.key; manual = false; fromTrigger = top.trigger!; }
  }
  const cur = avail.find((a) => a.key === current);
  if (!cur) { current = null; manual = false; fromTrigger = null; }
  else if (!manual && !STICKY.has(cur.kind) && cur.trigger !== fromTrigger) { current = null; fromTrigger = null; }
  return { current, manual, fromTrigger, seen: seen.slice(-200), closed: mem.closed };
}

export function openSheet(mem: SheetMemory, key: string): SheetMemory {
  return { ...mem, current: key, manual: true, fromTrigger: null };
}

export function closeSheet(mem: SheetMemory, avail: SheetRef[]): SheetMemory {
  const cur = avail.find((a) => a.key === mem.current);
  const closed = cur?.trigger ? [...mem.closed, cur.trigger].slice(-200) : mem.closed;
  return { ...mem, current: null, manual: false, fromTrigger: null, closed };
}

/** The small reopen row: what is still relevant but not on screen (coins fold into "Today's picks"; positions have their own pill). */
export function pillRow(avail: SheetRef[], current: string | null): SheetRef[] {
  return avail.filter((a) => a.key !== current && a.kind !== "position" && (a.kind !== "coin" || !!a.trigger)).slice(0, 4);
}
