import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import { api } from "../api";
import { usePolling } from "../components/bits";
import {
  Banner, Button, Checkbox, EmptyState, Input, ListRow, ListSection, LoadingBlocks, StatusLabel, Tabs, toneOf,
} from "../components/edition";
import { toolTitle } from "../components/setup/planEdit";
import { useEdition } from "../editions/edition";
import { ageWords } from "../lib/time";
import type { ResourceKind, ResourceRow } from "../types";

// All resources (TR-351): every part on the cell, one list per kind, each row with the projects that
// use it. Read-only: it is where you check what is on the cell and who uses what.
//
// The kind is in the address (/resources/<kind>), so a tab is a link someone can share. The kind keys
// are the API's and the address's, not page words; what a person reads is KINDS below.

type Words = { label: string; what: string; last?: string };
const KINDS: { kind: ResourceKind; factory: Words; sre?: Partial<Words> }[] = [
  { kind: "sources", factory: { label: "Connections", what: "Where events come from.", last: "last event" } }, // words-ok
  { kind: "triggers", // words-ok
    factory: { label: "Rules", what: "What happens that wakes the agents.", last: "last woke the agents" },
    sre: { label: "Check rules" } },
  { kind: "agents", factory: { label: "Agents", what: "The Tares agents that look when a project wakes.", last: "last worked" } },
  { kind: "tools", factory: { label: "Tools", what: "Outside services the agents can call, through MCP servers." } },
  { kind: "skills", factory: { label: "Skills", what: "Know-how the agents read." } },
  { kind: "keys", factory: { label: "Keys", what: "Keys for agents and services outside Tares.", last: "last used" } },
];

// What a person reads for each state the daemon sends. The tone stays keyed by the daemon's value.
const STATE_WORDS: Record<string, string> = {
  error: "failing",
  active: "on",
  set: "credentials set",
};
const stateWords = (st: string) => STATE_WORDS[st] ?? st;

const enc = encodeURIComponent;

/** The row's plain title (a connection's repo, a tool's service), or null when the name is all there is. */
function plainTitle(kind: ResourceKind, r: ResourceRow): string | null {
  const t = r.title ?? (kind === "tools" && r.url ? toolTitle(r.url).title : null);
  return t && t !== r.name ? t : null;
}

/** Where a row opens: the part's own page, in the first project that uses it when it has none.
 *  `view=` is the project page's address parameter, not a page word. */
function linkFor(kind: ResourceKind, r: ResourceRow): string | null {
  const p = r.used_by[0]?.id;
  switch (kind) {
    case "sources": return `/sources/${enc(r.name)}`; // words-ok
    case "triggers": return `/triggers/${enc(r.name)}`; // words-ok
    case "agents": return `/agents/${enc(r.name)}`;
    case "tools": return p ? `/projects/${enc(p)}?view=settings:mcp` : null; // words-ok
    case "skills": return p ? `/projects/${enc(p)}?view=skill:${enc(r.name)}` : null; // words-ok
    case "keys": return p ? `/projects/${enc(p)}?view=settings:keys` : "/settings"; // words-ok
  }
}

/** When it last did something, per kind. */
function lastAt(kind: ResourceKind, r: ResourceRow): string | null {
  const ts = kind === "sources" ? r.last_event_at : kind === "triggers" ? r.last_fired_at // words-ok
    : kind === "agents" ? r.last_run_at : kind === "keys" ? r.last_used_at : null;
  const t = ts ? Date.parse(ts) : NaN;
  return Number.isFinite(t) ? ageWords(Date.now() - t) : null;
}

