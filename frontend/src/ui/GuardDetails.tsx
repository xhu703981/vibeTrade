import { useEffect, useState } from "react";
import { makeApi, type Api } from "../api";

export type GuardView = {
  guard_run_id: string;
  transaction_sha256: string;
  tx_bytes_len: number | null;
  tx_base64_prefix: string | null;
  card_id?: string;
  verdict: string | null;
  decoded: { index: number; program: string; instruction: string; allowed: boolean | null }[] | null;
  lookup_tables: { tables: number; accounts_resolved: number } | null;
  simulation: { source: string; scope: string; success: boolean; error?: string | null;
    balances: { asset: string; unit: string; before: number | null; after: number | null; delta: number | null }[] } | null;
  structured_json: string | null;
  structured_status: string;
  rules: { name: string; result: "pass" | "block" }[];
  jev: { not_asked?: string; question?: { instructions?: string; criteria?: { true: string; false: string } };
    risk?: number | null; threshold?: number; status?: string };
  errors?: { stage: string; error: string }[];
  replaced?: boolean;
  card_status?: string;
};
const api = makeApi();
const finalLabel = (v: string | null) => ({ allow: "Passed", ask_human: "Ask you", block: "Blocked" })[v ?? ""] ?? "Unavailable";
const val = (v: number | null | undefined) => v == null ? "Unavailable" : v.toLocaleString("en-US");
const delta = (v: number | null) => v == null ? "Unavailable" : `${v > 0 ? "+" : ""}${val(v)}`;

/** Fetches a frozen capture only when expanded. GET cannot simulate, trade, research or call JEV.
 *  Keys and provenance checks prevent recycled codes/replacement cards displaying a different transaction's evidence. */
export function GuardDetails(props: { api?: Api; runId?: string | null; cardId?: string; code?: string }) {
  const [open, setOpen] = useState(false);
  const [data, setData] = useState<GuardView | null>(null);
  const [failed, setFailed] = useState(false);
  const key = `${props.runId ?? "legacy"}:${props.cardId ?? ""}:${props.code ?? ""}`;
  useEffect(() => { setOpen(false); setData(null); setFailed(false); }, [key]);
  useEffect(() => {
    if (!open || !props.runId) return;
    let active = true;
    setData(null); setFailed(false);
    const path = props.cardId && props.code
      ? `/cards/${encodeURIComponent(props.code)}/guard_view?card_id=${encodeURIComponent(props.cardId)}&guard_run_id=${encodeURIComponent(props.runId)}`
      : `/guard-runs/${encodeURIComponent(props.runId)}/guard_view`;
    (props.api ?? api).get(path).then((r) => {
      if (!active) return;
      if (r.guard_run_id !== props.runId || (props.cardId && r.card_id !== props.cardId)) { setFailed(true); return; }
      setData(r as GuardView);
    }).catch(() => { if (active) setFailed(true); });
    return () => { active = false; };
  }, [open, key, props.api]);
  const current = data?.guard_run_id === props.runId && (!props.cardId || data?.card_id === props.cardId) ? data : null;
  return <div className="guard-details">
    <button type="button" className="text-btn" onClick={() => setOpen(!open)} aria-expanded={open}>
      {open ? "Hide the checks" : "Show what the checks found"}
    </button>
    {open && <div className="guard-evidence">
      {!props.runId ? <p>Evidence unavailable for this card or event (legacy run or capture unavailable). Nothing was rerun.</p>
        : failed ? <p role="status">Evidence unavailable: the capture is missing, expired, or does not match this card. Nothing was rerun.</p>
        : !current ? <p role="status">Loading the recorded checks…</p>
        : <Evidence view={current} />}
    </div>}
  </div>;
}

