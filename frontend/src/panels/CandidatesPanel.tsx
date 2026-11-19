import { big, pct, short } from "./format";

export function CandidatesPanel(props: { candidates: any[]; research: any[] }) {
  if (!props.candidates.length) return <p className="empty">No coins yet. Set your limits and say “start research”.</p>;
  const res = new Map(props.research.map((r) => [r.mint, r]));
  return (
    <section className="panel" aria-label="Candidates">
      <h3>Candidates <span className="num soft">{props.candidates.length}</span></h3>
      <table className="grid small">
        <thead><tr><th>Symbol</th><th>Liquidity</th><th>Market cap</th><th>Round trip</th><th>Result</th></tr></thead>
        <tbody>
          {props.candidates.map((c) => {
            const r = res.get(c.mint);
            return (
              <tr key={c.mint} className={r?.status === "pass" ? "pass" : c.status?.startsWith("fail") ? "fail" : ""}>
                <td title={c.mint}>{c.symbol || short(c.mint)}{c.paid_promo ? ", promoted" : ""}</td>
                <td>${big(c.metrics?.liquidity_usd)}</td>
                <td>${big(c.metrics?.market_cap)}</td>
                <td className="num">{pct(c.roundtrip_loss_pct)}</td>
                <td title={[...(c.reasons ?? []), ...(r?.reasons ?? [])].join("\n")}>{r?.status_label ?? c.status_label}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </section>
  );
}
