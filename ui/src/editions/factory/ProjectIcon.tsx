import * as React from "react";

import { Input } from "../../components/ui/input";
import { cn } from "../../lib/utils";
import { ALL_EMOJI, searchEmoji } from "./emojiData";

// A project's icon, the way Notion page icons work: one native emoji, shown beside the project's
// name and never stored in it. `InlineIcon` is the small one every list uses; `ProjectIconHeader` is
// the heading's: the emoji large before the title and "Add icon" near it (quiet until the heading is
// hovered or the control has keyboard focus, always there on a touch screen or a phone). Either one
// opens a small picker; a change saves at once.

export function InlineIcon({ icon, className }: { icon?: string | null; className?: string }) {
  if (!icon) return null;
  return <span aria-hidden data-user-text data-testid="project-inline-icon" className={cn("mr-1.5 inline-block not-italic", className)}>{icon}</span>;
}

/** The inline icon for a waiting item, when its title is the project's name (or starts with it). */
export function ItemIcon({ project, title }: { project?: { name: string; icon?: string | null } | null; title: string }) {
  return project?.icon && title.startsWith(project.name) ? <InlineIcon icon={project.icon} /> : null;
}

const GHOST = "bg-transparent! border-0! p-0! shadow-none! font-normal! text-muted-foreground! hover:text-foreground! hover:bg-transparent!";

