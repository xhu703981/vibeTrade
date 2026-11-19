import { useEffect, useState, type ReactNode } from "react";

export type DrawerTab = { id: string; label: string; badge?: string | number | null; content: ReactNode };

/** "Details": everything that is not conversation (settings, coins table, cards, positions, activity, test tools).
 *  Tab contents stay mounted so state like a half-picked desk survives switching tabs. */
export function Drawer(props: { open: boolean; onClose: () => void; tabs: DrawerTab[]; tab: string; onTab: (id: string) => void; footer?: ReactNode }) {
  const [mounted, setMounted] = useState(props.open);
  useEffect(() => { if (props.open) setMounted(true); }, [props.open]);
  useEffect(() => {
    if (!props.open) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") props.onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [props.open, props.onClose]);
  return (
    <>
      <div className={`scrim ${props.open ? "on" : ""}`} onClick={props.onClose} aria-hidden />
      <aside className={`drawer ${props.open ? "open" : ""}`} aria-label="Details" aria-hidden={!props.open} inert={!props.open}>
        <header className="drawer-head">
          <h2>Details</h2>
          <button className="icon ghost" onClick={props.onClose} aria-label="Close details">
            <svg viewBox="0 0 24 24" width="18" height="18"><path d="M6 6l12 12M18 6L6 18" stroke="currentColor" strokeWidth="2" strokeLinecap="round" /></svg>
          </button>
        </header>
        <nav className="tabs" role="tablist">
          {props.tabs.map((t) => (
            <button key={t.id} role="tab" aria-selected={props.tab === t.id} className={props.tab === t.id ? "on" : ""} onClick={() => props.onTab(t.id)}>
              {t.label}{t.badge != null && t.badge !== "" && t.badge !== 0 ? <span className="badge">{t.badge}</span> : null}
            </button>
          ))}
        </nav>
        <div className="drawer-body">
          {mounted && props.tabs.map((t) => <div key={t.id} role="tabpanel" hidden={props.tab !== t.id}>{t.content}</div>)}
        </div>
        {props.footer && <footer className="drawer-foot">{props.footer}</footer>}
      </aside>
    </>
  );
}
