import { useEffect, useRef, useState, type ReactNode } from "react";

/** Contextual drawer: from the bottom on phones (drag the handle down to close, tap it to fold the sheet to its title),
 *  flush against the right edge on wide screens. Esc closes it too. */
export function Sheet(props: { title: ReactNode; label?: string; tone?: "bad" | "warn" | "ok"; onClose: () => void; children: ReactNode; sheetKey: string }) {
  const [drag, setDrag] = useState(0);
  const [collapsed, setCollapsed] = useState(false);
  const moved = useRef(0);
  const start = useRef<number | null>(null);
  const close = useRef(props.onClose);
  close.current = props.onClose;
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") close.current(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);
  const onDown = (e: React.PointerEvent) => { start.current = e.clientY; moved.current = 0; (e.target as HTMLElement).setPointerCapture?.(e.pointerId); };
  const onMove = (e: React.PointerEvent) => {
    if (start.current == null) return;
    moved.current = Math.max(moved.current, Math.abs(e.clientY - start.current));
    setDrag(Math.max(0, e.clientY - start.current));
  };
  const onUp = () => { if (start.current == null) return; start.current = null; if (drag > 90) close.current(); setDrag(0); };
  // a tap (or Enter / Space) on the handle folds the sheet down to its title, so the whole conversation is readable
  const onToggle = () => { if (moved.current < 6) setCollapsed((c) => !c); moved.current = 0; };
  return (
    <section className={`sheet ${props.tone ? `tone-${props.tone}` : ""} ${collapsed ? "collapsed" : ""}`} role="dialog"
             aria-label={props.label ?? (typeof props.title === "string" ? props.title : "Details")}
             key={props.sheetKey} style={drag ? { transform: `translateY(${drag}px)`, transition: "none" } : undefined}>
      <button type="button" className="sheet-grab" onPointerDown={onDown} onPointerMove={onMove} onPointerUp={onUp} onPointerCancel={onUp}
              onClick={onToggle} aria-expanded={!collapsed} aria-label={collapsed ? "Show details" : "Fold down to the title"}>
        <span />
      </button>
      <header className="sheet-head">
        <h2>{props.title}</h2>
        <button className="icon" onClick={props.onClose} aria-label="Close">
          <svg viewBox="0 0 24 24" width="18" height="18" aria-hidden><path d="M6 6l12 12M18 6L6 18" stroke="currentColor" strokeWidth="2" strokeLinecap="round" /></svg>
        </button>
      </header>
      <div className="sheet-body" hidden={collapsed}>{props.children}</div>
    </section>
  );
}
