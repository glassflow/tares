import * as React from "react";
import { Link, useNavigate } from "react-router-dom";

import { api, ApiError } from "../../api";
import { usePolling } from "../../components/bits";
import {
  Banner, Button, Collapse, Dialog, EmptyState, ListRow, ListSection, LoadingBlocks, StatusLabel, toneOf,
} from "../../components/edition";
import { formatCount } from "../../lib/number";
import { ageWords, formatStampUTC } from "../../lib/time";
import type { ProjectObjectKind, ProjectSummary } from "../../types";

// What a factory cell shows for a project that is not a factory project (TR-1396): one made through the
// API without a kind, or before the cell became a factory cell, and a draft still being planned. The
// factory pages answer only for factory projects. This page says what the project is and what it holds,
// and keeps the two things a person may still need here: pause it, or delete it.

const POLL_MS = 15000;

// The project's parts in the factory edition's words (WORDS.md: a source is a connection, a trigger a rule).
const KINDS: Record<ProjectObjectKind, { one: string; order: number }> = {
  source: { one: "Connection", order: 0 },
  trigger: { one: "Rule", order: 1 },
  agent: { one: "Agent", order: 2 },
  mcp_server: { one: "MCP server", order: 3 },
};

const STATUS_WORDS: Record<ProjectSummary["status"], string> = {
  active: "On",
  paused: "Paused",
  error: "Failing",
  draft: "Being planned",
  done: "Finished",
};

export default function OtherProject({ id }: { id: string }) {
  const { data: s, error, reload } = usePolling(() => api.projectSummary(id), POLL_MS);
  const [failed, setFailed] = React.useState<unknown>();
  const [missing, setMissing] = React.useState(false);
  // usePolling hands back the message only; one read of our own tells a missing project from a failure
  React.useEffect(() => {
    if (!error || s) return;
    let live = true;
    api.projectSummary(id).then(
      () => live && reload(),
      (e) => live && (setMissing(e instanceof ApiError && e.status === 404), setFailed(e)),
    );
    return () => { live = false; };
  }, [error, s, id]); // eslint-disable-line react-hooks/exhaustive-deps

  if (!s || s.id !== id) {
    if (missing) {
      return (
        <EmptyState title="No project at this address" data-testid="other-missing"
                    action={<Button asChild variant="secondary"><Link to="/projects">See every project</Link></Button>}>
          It may have been deleted, or the link is mistyped.
        </EmptyState>
      );
    }
    if (error && failed !== undefined) {
      return (
        <Banner tone="error" data-testid="other-failed"
                action={<Button variant="secondary" onClick={() => { setFailed(undefined); reload(); }}>Try again</Button>}>
          Tares could not read this project: {error}
        </Banner>
      );
    }
    return <LoadingBlocks data-testid="other-loading" />;
  }
  return <OtherProjectPage s={s} id={id} reload={reload} stale={error} />;
}

