import { GuardDetails } from "./GuardDetails";

/** A read-only exact-run link: no voice session, mandate mutation or engine required. */
export function GuardRunPage({ runId }: { runId: string }) {
  const valid = /^[A-Za-z0-9_-]{1,128}$/.test(runId);
  return <div className="vt">
    <header className="topbar"><span className="brand">Vibe</span><span className="spacer" /><a href="/">Back to desk</a></header>
    <main className="stage-v">
      <section aria-label="Recorded Guard check">
        <h2>Recorded Guard check</h2>
        <p className="small soft">Read-only evidence. Opening this page does not approve or rerun anything. A card timeout does not expire its recorded checks.</p>
        {valid ? <GuardDetails key={runId} runId={runId} expanded /> : <p role="status">Invalid Guard run ID. Nothing was requested.</p>}
      </section>
    </main>
  </div>;
}
