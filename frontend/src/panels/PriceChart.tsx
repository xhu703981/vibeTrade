// K-line chart for one pool: OHLCV candles from our backend (/api/market/ohlcv/{pool}, GeckoTerminal numbers only),
// drawn natively with lightweight-charts and themed from the design tokens. If the candles cannot be loaded, offer the
// DexScreener embed instead. Nothing here is ever sent to the voice agent.
// The TradingView attribution logo is off; the attribution notice and the tradingview.com link are in the Details
// footer and in THIRD_PARTY_NOTICES (as the library's licence asks).
import { CandlestickSeries, ColorType, HistogramSeries, LineStyle, createChart, type IChartApi, type IPriceLine, type UTCTimestamp } from "lightweight-charts";
import { useEffect, useRef, useState } from "react";
import type { Api } from "../api";
import type { Candles } from "../types";
import { useNow } from "../useBackend";
import { signedPct, updatedAgo } from "./format";

export const TIMEFRAMES = ["5m", "15m", "1h", "4h", "1d"] as const;
export type Tf = (typeof TIMEFRAMES)[number];

export function fmtPrice(p: number): string {
  if (!Number.isFinite(p)) return "\u2013";
  if (p === 0) return "0";
  const a = Math.abs(p);
  if (a >= 1000) return p.toLocaleString(undefined, { maximumFractionDigits: 0 });
  if (a >= 1) return p.toFixed(4);
  // small memecoin prices: keep 4 significant digits (0.00001234)
  const zeros = Math.max(0, -Math.floor(Math.log10(a)) - 1);
  return p.toFixed(Math.min(14, zeros + 4));
}

export function dexEmbedUrl(pool: string) {
  return `https://dexscreener.com/solana/${encodeURIComponent(pool)}?embed=1&theme=dark&trades=0&info=0`;
}

/** Colours, sizes and the font of the chart, read from the CSS tokens so the chart matches the screen. */
export function chartTheme() {
  const css = typeof document !== "undefined" ? getComputedStyle(document.documentElement) : null;
  const v = (n: string, d: string) => (css?.getPropertyValue(n).trim() || d);
  return {
    rule: v("--rule", "#2E363D"), slate: v("--slate", "#9AA3A9"), chalk: v("--chalk", "#E4E2DC"), raise: v("--raise", "#252C32"),
    up: v("--up", "#5BAE85"), down: v("--down", "#D9695F"), font: v("--font", "IBM Plex Sans, system-ui, sans-serif"),
    xs: parseInt(v("--t-xs", "12px"), 10) || 12,
  };
}

