import * as React from "react";
import { Check, ChevronDown } from "@/components/icons";

import { cn } from "@/lib/utils";
import { Button } from "@/components/edition/Button";
import { ERROR_BORDER, StackedField, useFieldIds, type FieldProps } from "@/components/edition/Field";
import { Input as BaseInput } from "@paper-kit/ui/input";
import type { SelectOption } from "@/components/edition/Select";

export interface SearchSelectProps extends FieldProps {
  options: SelectOption[];
  value: string;
  onValueChange: (value: string) => void;
  /** The words in the search box before anything is typed: "Search projects". */
  searchLabel: string;
  /** The line when nothing matches: "No project matches." */
  noMatch: string;
  disabled?: boolean;
  id?: string;
  className?: string;
  /** Lands on the button that opens the list; the search box gets `<id>-search`, each row `<id>-option`. */
  "data-testid"?: string;
}

/** A Select for a long list: the list opens with a search box on top, and typing keeps the rows whose
 *  label holds the words (any case). Arrow keys move, Enter picks, Escape closes. The first option is
 *  usually "All ..." and is always shown, so the filter can be taken off from any search. */
export function SearchSelect({
  label, hint, error, options, value, onValueChange, searchLabel, noMatch, disabled, id, className, "data-testid": testId,
}: SearchSelectProps) {
  const ids = useFieldIds(id);
  const listId = React.useId();
  const [open, setOpen] = React.useState(false);
  const [q, setQ] = React.useState("");
  const [at, setAt] = React.useState(0);
  const box = React.useRef<HTMLDivElement>(null);
  const button = React.useRef<HTMLButtonElement>(null);
  const list = React.useRef<HTMLUListElement>(null);
  const search = React.useRef<HTMLInputElement>(null);

  const needle = q.trim().toLowerCase();
  const keepFirst = options.length > 1;
  const shown = needle ? options.filter((o, i) => (i === 0 && keepFirst) || o.label.toLowerCase().includes(needle)) : options;
  const matches = needle && keepFirst ? shown.length - 1 : shown.length;
  const chosen = options.find((o) => o.value === value);
  const valueId = `${ids.id}-value`;

  // The row Enter picks: on open the one chosen now; while searching the first match, not the "All ..." row
  // kept on top. Set in the same step as the typing or the opening, so a quick Enter never sees an old row.
  const startAt = (text: string) => {
    const n = text.trim().toLowerCase();
    if (!n) return Math.max(0, options.findIndex((o) => o.value === value));
    const hits = options.filter((o, i) => i > 0 || !keepFirst ? o.label.toLowerCase().includes(n) : false).length;
    return keepFirst && hits > 0 ? 1 : 0;
  };
  const openList = () => { setQ(""); setAt(startAt("")); setOpen(true); };
  const type = (text: string) => { setQ(text); setAt(startAt(text)); };
  React.useEffect(() => {
    if (!open) return;
    const away = (e: MouseEvent) => { if (box.current && !box.current.contains(e.target as Node)) { setOpen(false); setQ(""); } };
    document.addEventListener("mousedown", away);
    return () => document.removeEventListener("mousedown", away);
  }, [open]);
  React.useEffect(() => {
    list.current?.querySelector(`[data-at="${at}"]`)?.scrollIntoView({ block: "nearest" });
  }, [at]);

  const close = (focusButton = true) => { setOpen(false); setQ(""); if (focusButton) button.current?.focus(); };
  const choose = (v: string) => { close(); if (v !== value) onValueChange(v); };
  const key = (e: React.KeyboardEvent) => {
    if (e.key === "ArrowDown") { e.preventDefault(); setAt((i) => Math.min(i + 1, shown.length - 1)); }
    else if (e.key === "ArrowUp") { e.preventDefault(); setAt((i) => Math.max(i - 1, 0)); }
    else if (e.key === "Enter") { e.preventDefault(); if (shown[at]) choose(shown[at].value); }
    else if (e.key === "Escape") { e.preventDefault(); close(); }
    else if (e.key === "Tab") close(false);
  };
  // focus leaving the whole control (Tab on, a click elsewhere) closes the list
  const leave = (e: React.FocusEvent) => {
    if (open && !box.current?.contains(e.relatedTarget as Node | null)) close(false);
  };

  return (
    <StackedField ids={ids} label={label} hint={hint} error={error}>
      <div ref={box} className={cn("relative min-w-0", className)} onBlur={leave}>
        <Button ref={button} id={ids.id} variant="secondary" disabled={disabled} aria-haspopup="listbox" aria-expanded={open}
                aria-labelledby={`${ids.labelId} ${valueId}`} aria-controls={open ? listId : undefined}
                aria-describedby={[hint && ids.hintId, error && ids.errorId].filter(Boolean).join(" ") || undefined}
                aria-invalid={error ? true : undefined}
                onClick={() => (open ? close() : openList())}
                onKeyDown={(e) => { if (e.key === "ArrowDown" && !open) { e.preventDefault(); openList(); } }}
                className={cn("h-9 w-full justify-between gap-2 border-input bg-background px-3 text-left text-sm font-normal text-foreground hover:border-input hover:bg-background",
                              error && ERROR_BORDER)}
                data-testid={testId}>
          <span id={valueId} className="min-w-0 truncate">{chosen ? chosen.label : ""}</span>
          <ChevronDown aria-hidden className="size-4 shrink-0 opacity-60" />
        </Button>
        {open && (
          // a press anywhere in the panel but the search box keeps focus in the box, so the keys still work
          <div className="motion-pop absolute inset-x-0 top-10 z-30 border border-border bg-popover shadow-[0_6px_18px_var(--sheet-shadow)]"
               onMouseDown={(e) => { if (e.target !== search.current) e.preventDefault(); }} onKeyDown={key}
               data-testid={testId && `${testId}-panel`}>
            <div className="border-b border-border p-2">
              <BaseInput ref={search} aria-label={searchLabel} autoFocus value={q} onChange={(e) => type(e.target.value)} placeholder={searchLabel}
                     role="combobox" aria-expanded aria-controls={listId} aria-autocomplete="list"
                     aria-activedescendant={shown[at] ? `${listId}-${at}` : undefined}
                     className="h-8 bg-field text-sm" data-testid={testId && `${testId}-search`} />
            </div>
            <ul ref={list} id={listId} role="listbox" aria-label={label} className="m-0 max-h-72 list-none overflow-y-auto p-1">
              {shown.map((o, i) => (
                <li key={o.value} id={`${listId}-${i}`} role="option" aria-selected={o.value === value} data-at={i}
                    data-testid={testId && `${testId}-option`} data-value={o.value}
                    onMouseDown={(e) => e.preventDefault()} onClick={() => choose(o.value)} onMouseMove={() => { if (at !== i) setAt(i); }}
                    className={cn("flex cursor-pointer items-center justify-between gap-2 px-2.5 py-1.5 text-sm",
                                  i === at && "bg-row-hover", o.value === value && "font-semibold")}>
                  <span className="min-w-0 [overflow-wrap:anywhere]">{o.label}</span>
                  {o.value === value && <Check aria-hidden className="size-4 shrink-0" />}
                </li>
              ))}
            </ul>
            <p role="status" className={cn("m-0 px-3.5 text-[13px] text-muted-foreground", matches === 0 ? "pb-2" : "sr-only")} data-testid={testId && matches === 0 ? `${testId}-none` : undefined}>
              {matches === 0 ? noMatch : ""}
            </p>
          </div>
        )}
      </div>
    </StackedField>
  );
}
