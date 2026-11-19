// Client-side speech gate for Eleven Realtime v1 Max.
//
// Realtime still generates a spoken line in the same turn as a tool call even when
// pre_tool_speech is off (and even after speculative_turn is disabled). That guessed
// line can contradict the backend ("five dollars" vs "five cents"). The official
// speak text only arrives after the tool returns, as a second utterance.
//
// We cannot stop the model from generating the first line, and we cannot inject our
// own TTS into the ElevenLabs voice session. Instead, while a mandate-style turn is
// armed: mute output, flush any guessed audio when the tool returns, and only then
// unmute so the user hears the official speak once. The guessed transcript line is
// also withheld from the on-screen log.

export const GATE_SPEAK_GRACE_MS = 4000;
export const GATE_ARM_MAX_MS = 15000;

export type VolumeFn = (volume: number) => void;
export type FlushFn = () => void;

export class SpeechGate {
  armed = false;
  toolSeen = false;
  idleMuted = false;
  swallowed: string[] = [];
  private speakTimer: ReturnType<typeof setTimeout> | null = null;
  private armTimer: ReturnType<typeof setTimeout> | null = null;

  constructor(
    private setVolume: VolumeFn,
    private flushOutput: FlushFn,
    private now: () => number = Date.now,
  ) {}

  /** Mute agent output until a tool result (or a timeout) for this user turn. */
  arm() {
    this.clearSpeakTimer();
    this.clearArmTimer();
    this.armed = true;
    this.toolSeen = false;
    this.swallowed = [];
    this.setVolume(0);
    this.armTimer = setTimeout(() => this.release(), GATE_ARM_MAX_MS);
  }

  /** Agent audio or a transcript has started while we are still waiting for a tool. */
  noteSpeaking() {
    if (!this.armed || this.toolSeen || this.speakTimer) return;
    this.speakTimer = setTimeout(() => this.release(), GATE_SPEAK_GRACE_MS);
  }

  onToolStart() {
    if (!this.armed) return;
    this.toolSeen = true;
    this.clearSpeakTimer();
    this.clearArmTimer();
  }

  /** Tool finished: drop guessed audio, unmute so the official speak can be heard. */
  release() {
    if (!this.armed) return;
    this.clearSpeakTimer();
    this.clearArmTimer();
    this.flushOutput();
    this.setVolume(1);
    this.armed = false;
    this.toolSeen = false;
  }

  /** End of call or user barge-in: never leave the speaker muted. */
  disarm() {
    this.clearSpeakTimer();
    this.clearArmTimer();
    // a barge-in does not lift the IDLE mute: only words addressed to the agent (or an update it reads) do
    if (!this.idleMuted) this.setVolume(1);
    this.armed = false;
    this.toolSeen = false;
    this.swallowed = [];
  }

  /** Mute (and hide) the agent's reply when IDLE speech was not addressed to us. */
  muteIdle() {
    this.idleMuted = true;
    this.setVolume(0);
  }

  unmuteIdle() {
    this.idleMuted = false;
    if (!this.armed) this.setVolume(1);
  }

  /** False = this is a pre-tool guess, keep it off the transcript. */
  displayAgent(text: string): boolean {
    if (this.idleMuted) {
      this.swallowed.push(text);
      return false;
    }
    if (!this.armed) return true;
    this.swallowed.push(text);
    this.noteSpeaking();
    return false;
  }

  private clearSpeakTimer() {
    if (this.speakTimer) clearTimeout(this.speakTimer);
    this.speakTimer = null;
  }

  private clearArmTimer() {
    if (this.armTimer) clearTimeout(this.armTimer);
    this.armTimer = null;
  }
}

/** Conversation internals we have to reach: the public SDK has no interrupt() helper. */
export function flushConversationOutput(conv: unknown) {
  if (!conv || typeof conv !== "object") return;
  const c = conv as { currentEventId?: number; lastInterruptTimestamp?: number; output?: { interrupt?: (ms?: number) => void } };
  const cur = typeof c.currentEventId === "number" ? c.currentEventId : 0;
  c.lastInterruptTimestamp = cur + 1;
  c.output?.interrupt?.(80);
}