/** "#5BAE85" + 0.4 -> "rgba(91,174,133,0.4)" (volume bars at 40%). */
export function alpha(hex: string, a: number): string {
  const h = hex.replace("#", "");
  const n = parseInt(h.length === 3 ? h.split("").map((c) => c + c).join("") : h, 16);
  return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`;
}

function toCandle(rows: Candles["candles"]) {
  return rows.map(([t, o, h, l, cl]) => ({ time: t as UTCTimestamp, open: o, high: h, low: l, close: cl }));
}

/** entryRatio: the price move since the fill (position.last_ratio), so entry = last close / ratio. stopPct: e.g. 24 -> −24%. */
export type PositionLines = { entryRatio: number; stopPct: number | null };

function Candlesticks(props: { candles: Candles["candles"]; height: number; lines?: PositionLines | null }) {
  const el = useRef<HTMLDivElement>(null);
  const chart = useRef<IChartApi | null>(null);
  const candle = useRef<ReturnType<IChartApi["addSeries"]> | null>(null);
  const vol = useRef<ReturnType<IChartApi["addSeries"]> | null>(null);
  const priceLines = useRef<IPriceLine[]>([]);
  const times = useRef("");
  const theme = useRef(chartTheme());
  useEffect(() => {
    if (!el.current) return;
    const th = (theme.current = chartTheme());
    const c = createChart(el.current, {
      autoSize: true,
      height: props.height,
      layout: { background: { type: ColorType.Solid, color: "transparent" }, textColor: th.slate, fontSize: th.xs, fontFamily: th.font, attributionLogo: false },
      grid: { vertLines: { color: th.rule }, horzLines: { color: th.rule } },
      rightPriceScale: { borderColor: th.rule },
      // fixed edges: the library then pulls the first and last time labels inside the chart instead of cutting them off
      // (on a phone a "16:00" at the left edge showed as "6:00")
      timeScale: { borderColor: th.rule, timeVisible: true, secondsVisible: false, fixLeftEdge: true, fixRightEdge: true },
      crosshair: { mode: 0, vertLine: { color: th.slate, labelBackgroundColor: th.raise }, horzLine: { color: th.slate, labelBackgroundColor: th.raise } },
    });
    candle.current = c.addSeries(CandlestickSeries, {
      upColor: th.up, downColor: th.down, borderVisible: false, wickUpColor: th.up, wickDownColor: th.down,
      lastValueVisible: false, priceLineVisible: false,
      priceFormat: { type: "custom", formatter: fmtPrice, minMove: 1e-12 },
    });
    vol.current = c.addSeries(HistogramSeries, { priceFormat: { type: "volume" }, priceScaleId: "vol", lastValueVisible: false, priceLineVisible: false });
    c.priceScale("vol").applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
    chart.current = c;
    times.current = "";
    priceLines.current = [];
    return () => { c.remove(); chart.current = null; candle.current = null; vol.current = null; priceLines.current = []; };
  }, [props.height]);
  useEffect(() => {
    const s = candle.current;
    if (!s || !vol.current) return;
    const th = theme.current;
    const rows = props.candles;
    const toVol = (r: Candles["candles"][number]) => ({ time: r[0] as UTCTimestamp, value: r[5], color: alpha(r[4] >= r[1] ? th.up : th.down, 0.4) });
    const key = rows.map((r) => r[0]).join(",");
    if (key !== times.current) {
      s.setData(toCandle(rows));
      vol.current.setData(rows.map(toVol));
      if (!times.current) chart.current?.timeScale().fitContent();
      times.current = key;
    } else if (rows.length) {
      s.update(toCandle(rows).at(-1)!);
      vol.current.update(toVol(rows.at(-1)!));
    }
    // last price: chalk on raise (not a red or green tag); entry (slate) and stop (down) as dashed lines when she holds it
    for (const pl of priceLines.current) s.removePriceLine(pl);
    priceLines.current = [];
    const last = rows.at(-1)?.[4];
    if (last == null) return;
    priceLines.current.push(s.createPriceLine({ price: last, color: th.raise, lineVisible: false, axisLabelVisible: true,
                                                axisLabelColor: th.raise, axisLabelTextColor: th.chalk, lineWidth: 1, lineStyle: LineStyle.Solid }));
    const ln = props.lines;
    if (ln && ln.entryRatio > 0) {
      const entry = last / ln.entryRatio;
      priceLines.current.push(s.createPriceLine({ price: entry, color: th.slate, lineStyle: LineStyle.Dashed, lineWidth: 1, axisLabelVisible: true,
                                                  axisLabelColor: th.raise, axisLabelTextColor: th.slate, title: "Entry" }));
      if (ln.stopPct != null) {
        priceLines.current.push(s.createPriceLine({ price: entry * (1 - Math.abs(ln.stopPct) / 100), color: th.down, lineStyle: LineStyle.Dashed, lineWidth: 1,
                                                    axisLabelVisible: true, axisLabelColor: th.raise, axisLabelTextColor: th.down, title: "Stop" }));
      }
    }
  }, [props.candles, props.lines?.entryRatio, props.lines?.stopPct]);
  return <div ref={el} className="kline" style={{ height: props.height }} data-testid="kline" />;
}

export function PriceChart(props: { api: Api; pool: string | null; height?: number; label?: string; compact?: boolean; lines?: PositionLines | null }) {
  const [tf, setTf] = useState<Tf>("15m");
  const [data, setData] = useState<Candles | null>(null);
  const [loading, setLoading] = useState(false);
  const [embed, setEmbed] = useState(false);
  const height = props.height ?? 300;
  const now = useNow(1000);
  useEffect(() => {
    if (!props.pool) return;
    let stop = false;
    let busy = false;   // one request per chart at a time: slow K-lines must not stack up
    const load = async (silent = false) => {
      if (busy) return;
      busy = true;
      if (!silent) setLoading(true);
      try {
        const r = (await props.api.get(`/market/ohlcv/${encodeURIComponent(props.pool!)}?tf=${tf}`)) as Candles;
        if (!stop) setData(r);
      } catch (e) {
        if (!stop) setData((prev) => prev ?? { ok: false, reason: "chart data unavailable", candles: [] });
      } finally {
        busy = false;
        if (!stop) setLoading(false);
      }
    };
    load(false);
    const t = setInterval(() => load(true), 8_000);
    return () => { stop = true; clearInterval(t); };
  }, [props.api, props.pool, tf]);

  if (!props.pool) return <div className="chart-box empty">No pool address for this coin, so there is no chart.</div>;
  const ok = !!data?.ok && (data.candles?.length ?? 0) > 1;
  const last = ok ? data!.candles[data!.candles.length - 1] : null;
  const first = ok ? data!.candles[0] : null;
  const chg = last && first && first[1] > 0 ? (100 * (last[4] - first[1])) / first[1] : null;
  const ago = updatedAgo(data?.fetched_at, now);
  return (
    <div className="chart-box" aria-label={`Price chart ${props.label ?? ""}`}>
      <div className="chart-head">
        {!props.compact && props.label && <b className="chart-label">{props.label}</b>}
        {!props.compact && last && <span className="chart-price num">${fmtPrice(data?.price ?? last[4])}</span>}
        {!props.compact && chg != null && <span className={`chg num ${chg >= 0 ? "up" : "down"}`}>{signedPct(chg)} <small>in view</small></span>}
        {ago && <span className="chart-ago" data-testid="chart-updated">{ago}</span>}
        <span className="spacer" />
        <div className="tf-tabs" role="tablist" aria-label="Timeframe">
          {TIMEFRAMES.map((x) => (
            <button key={x} role="tab" aria-selected={x === tf && !embed} className={x === tf && !embed ? "on" : ""} onClick={() => { setTf(x); setEmbed(false); }}>{x}</button>
          ))}
        </div>
      </div>
      {embed ? (
        <iframe className="dex-embed" title="DexScreener chart" src={dexEmbedUrl(props.pool)} style={{ height }} sandbox="allow-scripts allow-same-origin" />
      ) : ok ? (
        <Candlesticks candles={data!.candles} height={height} lines={props.lines} />
      ) : (
        <div className="chart-fallback" style={{ height }}>
          {loading && !data ? <span>Loading candles…</span> : (
            <>
              <span>Candles aren't available right now{data?.reason ? ` (${data.reason})` : ""}.</span>
              <button className="text-btn" onClick={() => setEmbed(true)}>Show the DexScreener chart instead</button>
            </>
          )}
        </div>
      )}
      <div className="chart-foot">
        <span>{embed ? "Source: DexScreener embed." : `Source: ${data?.source ?? "GeckoTerminal"} via the local backend.${data?.stale ? " Cached, the live fetch failed." : ""}`}</span>
        <span className="spacer" />
        <button className="text-btn" onClick={() => setEmbed(!embed)} aria-pressed={embed}>{embed ? "Our chart" : "DexScreener"}</button>
      </div>
    </div>
  );
}
