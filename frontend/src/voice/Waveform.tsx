import { useEffect, useRef } from "react";

// Speaking waveform: draws bars from the frequency data every frame; a flat line when silent.
export function Waveform(props: { active: boolean; read: () => Uint8Array | undefined; color?: string }) {
  const ref = useRef<HTMLCanvasElement>(null);
  const active = useRef(props.active);
  active.current = props.active;
  const read = useRef(props.read);
  read.current = props.read;
  useEffect(() => {
    let raf = 0;
    const draw = () => {
      const cv = ref.current;
      const ctx = cv?.getContext?.("2d");
      if (cv && ctx) {
        const w = cv.width, h = cv.height;
        ctx.clearRect(0, 0, w, h);
        ctx.fillStyle = props.color ?? "#7dd3fc";
        let data: Uint8Array | undefined;
        try { data = active.current ? read.current() : undefined; } catch { data = undefined; }
        const n = 32;
        for (let i = 0; i < n; i++) {
          const v = data && data.length ? data[Math.floor((i * data.length) / n)] / 255 : 0.04;
          const bh = Math.max(2, v * h);
          ctx.fillRect(i * (w / n) + 1, (h - bh) / 2, w / n - 2, bh);
        }
      }
      raf = requestAnimationFrame(draw);
    };
    raf = requestAnimationFrame(draw);
    return () => cancelAnimationFrame(raf);
  }, [props.color]);
  return <canvas ref={ref} width={240} height={48} className={`wave ${props.active ? "on" : ""}`} aria-hidden />;
}
