import * as React from "react";
import { Link, useSearchParams } from "react-router-dom";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";

import { ApiError } from "../../api";
import { Banner, Button, Skeleton } from "../../components/edition";
import { Input } from "../../components/ui/input";
import { cn } from "../../lib/utils";
import { usePagePolling } from "../usePagePolling";
import { useSample } from "../sample";
import {
  addDocLine, factoryDoc, factoryDocs,
  type DocProject, type DocRow, type DocView, type DocsPage,
} from "./FactoryDocs.api";
import { isSample, sampleBox, sampleLoad } from "./docsSamples";

// Docs and memory: the crew's shared rules and memory, and each project's spec, plan, rules and
// memory, read as documents. One read per page load (the list and the open document); picking a
// document reads again. A line is added through the route the document names. The document text is
// the people's and the crew's own (markdown, never raw HTML) and is exempt from the words rule.

const POLL_MS = 60_000;
const SHOWN_LINES = 100;   // a memory shows its newest lines; the rest is one click away
const SAME_LINE = "That line is already there.";
const NO_LINE = "Write the line first.";

// A row is one line of a document: no paragraph margins, no pictures (they would call out), links
// open in a new tab. Code and tables scroll inside their row instead of widening the page.
const MARKDOWN: Components = {
  p: ({ children }) => <>{children}</>,
  img: ({ alt }) => <>{alt}</>,
  a: ({ href, children }) => <a href={href} target="_blank" rel="noreferrer noopener" className="underline!">{children}</a>,
  code: ({ children }) => <code className="bg-muted px-[5px] py-px font-mono text-[12.5px]">{children}</code>,
  pre: ({ children }) => (
    <pre className="my-1! max-w-full overflow-x-auto border border-border bg-muted p-2.5 text-[12.5px] [&_code]:bg-transparent [&_code]:p-0">{children}</pre>
  ),
  table: ({ children }) => <div className="my-1 max-w-full overflow-x-auto"><table className="border-collapse text-[13px]">{children}</table></div>,
  th: ({ children }) => <th className="border border-border px-2 py-1 text-left">{children}</th>,
  td: ({ children }) => <td className="border border-border px-2 py-1">{children}</td>,
};

const ROW_TEXT: Record<DocRow["kind"], string> = {
  h1: "text-[18px] font-bold",
  h2: "mt-2.5 text-base font-bold",
  h3: "text-[15px] font-semibold",
  li: "",
  p: "",
};

function Row({ row }: { row: DocRow }) {
  const who = [row.by, row.when].filter(Boolean).join(", ");
  return (
    <div className="grid gap-x-4 gap-y-0.5 border-b border-dotted border-border py-[7px] last:border-b-0 md:grid-cols-[minmax(0,1fr)_190px]" data-testid="doc-row" data-kind={row.kind}>
      <div className={cn("min-w-0 [overflow-wrap:anywhere]", ROW_TEXT[row.kind])}>
        {row.kind === "li" && <span aria-hidden className="mr-2.5 inline-block size-[5px] bg-foreground align-middle" />}
        <ReactMarkdown remarkPlugins={[remarkGfm]} components={MARKDOWN}>{row.text}</ReactMarkdown>
      </div>
      <div data-testid="doc-row-by" className={cn("font-mono text-xs text-muted-foreground md:text-right", !who && "max-md:hidden")}>{who}</div>
    </div>
  );
}

function AddLine({ doc, sample, reload }: { doc: DocView; sample: string | null; reload: () => void }) {
  const start = sample ? sampleBox(sample) : { prefill: "", error: "" };
  const [text, setText] = React.useState(start.prefill);
  const [error, setError] = React.useState(start.error);
  const [busy, setBusy] = React.useState(false);
  const add = doc.add!;
  const inputId = React.useId();

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    const line = text.trim();
    if (!line) { setError(NO_LINE); return; }
    setBusy(true);
    setError("");
    try {
      // a sample has no cell to write to: it answers as the cell does for a line already there
      const { added } = sample
        ? { added: !doc.lines.some((r) => r.text === line) }
        : await addDocLine(add.path, line);
      if (added === false) setError(SAME_LINE);
      else { setText(""); reload(); }
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "The line was not added. Try again in a moment.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <form onSubmit={submit} className="border-t border-border bg-[var(--wash)] px-5 py-3.5" data-testid="doc-add">
      <label htmlFor={inputId} className="mb-1.5 block text-xs text-muted-foreground">{add.hint}</label>
      <div className="flex gap-2">
        <Input id={inputId} value={text} maxLength={500} placeholder="One short line" className="min-w-0 flex-1 bg-[var(--white)]!"
               aria-invalid={error ? true : undefined} aria-describedby={error ? `${inputId}-error` : undefined}
               onChange={(e) => { setText(e.target.value); if (error) setError(""); }} />
        <Button type="submit" busy={busy}>Add line</Button>
      </div>
      {error && <p id={`${inputId}-error`} role="alert" data-testid="doc-add-error" className="m-0! mt-1.5! text-[13px] text-[color:var(--err)]">{error}</p>}
    </form>
  );
}

