import { useMemo, useState } from "react";
import { Link } from "react-router-dom";

import { api } from "../api";
import { usePolling } from "../components/bits";
import { Banner, Button, EmptyState, Input, ListRow, ListSection, Select, SkeletonRows } from "../components/edition";
import { formatCount } from "../lib/number";
import { ageWords, formatStampUTC, spanWords } from "../lib/time";
import type { QueryLogEntry } from "../types";

// Reads: every read an agent (over MCP), a script (over the REST API) or this console made, newest
// first, from the daemon's read log (/api/activity/queries, the last LIMIT). It opens on agents' reads,
// the traffic worth watching. What was read is the caller's own selector (data-user-text).

const LIMIT = 150;
// The client filter's sentinel for "every client". Radix Select refuses "" as an item value.
const ALL = "all";
const AGENTS = "mcp";
// The daemon tags each read with the client that made it (ReadBody.client: http | mcp | ui).
// name: on a row; filter: in the client picker; from: in "No reads from …".
const CLIENTS: Record<string, { name: string; filter: string; from: string }> = {
  mcp: { name: "Agent (MCP)", filter: "Agents over MCP", from: "agents over MCP" },
  http: { name: "REST API", filter: "The REST API", from: "the REST API" },
  ui: { name: "This console", filter: "This console", from: "this console" },
};
const clientName = (c: string) => CLIENTS[c]?.name ?? c;

/** "1h" → "1 hour", "15m" → "15 minutes"; anything else as it was sent. */
export function windowWords(w: string) {
  const m = /^(\d+)\s*([smhd])$/.exec(w.trim());
  if (!m) return w;
  const unit = { s: 1e3, m: 6e4, h: 36e5, d: 864e5 }[m[2] as "s" | "m" | "h" | "d"];
  return spanWords(Number(m[1]) * unit);
}

export default function ReadsPage() {
  const { data, error, reload } = usePolling(() => api.queries(LIMIT));
  const [q, setQ] = useState("");
  const [client, setClient] = useState(AGENTS);

  // Agents first (always offered: it is the default), then every other client the log holds.
  const options = useMemo(() => {
    const seen = Array.from(new Set([AGENTS, ...(data ?? []).map((r) => r.client)]));
    return [
      ...seen.map((c) => ({ value: c, label: CLIENTS[c]?.filter ?? c })),
      { value: ALL, label: "Every client" },
    ];
  }, [data]);

  const shown = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return (data ?? []).filter((r) =>
      (client === ALL || r.client === client) &&
      (!needle || r.key.toLowerCase().includes(needle) || clientName(r.client).toLowerCase().includes(needle)));
  }, [data, q, client]);

  return (
    <div className="grid min-w-0 gap-5" data-testid="reads-page">
      <header className="min-w-0">
        <h1 className="m-0 mb-1.5 text-[22px] md:text-[28px]">Reads</h1>
        <p className="m-0 max-w-2xl text-sm text-muted-foreground">
          Every read an agent made over MCP, a script made over the REST API, or this console made, newest
          first. The last {formatCount(LIMIT)}.
        </p>
      </header>

      {error && !data && (
        <Banner tone="error" data-testid="reads-failed"
                action={<Button variant="secondary" onClick={reload}>Try again</Button>}>
          Tares could not list its reads: {error}
        </Banner>
      )}
      {!data && !error && <SkeletonRows count={6} data-testid="reads-loading" />}
      {data && !data.length && (
        <EmptyState title="No reads yet" data-testid="reads-empty">
          When an agent reads over MCP or the REST API, the read shows up here.{" "}
          <Link to="/connect">Connect an agent</Link>.
        </EmptyState>
      )}
      {data && data.length > 0 && (
        <>
          <div className="grid min-w-0 gap-3 sm:grid-cols-[minmax(0,20rem)_minmax(0,14rem)] sm:items-end">
            <Input label="Search the reads" placeholder="What was read, or the client" value={q}
                   onChange={(e) => setQ(e.target.value)} data-testid="reads-search" />
            <Select label="Client" value={client} onValueChange={setClient} options={options}
                    data-testid="reads-client" />
          </div>
          {shown.length ? (
            <ListSection paper title="Newest first"
                         aside={<span data-testid="reads-count">{formatCount(shown.length)} of {formatCount(data.length)}</span>}>
              {shown.map((r) => <ReadRow key={r.id} row={r} />)}
            </ListSection>
          ) : (
            <EmptyState data-testid="reads-none">
              No reads from {client === ALL ? "any client" : CLIENTS[client]?.from ?? client}
              {q.trim() && <> matching “{q.trim()}”</>} in the last {formatCount(data.length)}.
              {client === AGENTS && <> <Link to="/connect">Connect an agent</Link>, or pick every client.</>}
            </EmptyState>
          )}
        </>
      )}
    </div>
  );
}

function ReadRow({ row }: { row: QueryLogEntry }) {
  const stats = row.id.startsWith("s_");
  return (
    <ListRow data-testid="reads-row"
             className="grid grid-cols-1 items-start gap-x-5 gap-y-1 md:grid-cols-[9rem_8rem_minmax(0,1fr)_7rem]">
      {/* An instant: its age in words, as Explore says it, and the UTC stamp on hover. */}
      <span className="whitespace-nowrap text-[13px] text-muted-foreground" data-testid="reads-when"
            title={formatStampUTC(row.queried_at)}>
        {ageWords(Date.now() - Date.parse(row.queried_at))}
      </span>
      <span className="text-[13px] text-foreground" data-testid="reads-client-name">{clientName(row.client)}</span>
      <div className="grid min-w-0 gap-0.5">
        <span className="font-mono text-[13px] text-foreground [overflow-wrap:anywhere]" data-user-text
              data-testid="reads-what">{row.key}</span>
        <span className="text-xs text-muted-foreground" data-testid="reads-meta">
          {stats ? "Stats" : "A read"} over {windowWords(row.window)}
        </span>
      </div>
      <span className="text-[13px] text-muted-foreground md:text-right" data-testid="reads-returned">
        {formatCount(row.rows_returned)} returned
      </span>
    </ListRow>
  );
}
