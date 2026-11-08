import { useState } from "react";

/** A section that folds, with a pin: a pinned one is open on every visit and does not fold.
 *  Pins are this browser's, per section, across projects. */
export default function Fold({ id, title, icon, meta, pinnedByDefault, children }: {
  id: string; title: string; icon?: React.ReactNode; meta: React.ReactNode; pinnedByDefault?: boolean;
  children: React.ReactNode;
}) {
  const key = `tares.factory.pin.${id}`;
  const [pinned, setPinned] = useState<boolean>(() => {
    try {
      const v = localStorage.getItem(key);
      return v === null ? !!pinnedByDefault : v === "1";
    } catch { return !!pinnedByDefault; }
  });
  const [open, setOpen] = useState(pinned);
  const togglePin = (e: React.MouseEvent) => {
    e.preventDefault(); e.stopPropagation();
    const next = !pinned;
    setPinned(next);
    if (next) setOpen(true);
    try { localStorage.setItem(key, next ? "1" : "0"); } catch { /* the pin lasts this visit */ }
  };
  return (
    <details className={`fo-panel fo-fold${pinned ? " is-pinned" : ""}`} open={pinned || open}
             onToggle={(e) => {
               // only its own: a fold inside (the merged pull requests) toggling is not this one
               if (e.target !== e.currentTarget || pinned) return;
               setOpen((e.target as HTMLDetailsElement).open);
             }}>
      <summary onClick={(e) => { if (pinned) e.preventDefault(); }}>
        <span className="fo-fold-h">{icon}{title}</span>
        <span className="fo-fold-right">
          <span className="fo-meta">{meta}</span>
          <button type="button" className={`fo-pin${pinned ? " on" : ""}`} onClick={togglePin}
                  aria-pressed={pinned} aria-label={pinned ? `Unpin ${title}` : `Pin ${title} open`}
                  title={pinned ? "Pinned open: click to let it fold" : "Pin open: stays open on every visit"}>
            <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">
              <path d="M9.5 1.5l5 5-2 .5-2.5 2.5.5 3-1.5 1.5-3-3L2.5 14.5 1.5 13.5 5 10 2 7l1.5-1.5 3 .5L9 3.5z"
                    fill={pinned ? "currentColor" : "none"} stroke="currentColor" strokeWidth="1.2" strokeLinejoin="round" />
            </svg>
          </button>
        </span>
      </summary>
      {children}
    </details>
  );
}