function DocPane({ doc, sample, stale, reload }: { doc: DocView; sample: string | null; stale: boolean; reload: () => void }) {
  const [all, setAll] = React.useState(false);
  const cut = doc.kind === "memory" && !all && doc.lines.length > SHOWN_LINES ? doc.lines.length - SHOWN_LINES : 0;
  return (
    <section aria-label="Document" aria-busy={stale || undefined} data-testid="doc-pane"
             className={cn("min-w-0 border border-border bg-card", stale && "opacity-60")}>
      <div className="border-b border-border px-5 py-4">
        <h2 data-user-text className="m-0! text-xl!" data-testid="doc-title">{doc.title}</h2>
        <div className="mt-1 text-xs text-muted-foreground" data-testid="doc-changed">{doc.changed}</div>
      </div>
      <div data-words-exempt className="px-5 pb-5 pt-2" data-testid="doc-body">
        {doc.empty ? (
          <div className="py-5 text-muted-foreground">Nothing here yet. Add the first line below.</div>
        ) : (
          <>
            {cut > 0 && (
              <button type="button" onClick={() => setAll(true)} data-testid="doc-earlier"
                      className="block! w-full border-0! border-b! border-dotted! border-border! bg-transparent! px-0! py-2.5! text-left text-[13px]! font-normal! text-[color:var(--accent-deep)]! underline! cursor-pointer">
                Show {cut.toLocaleString("en")} earlier lines
              </button>
            )}
            {doc.lines.slice(cut).map((r, i) => <Row key={cut + i} row={r} />)}
          </>
        )}
      </div>
      {doc.add && <AddLine doc={doc} sample={sample} reload={reload} />}
    </section>
  );
}

function DocList({ page, openId, finishedOpen, setFinishedOpen, to }: {
  page: DocsPage; openId: string | null; finishedOpen: boolean; setFinishedOpen: (v: boolean) => void; to: (id: string) => string;
}) {
  const item = (id: string, label: string) => (
    <Link key={id} to={to(id)} data-testid="doc-link" aria-current={id === openId ? "page" : undefined}
          className={cn("flex items-center gap-2 border-l-[3px] border-transparent px-3.5 py-[7px] text-sm text-foreground! no-underline! hover:bg-[var(--row-hover)]",
                        id === openId && "border-[color:var(--accent)] bg-muted font-semibold")}>
      {label}
    </Link>
  );
  const group = (p: DocProject) => (
    <div key={p.id} data-testid="doc-group">
      <div className="px-3.5 pb-1 pt-3 text-xs font-semibold"><span data-user-text>{p.name}</span></div>
      {p.docs.map((d) => item(d.id, d.label))}
    </div>
  );
  const n = page.finished.length;
  return (
    <nav aria-label="Documents" className="hidden border border-border bg-card pb-1 md:block" data-testid="doc-list">
      <div data-testid="doc-group">
        <div className="px-3.5 pb-1 pt-3 text-xs font-semibold text-muted-foreground">For every project</div>
        {page.shared.map((d) => item(d.id, d.label))}
      </div>
      {page.projects.map(group)}
      {finishedOpen && page.finished.map(group)}
      {n > 0 && (
        <button type="button" onClick={() => setFinishedOpen(!finishedOpen)} data-testid="doc-finished"
                className="mt-1 block! w-full border-0! border-t! border-border! bg-transparent! px-3.5! py-2.5! text-left text-[13px]! font-normal! text-[color:var(--accent-deep)]! underline! cursor-pointer">
          {finishedOpen ? "Hide finished projects" : `Show ${n} finished ${n === 1 ? "project" : "projects"}`}
        </button>
      )}
    </nav>
  );
}

