// IDLE / ENGAGED wake-by-name. In IDLE the agent only answers when addressed as Vibe
// (or Vibe Trader, hey Vibe, vibes, ...). Otherwise we mute the reply and block tools.
// ENGAGED: every turn counts until a quiet timeout, or the words are clearly for someone else.
// When the agent speaks (alert or a question) we go ENGAGED so the reply needs no name.

export const WAKE_RE = /\b(?:hey|hi|yo|ok(?:ay)?)?\s*,?\s*vibe(?:s)?(?:[\s-]*trader)?\b/i;
export const OTHER_RE = /\b(two|three|four|a|one)\s+(coffees?|lattes?|teas?|cappuccinos?)\b|\b(starbucks|dunkin|drive[\s-]*through|drive[\s-]*thru)\b|\b(can i (get|have|order)|i'?ll (have|take|get)|order(?:ing)?)\b.{0,40}\b(coffee|latte|grande|venti|tall)\b|\b(thanks|thank you),?\s+(guys|man|buddy|dude|hon(?:ey)?)\b|\b(see you|talk later|love you)\b|\b(not (to |for )?you|i'?m (on a call|talking to))\b/i;
export const TRADING_RE = /\b(coin|buy|sell|approve|reject|confirm|mandate|budget|filter|research|positions?|stop|profit|card|trade|status|exit|desk|jup|jto|pyth|bonk|wif|ray|cbbtc|wbtc|weth|dollar|cent|penny|percent|break[\s-]*even)\b|\b\d{2}\b/i;
export const FILLER_RE = /^(?:\s*(?:h+m+|u+m+|u+h+|e+r+m*|okay|ok|so|well|yeah|right|wait|hold on)[\s,.!?…-]*)+$/i;

export const DEFAULT_IDLE_TIMEOUT_MS = 25_000;
// After the agent asks a question, wait longer for the answer before needing the name again.
export const QUESTION_TIMEOUT_MS = 90_000;

export type Mode = "idle" | "engaged";
export type Decision = { addressed: boolean; reason: string; mode: Mode };

export function hasWakeName(text: string): boolean {
  return WAKE_RE.test(text || "");
}

export function clearlyOther(text: string): boolean {
  const t = (text || "").trim();
  if (!t || hasWakeName(t) || TRADING_RE.test(t)) return false;
  return OTHER_RE.test(t);
}

export function clearlyTrading(text: string): boolean {
  const t = (text || "").trim();
  return !!t && TRADING_RE.test(t) && !clearlyOther(t);
}

export function isFiller(text: string): boolean {
  return FILLER_RE.test((text || "").trim());
}

/** True / false when sure; null when ENGAGED and ambiguous (prefer responding). */
export function addressedDeterministic(text: string, mode: Mode): boolean | null {
  const t = (text || "").trim();
  if (!t) return false;
  if (hasWakeName(t)) return true;
  if (mode !== "engaged") return false;
  if (clearlyOther(t)) return false;
  if (clearlyTrading(t) || isFiller(t)) return true;
  return null;
}

/** "approve 83", "reject 47", "yes 83": a reply to a confirmation card that is on screen now. */
export function answersCard(text: string, codes: string[]): boolean {
  if (!codes.length) return false;
  const t = (text || "").toLowerCase();
  if (!/\b(approve|approved|reject|cancel|yes|no|confirm|go ahead|do it)\b/.test(t)) return false;
  return codes.some((c) => new RegExp(`\\b${c}\\b`).test(t));
}

export const IDLE_NOTE =
  'You are now IDLE. Only respond if the user addresses you as Vibe, Vibe Trader, or hey Vibe (including ASR variants like "vibe trader" or "vibes"). Otherwise call skip_turn if you have that tool, say nothing, and call no other tool.';
export const ENGAGED_NOTE =
  "You are now ENGAGED. Respond to the user normally; they do not need to say your name.";

export class Engagement {
  mode: Mode = "idle";
  lastExchangeAt = 0;
  awaitingAnswer = false;
  /** The last user turn answered a question the agent had just asked (never second-guess it as side talk). */
  answeredQuestion = false;
  /** The agent's latest line, sent as context to the addressee check. */
  lastAgentLine = "";
  /** Codes of confirmation cards waiting for her answer: "approve 83" is for us even without the name. */
  pendingCodes: string[] = [];
  constructor(
    public idleTimeoutMs: number = DEFAULT_IDLE_TIMEOUT_MS,
    private now: () => number = Date.now,
  ) {}

  engage(reason = "exchange") {
    const was = this.mode;
    this.mode = "engaged";
    this.lastExchangeAt = this.now();
    return was !== "engaged" ? reason : null;
  }

  idle(reason = "timeout") {
    const was = this.mode;
    this.mode = "idle";
    return was !== "idle" ? reason : null;
  }

  /** The agent said something: stay ENGAGED; a question waits longer for the reply. */
  agentLine(text: string) {
    this.engage("agent");
    this.lastAgentLine = (text || "").slice(-300);
    this.awaitingAnswer = /\?\s*$/.test((text || "").trim());
  }

  /** Someone is still talking or a tool is running: the quiet timer has not started yet. */
  hold() {
    if (this.mode === "engaged") this.lastExchangeAt = this.now();
  }

  /** Call on a timer. Returns "timeout" when it just flipped to IDLE. */
  tick(): string | null {
    if (this.mode !== "engaged") return null;
    const limit = this.awaitingAnswer ? Math.max(this.idleTimeoutMs, QUESTION_TIMEOUT_MS) : this.idleTimeoutMs;
    if (this.now() - this.lastExchangeAt >= limit) {
      this.awaitingAnswer = false;
      return this.idle("timeout");
    }
    return null;
  }

  onUserText(text: string): Decision {
    this.answeredQuestion = (this.mode === "engaged" && this.awaitingAnswer) || answersCard(text, this.pendingCodes);
    const sure = this.answeredQuestion && !clearlyOther(text) ? true : addressedDeterministic(text, this.mode);
    if (sure === true) {
      this.awaitingAnswer = false;
      this.engage("addressed");
      return { addressed: true, reason: hasWakeName(text) ? "wake" : "engaged", mode: this.mode };
    }
    if (sure === false) {
      if (this.mode === "engaged") this.idle("side_talk");
      return { addressed: false, reason: this.mode === "idle" ? "no_name" : "side_talk", mode: this.mode };
    }
    // ambiguous while ENGAGED: prefer responding
    this.awaitingAnswer = false;
    this.engage("unsure");
    return { addressed: true, reason: "unsure", mode: this.mode };
  }

  /** A later shadow/LLM result said this was not for us. */
  markSideTalk() {
    if (this.answeredQuestion) return;   // she was answering our question: the classifier is wrong
    this.idle("side_talk");
  }
}