export function Evidence({ view: v }: { view: GuardView }) {
  const risk = v.jev.risk;
  const validRisk = typeof risk === "number" && Number.isFinite(risk) && risk >= 0 && risk <= 1;
  return <>
    <p className="small soft">Recorded for this transaction, not rerun. {v.replaced ? "This card was replaced; this is its original capture." : ""}</p>
    <section className="guard-step">
      <h3>1. Transaction bytes</h3>
      <p>{v.tx_bytes_len == null ? "Length unavailable" : `${val(v.tx_bytes_len)} bytes`}</p>
      <p className="small soft">Base64 prefix · first 60 characters</p>
      <pre>{v.tx_base64_prefix ?? "Unavailable"}</pre>
    </section>
    <section className="guard-step">
      <h3>2. Decoded instructions</h3>
      {v.decoded == null ? <p>Decode unavailable.</p> : <ol className="guard-ix">{v.decoded.map((ix) => <li key={ix.index}>
        <span>{ix.program} · {ix.instruction}</span>
        <span className={ix.allowed === true ? "up" : ix.allowed === false ? "down" : "soft"}>
          {ix.allowed === true ? "Allowed" : ix.allowed === false ? "Not allowed" : "Not checked"}</span>
      </li>)}</ol>}
      <p className="small">{v.lookup_tables ? `Lookup tables: ${v.lookup_tables.accounts_resolved} accounts resolved (${v.lookup_tables.tables} tables)` : "Lookup tables: unavailable"}</p>
    </section>
    <section className="guard-step">
      <h3>3. Simulated balances</h3>
      {!v.simulation ? <p>Profile unavailable.</p> : <>
        <p className="small soft">surfnet_profileTransaction · profiled wallet accounts, not total portfolio</p>
        {v.simulation.error && <p className="down">Simulation failed; these are the recorded effects, not a completed trade.</p>}
        <dl className="guard-balances">{v.simulation.balances.map((b) => <div key={b.asset}>
          <dt>{b.asset} <span className="soft small">{b.unit}</span></dt>
          <dd className="num">{val(b.before)} to {val(b.after)}</dd>
          <dd className="small">Delta {delta(b.delta)}</dd>
        </div>)}</dl>
      </>}
    </section>
    <section className="guard-step">
      <h3>4. Structured view sent to JEV</h3>
      <p className="small soft">No addresses. No token names.</p>
      {v.structured_json != null && v.structured_status === "sent_to_jev"
        ? <pre aria-label="Exact JEV state JSON">{v.structured_json}</pre>
        : v.structured_status === "not_sent"
          ? <p>Not sent. {v.jev.not_asked === "blocked by rules" ? "Blocked by rules before JEV." : "No JEV state was sent in this run."}</p>
          : <p>Exact JEV state capture unavailable. No input was reconstructed.</p>}
    </section>
    <section className="guard-step">
      <h3>5. Rules actually checked</h3>
      {!v.rules.length ? <p>No rule results captured; not inferred as passed.</p> : <ul className="guard-rules">{v.rules.map((r, i) => <li key={i}>
        <span>{r.name}</span><span className={r.result === "block" ? "down" : "up"}>{r.result === "block" ? "Block" : "Pass"}</span>
      </li>)}</ul>}
    </section>
    <section className="guard-step">
      <h3>6. JEV and final verdict</h3>
      {v.jev.not_asked ? <p>Not asked: {v.jev.not_asked}</p> : <>
        <p>{v.jev.question?.instructions ?? "Question unavailable"}</p>
        {v.jev.question?.criteria && <p className="small soft">Yes: {v.jev.question.criteria.true}<br />No: {v.jev.question.criteria.false}</p>}
        <p>Risk: {validRisk ? risk.toFixed(3) : "Unavailable (no answer recorded)"} · threshold {v.jev.threshold ?? "unavailable"}</p>
        {validRisk && <div className="guard-risk" role="meter" aria-label="JEV risk" aria-valuemin={0} aria-valuemax={1} aria-valuenow={risk}>
          <span className="guard-risk-fill" style={{ width: `${risk * 100}%` }} /><i className="guard-risk-mark" style={{ left: `${(v.jev.threshold ?? 0.5) * 100}%` }} />
        </div>}
        {validRisk && <div className="guard-risk-labels small soft"><span>0</span><span>0.5</span><span>1</span></div>}
      </>}
      <p className={`guard-final ${v.verdict === "block" ? "down" : v.verdict === "allow" ? "up" : ""}`}>{finalLabel(v.verdict)}</p>
      {!!v.errors?.length && <p className="small soft">Some evidence unavailable: {v.errors.map((e) => `${e.stage} (${e.error})`).join(", ")}. Guard decisions were not changed by capture.</p>}
    </section>
  </>;
}
