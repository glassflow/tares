import * as React from "react";
import { useSearchParams } from "react-router-dom";

import { ApiError } from "../../api";
import { Banner, Button, EmptyState, Loading, Skeleton } from "../../components/edition";
import { Textarea } from "../../components/ui/textarea";
import { cn } from "../../lib/utils";
import { usePagePolling } from "../usePagePolling";
import { useSample } from "../sample";
import {
  addRunbookLine, removeRunbookLine, sreRunbooks,
  type RunbookLine, type RunbookService, type RunbooksData,
} from "./Runbooks.api";
import { isSample, sampleAdd, sampleLoad, sampleRemove, sampleStart } from "./sample/runbooks";

// Runbooks and memory: per service, what the investigator should know. A runbook is the lines
// people wrote (add one, remove one); memory is what the investigator wrote back, shown apart and
// never changed here. One read per page load, then again every minute; a write answers with the new
// whole screen, which replaces what is shown until the next read. The lines are written by people
// and agents, so they are exempt from the words rule; service names are chosen by people.

const POLL_MS = 60_000;
const MAX_LINE = 500;
const NO_LINE = "Write the line to add.";
const NOT_SAVED = "The line was not saved. Nothing changed, try again.";
const NOT_REMOVED = "The line was not removed. Nothing changed, try again.";

const countWords = (n: number) => (n === 0 ? "none" : n === 1 ? "1 line" : `${n} lines`);
const meta = (l: RunbookLine) => [l.by, l.when_words].filter(Boolean).join(", ");
const sentence = (e: unknown, fallback: string) => (e instanceof ApiError && e.message ? e.message : fallback);

function RunbooksSkeleton() {
  return (
    <Loading className="grid items-start gap-[18px] md:grid-cols-[260px_minmax(0,1fr)]" data-testid="rb-loading">
      <div className="hidden border border-border bg-card md:block">
        {[0, 1, 2, 3, 4].map((i) => <div key={i} className="border-t border-border px-3.5 py-3.5 first:border-t-0"><Skeleton width="55%" height={10} /></div>)}
      </div>
      <div className="border border-border bg-card">
        {[0, 1, 2, 3].map((i) => <div key={i} className="border-t border-border px-[18px] py-4 first:border-t-0"><Skeleton width={i === 0 ? "60%" : "85%"} height={10} /></div>)}
      </div>
    </Loading>
  );
}

function ServiceList({ services, current, pick }: { services: RunbookService[]; current: string; pick: (name: string) => void }) {
  return (
    <nav aria-label="Services" className="hidden border border-border bg-card md:block" data-testid="rb-list">
      <div className="max-h-[560px] overflow-y-auto">
        {services.map((s) => (
          <button
            key={s.name} type="button" data-testid="rb-service" data-service={s.name}
            aria-current={s.name === current ? "true" : undefined} onClick={() => pick(s.name)}
            className={cn(
              "flex! w-full items-center justify-between gap-2.5 border-0! border-t! border-border! bg-transparent! px-3.5! py-2.5! text-left text-sm! font-normal! text-foreground! first:border-t-0! hover:bg-[var(--row-hover)]! cursor-pointer",
              s.name === current && "bg-muted! font-semibold! shadow-[inset_3px_0_0_var(--accent)]",
            )}
          >
            <span data-user-text className="min-w-0 [overflow-wrap:anywhere]">{s.name}</span>
            <span className="whitespace-nowrap text-[13px] font-normal text-muted-foreground" data-testid="rb-count">{countWords(s.runbook.length)}</span>
          </button>
        ))}
      </div>
    </nav>
  );
}

/** The phone's list: one select with every service, the open one chosen. */
function ServicePicker({ services, current, pick }: { services: RunbookService[]; current: string; pick: (name: string) => void }) {
  return (
    <label className="flex h-10 items-center justify-between gap-3 border border-border bg-[var(--input-bg)] px-3 text-sm md:hidden">
      <span className="text-muted-foreground">Service</span>
      <span className="flex min-w-0 max-w-[70%] items-center gap-1.5">
        <select
          data-testid="rb-picker" data-user-text value={current} onChange={(e) => pick(e.target.value)}
          className="min-w-0 cursor-pointer appearance-none border-0! bg-transparent! bg-none! p-0! pr-0! text-right text-sm! text-foreground!"
        >
          {services.map((s) => <option key={s.name} value={s.name}>{s.name}</option>)}
        </select>
        <span aria-hidden className="text-[11px] leading-none text-foreground">{"▾"}</span>
      </span>
    </label>
  );
}

