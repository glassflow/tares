import * as React from "react";
import { Link, useSearchParams } from "react-router-dom";

import { ApiError } from "../../api";
import { Banner, Button, Skeleton } from "../../components/edition";
import { Check, ChevronDown } from "../../components/icons";
import { Input } from "../../components/ui/input";
import { cn } from "../../lib/utils";
import { usePagePolling } from "../usePagePolling";
import { DocPane } from "./FactoryDocs";
import { PageHead, ProjectShell } from "./ProjectShell";
import { projectDocs, type ProjectDocLink, type ProjectDocsPage } from "./FactoryDocs.api";

// /projects/:id/docs, Docs and memory in the project's frame (TR-1245): every document of one project.
// A picker above the document holds them in three groups (the spec, plan, rules and memory; the
// working notes, one per ticket; the notes) with a search box, so the document has the page's width,
// read as the Docs and memory page reads it. ?doc=<id> opens one. The shared rules and memory stay
// on Docs and memory.

const POLL_MS = 60_000;
const LINK = "text-[color:var(--accent-deep)]! underline!";

/** The open document, and a panel to pick another: a search box over every document of the project,
 *  in its groups (TR-1245). Typing matches a ref or words of the title; arrow keys move, Enter
 *  opens, Escape closes. Drawn as the Docs and memory page's project picker is. */
