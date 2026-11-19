// Backend event -> make the agent speak: sendUserMessage("[System update] " + speak).
// Only live events with a non-empty speak are broadcast (replayed history is not); queued while the agent speaks or a tool runs.
// Updates that only echo a recent tool result or agent line are dropped, so a quick tool's speak is not restated.
import type { BackendEvent } from "../types";
import { SYSTEM_MARK, URGENT_MARK } from "./utterances";

export const SILENT_TYPES = new Set(["research_payment", "mandate_updated", "pipeline_status_quiet"]);
/** After a tool already returned a speak line, these event types are the same outcome and must not be read again. */
export const TOOL_COVERED_TYPES = new Set(["mandate_amended", "round_started"]);
export const ECHO_WINDOW_MS = 8000;

export function shouldSpeak(ev: BackendEvent): boolean {
  return !ev.replay && !!ev.speak && !SILENT_TYPES.has(ev.type);
}

/** Quotation marks made the agent start mid-quote or narrate ("the update says..."): send plain words. */
export function plainSpeak(s: string): string {
  return (s ?? "").replace(/["“”]/g, "").replace(/\s+/g, " ").trim();
}

export function broadcastText(ev: BackendEvent): string {
  return (ev.loud ? URGENT_MARK : SYSTEM_MARK) + " " + plainSpeak(ev.speak);
}

const STOP = new Set(["a", "an", "the", "and", "or", "to", "of", "for", "in", "on", "at", "is", "it", "as", "by", "be",
  "do", "if", "my", "we", "you", "your", "our", "with", "from", "that", "this", "they", "them", "are", "was", "will",
  "just", "now", "then", "than", "but", "not", "no", "so", "its", "i", "me", "got"]);

/** Lowercase, strip system-update markers and punctuation, collapse spaces. */
export function normSpeak(s: string): string {
  return (s ?? "")
    .toLowerCase()
    .replace(/\[\s*(system|urgent)\s+update\s*\]/gi, " ")
    .replace(/[^a-z0-9.%+$]+/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

function tokens(s: string): string[] {
  return normSpeak(s).split(" ").filter(Boolean);
}

function content(s: string): string[] {
  return tokens(s).filter((w) => w.length >= 2 && !STOP.has(w));
}

/** True when `candidate` only restates `recent` (same facts, no new numbers or codes). */
export function isEchoSpeak(candidate: string, recent: string[]): boolean {
  const c = normSpeak(candidate);
  if (!c) return false;
  const cWords = content(candidate);
  const cNums = tokens(candidate).filter((w) => /\d/.test(w));
  for (const r of recent) {
    const n = normSpeak(r);
    if (!n) continue;
    if (c === n) return true;
    if (c.length >= 24 && n.length >= 24 && (c.includes(n) || n.includes(c))) return true;
    const rWords = new Set(content(r));
    const rNums = new Set(tokens(r).filter((w) => /\d/.test(w)));
    if (cWords.length >= 4 && rWords.size >= 4) {
      const overlap = cWords.filter((w) => rWords.has(w)).length;
      const newNums = cNums.filter((w) => !rNums.has(w));
      if (overlap / cWords.length >= 0.75 && newNums.length === 0) return true;
    }
  }
  return false;
}

/** Duplex: while the user is talking (VAD says speech, or a final transcript just landed), system updates wait. */
export const VAD_SPEECH = 0.5;
export const VAD_HOLD_MS = 1200;
export const TRANSCRIPT_HOLD_MS = 1500;
/** An urgent update (stop loss sold, a crash) waits at most this long for the user to pause. */
export const URGENT_MAX_HOLD_MS = 4000;

export class UserActivity {
  lastVoiceAt = 0;
  lastFinalAt = 0;
  constructor(private now: () => number = Date.now) {}
  vad(score: number) {
    if (score >= VAD_SPEECH) this.lastVoiceAt = this.now();
  }
  finalTranscript() {
    this.lastFinalAt = this.now();
  }
  talking(): boolean {
    const t = this.now();
    return t - this.lastVoiceAt < VAD_HOLD_MS || t - this.lastFinalAt < TRANSCRIPT_HOLD_MS;
  }
}

/** After an update is sent, the next one waits for the agent's reply (or this long), so updates never cut each other off. */
export const REPLY_WAIT_MS = 8000;

/** Research-round progress: when several queue up, only the latest is still news ("1 coin qualified" makes
 * "Found 52 candidate coins" stale), so a merged update keeps just the last one. */
export const PROGRESS_TYPES = new Set(["mandate_frozen", "round_started", "candidates_found", "free_check_result",
  "pipeline_status", "research_done", "desk_ready"]);

type Item = { text: string; speak: string; loud: boolean; at: number; type: string };
type Memory = { text: string; at: number; fromTool: boolean };

export class BroadcastQueue {
  private q: Item[] = [];
  private seen = new Set<number>();
  private heard: Memory[] = [];
  private awaitingUntil = 0;
  constructor(
    private send: (text: string) => void,
    private canSpeak: () => boolean,
    private userTalking: () => boolean = () => false,
    private now: () => number = Date.now,
  ) {}

  /** Remember a tool speak line or an agent utterance so a later system update is not read twice. */
  remember(text: string, fromTool = false) {
    const t = (text ?? "").trim();
    if (!t) return;
    this.heard.push({ text: t, at: this.now(), fromTool });
    this.heard = this.heard.filter((h) => this.now() - h.at <= ECHO_WINDOW_MS).slice(-8);
  }

  private recent(): Memory[] {
    const t = this.now();
    this.heard = this.heard.filter((h) => t - h.at <= ECHO_WINDOW_MS);
    return this.heard;
  }

  private isDuplicate(ev: BackendEvent): boolean {
    const mem = this.recent();
    if (isEchoSpeak(ev.speak, mem.map((h) => h.text))) return true;
    if (TOOL_COVERED_TYPES.has(ev.type) && mem.some((h) => h.fromTool)) return true;
    return false;
  }

  push(ev: BackendEvent) {
    if (!shouldSpeak(ev) || this.seen.has(ev.id)) return;
    if (!ev.loud && this.isDuplicate(ev)) {
      this.seen.add(ev.id);
      return;
    }
    this.seen.add(ev.id);
    const item = { text: broadcastText(ev), speak: ev.speak.trim(), loud: !!ev.loud, at: this.now(), type: ev.type };
    if (ev.loud) this.q.unshift(item);
    else this.q.push(item);
    this.flush();
  }

  /** The user just spoke or typed to the agent: hold updates until it answers (or REPLY_WAIT_MS). */
  expectReply() {
    this.awaitingUntil = this.now() + REPLY_WAIT_MS;
  }

  /** The agent answered (a line or a tool call): the next update may go out. */
  agentReplied() {
    this.awaitingUntil = 0;
  }

  /** Sends one message at a time and waits for the agent's reply before the next, so a fresh update never
   * interrupts the agent's answer to the previous one. Queued routine updates go out together in one message
   * (in order), so a stale progress line is never read after the outcome. */
  flush() {
    // A tool can return the same words while its event waited in the queue: drop routine items that are now echoes.
    if (this.q.length) {
      const said = this.recent().map((h) => h.text);
      this.q = this.q.filter((it) => it.loud || !isEchoSpeak(it.speak, said));
    }
    const head = this.q[0];
    if (!head || !this.canSpeak()) return;
    const urgentDue = head.loud && this.now() - head.at >= URGENT_MAX_HOLD_MS;
    if (this.now() < this.awaitingUntil && !head.loud) return;   // urgent news does not wait for the reply
    if (this.userTalking() && !urgentDue) return;
    let text: string;
    if (head.loud) {
      text = this.q.shift()!.text;
    } else {
      const batch: Item[] = [];
      while (this.q.length && !this.q[0].loud) batch.push(this.q.shift()!);
      const lastProgress = batch.map((b) => PROGRESS_TYPES.has(b.type)).lastIndexOf(true);
      for (let i = batch.length - 1; i >= 0; i--) if (i !== lastProgress && PROGRESS_TYPES.has(batch[i].type)) batch.splice(i, 1);
      text = batch.length === 1 ? batch[0].text : SYSTEM_MARK + " " + batch.map((b) => endSentence(plainSpeak(b.speak))).join(" ");
    }
    this.remember(text, false);
    this.awaitingUntil = this.now() + REPLY_WAIT_MS;
    this.send(text);
  }

  get pending() {
    return this.q.length;
  }

  clear() {
    this.q = [];
    this.heard = [];
    this.awaitingUntil = 0;
  }
}

function endSentence(s: string): string {
  const t = s.trim();
  return /[.!?]$/.test(t) ? t : t + ".";
}
