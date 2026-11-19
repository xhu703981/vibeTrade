// Shadow listener (旁听通道): stream the user's live words to the backend while they are still talking.
// Partials (tentative_user_transcript) are throttled to one POST per PARTIAL_DEBOUNCE_MS with the latest text;
// a finalized transcript is sent at once. The backend only stages a reading and does free prework; nothing commits.
// Eleven Realtime v1 Max sends no tentative transcripts (checked), so with Realtime this runs on per-phrase finals.
import { SYSTEM_MARK, URGENT_MARK } from "./utterances";

export const PARTIAL_DEBOUNCE_MS = 200;

export type Hearing = { kind: string; label: string; final: boolean; at: number } | null;
export type ShadowResult = { ok: boolean; hearing?: Hearing; side_talk?: boolean; stale?: boolean; gate_speech?: boolean; addressed?: boolean; wake_name?: boolean };
type Post = (body: { text: string; final: boolean; seq: number }) => Promise<ShadowResult>;

export class ShadowStream {
  private seq = 0;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private pending: string | null = null;
  private lastSent = "";
  constructor(
    private post: Post,
    private onResult: (r: ShadowResult, final: boolean) => void = () => {},
    private debounceMs = PARTIAL_DEBOUNCE_MS,
  ) {}

  partial(text: string) {
    const t = clean(text);
    if (!t) return;
    this.pending = t;
    if (this.timer) return;
    this.timer = setTimeout(() => {
      this.timer = null;
      const p = this.pending;
      this.pending = null;
      if (p && p !== this.lastSent) this.send(p, false);
    }, this.debounceMs);
  }

  final(text: string) {
    const t = clean(text);
    if (this.timer) clearTimeout(this.timer);
    this.timer = null;
    this.pending = null;
    if (t) this.send(t, true);
  }

  private send(text: string, final: boolean) {
    this.lastSent = text;
    const seq = ++this.seq;
    this.post({ text, final, seq }).then((r) => this.onResult(r ?? { ok: false }, final)).catch(() => {});
  }
}

function clean(text: string | null | undefined): string {
  const t = (text ?? "").trim();
  if (!t || t.startsWith(SYSTEM_MARK) || t.startsWith(URGENT_MARK)) return "";   // our own broadcasts are never her words
  return t.slice(0, 2000);
}

export const SIDE_TALK_NOTE = "On-screen note: the user's last words look like side talk to someone else, not to you. Stay silent unless they were for you.";