export function ProjectIconHeader({ icon, canEdit, onChange, className, children, shown = false }: {
  icon: string | null;
  canEdit: boolean;
  onChange: (icon: string | null) => void;
  className?: string;
  children: React.ReactNode;
  /** Show "Add icon" at all times, not only on hover (a settings row has no heading to hover). */
  shown?: boolean;
}) {
  const [open, setOpen] = React.useState(false);
  const [query, setQuery] = React.useState("");
  const trigger = React.useRef<HTMLButtonElement>(null);
  const panel = React.useRef<HTMLDivElement>(null);
  const search = React.useRef<HTMLInputElement>(null);
  const groups = React.useMemo(() => searchEmoji(query), [query]);

  const refocus = React.useRef(false);
  const close = React.useCallback((backToTrigger: boolean) => {
    setOpen(false);
    setQuery("");
    refocus.current = backToTrigger;
  }, []);
  // after a pick the trigger may have been replaced ("Add icon" became the emoji): focus it right
  // after the commit that draws it, before the browser paints, so focus is never on nothing
  React.useLayoutEffect(() => {
    if (!refocus.current) return;
    refocus.current = false;
    trigger.current?.focus();
  });
  const pick = (next: string | null) => { close(true); onChange(next); };

  React.useEffect(() => {
    if (!open) return;
    search.current?.focus();
    const away = (e: PointerEvent) => {
      const t = e.target as Node;
      if (!panel.current?.contains(t) && !trigger.current?.contains(t)) close(false);
    };
    document.addEventListener("pointerdown", away);
    return () => document.removeEventListener("pointerdown", away);
  }, [open, close]);

  const keys = (e: React.KeyboardEvent<HTMLDivElement>) => {
    if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); close(true); return; }
    if (!["ArrowRight", "ArrowLeft", "ArrowDown", "ArrowUp"].includes(e.key)) return;
    const buttons = [...(panel.current?.querySelectorAll<HTMLButtonElement>("[data-emoji]") ?? [])];
    if (e.target === search.current) {
      if (e.key === "ArrowDown" && buttons[0]) { e.preventDefault(); buttons[0].focus(); }
      return;
    }
    const at = buttons.indexOf(e.target as HTMLButtonElement);
    if (at < 0) return;
    e.preventDefault();
    const grid = buttons[at].parentElement as HTMLElement;
    const cols = Math.max(1, Math.round(grid.clientWidth / buttons[at].offsetWidth));
    const to = e.key === "ArrowRight" ? at + 1 : e.key === "ArrowLeft" ? at - 1 : e.key === "ArrowDown" ? at + cols : at - cols;
    if (to < 0) search.current?.focus();
    else buttons[Math.min(to, buttons.length - 1)]?.focus();
  };

  const toggle = () => { if (open) close(false); else setOpen(true); };
  const icons = groups.flatMap((g) => g.items);

  return (
    <div className={cn("group/icon relative", className)}
         onBlur={(e) => {
           const to = e.relatedTarget as Node | null;
           if (open && to && !panel.current?.contains(to) && to !== trigger.current) close(false);
         }}>
      {icon && canEdit && (
        <button type="button" ref={trigger} onClick={toggle} aria-label="Change icon" aria-haspopup="dialog" aria-expanded={open}
                data-user-text data-testid="project-icon"
                className="-ml-0.5 flex! size-11 shrink-0 items-center justify-center border-0! bg-transparent! p-0! text-[32px]! leading-none! shadow-none! hover:bg-[var(--row-hover)]! md:size-12 md:text-[38px]!">
          {icon}
        </button>
      )}
      {icon && !canEdit && (
        <span role="img" aria-label="Project icon" data-user-text data-testid="project-icon"
              className="shrink-0 text-[32px] leading-none md:text-[38px]">{icon}</span>
      )}
      {children}
      {!icon && canEdit && (
        <button type="button" ref={trigger} onClick={toggle} aria-haspopup="dialog" aria-expanded={open} data-testid="project-add-icon"
                className={cn(GHOST, "h-auto! text-[13px]!", shown ? "text-[color:var(--accent-deep)]! underline!" : "opacity-0 transition-opacity group-hover/icon:opacity-100 focus-visible:opacity-100 aria-expanded:opacity-100 max-md:opacity-100 [@media(hover:none)]:opacity-100")}>
          Add icon
        </button>
      )}
      {open && (
        <div ref={panel} role="dialog" aria-label="Choose an icon" data-testid="icon-picker" onKeyDown={keys}
             className="absolute left-0 top-full z-30 mt-2 w-[352px] max-w-[calc(100vw-32px)] border border-border bg-popover shadow-[0_6px_18px_var(--sheet-shadow)]">
          <div className="flex items-center gap-2 border-b border-border p-2">
            <Input ref={search} value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Search emoji" aria-label="Search emoji"
                   autoComplete="off" spellCheck={false}
                   onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); if (icons[0]) pick(icons[0].char); } }}
                   className="h-8 bg-[var(--input-bg)]!" data-testid="icon-search" />
            <button type="button" aria-label="Pick a random emoji" data-testid="icon-random"
                    onClick={() => pick(ALL_EMOJI[Math.floor(Math.random() * ALL_EMOJI.length)].char)}
                    className={cn(GHOST, "h-8 shrink-0 px-1.5! text-[13px]! hover:bg-[var(--row-hover)]!")}>
              Random
            </button>
            {icon && (
              <button type="button" onClick={() => pick(null)} data-testid="icon-remove"
                      className={cn(GHOST, "h-8 shrink-0 px-1.5! text-[13px]! hover:bg-[var(--row-hover)]!")}>
                Remove
              </button>
            )}
          </div>
          <div className="max-h-[280px] overflow-y-auto p-2" data-testid="icon-grid">
            {groups.length === 0 && <p className="m-0! px-1 py-2 text-[13px] text-muted-foreground" data-testid="icon-none">No emoji matches.</p>}
            {groups.map((g) => (
              <div key={g.label} role="group" aria-label={g.label} className="mb-2 last:mb-0">
                <div className="px-1 pb-1 text-xs font-semibold text-muted-foreground">{g.label}</div>
                <div className="grid grid-cols-[repeat(auto-fill,minmax(36px,1fr))]">
                  {g.items.map((x) => (
                    <button key={x.char} type="button" aria-label={x.name} title={x.name} data-emoji={x.char} data-user-text data-testid="icon-option"
                            onClick={() => pick(x.char)}
                            className="flex! h-9 items-center justify-center border-0! bg-transparent! p-0! text-[21px]! font-normal! leading-none! shadow-none! hover:bg-[var(--row-hover)]! focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-[var(--accent)]">
                      {x.char}
                    </button>
                  ))}
                </div>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
