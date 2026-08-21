import { useEffect, useRef, useState } from "react";

export function StatusBadge({ status }: { status: string | undefined }) {
  const s = status ?? "starting";
  return <span className={`badge ${s}`}>{s}</span>;
}

export function TimeAgo({ ts }: { ts: string | null | undefined }) {
  if (!ts) return <span className="dim">—</span>;
  const secs = Math.max(0, (Date.now() - new Date(ts).getTime()) / 1000);
  let label: string;
  if (secs < 60) label = `${Math.round(secs)}s ago`;
  else if (secs < 3600) label = `${Math.round(secs / 60)}m ago`;
  else if (secs < 86400) label = `${Math.round(secs / 3600)}h ago`;
  else label = `${Math.round(secs / 86400)}d ago`;
  return <span title={new Date(ts).toLocaleString()}>{label}</span>;
}

/** Fetch on mount and refetch on an interval; pause when the tab is hidden. */
export function usePolling<T>(fn: () => Promise<T>, intervalMs = 5000): {
  data: T | undefined;
  error: string | undefined;
  reload: () => void;
} {
  const [data, setData] = useState<T>();
  const [error, setError] = useState<string>();
  const fnRef = useRef(fn);
  fnRef.current = fn;
  const [tick, setTick] = useState(0);

  useEffect(() => {
    let live = true;
    // The FIRST load always runs, even in a hidden tab: skipping it leaves data undefined, which
    // every page renders as its empty state — a background tab would claim you have no views.
    // Only the polling refreshes pause while hidden.
    const load = (force = false) => {
      if (document.hidden && !force) return;
      fnRef.current()
        .then((d) => { if (live) { setData(d); setError(undefined); } })
        .catch((e) => { if (live) setError(String(e.message ?? e)); });
    };
    load(true);
    const id = setInterval(() => load(), intervalMs);
    return () => { live = false; clearInterval(id); };
  }, [intervalMs, tick]);

  return { data, error, reload: () => setTick((t) => t + 1) };
}

/** A failed load, said out loud. The rule: a fetch that failed is NEVER rendered as an empty
 *  state — "no views" and "the daemon couldn't answer" are different facts, and telling the user
 *  the first when the second is true sends them off to fix nothing. Pair with usePolling's
 *  `error` (which keeps the last good `data`, so this sits above stale rows). */
export function ErrorState({ error, what, onRetry }: {
  error: string;
  what?: string;          // what failed to load, e.g. "views"; omit for the generic wording
  onRetry?: () => void;   // usually usePolling's reload
}) {
  return (
    <div className="alert error">
      <strong>{what ? `Couldn’t load ${what}` : "Couldn’t load this page"}</strong> · {error}
      {onRetry && (
        <button className="btn" style={{ marginLeft: 10 }} onClick={onRetry}>Retry</button>
      )}
    </div>
  );
}

/** "There is genuinely nothing here" — only ever rendered when the load SUCCEEDED. */
export function EmptyState({ children }: { children: React.ReactNode }) {
  return <div className="empty">{children}</div>;
}

/** Bytes as a human size, binary units ("2.4 GiB"). Null/undefined is *unknown*, not zero — the
 *  metering API returns null for numbers it genuinely cannot know (no configured limit, a stat()
 *  that failed), and rendering those as "0 B" would be a lie. */
export function formatBytes(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n)) return "—";
  if (n < 1024) return `${Math.round(n)} B`;
  const units = ["KiB", "MiB", "GiB", "TiB", "PiB"];
  let v = n / 1024;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i += 1; }
  return `${v >= 10 ? Math.round(v) : v.toFixed(1)} ${units[i]}`;
}

/** USD cost for display. Null/undefined is *unknown*, not free — historical runs and unpriced
 *  models have no cost, and "$0.00" would claim they were. Four decimals below a dollar: agent
 *  runs cost fractions of a cent, and two decimals would flatten them all to "$0.00". */
export function fmtCost(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v)) return "—";
  return v >= 1 ? `$${v.toFixed(2)}` : `$${v.toFixed(4)}`;
}

/** Token counts, compact ("12.4k", "1.2M"). Null/undefined renders as unknown. */
export function fmtTokens(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n)) return "—";
  if (n < 1000) return String(Math.round(n));
  if (n < 1_000_000) return `${(n / 1000).toFixed(n < 10_000 ? 1 : 0)}k`;
  return `${(n / 1_000_000).toFixed(1)}M`;
}

/** How a label rewrites its values, in as few characters as fit a table cell. "" when it passes
 *  values straight through. One implementation so the editor and the read-only tables can never
 *  describe the same rule differently. */
export function ruleSummary(pattern?: string, renames = 0): string {
  if (pattern && renames) return `regex +${renames}`;
  if (pattern) return "regex";
  if (renames) return `${renames} rename${renames === 1 ? "" : "s"}`;
  return "";
}

