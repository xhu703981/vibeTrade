import { useState } from "react";
import type { AppState } from "../types";
import { useNow } from "../useBackend";
import { usdF } from "./format";

export function TopupPanel(props: {
  topup: NonNullable<AppState["topup"]>;
  buttonsEnabled: boolean;
  onApprove: (usd: number) => void;
  onReject: () => void;
}) {
  const t = props.topup;
  const now = useNow();
  const [amt, setAmt] = useState(String(t.suggest_usdc));
  const left = Math.max(0, Math.round(t.expires_at - now));
  return (
    <section className="panel topup" role="alertdialog" aria-label="Research top-up">
      <h3>Research budget used up</h3>
      <p className="awaiting">Spent {usdF(t.spent_usdc)} of {usdF(t.budget_usdc)}; the next step needs {usdF(t.needed_usdc)}.</p>
      <p className="say">Say “add {t.suggest_usdc} dollars” or “don't add”. With no answer in {left} s, nothing is added.</p>
      <div className="row">
      <label className="amt-in">$<input aria-label="Top-up amount" value={amt} onChange={(e) => setAmt(e.target.value)} size={5} /></label>
      <button className="ghost-btn" onClick={props.onReject}>Don't add</button>
      <button className="primary" disabled={!props.buttonsEnabled || !(Number(amt) > 0)} onClick={() => props.onApprove(Number(amt))}>
        Add
      </button>
      </div>
    </section>
  );
}
