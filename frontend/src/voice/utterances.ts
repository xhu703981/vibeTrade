// Store of the user's raw words: only final speech-recognition results (onMessage role=user) and text the user typed.
// System updates ([System update] / [URGENT update] prefix) are never stored, so 'To approve, say "approve 47"' can never count as the user's words.
export const SYSTEM_MARK = "[System update]";
export const URGENT_MARK = "[URGENT update]";
export const LEGACY_MARKS = ["【系统播报】", "【紧急播报】"];
export const MAX_AGE_MS = 90_000;

export type Utterance = { id: number; text: string; source: "voice" | "typed"; at: number; used: boolean; ignored?: boolean };

export class UtteranceStore {
  private items: Utterance[] = [];
  private seq = 0;
  constructor(private now: () => number = () => Date.now()) {}

  record(text: string, source: "voice" | "typed", opts: { ignored?: boolean } = {}): Utterance | null {
    const t = (text ?? "").trim();
    const lt = t.toLowerCase();
    if (!t || [SYSTEM_MARK, URGENT_MARK, ...LEGACY_MARKS].some((m) => lt.includes(m.toLowerCase()))) return null;
    const u = { id: ++this.seq, text: t, source, at: this.now(), used: !!opts.ignored, ignored: !!opts.ignored };
    this.items.push(u);
    this.items = this.items.slice(-20);
    return u;
  }

  /** The latest unexpired, non-ignored user utterance. Approval-type actions require it to be unused. */
  latest(opts: { unusedOnly?: boolean } = {}): Utterance | null {
    const items = this.items.filter((x) => !x.ignored);
    const u = items[items.length - 1];
    if (!u || this.now() - u.at > MAX_AGE_MS) return null;
    if (opts.unusedOnly && u.used) return null;
    return u;
  }

  markUsed(u: Utterance | null) {
    if (u) u.used = true;
  }

  all() {
    return [...this.items];
  }
}