/** Pick-one dropdown wearing the same menu as `Combo` — replaces native <select>, whose OPEN menu
 *  is drawn by the OS and cannot be themed at all (restyling the closed box only makes the mismatch
 *  more obvious the moment you click it). Unlike `Combo` there is no free text: the value is always
 *  one of `options`.
 *
 *  Keeps what a native select gives you for free: full keyboard control, and `disabled`. */
export function Picker({ value, onChange, options, labels, className, style, disabled, title, ariaLabel }: {
  value: string; onChange: (v: string) => void; options: string[];
  labels?: Record<string, string>;   // display text per option (defaults to the option itself)
  className?: string; style?: React.CSSProperties;
  disabled?: boolean; title?: string; ariaLabel?: string;
}) {
  const [open, setOpen] = useState(false);
  const [hi, setHi] = useState(Math.max(0, options.indexOf(value)));
  const text = (o: string) => labels?.[o] ?? o;

  const pick = (o: string) => { onChange(o); setOpen(false); };

  return (
    <div className={"combo picker" + (className ? ` ${className}` : "")} style={style}>
      <button type="button" className="picker-btn" disabled={disabled} title={title}
              aria-label={ariaLabel} aria-haspopup="listbox" aria-expanded={open}
              onClick={() => { setHi(Math.max(0, options.indexOf(value))); setOpen((o) => !o); }}
              onBlur={() => setOpen(false)}
              onKeyDown={(e) => {
                if (e.key === "Escape") { setOpen(false); return; }
                if (!open) {
                  // Enter/Space/arrows open it, matching a native select.
                  if (["Enter", " ", "ArrowDown", "ArrowUp"].includes(e.key)) {
                    e.preventDefault(); setHi(Math.max(0, options.indexOf(value))); setOpen(true);
                  }
                  return;
                }
                if (e.key === "ArrowDown") { e.preventDefault(); setHi((h) => Math.min(h + 1, options.length - 1)); }
                else if (e.key === "ArrowUp") { e.preventDefault(); setHi((h) => Math.max(h - 1, 0)); }
                else if (e.key === "Enter" || e.key === " ") { e.preventDefault(); pick(options[hi]); }
              }}>
        <span className="picker-value">{text(value)}</span>
        <svg className="picker-caret" width="10" height="7" viewBox="0 0 10 7" fill="none" aria-hidden="true">
          <path d="M1 1l4 4 4-4" stroke="currentColor" strokeWidth="1.5" />
        </svg>
      </button>
      {open && (
        <div className="combo-list" role="listbox">
          {options.map((o, i) => (
            <div key={o} role="option" aria-selected={o === value}
                 className={"combo-item" + (i === hi ? " active" : "")}
                 onMouseDown={(e) => { e.preventDefault(); pick(o); }}
                 onMouseEnter={() => setHi(i)}>
              {text(o)}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

/** Input with styled suggestions — replaces native <datalist> (which can't be themed).
 *  Free text stays allowed; suggestions filter as you type. */
export function Combo({ value, onChange, options, placeholder, style, className, hints, hintClass }: {
  value: string; onChange: (v: string) => void; options: string[];
  placeholder?: string; style?: React.CSSProperties; className?: string;
  hints?: Record<string, string>;   // per-option annotation, right-aligned (e.g. coverage, a type tag)
  hintClass?: string;               // className for the annotation (default "dim"; "chip" for a tag)
}) {
  const [open, setOpen] = useState(false);
  const [hi, setHi] = useState(0);
  const needle = value.trim().toLowerCase();
  const shown = options.filter((o) => !needle || o.toLowerCase().includes(needle));

  const pick = (o: string) => { onChange(o); setOpen(false); };

  return (
    <div className={"combo" + (className ? ` ${className}` : "")} style={style}>
      <input type="text" value={value} placeholder={placeholder} autoComplete="off" data-1p-ignore data-lpignore="true"
             onChange={(e) => { onChange(e.target.value); setOpen(true); setHi(0); }}
             onFocus={() => setOpen(true)}
             onBlur={() => setOpen(false)}
             onKeyDown={(e) => {
               if (!open || !shown.length) return;
               if (e.key === "ArrowDown") { e.preventDefault(); setHi((h) => Math.min(h + 1, shown.length - 1)); }
               else if (e.key === "ArrowUp") { e.preventDefault(); setHi((h) => Math.max(h - 1, 0)); }
               else if (e.key === "Enter") { e.preventDefault(); pick(shown[hi]); }
               else if (e.key === "Escape") setOpen(false);
             }} />
      {open && shown.length > 0 && (
        <div className="combo-list">
          {shown.map((o, i) => (
            <div key={o} className={"combo-item" + (i === hi ? " active" : "")}
                 style={hints ? { display: "flex", justifyContent: "space-between", gap: 12 } : undefined}
                 onMouseDown={(e) => { e.preventDefault(); pick(o); }}
                 onMouseEnter={() => setHi(i)}>
              <span>{o}</span>
              {hints?.[o] && <span className={hintClass ?? "dim"}>{hints[o]}</span>}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
