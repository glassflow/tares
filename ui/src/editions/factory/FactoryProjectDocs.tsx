import * as React from "react";
import { Link, useSearchParams } from "react-router-dom";

import { ApiError } from "../../api";
import { Banner, Button, Skeleton } from "../../components/edition";
import { cn } from "../../lib/utils";
import { usePagePolling } from "../usePagePolling";
import { DocPane } from "./FactoryDocs";
import { PageHead, ProjectShell } from "./ProjectShell";
import { projectDocs, type ProjectDocLink, type ProjectDocsPage } from "./FactoryDocs.api";

// /projects/:id/docs, Docs and memory in the project's frame (TR-1245): every document of one project. The list on the left in
// three groups (the spec, plan, rules and memory; the working notes, one per ticket; the notes),
// the open document on the right, read as the Docs and memory page reads it. ?doc=<id> opens one.
// The shared rules and memory stay on Docs and memory.

const POLL_MS = 60_000;
const LINK = "text-[color:var(--accent-deep)]! underline!";

function Item({ d, open, to }: { d: ProjectDocLink; open: boolean; to: string }) {
  return (
    <Link to={to} data-testid="project-doc-link" aria-current={open ? "page" : undefined}
          className={cn("flex min-w-0 items-baseline gap-2 border-l-[3px] border-transparent px-3.5 py-[7px] text-sm text-foreground! no-underline! hover:bg-[var(--row-hover)]",
                        open && "border-[color:var(--accent)] bg-muted font-semibold")}>
      {d.ticket && <span className="shrink-0 font-mono text-xs font-medium">{d.ticket}</span>}
      <span className="min-w-0 truncate" title={d.title} data-user-text>{d.title}</span>
    </Link>
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

  const to = (doc: string) => `?doc=${encodeURIComponent(doc)}`;
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
          <select aria-label="Document" value={openId ?? ""} data-testid="project-doc-select"
                  onChange={(e) => setParams((q) => { const n = new URLSearchParams(q); n.set("doc", e.target.value); return n; })}
                  className="block h-10 w-full min-w-0 border! border-border! bg-background! px-2.5! text-sm! text-foreground! md:hidden!">
            {data.groups.map((g) => (
              <optgroup key={g.key} label={g.title}>
                {g.docs.map((d) => <option key={d.id} value={d.id} data-user-text>{d.ticket ? `${d.ticket} ${d.title}` : d.title}</option>)}
              </optgroup>
            ))}
          </select>
          <div className="grid items-start gap-5 md:grid-cols-[300px_minmax(0,1fr)]">
            <nav aria-label="Documents" className="hidden border border-border bg-card pb-1 md:block" data-testid="project-doc-list">
              {data.groups.map((g, i) => (
                <div key={g.key} className={cn(i > 0 && "border-t border-border")} data-testid="project-doc-group" data-group={g.key}>
                  <div className="flex items-baseline justify-between px-3.5 pb-1 pt-3 text-xs font-semibold text-muted-foreground">
                    <span>{g.title}</span>{g.docs.length > 1 && g.key !== "project" && <span className="font-normal">{g.docs.length}</span>}
                  </div>
                  <div className={cn(g.key === "working" && g.docs.length > 12 && "max-h-[420px] overflow-y-auto")}>
                    {g.docs.map((d) => <Item key={d.id} d={d} open={d.id === openId} to={to(d.id)} />)}
                  </div>
                </div>
              ))}
            </nav>
            <div className="grid min-w-0 gap-2">
              {open?.ticket_href && (
                <div className="text-[13px]" data-testid="project-doc-ticket">
                  <Link to={open.ticket_href} className={LINK}>Open ticket {open.ticket}</Link>
                </div>
              )}
              {doc && <DocPane key={doc.id} doc={doc} sample={null} stale={stale} reload={reload} />}
            </div>
          </div>
        </>
      )}
    </div>
  );
}

export default function FactoryProjectDocs() {
  return <ProjectShell page="docs">{({ id }) => <ProjectDocs id={id} />}</ProjectShell>;
}
