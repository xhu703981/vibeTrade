import { useEffect, useRef, type ReactNode } from "react";

/** idle: listening for "Vibe" (slate, flat). listening: in conversation, her voice drives a slate wave.
 *  speaking: the agent's voice drives an amber wave. working: background research, amber line with a slow highlight. */
export type VoiceState = "off" | "connecting" | "muted" | "idle" | "listening" | "speaking" | "working";

export const VOICE_LABEL: Record<VoiceState, string> = {
  off: "Voice is off", connecting: "Connecting", muted: "Muted", idle: "Listening for ‘Vibe’",
  listening: "In conversation", speaking: "In conversation", working: "Working on research",
};

/** Average of a frequency array, 0..1 (speech sits low in the spectrum, so only the first half counts). */
export function levelOf(data: Uint8Array | undefined | null): number {
  if (!data || !data.length) return 0;
  const n = Math.max(1, Math.floor(data.length / 2));
  let s = 0;
  for (let i = 0; i < n; i++) s += data[i];
  return Math.min(1, (s / n / 255) * 2.2);
}

const cssVar = (name: string) => getComputedStyle(document.documentElement).getPropertyValue(name).trim() || "#9AA3A9";

/** The one bold thing on screen: a 2px line across the conversation, drawn on a canvas from the real audio level. */
export function VoiceLine(props: { state: VoiceState; sample: () => Uint8Array | null | undefined; aside?: ReactNode }) {
  const canvas = useRef<HTMLCanvasElement>(null);
  const state = useRef(props.state);
  state.current = props.state;
  const sample = useRef(props.sample);
  sample.current = props.sample;
  useEffect(() => {
    const cv = canvas.current;
    if (!cv) return;
    const ctx = cv.getContext("2d");
    if (!ctx) return;
    const still = typeof window.matchMedia === "function" && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const colors = { slate: cssVar("--slate"), amber: cssVar("--amber") };
    let env = new Float32Array(0);
    let raf = 0;
    const draw = (t: number) => {
      const dpr = window.devicePixelRatio || 1;
      const w = Math.max(1, Math.round(cv.clientWidth * dpr)), h = Math.max(1, Math.round(cv.clientHeight * dpr));
      if (cv.width !== w || cv.height !== h) { cv.width = w; cv.height = h; }
      ctx.clearRect(0, 0, w, h);
      const st = state.current;
      const mid = h / 2, lw = 2 * dpr;
      const amber = st === "speaking" || st === "working";
      ctx.fillStyle = ctx.strokeStyle = amber ? colors.amber : colors.slate;
      ctx.lineWidth = lw;
      const wave = !still && (st === "listening" || st === "speaking");
      let data: Uint8Array | null | undefined = null;
      if (wave) { try { data = sample.current(); } catch { data = null; } }
      if (wave && data && data.length) {
        const step = 3 * dpr, n = Math.ceil(w / step) + 1;
        if (env.length !== n) env = new Float32Array(n);
        const bins = Math.max(1, Math.floor(data.length / 2));
        ctx.beginPath();
        for (let i = 0; i < n; i++) {
          const x = i * step;
          // spread the low (speech) half of the spectrum across the line, mirrored from the centre outwards
          const f = Math.abs(x / w - 0.5) * 2;
          const target = data[Math.min(bins - 1, Math.floor(f * bins))] / 255;
          env[i] = env[i] * 0.7 + target * 0.3;
          const taper = Math.sin(Math.PI * Math.min(1, Math.max(0, x / w)));
          const y = mid + Math.sin(x / (14 * dpr) - t / 120) * env[i] * taper * (mid - lw);
          if (i) ctx.lineTo(x, y); else ctx.moveTo(x, y);
        }
        ctx.stroke();
      } else if (st === "working") {
        ctx.globalAlpha = 0.45;
        ctx.fillRect(0, mid - lw / 2, w, lw);
        ctx.globalAlpha = 1;
        const seg = w * 0.12;
        const x = still ? (w - seg) / 2 : ((t / 4000) % 1) * (w + seg) - seg;   // one slow pass every 4 s
        ctx.fillRect(Math.max(0, x), mid - lw / 2, Math.min(seg, w - Math.max(0, x)), lw);
      } else {
        ctx.fillRect(0, mid - lw / 2, w, lw);
      }
      raf = requestAnimationFrame(draw);
    };
    raf = requestAnimationFrame(draw);
    return () => cancelAnimationFrame(raf);
  }, []);
  return (
    <div className={`voiceline vs-${props.state}`} data-state={props.state}>
      <span className="vl-label" role="status">{VOICE_LABEL[props.state]}</span>
      <canvas ref={canvas} className="vl-canvas" aria-hidden />
      {props.aside}
    </div>
  );
}

/** The microphone: the main button and the only round one; its outline turns amber while she is speaking. */
export function MicButton(props: { level: () => number; muted: boolean; connected: boolean; disabled?: boolean; onClick: () => void; label: string }) {
  const ref = useRef<HTMLButtonElement>(null);
  const level = useRef(props.level);
  level.current = props.level;
  useEffect(() => {
    let raf = 0;
    const tick = () => {
      let v = 0;
      try { v = level.current(); } catch { v = 0; }
      ref.current?.classList.toggle("talking", v > 0.12);
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, []);
  return (
    <button ref={ref} type="button" className={`mic ${props.muted ? "muted" : ""} ${props.connected ? "live" : ""}`} onClick={props.onClick}
            disabled={props.disabled} aria-label={props.label} aria-pressed={props.connected ? props.muted : undefined}>
      {props.muted ? (
        <svg viewBox="0 0 24 24" width="22" height="22" aria-hidden><path d="M9 9v2a3 3 0 005 2.2M15 10V6a3 3 0 00-5.6-1.5M5 11a7 7 0 0011.5 5.4M19 11a7 7 0 01-.6 2.8M12 18v3M3 3l18 18" stroke="currentColor" strokeWidth="1.7" fill="none" strokeLinecap="round" /></svg>
      ) : (
        <svg viewBox="0 0 24 24" width="22" height="22" aria-hidden><rect x="9" y="3" width="6" height="11" rx="3" stroke="currentColor" strokeWidth="1.7" fill="none" /><path d="M5 11a7 7 0 0014 0M12 18v3" stroke="currentColor" strokeWidth="1.7" fill="none" strokeLinecap="round" /></svg>
      )}
    </button>
  );
}
