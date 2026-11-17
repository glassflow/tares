import { useMemo, useState } from "react";
import { Link } from "react-router-dom";

import { api } from "../api";
import { sourceTitle, usePolling } from "../components/bits";
import {
  Banner, Button, EmptyState, Input, ListRow, ListSection, LoadingBlocks, Select, StatusLabel, toneOf,
} from "../components/edition";
import { formatCount } from "../lib/number";
import { ageWords } from "../lib/time";
import type { ConnectorSpec, Source } from "../types";

// The words a person reads for each health state (statusTones.ts SourceHealthState). The tone stays keyed
// by the daemon's value.
const STATE_WORDS: Record<string, string> = {
  starting: "starting",
  ok: "healthy",
  error: "failing",
  paused: "paused",
  push: "receives pushes",
};
const stateOf = (s: Source) => s.health?.status ?? "starting";
const stateWords = (st: string) => STATE_WORDS[st] ?? st;

// A connection can be in several projects: `projects` lists them all, older ones only name an owner.
const memberOf = (s: Source) => s.projects ?? (s.owned_by ? [s.owned_by] : []);
const kindOf = (specs: Record<string, ConnectorSpec> | undefined, id: string) => specs?.[id]?.label ?? id;

const ALL = "all";
const NONE = "none";

// The connections list, which Advanced links to. Instance-wide numbers (storage, totals) live in
// Settings (components/UsagePanels.tsx): on a real cell this list is long enough to push them below the
// fold, and they were never about the connections.
//
// Rows, not a table: eight columns cannot fit at 390, and every row is one link to its page. So the
// projects are named, not linked (a link inside a link is invalid); the connection's page links them.
export default function Sources() {
  const { data: sources, error } = usePolling(() => api.sources());
  // Host capabilities gate local-only actions: Find in Docker is hidden where Docker isn't reachable
  // (a hosted cell, or a local box without Docker). Shown until known false.
  const { data: caps } = usePolling(() => api.capabilities());
  const { data: projectList } = usePolling(() => api.projects(), 30000);
  const { data: specs } = usePolling(() => api.connectors(), 60000);

  const [q, setQ] = useState("");
  const [status, setStatus] = useState(ALL);
  const [connector, setConnector] = useState(ALL);
  const [project, setProject] = useState(ALL);

  const docker = caps?.discover_docker !== false;

  const statuses = useMemo(() => Array.from(new Set((sources ?? []).map(stateOf))).sort(), [sources]);
  const connectors = useMemo(() => Array.from(new Set((sources ?? []).map((s) => s.connector))).sort(), [sources]);
  const projectName = useMemo(() => {
    const m: Record<string, string> = {};
    for (const p of projectList?.projects ?? []) m[p.id] = p.name;
    return m;
  }, [projectList]);
  const owningProjects = useMemo(
    () => Array.from(new Set((sources ?? []).flatMap(memberOf))).sort(
      (a, b) => (projectName[a] ?? a).localeCompare(projectName[b] ?? b)),
    [sources, projectName],
  );

  const shown = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return (sources ?? []).filter((s) => {
      const of = memberOf(s);
      return (status === ALL || stateOf(s) === status)
        && (connector === ALL || s.connector === connector)
        && (project === ALL || (project === NONE ? !of.length : of.includes(project)))
        && (!needle || s.name.toLowerCase().includes(needle)
          || (sourceTitle(s.config) ?? "").toLowerCase().includes(needle)
          || s.connector.toLowerCase().includes(needle)
          || kindOf(specs, s.connector).toLowerCase().includes(needle));
    });
  }, [sources, specs, q, status, connector, project]);

  return (
    <div className="grid min-w-0 gap-5" data-testid="connections-page">
      <header className="flex flex-wrap items-end justify-between gap-x-6 gap-y-3">
        <div className="min-w-0">
          <h1 className="m-0 mb-1.5 text-[22px] md:text-[28px]">Connections</h1>
          <p className="m-0 max-w-2xl text-sm text-muted-foreground">
            Everything Tares receives, kept whole and in one shape so agents can read across it.
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          {docker && <Button variant="secondary" asChild><Link to="/sources/discover">Find in Docker</Link></Button>}
          <Button variant="secondary" asChild><Link to="/sources/import">Import</Link></Button>
          <Button variant="secondary" asChild><Link to="/sources/export">Export</Link></Button>
          <Button asChild><Link to="/sources/new">Add a connection</Link></Button>
        </div>
      </header>

      {error && <Banner tone="error">Tares is not answering: {error}</Banner>}

      {!sources && !error && <LoadingBlocks />}

      {sources && sources.length === 0 && (
        // No action of its own: Add a connection is already in the head, and one amber button per page.
        <EmptyState title="No connections yet" data-testid="connections-empty">
          Add one by hand or import a file of them{docker ? ", or find what is running in Docker on this machine" : ""}.
        </EmptyState>
      )}

      {sources && sources.length > 0 && (
        <>
          {/* At 390 search takes the row and the filters go two to a row, so the list starts on the first screen. */}
          <div className="grid grid-cols-2 items-end gap-3 lg:grid-cols-[minmax(0,1fr)_repeat(3,minmax(0,11rem))]">
            <div className="col-span-2 min-w-0 lg:col-span-1">
              <Input label="Search" placeholder="Name or kind" value={q} onChange={(e) => setQ(e.target.value)}
                     data-testid="connections-search" />
            </div>
            <Select label="Status" value={status} onValueChange={setStatus} data-testid="connections-status"
                    options={[{ value: ALL, label: "Any status" },
                              ...statuses.map((s) => ({ value: s, label: stateWords(s) }))]} />
            <Select label="Kind" value={connector} onValueChange={setConnector} data-testid="connections-kind"
                    options={[{ value: ALL, label: "Any kind" },
                              ...connectors.map((c) => ({ value: c, label: kindOf(specs, c) }))]} />
            {owningProjects.length > 0 && (
              <Select label="Project" value={project} onValueChange={setProject} data-testid="connections-project"
                      options={[{ value: ALL, label: "Any project" }, { value: NONE, label: "In no project" },
                                ...owningProjects.map((id) => ({ value: id, label: projectName[id] ?? id }))]} />
            )}
          </div>

          <ListSection paper title="All connections"
                       aside={<span data-testid="connections-count">{shown.length} of {sources.length}</span>}>
            {shown.map((s) => <Row key={s.name} s={s} kind={kindOf(specs, s.connector)} projectName={projectName} />)}
            {!shown.length && <EmptyState data-testid="connections-none">Nothing matches these filters.</EmptyState>}
          </ListSection>
        </>
      )}
    </div>
  );
}

