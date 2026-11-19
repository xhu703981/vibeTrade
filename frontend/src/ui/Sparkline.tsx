import { useEffect, useState } from "react";
import type { Api } from "../api";
import type { Candles } from "../types";

/** Mini K-line for a picks row: the closes of the last ~day of 15m candles as a quiet slate line;
 *  only the last point carries colour (up / down). `dir` overrides the direction (e.g. the row's 24h change). */
export function Sparkline(props: { api: Api; pool: string | null; width?: number; height?: number; dir?: "up" | "down" }) {
  const [rows, setRows] = useState<Candles["candles"] | null>(null);
  const w = props.width ?? 80, h = props.height ?? 24;
  useEffect(() => {
    if (!props.pool) return;
    let stop = false;
    const load = () => props.api.get(`/market/ohlcv/${encodeURIComponent(props.pool!)}?tf=15m`)
      .then((x) => { const r = x as unknown as Candles; if (!stop && r?.ok) setRows(r.candles.slice(-96)); })
      .catch(() => {});
    load();
    const t = setInterval(load, 30_000);
    return () => { stop = true; clearInterval(t); };
  }, [props.api, props.pool]);
  const sp = rows && rows.length > 1 ? sparkPath(rows.map((r) => r[4]), w, h) : null;
  return (
    <svg className="spark" width={w} height={h} viewBox={`0 0 ${w} ${h}`} aria-hidden>
      {sp && <path d={sp.d} />}
      {sp && <circle className={props.dir ?? sp.className} cx={sp.last[0]} cy={sp.last[1]} r={2} />}
    </svg>
  );
}

export function sparkPath(closes: number[], w: number, h: number): { d: string; className: string; last: [number, number] } {
  const lo = Math.min(...closes), hi = Math.max(...closes);
  const span = hi - lo || 1;
  const pad = 2.5;   // room for the end dot
  const pts = closes.map((c, i) => [pad + (i / (closes.length - 1)) * (w - 2 * pad), h - pad - ((c - lo) / span) * (h - 2 * pad)] as [number, number]);
  const d = pts.map(([x, y], i) => `${i ? "L" : "M"}${x.toFixed(1)},${y.toFixed(1)}`).join(" ");
  return { d, className: closes[closes.length - 1] >= closes[0] ? "up" : "down", last: pts[pts.length - 1] };
}