export default function Resources() {
  const { kind: raw } = useParams();
  const navigate = useNavigate();
  const { edition } = useEdition();
  const kind: ResourceKind = (KINDS.find((k) => k.kind === raw)?.kind) ?? "sources"; // words-ok
  const { data, error, reload } = usePolling(() => api.resources(), 15000);
  const [filter, setFilter] = useState("");
  const [loose, setLoose] = useState(false);

  const wordsOf = (k: (typeof KINDS)[number]): Words => ({ ...k.factory, ...(edition === "sre" ? k.sre : {}) });
  const words = wordsOf(KINDS.find((k) => k.kind === kind)!);
  const all = data?.[kind] ?? [];
  const needle = filter.trim().toLowerCase();
  const rows = all.filter((r) =>
    (!loose || r.used_by.length === 0)
    && (!needle || `${r.name} ${plainTitle(kind, r) ?? ""} ${r.what} ${r.used_by.map((u) => u.name).join(" ")}`
      .toLowerCase().includes(needle)));
  const rules = edition === "sre" ? "check rule" : "rule";

  const list = data && (
    <div className="grid min-w-0 gap-4 pt-4">
      <p className="m-0 text-sm text-muted-foreground" data-testid="resources-what">{words.what}</p>
      {all.length === 0 ? (
        <EmptyState title={`No ${words.label.toLowerCase()} yet`} data-testid="resources-empty">
          Nothing of this kind is on this Tares yet.
        </EmptyState>
      ) : (
        <>
          <div className="grid items-end gap-3 md:grid-cols-[minmax(0,24rem)_auto]">
            <Input label="Search" placeholder="Name, what it is, or a project" value={filter}
                   onChange={(e) => setFilter(e.target.value)} data-testid="resources-search" />
            <Checkbox label="Only those in no project" checked={loose}
                      onCheckedChange={(v) => setLoose(v === true)} data-testid="resources-loose" />
          </div>
          <ListSection paper title={words.label}
                       aside={<span data-testid="resources-count">{rows.length} of {all.length}</span>}>
            {rows.map((r, i) => <Row key={`${r.name}:${i}`} kind={kind} r={r} last={words.last} />)}
            {!rows.length && <EmptyState data-testid="resources-none">Nothing matches.</EmptyState>}
          </ListSection>
        </>
      )}
    </div>
  );

  return (
    <div className="grid min-w-0 gap-5" data-testid="resources-page">
      <header className="min-w-0">
        <h1 className="m-0 mb-1.5 text-[22px] md:text-[28px]">All resources</h1>
        <p className="m-0 max-w-2xl text-sm text-muted-foreground">
          Every connection, {rules} and agent on this Tares, and the projects that use each.
        </p>
      </header>

      {error && (
        <Banner tone="error" action={<Button variant="secondary" onClick={reload}>Try again</Button>}>
          Tares is not answering: {error}
        </Banner>
      )}
      {!data && !error && <LoadingBlocks />}

      {data && (
        // Manual activation: each tab is an address, so arrowing across them only moves focus, and a
        // click is one history entry (automatic activation calls back on mousedown and again on focus).
        <Tabs aria-label="Kinds of resource" value={kind} activationMode="manual"
              onValueChange={(v) => { setFilter(""); setLoose(false); navigate(`/resources/${v}`); }}
              items={KINDS.map((k) => ({
                value: k.kind,
                label: `${wordsOf(k).label} (${data[k.kind].length})`,
                content: k.kind === kind ? list : null,
              }))} />
      )}
    </div>
  );
}

function Row({ kind, r, last }: { kind: ResourceKind; r: ResourceRow; last?: string }) {
  const to = linkFor(kind, r);
  const title = plainTitle(kind, r);
  const name = title ?? r.name;
  const when = last ? lastAt(kind, r) : null;
  return (
    <ListRow data-testid="resource-row"
             className="grid grid-cols-1 items-start gap-x-6 gap-y-1 md:grid-cols-[minmax(0,1fr)_auto]">
      <div className="grid min-w-0 gap-1">
        <div className="flex min-w-0 flex-wrap items-baseline gap-x-2">
          {to
            ? <Link to={to} className="font-medium text-foreground" data-user-text data-testid="resource-name">{name}</Link>
            : <span className="font-medium text-foreground" data-user-text data-testid="resource-name">{name}</span>}
          {title && <span className="font-mono text-xs text-muted-foreground" data-user-text>{r.name}</span>}
        </div>
        {/* What it is comes from the daemon in its own words (a connector's label, a rule's sentence). */}
        <div className="text-[13px] text-muted-foreground">
          <span data-user-text>{r.what}</span>
          {r.detail && r.state !== "receiving" && <span data-user-text>. {r.detail}</span>}
        </div>
        <div className="text-[13px] text-muted-foreground" data-testid="resource-used-by">
          {r.used_by.length === 0 ? "In no project" : (
            <>In {r.used_by.map((u, j) => (
              <span key={u.id}>{j > 0 && ", "}<Link to={`/projects/${enc(u.id)}`} data-user-text>{u.name}</Link></span>
            ))}</>
          )}
        </div>
      </div>
      <div className="grid gap-1 text-[13px] md:justify-items-end md:text-right">
        <StatusLabel tone={toneOf("resource", r.state)} data-testid="resource-state">{stateWords(r.state)}</StatusLabel>
        {last && <span className="text-muted-foreground" data-testid="resource-last">{last} {when ?? "never"}</span>}
      </div>
    </ListRow>
  );
}