/** The narrow page's list: one select with every document, the open one chosen. */
function DocSelect({ page, openId, pick }: { page: DocsPage; openId: string | null; pick: (id: string) => void }) {
  return (
    <select aria-label="Document" value={openId ?? ""} onChange={(e) => pick(e.target.value)} data-testid="doc-select"
            className="block h-10 w-full min-w-0 border! border-border! bg-background! px-2.5! text-sm! text-foreground! md:hidden">
      <optgroup label="For every project">
        {page.shared.map((d) => <option key={d.id} value={d.id}>{d.title}</option>)}
      </optgroup>
      {[...page.projects, ...page.finished].map((p) => (
        <optgroup key={p.id} label={p.name}>
          {p.docs.map((d) => <option key={d.id} value={d.id} data-user-text>{`${d.label}: ${p.name}`}</option>)}
        </optgroup>
      ))}
    </select>
  );
}

function Loading() {
  return (
    <div role="status" aria-busy="true" aria-label="Loading" className="grid gap-5 md:grid-cols-[260px_minmax(0,1fr)]" data-testid="docs-loading">
      <div className="hidden gap-3 border border-border bg-card p-3.5 md:grid">
        {[0, 1, 2, 3, 4, 5].map((i) => <Skeleton key={i} width="70%" />)}
      </div>
      <div className="grid gap-3 border border-border bg-card p-5">
        {[0, 1, 2, 3].map((i) => (
          <div key={i} className="grid gap-2"><Skeleton width="90%" /><Skeleton width="60%" /></div>
        ))}
      </div>
    </div>
  );
}

export default function FactoryDocs() {
  const named = useSample();
  const sample = isSample(named) ? named : null;
  const [params, setParams] = useSearchParams();
  const docId = params.get("doc");
  const [showFinished, setShowFinished] = React.useState(false);

  // A document the page does not show (an old link) opens the first shared one instead.
  const seen = React.useRef(docId);
  const { data, error, reload } = usePagePolling<DocsPage>(async () => {
    if (sample) return sampleLoad(sample, docId);
    if (!docId) return factoryDocs();
    try {
      return await factoryDoc(docId);
    } catch (e) {
      if (!(e instanceof ApiError && e.status === 404)) throw e;
      seen.current = null;
      setParams((q) => { const n = new URLSearchParams(q); n.delete("doc"); return n; }, { replace: true });
      return factoryDocs();
    }
  }, POLL_MS);
  React.useEffect(() => {
    if (seen.current === docId) return;
    seen.current = docId;
    reload();
  }, [docId]); // eslint-disable-line react-hooks/exhaustive-deps

  const pick = (id: string) => setParams((q) => { const n = new URLSearchParams(q); n.set("doc", id); return n; });
  const to = (id: string) => { const n = new URLSearchParams(params); n.set("doc", id); return `?${n.toString()}`; };

  const doc = data?.doc ?? null;
  const openId = docId ?? doc?.id ?? null;
  const stale = Boolean(docId) && doc?.id !== docId && !error;
  const inFinished = Boolean(doc?.project && data?.finished.some((p) => p.id === doc.project!.id));

  return (
    <div className="grid gap-6 md:gap-8" data-testid="factory-docs">
      <div>
        <h1 className="m-0! mb-1.5! text-[22px]! md:text-[28px]!">Docs and memory</h1>
        <p className="m-0! max-w-[640px] text-[15px] text-muted-foreground">What the crew follows and remembers.</p>
      </div>

      {error && (
        <Banner tone="error" action={<Button variant="secondary" onClick={reload}>Retry</Button>}>
          <b>Documents did not load.</b> <span className="text-foreground">The cell did not answer. Try again in a moment.</span>
        </Banner>
      )}
      {!data && !error && <Loading />}
      {data?.empty && (
        <div className="border border-border bg-card px-4 py-7 text-muted-foreground" data-testid="docs-empty">
          Nothing written yet. The crew writes its rules and memory here as it works.
        </div>
      )}
      {data && !data.empty && (
        <>
          <DocSelect page={data} openId={openId} pick={pick} />
          <div className="grid items-start gap-5 md:grid-cols-[260px_minmax(0,1fr)]">
            <DocList page={data} openId={openId} to={to} finishedOpen={showFinished || inFinished} setFinishedOpen={setShowFinished} />
            {doc && <DocPane key={doc.id} doc={doc} sample={sample} stale={stale} reload={reload} />}
          </div>
        </>
      )}
    </div>
  );
}