function Row({ s, kind, projectName }: { s: Source; kind: string; projectName: Record<string, string> }) {
  const title = sourceTitle(s.config);
  const st = stateOf(s);
  // "source" names the tone table, it is not a word on the page.
  const tone = toneOf("source", st); // words-ok
  const of = memberOf(s);
  const h = s.health;
  const last = h?.last_ingest ? Date.parse(h.last_ingest) : NaN;
  return (
    <ListRow to={`/sources/${encodeURIComponent(s.name)}`} data-testid="connection-row"
             className="grid grid-cols-1 items-start gap-x-6 gap-y-1 md:grid-cols-[minmax(0,1fr)_auto]">
      <div className="grid min-w-0 gap-1">
        <div className="flex min-w-0 flex-wrap items-baseline gap-x-2">
          <span className="font-medium text-foreground" data-user-text>{title ?? s.name}</span>
          {title && title !== s.name && <span className="font-mono text-xs text-muted-foreground" data-user-text>{s.name}</span>}
        </div>
        <div className="text-[13px] text-muted-foreground">
          <span data-user-text>{kind}</span>
          {st === "push" ? "" : `, every ${s.poll}`}
          {", "}
          {of.length ? <>in <span data-user-text>{of.map((id) => projectName[id] ?? id).join(", ")}</span></> : "in no project"}
        </div>
        {h?.last_error && (
          <div className="line-clamp-2 font-mono text-xs text-err" data-user-text data-testid="connection-error">{h.last_error}</div>
        )}
      </div>
      <div className="grid gap-1 text-[13px] md:justify-items-end md:text-right">
        <StatusLabel tone={tone}>{stateWords(st)}</StatusLabel>
        <span className="text-muted-foreground">
          {formatCount(h?.events_total ?? 0)} events
          {Number.isFinite(last) ? `, last ${ageWords(Date.now() - last)}` : ""}
        </span>
      </div>
    </ListRow>
  );
}