function DocPicker({ groups, openId, onPick }: {
  groups: ProjectDocsPage["groups"]; openId: string | null; onPick: (id: string) => void;
}) {
  const [open, setOpen] = React.useState(false);
  const [q, setQ] = React.useState("");
  const [at, setAt] = React.useState(0);
  const box = React.useRef<HTMLDivElement>(null);
  const button = React.useRef<HTMLButtonElement>(null);
  const listId = React.useId();
  const needle = q.trim().toLowerCase();
  const match = (d: ProjectDocLink) => !needle || `${d.ticket ?? ""} ${d.title}`.toLowerCase().includes(needle);
  const shownGroups = groups.map((g) => ({ ...g, docs: g.docs.filter(match) })).filter((g) => g.docs.length > 0);
  const flat = shownGroups.flatMap((g) => g.docs);
  const chosen = groups.flatMap((g) => g.docs).find((d) => d.id === openId);
  const chosenGroup = groups.find((g) => g.docs.some((d) => d.id === openId));
  React.useEffect(() => { setAt(0); }, [q, open]);
  React.useEffect(() => {
    if (!open) return;
    const away = (e: MouseEvent) => { if (box.current && !box.current.contains(e.target as Node)) setOpen(false); };
    document.addEventListener("mousedown", away);
    return () => document.removeEventListener("mousedown", away);
  }, [open]);
  const close = () => { setOpen(false); setQ(""); button.current?.focus(); };
  const choose = (id: string) => { close(); onPick(id); };
  const key = (e: React.KeyboardEvent) => {
    if (e.key === "ArrowDown") { e.preventDefault(); setAt((i) => Math.min(i + 1, flat.length - 1)); }
    else if (e.key === "ArrowUp") { e.preventDefault(); setAt((i) => Math.max(i - 1, 0)); }
    else if (e.key === "Enter") { e.preventDefault(); if (flat[at]) choose(flat[at].id); }
    else if (e.key === "Escape") { e.preventDefault(); close(); }
  };
  const total = groups.reduce((n, g) => n + g.docs.length, 0);
  return (
    <div ref={box} className="relative w-full max-w-[560px]" data-testid="project-doc-picker">
      <button ref={button} type="button" aria-haspopup="listbox" aria-expanded={open} data-testid="project-doc-picker-button"
              onClick={() => setOpen((o) => !o)}
              className="flex! h-10 w-full items-center justify-between gap-3 border! border-input! bg-card! px-3! text-left text-sm! font-normal! text-foreground! hover:border-foreground!">
        <span className="flex min-w-0 items-baseline gap-2">
          {chosenGroup && <span className="shrink-0 text-xs text-muted-foreground">{chosenGroup.title}</span>}
          {chosen?.ticket && <span className="shrink-0 font-mono text-xs font-medium">{chosen.ticket}</span>}
          <span className="min-w-0 truncate font-semibold" data-user-text>{chosen ? chosen.title : "Choose a document"}</span>
        </span>
        <span className="flex shrink-0 items-center gap-2 text-xs text-muted-foreground">
          {total} {total === 1 ? "document" : "documents"}
          <ChevronDown aria-hidden className="size-4 opacity-60" />
        </span>
      </button>
      {open && (
        <div className="absolute inset-x-0 top-11 z-30 border border-border bg-popover shadow-[0_6px_18px_var(--sheet-shadow)]" data-testid="project-doc-picker-panel">
          <div className="border-b border-border p-2">
            <Input autoFocus value={q} onChange={(e) => setQ(e.target.value)} onKeyDown={key} placeholder="Search by ref or title"
                   role="combobox" aria-expanded aria-controls={listId} aria-label="Search documents" aria-autocomplete="list"
                   aria-activedescendant={flat[at] ? `${listId}-${flat[at].id}` : undefined}
                   className="h-8 bg-[var(--input-bg)]!" data-testid="project-doc-search" />
          </div>
          <ul id={listId} role="listbox" aria-label="Documents" className="m-0! max-h-[min(420px,60vh)] list-none! overflow-y-auto p-1!">
            {flat.length === 0 && <li className="px-2.5 py-2 text-[13px] text-muted-foreground" data-testid="project-doc-none">No document matches.</li>}
            {shownGroups.map((g) => (
              <React.Fragment key={g.key}>
                <li role="presentation" className="flex items-baseline justify-between px-2.5 pb-1 pt-2 text-xs font-semibold text-muted-foreground" data-testid="project-doc-group" data-group={g.key}>
                  <span>{g.title}</span>{g.docs.length > 1 && <span className="font-normal">{g.docs.length}</span>}
                </li>
                {g.docs.map((d) => {
                  const i = flat.indexOf(d);
                  const on = d.id === openId;
                  return (
                    <li key={d.id} id={`${listId}-${d.id}`} role="option" aria-selected={on} data-testid="project-doc-link"
                        onMouseDown={(e) => e.preventDefault()} onClick={() => choose(d.id)} onMouseEnter={() => setAt(i)}
                        className={cn("flex cursor-pointer items-baseline gap-2 px-2.5 py-1.5 text-sm", i === at && "bg-[var(--row-hover)]", on && "font-semibold")}>
                      {d.ticket && <span className="shrink-0 font-mono text-xs font-medium">{d.ticket}</span>}
                      <span className="min-w-0 flex-1 [overflow-wrap:anywhere]" data-user-text>{d.title}</span>
                      {on && <Check aria-hidden className="size-4 shrink-0 self-center" />}
                    </li>
                  );
                })}
              </React.Fragment>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

function ProjectDocs({ id }: { id: string }) {
  const [params, setParams] = useSearchParams();
  const docId = params.get("doc");
  const seen = React.useRef(docId);
  const { data, error, reload } = usePagePolling<ProjectDocsPage>(async () => {
    try {
      return await projectDocs(id, docId);
    } catch (e) {
      // a document that is not this project's (an old link): the first one opens instead
      if (!(docId && e instanceof ApiError && e.status === 404)) throw e;
      seen.current = null;
      setParams((q) => { const n = new URLSearchParams(q); n.delete("doc"); return n; }, { replace: true });
      return projectDocs(id);
    }
  }, POLL_MS);
  React.useEffect(() => {
    if (seen.current === docId) return;
    seen.current = docId;
    reload();
  }, [docId]); // eslint-disable-line react-hooks/exhaustive-deps

  const doc = data?.doc ?? null;
  const openId = docId ?? doc?.id ?? null;
  const stale = Boolean(docId) && doc?.id !== docId && !error;
  const all = data ? data.groups.flatMap((g) => g.docs) : [];
  const open = all.find((d) => d.id === doc?.id);
  return (
    <div className="grid min-w-0 gap-6" data-testid="project-docs-page">
      <PageHead title="Docs and memory" sub={
        <span className="text-xs">
          {data ? <>Every document of <span data-user-text>{data.project.name}</span>. The rules and memory for every project are on <Link to="/docs" className={LINK}>Docs and memory</Link>.</> : " "}
        </span>} />
      {error && !data && (
        <Banner tone="error" action={<Button variant="secondary" onClick={reload}>Retry</Button>}>
          <b>The documents did not load.</b> <span className="text-foreground">Check your connection, then try again.</span>
        </Banner>
      )}
      {!data && !error && <Skeleton className="h-60" />}
      {data && data.total === 0 && (
        <div className="border border-border bg-card px-4 py-7 text-muted-foreground" data-testid="project-docs-empty">
          No documents yet. The spec and plan show here once they are written.
        </div>
      )}
      {data && data.total > 0 && (
        <>
          <DocPicker groups={data.groups} openId={openId}
                     onPick={(id) => setParams((q) => { const n = new URLSearchParams(q); n.set("doc", id); return n; })} />
          {/* the document has the page's width, as a reading column of at most 880px centred in its card */}
          <div className="grid min-w-0 gap-2 [&_[data-testid=doc-pane]>*]:px-[max(20px,calc((100%-880px)/2))]!">
            {open?.ticket_href && (
              <div className="text-[13px]" data-testid="project-doc-ticket">
                <Link to={open.ticket_href} className={LINK}>Open ticket {open.ticket}</Link>
              </div>
            )}
            {doc && <DocPane key={doc.id} doc={doc} sample={null} stale={stale} reload={reload} />}
          </div>
        </>
      )}
    </div>
  );
}

export default function FactoryProjectDocs() {
  return <ProjectShell page="docs">{({ id }) => <ProjectDocs id={id} />}</ProjectShell>;
}
