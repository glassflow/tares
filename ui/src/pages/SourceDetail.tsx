import { useEffect, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";

import { api } from "../api";
import Dependents from "../components/Dependents";
import { ProjectLink } from "../components/ProjectBadge";
import IngestEndpoint from "../components/IngestEndpoint";
import SourceForm from "../components/SourceForm";
import { ruleSummary, sourceTitle, usePolling } from "../components/bits";
import { kindOf, memberOf, stateOf, stateWords } from "../components/connectionState";
import {
  Banner, Button, Checkbox, Collapse, Dialog, EmptyState, ListRow, ListSection, LoadingBlocks, Panel, ProgressBar, Stat,
  StatusLabel, Tabs, toneOf,
} from "../components/edition";
import { formatCount } from "../lib/number";
import { ageWords, formatStampUTC } from "../lib/time";
import type { ConnectorSpec, Source, SourceEvent, SourceField, SourceFieldsProfile, SourceLabelProfile } from "../types";

type Tab = "fields" | "events" | "config";

const errText = (e: unknown) => String((e as Error).message ?? e);
// The daemon's name for this kind of object when asking what depends on it; not a word on the page.
const DEPENDENTS_KIND = "source"; // words-ok

// One connection's page: what it is, whether it is healthy, the labels agents slice it by, what else is
// in its data, what it received last, and its settings.
export default function SourceDetail() {
  const { name = "" } = useParams();
  const nav = useNavigate();
  const [specs, setSpecs] = useState<Record<string, ConnectorSpec>>();
  const [tab, setTab] = useState<Tab>("fields");
  const [actionError, setActionError] = useState<string>();
  const [busy, setBusy] = useState(false);
  const [confirmDel, setConfirmDel] = useState(false);
  const [purge, setPurge] = useState(false);
  const [cascade, setCascade] = useState(true);
  const tabsRef = useRef<HTMLDivElement>(null);

  const { data: source, error, reload } = usePolling(() => api.source(name));

  useEffect(() => { api.connectors().then(setSpecs, () => setSpecs({})); }, []);

  // Editing labels opens the Configuration tab, which sits below the fold, so scroll to it: otherwise
  // the page looks unchanged.
  const openConfig = () => {
    setTab("config");
    requestAnimationFrame(() => tabsRef.current?.scrollIntoView({ behavior: "smooth", block: "start" }));
  };

  if (error) return <Banner tone="error">Tares could not load this connection: {error}</Banner>;
  if (!source || !specs) return <LoadingBlocks />;

  const spec = specs[source.connector];
  const h = source.health;
  const st = stateOf(source);
  // "source" names the tone table, it is not a word on the page.
  const tone = toneOf("source", st); // words-ok
  const title = sourceTitle(source.config);
  const projects = memberOf(source);
  const last = h?.last_ingest ? Date.parse(h.last_ingest) : NaN;
  const polls = h?.polls ?? 0;

  const act = (fn: () => Promise<unknown>) => async () => {
    setActionError(undefined);
    setBusy(true);
    try { await fn(); reload(); } catch (e) { setActionError(errText(e)); } finally { setBusy(false); }
  };

  const remove = async () => {
    setBusy(true);
    setActionError(undefined);
    try { await api.deleteSource(name, purge, cascade); nav("/sources"); }
    catch (e) { setConfirmDel(false); setActionError(errText(e)); }
    finally { setBusy(false); }
  };

  return (
    <div className="grid min-w-0 gap-5" data-testid="connection-page">
      <header className="flex flex-wrap items-start justify-between gap-x-6 gap-y-3">
        <div className="grid min-w-0 gap-1.5">
          <h1 className="m-0 text-[22px] md:text-[28px]" data-user-text data-testid="connection-title">
            {title && title !== source.name ? title : source.name}
          </h1>
          {title && title !== source.name && (
            <div className="font-mono text-xs text-muted-foreground" data-user-text>{source.name}</div>
          )}
          <p className="m-0 text-sm text-muted-foreground" data-testid="connection-kind">
            {kindOf(specs, source.connector)}
            {st !== "push" && spec?.mode !== "push" && `, checked every ${source.poll}`}
            {projects.length > 0
              ? <>, in {projects.map((id, k) => <span key={id}>{k > 0 && ", "}<ProjectLink id={id} /></span>)}</>
              : ", in no project"}
          </p>
          <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1 text-sm text-muted-foreground">
            <StatusLabel tone={tone} data-testid="connection-state">{stateWords(st)}</StatusLabel>
            <span data-testid="connection-last" title={Number.isFinite(last) ? formatStampUTC(last) : undefined}>
              {Number.isFinite(last) ? `last received ${ageWords(Date.now() - last)}` : "nothing received yet"}
            </span>
          </div>
        </div>
        <div className="flex flex-wrap gap-2">
          {source.paused
            ? <Button variant="secondary" busy={busy} onClick={act(() => api.resumeSource(name))}>Resume</Button>
            : <Button variant="secondary" busy={busy} onClick={act(() => api.pauseSource(name))}>Pause</Button>}
          <Button variant="quiet" className="text-err hover:text-err" data-testid="connection-delete"
                  onClick={() => { setPurge(false); setCascade(true); setConfirmDel(true); }}>Delete</Button>
        </div>
      </header>

      <Collapse>{actionError && <Banner tone="error" data-testid="connection-action-error">{actionError}</Banner>}</Collapse>

      <Collapse>{h?.last_error && (
        <Banner tone="error" data-testid="connection-error">
          Failing {h.consecutive_errors === 1 ? "once" : `${formatCount(h.consecutive_errors)} times in a row`}:{" "}
          <span className="font-mono" data-user-text>{h.last_error}</span>
        </Banner>
      )}</Collapse>

      {spec?.mode === "push" && <IngestEndpoint source={source} />}

      <Stat label="Events kept" value={formatCount(h?.events_total ?? 0)} data-testid="connection-events"
            note={`${formatCount(h?.events_since_start ?? 0)} since Tares restarted${
              polls > 0 ? `, from ${formatCount(polls)} ${polls === 1 ? "check" : "checks"}` : ""}`} />

      <LabelsSummary source={source} onEdit={openConfig} />

      <div ref={tabsRef} className="scroll-mt-16">
        <Tabs aria-label="About this connection" value={tab} onValueChange={(v) => setTab(v as Tab)}
              items={[
                { value: "fields", label: "Fields", content: <FieldsPanel name={name} source={source} /> },
                { value: "events", label: "Recent events", content: <RecentEvents name={name} /> },
                {
                  value: "config", label: "Configuration",
                  content: spec
                    ? <div className="pt-4">
                        <SourceForm
                          connector={source.connector}
                          spec={spec}
                          lockName
                          initial={{ name: source.name, type: source.type, poll: source.poll, config: source.config }}
                          submitLabel="Save changes"
                          onSubmit={async (body) => {
                            await api.updateSource(name, body);
                            setTab("fields");
                            reload();
                          }}
                        />
                      </div>
                    : <EmptyState className="pt-4">Tares does not know this kind of connection, so its settings cannot be edited here.</EmptyState>,
                },
              ]} />
      </div>

      <Dialog open={confirmDel} onOpenChange={(o) => { if (!o && !busy) setConfirmDel(false); }}
              data-testid="connection-delete-dialog"
              title={`Delete ${title ?? source.name}?`}
              description="Its settings are removed. The events it kept stay unless you remove them too."
              footer={<>
                <Button variant="secondary" disabled={busy} onClick={() => setConfirmDel(false)}>Cancel</Button>
                <Button busy={busy} onClick={() => void remove()} data-testid="connection-delete-confirm">Delete</Button>
              </>}>
        <div className="grid gap-3">
          <Dependents kind={DEPENDENTS_KIND} name={name} cascade={cascade} onChange={(c) => setCascade(c)} />
          <Checkbox checked={purge} onCheckedChange={(c) => setPurge(c === true)} data-testid="connection-purge"
                    label={`Also remove the events it kept${h?.events_total ? ` (${formatCount(h.events_total)})` : ""}`} />
        </div>
      </Dialog>
    </div>
  );
}

type LabelConfig = { name: string; field?: string; const?: string; primary?: boolean };

// The declared labels, the most important setting of a connection, so they sit on the page and not only in
// the form: every event carries them, and the key names the timeline agents read. Shut by default: the
// Labels list under Fields says all this and more (coverage, top values, rules), so on a connection with
// many labels this was mostly page length. Shut, the head keeps the count and the key.
function LabelsSummary({ source, onEdit }: { source: Source; onEdit: () => void }) {
  const labels = (source.config?.labels ?? []) as LabelConfig[];
  const [open, setOpen] = useState(false);
  const key = labels.find((l) => l.primary)?.name;
  return (
    <Panel title="Labels and key" data-testid="connection-labels"
           action={<Button variant="secondary" onClick={onEdit}>Edit</Button>}>
      {labels.length === 0 ? (
        <p className="m-0 text-sm text-muted-foreground">
          No labels yet, so events take the default key for their kind and agents cannot slice this connection
          by anything. Fields below lists what is in the data; make the useful ones labels.
        </p>
      ) : (
        <div className="grid gap-2">
          <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1 text-sm">
            <span data-testid="connection-labels-summary">
              {labels.length} {labels.length === 1 ? "label" : "labels"}
              {key && <>, the key is <span className="font-mono" data-user-text>{key}</span></>}
            </span>
            <Button variant="quiet" aria-expanded={open} onClick={() => setOpen((o) => !o)}
                    data-testid="connection-labels-toggle">
              {open ? "Hide them" : "Show them"}
            </Button>
          </div>
          {open && (
            <ul className="m-0 grid list-none divide-y divide-border p-0">
              {labels.map((l) => (
                <li key={l.name} className="flex flex-wrap items-baseline gap-x-3 gap-y-1 py-2 text-sm">
                  <span className="font-mono" data-user-text>{l.name}</span>
                  {l.primary && <Tag>key</Tag>}
                  <span className="text-muted-foreground">
                    {l.field
                      ? <>from <span className="font-mono" data-user-text>{l.field}</span></>
                      : <>always <span className="font-mono" data-user-text>{String(l.const ?? "")}</span></>}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </Panel>
  );
}

function Tag({ children, title }: { children: React.ReactNode; title?: string }) {
  return (
    <span title={title}
          className="border border-border px-1.5 font-mono text-[11px] uppercase tracking-[0.08em] text-muted-foreground">
      {children}
    </span>
  );
}

function RecentEvents({ name }: { name: string }) {
  const { data: events, error } = usePolling(() => api.sourceEvents(name, 30));
  if (error) return <Banner tone="error" className="mt-4">Tares could not load the recent events: {error}</Banner>;
  if (!events) return <LoadingBlocks />;
  if (!events.length) return <EmptyState className="pt-4">Nothing received from this connection yet.</EmptyState>;
  return (
    <ul className="m-0 grid list-none divide-y divide-border p-0" data-testid="connection-recent">
      {events.map((e, i) => <EventRow key={i} e={e} />)}
    </ul>
  );
}

function EventRow({ e }: { e: SourceEvent }) {
  const at = Date.parse(e.ingest_time);
  return (
    <li className="grid min-w-0 gap-1 py-3" data-testid="connection-event">
      <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1 text-xs text-muted-foreground">
        <span className="min-w-0 font-mono [overflow-wrap:anywhere]" data-user-text>
          {e.key}{e.event_type && <> · {e.event_type}</>}
        </span>
        <span title={Number.isFinite(at) ? formatStampUTC(at) : undefined}>
          {Number.isFinite(at) ? ageWords(Date.now() - at) : ""}
        </span>
      </div>
      <div className="whitespace-pre-wrap font-mono text-xs [overflow-wrap:anywhere]" data-user-text>{e.text}</div>
    </li>
  );
}

const FIELDS_PAGE = 12;
const shorten = (v: string) => (v.length > 44 ? v.slice(0, 43) + "…" : v);

// The labels and the other fields, sampled from recent events. Nested contexts (a Prometheus label set)
// are flattened to sub-fields (metric.service, …) so the real axes show; the daemon marks which are
// labels or the key. Coverage shows only when it varies (100% everywhere is noise), so partial fields
// stand out.
function FieldsPanel({ name, source }: { name: string; source: Source }) {
  // The rules live in the connection's settings; the profile only reports coverage and values. Matching by
  // name is what lets this list say WHICH labels rewrite their values.
  const rules = Object.fromEntries(
    ((source.config?.labels ?? []) as Array<{ name: string; pattern?: string; map?: object }>)
      .map((l) => [l.name, ruleSummary(l.pattern, Object.keys(l.map ?? {}).length)]));
  const { data, error } = usePolling(() => api.sourceFields(name), 5000);
  const [showAll, setShowAll] = useState(false);
  if (error) return <Banner tone="error" className="mt-4">Tares could not sample the fields: {error}</Banner>;
  if (!data) return <LoadingBlocks />;
  if (!data.fields.length) return <EmptyState className="pt-4">No events to sample yet, so no fields to show.</EmptyState>;

  // Only the fields that are not labels already: the labels are listed above with more detail, so this
  // answers the one useful question left, what else in the data could become a label.
  const candidates = data.fields.filter((f) => !f.is_label && !f.is_key);
  // Densest first: the best candidates lead, long tails fold behind Show all.
  const sorted = [...candidates].sort((a, b) => b.coverage - a.coverage || a.name.localeCompare(b.name));
  const shown = showAll ? sorted : sorted.slice(0, FIELDS_PAGE);
  const labels = data.labels ?? [];
  const sampled = `${formatCount(data.sampled)} recent ${data.sampled === 1 ? "event" : "events"} sampled`;

  return (
    <div className="grid gap-6 pt-4">
      {labels.length > 0 && (
        <ListSection paper title="Labels" aside={sampled} data-testid="connection-field-labels">
          {labels.map((l) => (
            <ProfileRow key={l.name} p={l} profile={data} full={labels.every((x) => x.coverage === data.sampled)}
                        tags={<>
                          <Tag>{l.is_key ? "key" : "label"}</Tag>
                          {rules[l.name] && <Tag title="This label rewrites its values before they are kept">rewrites: {rules[l.name]}</Tag>}
                        </>}
                        missing="No sampled event carries this label. Check its field or pattern under Configuration." />
          ))}
        </ListSection>
      )}
      {/* "not labels" earns its place: without it a missing field reads as a bug, not as "it is a label above". */}
      <ListSection paper title="Other fields" aside={`Not labels. ${sampled}`} data-testid="connection-fields">
        {sorted.length === 0
          ? <EmptyState>Every field in the sample is a label already.</EmptyState>
          : shown.map((f) => (
              <ProfileRow key={f.name} p={f} profile={data} full={candidates.every((x) => x.coverage === data.sampled)}
                          missing="Not in the sample." />
            ))}
        {sorted.length > FIELDS_PAGE && (
          <Button variant="quiet" className="mt-2" onClick={() => setShowAll((s) => !s)} data-testid="connection-fields-more">
            {showAll ? "Show fewer" : `Show all ${sorted.length} fields`}
          </Button>
        )}
      </ListSection>
    </div>
  );
}

// One label or field: its name, how many sampled events carry it (when that varies), how many values it
// takes and the commonest of them. Three columns from md up, the density the old table had; stacked
// below that, so it fits at 390.
function ProfileRow({ p, profile, full, tags, missing }: {
  p: SourceField | SourceLabelProfile; profile: SourceFieldsProfile; full: boolean;
  tags?: React.ReactNode; missing: string;
}) {
  const of = Math.max(1, profile.sampled);
  return (
    <ListRow data-testid="connection-field"
             className={`grid min-w-0 gap-x-6 gap-y-1.5 md:grid-cols-[minmax(0,2fr)_minmax(0,11rem)_minmax(0,3fr)] md:items-start${
               p.coverage === 0 ? " opacity-60" : ""}`}>
      <div className="grid min-w-0 gap-1">
        <div className="flex min-w-0 flex-wrap items-baseline gap-x-2 gap-y-1">
          <span className="font-mono text-sm [overflow-wrap:anywhere]" data-user-text>{p.name}</span>
          {tags}
        </div>
        {p.coverage === 0 && <div className="text-xs text-muted-foreground">{missing}</div>}
      </div>
      <div className="grid min-w-0 gap-1.5">
        <div className="text-xs text-muted-foreground">
          {!full && `in ${formatCount(p.coverage)} of ${formatCount(profile.sampled)}, `}
          {formatCount(p.distinct)} {p.distinct === 1 ? "value" : "values"}
        </div>
        {!full && (
          <ProgressBar className="max-w-60" value={(p.coverage / of) * 100}
                       aria-label={`${p.name}: share of sampled events that carry it`} />
        )}
      </div>
      {p.values.length > 0 && (
        <div className="flex min-w-0 flex-wrap gap-1.5">
          {p.values.slice(0, 6).map((v) => (
            <span key={v.value} title={`${v.value}, ${formatCount(v.events)} events`} data-user-text
                  className="max-w-full truncate border border-border bg-muted px-1.5 py-0.5 font-mono text-xs">
              {shorten(v.value)} <span className="text-muted-foreground">({formatCount(v.events)})</span>
            </span>
          ))}
        </div>
      )}
    </ListRow>
  );
}