function OtherProjectPage({ s, id, reload, stale }: {
  s: ProjectSummary; id: string; reload: () => void; stale?: string;
}) {
  const navigate = useNavigate();
  const [busy, setBusy] = React.useState(false);
  const [problem, setProblem] = React.useState<string>();
  const [confirmDelete, setConfirmDelete] = React.useState(false);

  const parts = [...(s.objects ?? [])].sort((a, b) =>
    (KINDS[a.kind]?.order ?? 9) - (KINDS[b.kind]?.order ?? 9) || a.name.localeCompare(b.name));
  const connections = parts.filter((o) => KINDS[o.kind]?.one === "Connection").length;

  const act = async (fn: () => Promise<unknown>, after?: () => void) => {
    setBusy(true); setProblem(undefined);
    try { await fn(); if (after) after(); else reload(); }
    // a failed delete closes its dialog, so the banner saying why is not left behind it
    catch (e) { setProblem(String((e as Error).message ?? e)); setConfirmDelete(false); }
    setBusy(false);
  };

  return (
    <div className="grid min-w-0 max-w-3xl gap-5" data-testid="other-project">
      <header className="grid min-w-0 gap-1.5">
        <div className="flex min-w-0 flex-wrap items-baseline gap-x-3 gap-y-1">
          <h1 className="m-0 text-[22px] [overflow-wrap:anywhere] md:text-[28px]" data-user-text data-testid="other-name">
            {s.name}
          </h1>
          <StatusLabel tone={toneOf("projectRun", s.status)} data-testid="other-status">{STATUS_WORDS[s.status] ?? s.status}</StatusLabel>
        </div>
        <p className="m-0 text-sm text-muted-foreground" data-testid="other-what">
          Not a software factory project: it has no tickets, crew or decisions.
          {s.lead && <> Led by <span data-user-text>{s.lead.name}</span>.</>}
          {" "}<span title={formatStampUTC(s.created_at)}>Made {ageWords(Date.now() - Date.parse(s.created_at))}</span>.
        </p>
      </header>

      <Collapse>{stale && <Banner tone="warn" data-testid="other-stale">Showing what Tares read last; the latest read failed: {stale}</Banner>}</Collapse>
      <Collapse>{problem && <Banner tone="error" data-testid="other-problem">{problem}</Banner>}</Collapse>

      {s.goal && (
        <section className="grid min-w-0 gap-1" data-testid="other-goal">
          <h2 className="m-0 text-[15px]">What it is for</h2>
          <p className="m-0 text-sm [overflow-wrap:anywhere]" data-user-text>{s.goal}</p>
        </section>
      )}

      <ListSection paper title="What it holds" data-testid="other-parts"
                   aside={parts.length > 0 && <span data-testid="other-count">{formatCount(parts.length)}</span>}>
        {parts.length ? parts.map((o) => (
          <ListRow key={`${o.kind}/${o.name}`} data-testid="other-part"
                   className="grid grid-cols-[7rem_minmax(0,1fr)] items-baseline gap-x-4 gap-y-0.5">
            <span className="text-[13px] text-muted-foreground" data-testid="other-part-kind">{KINDS[o.kind]?.one ?? o.kind}</span>
            <span className="min-w-0">
              <span className="font-mono text-[13px] [overflow-wrap:anywhere]" data-user-text data-testid="other-part-name">{o.name}</span>
              {o.missing && <span className="ml-2 text-xs text-muted-foreground" data-testid="other-part-missing">deleted outside the project</span>}
            </span>
          </ListRow>
        )) : (
          <EmptyState data-testid="other-empty">It holds no connections, rules, agents or MCP servers.</EmptyState>
        )}
      </ListSection>

      {/* the Default project holds whatever was made outside a project; it is never paused or deleted */}
      {!s.default && (
        <div className="flex flex-wrap gap-2" data-testid="other-actions">
          {s.status === "paused" ? (
            <Button variant="secondary" busy={busy} onClick={() => act(() => api.resumeProject(id))} data-testid="other-resume">
              Turn back on
            </Button>
          ) : s.status !== "done" && (
            <Button variant="secondary" busy={busy} onClick={() => act(() => api.pauseProject(id))} data-testid="other-pause">
              Pause
            </Button>
          )}
          <Dialog open={confirmDelete} onOpenChange={setConfirmDelete} data-testid="other-delete-dialog"
                  title={`Delete ${s.name}?`}
                  description={connections
                    ? "Its rules, agents and MCP servers go with it. Its connections stay, for other projects to use."
                    : "Its rules, agents and MCP servers go with it."}
                  trigger={<Button variant="quiet" data-testid="other-delete">Delete project</Button>}
                  footer={<>
                    <Button variant="secondary" onClick={() => setConfirmDelete(false)}>Keep it</Button>
                    <Button busy={busy} data-testid="other-delete-confirm"
                            onClick={() => act(() => api.deleteProject(id), () => navigate("/projects", { replace: true }))}>
                      Delete project
                    </Button>
                  </>} />
        </div>
      )}
    </div>
  );
}