function AddLine({ service, sample, shown, start, onData, reload }: {
  service: string; sample: string | null; shown: RunbooksData; start: { text: string; error: string };
  onData: (d: RunbooksData) => void; reload: () => void;
}) {
  const [text, setText] = React.useState(start.text);
  const [error, setError] = React.useState(start.error);
  const [busy, setBusy] = React.useState(false);
  const formRef = React.useRef<HTMLFormElement>(null);
  const id = React.useId();

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    const line = text.trim();
    if (!line) { setError(NO_LINE); return; }
    setBusy(true);
    setError("");
    try {
      // a sample has no cell to write to: it answers as the cell does, on its own copy of the data
      onData(sample ? sampleAdd(sample, shown, service, line) : await addRunbookLine(service, line));
      setText("");
    } catch (err) {
      setError(sentence(err, NOT_SAVED));
      if (err instanceof ApiError && err.status === 404) reload();
    } finally {
      setBusy(false);
    }
  };

  return (
    <form ref={formRef} onSubmit={submit} className="border-t border-border bg-muted px-[18px] py-3.5" data-testid="rb-add">
      <div className="flex flex-wrap gap-2.5">
        <Textarea
          id={id} rows={1} value={text} maxLength={MAX_LINE} data-testid="rb-add-input"
          aria-label="New line for this runbook" placeholder="Add one line the investigator should know about this service"
          aria-invalid={error ? true : undefined} aria-describedby={`${id}-note`}
          onChange={(e) => { setText(e.target.value); if (error) setError(""); }}
          onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); formRef.current?.requestSubmit(); } }}
          className="min-h-9 max-h-40 min-w-[200px] flex-1 resize-none bg-[var(--input-bg)]! py-[7px]! [field-sizing:content]"
        />
        <Button type="submit" busy={busy} className="max-md:w-full">Add line</Button>
      </div>
      {error ? (
        <p id={`${id}-note`} role="alert" data-testid="rb-add-error" className="m-0! mt-2.5! text-[13px] text-[color:var(--err)]">{error}</p>
      ) : (
        <p id={`${id}-note`} className="m-0! mt-2.5! text-[13px] text-muted-foreground">
          One line, up to {MAX_LINE} characters. The investigator reads it from its next investigation.
        </p>
      )}
    </form>
  );
}

function Line({ line, index, busy, remove, label }: { line: RunbookLine; index: number; busy: boolean; remove: (i: number) => void; label: string }) {
  const who = meta(line);
  return (
    <div className="grid grid-cols-[minmax(0,1fr)_auto] gap-x-3.5 gap-y-1 border-t border-border px-[18px] py-3 first:border-t-0" data-testid="rb-line">
      <div data-words-exempt className="min-w-0 [overflow-wrap:anywhere]" data-testid="rb-line-text">{line.text}</div>
      <Button
        variant="secondary" busy={busy} onClick={() => remove(index)} data-testid="rb-remove"
        aria-label={`Remove line ${index + 1} of ${label}`}
        className="h-[30px]! self-center px-2.5! text-[13px]! [grid-row:1/span_2] [grid-column:2]"
      >
        Remove
      </Button>
      {who && <div className="text-[13px] text-muted-foreground" data-testid="rb-line-by"><span data-user-text>{who}</span></div>}
    </div>
  );
}

/** What the investigator wrote back. Read only: there is nothing here to click or type in. */
function Memory({ svc }: { svc: RunbookService }) {
  return (
    <section aria-label="What the investigator remembered" className="min-w-0 border border-dashed border-border" data-testid="rb-memory">
      <div className="px-[18px] py-3.5">
        <h2 className="m-0! text-sm! font-semibold! text-muted-foreground!">What the investigator remembered</h2>
        <p className="m-0! mt-0.5! text-[13px] text-muted-foreground">Kept by the investigator. You can read these, not change them.</p>
      </div>
      {svc.memory.map((l, i) => (
        <div key={i} className="grid gap-1 border-t border-dashed border-border px-[18px] py-3 text-muted-foreground" data-testid="rb-memory-line">
          <div data-words-exempt className="min-w-0 [overflow-wrap:anywhere]" data-testid="rb-memory-text">{l.text}</div>
          <div className="text-[13px]"><span data-user-text>{meta(l)}</span></div>
        </div>
      ))}
    </section>
  );
}

function Pane({ svc, sample, shown, start, onData, reload }: {
  svc: RunbookService; sample: string | null; shown: RunbooksData; start: { text: string; error: string };
  onData: (d: RunbooksData) => void; reload: () => void;
}) {
  const [removing, setRemoving] = React.useState<number | null>(null);
  const [removeError, setRemoveError] = React.useState("");

  const remove = async (index: number) => {
    setRemoving(index);
    setRemoveError("");
    try {
      onData(sample ? sampleRemove(shown, svc.name, index) : await removeRunbookLine(svc.name, index));
    } catch (err) {
      setRemoveError(sentence(err, NOT_REMOVED));
      if (err instanceof ApiError && err.status === 404) reload();
    } finally {
      setRemoving(null);
    }
  };

  const none = svc.runbook.length === 0;
  return (
    <div className="grid min-w-0 gap-3.5">
      <section aria-label="Runbook" className="min-w-0 border border-border bg-card" data-testid="rb-pane">
        <div className="border-b border-border px-[18px] py-3.5">
          <h2 data-user-text className="m-0! text-base!" data-testid="rb-title">{svc.name}</h2>
          {svc.used_words && <div className="mt-[3px] text-[13px] text-muted-foreground" data-testid="rb-used">{svc.used_words}</div>}
        </div>
        {none ? (
          <EmptyState title={`No runbook for ${svc.name} yet`} data-testid="rb-nolines" className="[&_h3]:text-base!">
            {svc.memory.length > 0
              ? "Add the first line below."
              : `Add the first line below. The investigator reads it from its next investigation of ${svc.name}.`}
          </EmptyState>
        ) : (
          svc.runbook.map((l, i) => <Line key={`${i}-${l.text}`} line={l} index={i} busy={removing === i} remove={remove} label={svc.name} />)
        )}
        {removeError && <p role="alert" data-testid="rb-remove-error" className="m-0! border-t border-border px-[18px] py-2.5 text-[13px] text-[color:var(--err)]">{removeError}</p>}
        <AddLine key={svc.name} service={svc.name} sample={sample} shown={shown} start={start} onData={onData} reload={reload} />
      </section>
      {svc.memory.length > 0 && <Memory svc={svc} />}
    </div>
  );
}

export default function Runbooks() {
  const named = useSample();
  const sample = isSample(named) ? named : null;
  const [params, setParams] = useSearchParams();
  const first = React.useRef(sample ? sampleStart(sample) : { selected: "", text: "", error: "" });

  const { data, error, reload } = usePagePolling<RunbooksData>(
    () => (sample ? sampleLoad(sample) : sreRunbooks()),
    POLL_MS,
  );
  // a write answers with the whole new screen; it stands until the next read replaces it
  const [fresh, setFresh] = React.useState<RunbooksData>();
  React.useEffect(() => setFresh(undefined), [data]);
  const shown = fresh ?? data;

  const services = shown?.services ?? [];
  const wanted = params.get("service") ?? first.current.selected;
  const svc = services.find((s) => s.name === wanted) ?? services[0];
  const pick = (name: string) => setParams((q) => { const n = new URLSearchParams(q); n.set("service", name); return n; }, { replace: true });
  // what a sample's add box holds is for the service it opens on only
  const start = svc && !params.get("service") && svc.name === (first.current.selected || services[0]?.name)
    ? { text: first.current.text, error: first.current.error }
    : { text: "", error: "" };

  return (
    <div className="grid gap-6 md:gap-8" data-testid="sre-runbooks">
      <div>
        <h1 className="m-0! mb-1.5! text-[22px]! md:text-[28px]!">Runbooks and memory</h1>
        {shown && (
          <p className="m-0! max-w-[900px] text-[15px] text-muted-foreground">
            {services.length === 0
              ? "What the investigator should know about each service."
              : "What the investigator should know about each service. People write the lines; the investigator adds what it learned."}
          </p>
        )}
      </div>

      {error && (
        <Banner tone="error" action={<Button variant="secondary" onClick={reload}>Try now</Button>} data-testid="rb-error">
          <b>Runbooks did not load.</b> <span className="text-foreground">Tares could not read them just now. It will try again in 60 seconds.</span>
        </Banner>
      )}
      {!shown && !error && <RunbooksSkeleton />}
      {shown && services.length === 0 && (
        <EmptyState title="No services yet" className="border border-dashed border-border" data-testid="rb-empty">
          Services show here once the watcher has seen logs from them. Then you can write what the investigator should know about each one.
        </EmptyState>
      )}
      {shown && svc && (
        <div className="grid items-start gap-3.5 md:grid-cols-[260px_minmax(0,1fr)] md:gap-[18px]">
          <ServiceList services={services} current={svc.name} pick={pick} />
          <div className="grid min-w-0 gap-3.5">
            <ServicePicker services={services} current={svc.name} pick={pick} />
            <Pane key={svc.name} svc={svc} sample={sample} shown={shown} start={start} onData={setFresh} reload={reload} />
          </div>
        </div>
      )}
    </div>
  );
}
