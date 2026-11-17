import { useEffect, useState, type ReactNode } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";

import { api } from "../api";
import IngestSetup from "../components/IngestSetup";
import { useProjectName } from "../components/ProjectBadge";
import SourceForm from "../components/SourceForm";
import { Banner, Button, ListRow, ListSection, LoadingBlocks, Panel, Textarea } from "../components/edition";
import { copyText } from "../editions/copyText";
import type { ConnectorSpec } from "../types";

type Created = { name: string; key: string; authKey?: string; keyErr?: string };

// What each kind does, in page words. The daemon's descriptions (tares/connectors SPECS) are written
// for the MCP planner and the CLI, in its own terms, so the page says it here and leaves the daemon
// alone, as SourceForm's HELP does for fields. A kind missing from this map falls back to the daemon's
// text, marked as system text; source-new.e2e.cjs walks /api/connectors and fails when an offered kind
// has no entry, so the fallback only shows between a new kind landing and its words.
const KIND_HELP: Record<string, string> = {
  prometheus: "Asks Prometheus your PromQL queries on every check and keeps each answer.",
  docker_logs: "Follows a running container's logs, every line or only the lines that match a pattern.",
  loki: "Reads a LogQL stream selector from Loki, Grafana Cloud included: one event per log line, with its labels.",
  prometheus_alerts: "Reads the alerts Prometheus's own rules raise: an event while one is active and one when it clears. No Alertmanager needed.",
  alertmanager: "Alertmanager sends its alerts here: one event per alert it routes, with its grouping, silences and status.",
  reference: "Documents such as project notes, schemas and runbooks (JSON, CSV, Markdown or text), shown whenever an agent looks at what they are labelled with.",
  webhook: "Anything that can post JSON or NDJSON sends it here. Every event is kept whole; labels pick out the fields that matter.",
  memory: "What agents note for later with the remember tool. Rules and agents read it like any other connection.",
  otlp: "OpenTelemetry logs, traces and metrics over OTLP/HTTP. One connection takes every service; service.name becomes a label.",
  github: "Checks one repository with a personal token: its commits and the pull requests opened, merged or closed. For everything in every repository as it happens, install the GitHub App under Settings.",
  vercel: "A Vercel log drain sends its logs here: one event per log entry, labelled by project and environment.",
  http_poll: "Calls any JSON endpoint on a schedule and keeps one event per item: a status page, a weather service, your own API. Discover reads its fields for you.",
  postgres: "Reads new and changed rows from a table, by an id or updated_at column, so your application's own data is on the timeline.",
  claude_code: "Follows the Claude Code session transcripts on this machine: one event per message, with repository, branch and model. Secrets are removed before anything is kept.",
};

// The three ways a kind gets its events, in the order a person usually reaches for them.
const MODES: { mode: ConnectorSpec["mode"]; title: string }[] = [
  { mode: "poll", title: "Tares reads it on a schedule" },
  { mode: "push", title: "It sends to Tares" },
  { mode: "reference", title: "Documents you add" },
];

// What the YAML box added, in page words, each a link to its page.
const ADDED: { key: "sources" | "triggers" | "agents" | "mcp_servers"; word: string; to: (n: string) => string }[] = [
  { key: "sources", word: "connection", to: (n) => `/sources/${encodeURIComponent(n)}` }, // words-ok
  { key: "triggers", word: "rule", to: (n) => `/triggers/${encodeURIComponent(n)}` }, // words-ok
  { key: "agents", word: "agent", to: (n) => `/agents/${encodeURIComponent(n)}` },
  { key: "mcp_servers", word: "MCP server", to: () => "/mcp-servers" },
];

// The YAML box's example: the top-level keys are the file format's, not page words.
const YAML_EXAMPLE = "sources:\n  - name: metrics\n    connector: prometheus\n    config: ..."; // words-ok

/** Paste YAML (from the docs, a tutorial, a teammate) and merge it in. Always merge, never replace:
 *  this door is for adding, and it must be impossible to wipe the connections from here. The full
 *  import and export (replace included) is its own page. */
function YamlQuickAdd() {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<ReactNode>();
  const [done, setDone] = useState<{ word: string; name: string; to: string }[]>();

  const run = async () => {
    setBusy(true); setErr(undefined); setDone(undefined);
    try {
      const r = await api.importYaml(text, "merge");
      const added = ADDED.flatMap((a) => (r.names[a.key] ?? []).map((name) => ({ word: a.word, name, to: a.to(name) })));
      if (!added.length) {
        setErr(<>That YAML has nothing to add. Put entries under{" "}
          <code data-words-exempt>sources:</code>, <code data-words-exempt>triggers:</code>,{" "}
          <code data-words-exempt>agents:</code> or <code data-words-exempt>mcp_servers:</code>.</>);
      } else { setDone(added); setText(""); }
    } catch (e) { setErr(<>Tares did not add it: <span data-user-text>{String((e as Error).message ?? e)}</span></>); }
    setBusy(false);
  };

  return (
    <Panel title="Paste YAML" data-testid="yaml-add">
      <div className="grid gap-3">
        {err && <Banner tone="error" data-testid="yaml-error">{err}</Banner>}
        {done && (
          <Banner data-testid="yaml-added">
            Added{" "}
            {done.map((d, i) => (
              <span key={`${d.word}:${d.name}`}>
                {i > 0 && ", "}{d.word} <Link to={d.to} className="font-mono" data-user-text>{d.name}</Link>
              </span>
            ))}.
          </Banner>
        )}
        <Textarea label="Connections, rules or agents as YAML" className="min-h-32 font-mono text-[13px]"
                  hint="Checked first, then added to what you have. Nothing already there is removed."
                  value={text} onChange={(e) => setText(e.target.value)} data-testid="yaml-text"
                  placeholder={YAML_EXAMPLE} />
        <div className="flex flex-wrap items-center gap-2">
          <Button onClick={() => void run()} busy={busy} disabled={!text.trim()} data-testid="yaml-submit">Add them</Button>
          <Button variant="quiet" asChild><Link to="/sources/import">Import or export a file</Link></Button>
        </div>
      </div>
    </Panel>
  );
}

export default function SourceNew() {
  const nav = useNavigate();
  // ?project=<id>: the connection joins that project (from a project's page); none = the Default project
  // ?kind=<connector>: the kind picked, so the back button returns from its form to the picker.
  const [params] = useSearchParams();
  const project = params.get("project") ?? undefined;
  const connector = params.get("kind") ?? undefined;
  const projectName = useProjectName(project)?.name;
  const [specs, setSpecs] = useState<Record<string, ConnectorSpec>>();
  const [specsErr, setSpecsErr] = useState<string>();
  const [created, setCreated] = useState<Created>();
  // Host capabilities gate local-only kinds: docker_logs shells out to `docker logs` on the Tares
  // host, so on a hosted cell (no Docker socket) it can only ever receive nothing. Enabled until
  // known false, matching Sources.tsx.
  const [caps, setCaps] = useState<{ discover_docker: boolean }>();
  // Auth mode. When on, a push producer needs a key in the header, so one is made for the connection
  // at creation (shown once). When off, the address alone is enough.
  const [authOn, setAuthOn] = useState(false);
  const [showYaml, setShowYaml] = useState(false);

  useEffect(() => {
    api.connectors().then(setSpecs, (e) => setSpecsErr(String((e as Error).message ?? e)));
    api.capabilities().then(setCaps, () => {});
    api.health().then((h) => setAuthOn(h.auth_required)).catch(() => {});
  }, []);

  const unavailable = (key: string) => key === "docker_logs" && caps?.discover_docker === false;
  // `internal` kinds are made by Tares itself (agent findings, ticket sync) and `credential_managed`
  // ones with their credential (the GitHub App): nothing to set up here, even by typing the address.
  const offered = Object.entries(specs ?? {}).filter(([, s]) => !s.internal && !s.credential_managed);
  const spec = connector && !unavailable(connector) ? offered.find(([k]) => k === connector)?.[1] : undefined;
  const picking = !spec && !created;
  // This page's address with another kind (or none), keeping the project.
  const pageFor = (kind?: string) => {
    const q = new URLSearchParams();
    if (project) q.set("project", project);
    if (kind) q.set("kind", kind);
    return q.size ? `/sources/new?${q}` : "/sources/new";
  };

  return (
    <div className="grid min-w-0 gap-5" data-testid="add-connection-page">
      <header className="flex flex-wrap items-end justify-between gap-x-6 gap-y-3">
        <div className="min-w-0">
          <h1 className="m-0 mb-1.5 text-[22px] md:text-[28px]">Add a connection</h1>
          <p className="m-0 max-w-2xl text-sm text-muted-foreground">
            Pick what to connect, fill in its settings and save. Tares starts on it at once.
          </p>
          {project && (
            <p className="m-0 mt-1.5 text-sm text-muted-foreground" data-testid="add-connection-project">
              It joins <Link to={`/projects/${encodeURIComponent(project)}`} data-user-text>{projectName ?? project}</Link>.
            </p>
          )}
        </div>
        {picking && (
          <Button variant="secondary" aria-expanded={showYaml} onClick={() => setShowYaml((v) => !v)} data-testid="yaml-toggle">
            {showYaml ? "Hide the YAML box" : "Paste YAML"}
          </Button>
        )}
      </header>

      {picking && showYaml && <YamlQuickAdd />}

      {specsErr && <Banner tone="error" data-testid="kinds-error">Tares could not list what it connects to: <span data-user-text>{specsErr}</span></Banner>}
      {!specs && !specsErr && <LoadingBlocks />}
      {specs && connector && !spec && (
        <Banner tone="warn" data-testid="kind-unknown">
          Tares cannot add a <span className="font-mono" data-user-text>{connector}</span> connection here. Pick one of the kinds below.
        </Banner>
      )}

      {specs && picking && MODES.map(({ mode, title }) => {
        const kinds = offered.filter(([, s]) => s.mode === mode);
        if (!kinds.length) return null;
        return (
          <ListSection paper key={mode} title={title} data-testid={`kinds-${mode}`}>
            {kinds.map(([key, s]) => (
              <KindRow key={key} kind={key} spec={s} unavailable={unavailable(key)}
                       to={key === "claude_code" ? "/sources/claude-code" : pageFor(key)} />
            ))}
          </ListSection>
        );
      })}

      {spec && created && (
        <CreatedPanel created={created} connector={connector!} authOn={authOn}
                      onAnother={() => { setCreated(undefined); nav(pageFor()); }} />
      )}

      {spec && !created && (
        <Panel title={spec.label} data-testid="add-connection-kind"
               action={<Button variant="quiet" asChild><Link to={pageFor()} data-testid="pick-another">Pick another kind</Link></Button>}>
          <div className="grid gap-4">
            <KindHelp kind={connector!} spec={spec} />
            <SourceForm
              connector={connector!}
              spec={spec}
              submitLabel="Create connection"
              onSubmit={async (body) => {
                const res = await api.createSource(project ? { ...body, project } : body);
                if (spec.mode !== "push") {
                  nav(`/sources/${body.name}`);
                  return;
                }
                // A cell that requires keys: make this producer its own key (shown once). Failing to
                // make one undoes nothing; the connection exists, so say why and where to make one by
                // hand. An open cell needs no key.
                let authKey: string | undefined, keyErr: string | undefined;
                if (authOn) {
                  try {
                    const k = await api.createKey(`ingest: ${body.name}`, ["ingest"]); // words-ok
                    authKey = k.secret;
                  } catch (e) { keyErr = String((e as Error).message ?? e); }
                }
                setCreated({ name: body.name, key: res.ingest_key || body.name, authKey, keyErr });
              }}
            />
          </div>
        </Panel>
      )}
    </div>
  );
}

function KindHelp({ kind, spec }: { kind: string; spec: ConnectorSpec }) {
  const help = KIND_HELP[kind];
  return help
    ? <span className="block text-sm text-muted-foreground" data-help="page">{help}</span>
    : <span className="block text-sm text-muted-foreground" data-help="daemon" data-words-exempt>{spec.description}</span>;
}

// A row is a link to this page with the kind picked (or, for Claude Code, to its own setup page). A
// kind the cell cannot run is a plain row that says why, with nothing to click.
function KindRow({ kind, spec, unavailable, to }: {
  kind: string; spec: ConnectorSpec; unavailable: boolean; to: string;
}) {
  const body = (
    <span className="grid min-w-0 gap-1">
      <span className="font-medium">{spec.label}</span>
      <KindHelp kind={kind} spec={spec} />
      {unavailable && (
        <span className="text-[13px] text-muted-foreground" data-testid="kind-unavailable">
          Needs Docker on the machine Tares runs on, which this one does not have.
        </span>
      )}
    </span>
  );
  return unavailable
    ? <ListRow aria-disabled="true" className="text-muted-foreground hover:bg-transparent" data-testid="connection-kind" data-kind={kind}>{body}</ListRow>
    : <ListRow to={to} className="text-foreground" data-testid="connection-kind" data-kind={kind}>{body}</ListRow>;
}

// A push connection was just made: its address (and key, on a cell that requires one) right here,
// so it can be pasted into the producer (a Vercel drain, Alertmanager) without hunting for it.
function CreatedPanel({ created, connector, authOn, onAnother }: {
  created: Created; connector: string; authOn: boolean; onAnother: () => void;
}) {
  // Built from the browser's own origin, so it is right wherever the console is served. OTLP uses the
  // /v1/* endpoints (the connection is chosen by header), not a path key.
  const origin = window.location.origin;
  const url = connector === "otlp" ? `${origin}/v1/logs` : `${origin}/ingest/${created.key}`; // words-ok
  return (
    <Panel data-testid="connection-created">
      <div className="grid gap-4">
        <h2 className="m-0 text-[17px]">Added <span className="font-mono" data-user-text>{created.name}</span></h2>
        <div className="grid gap-2">
          <p className="m-0 text-sm">Point your producer at this address:</p>
          <CopyLine text={url} what="address" data-testid="created-url" />
          <p className="m-0 text-[13px] text-muted-foreground">
            {connector === "otlp"
              ? <>Point an OTLP/HTTP exporter here (also <code data-words-exempt>/v1/traces</code> and <code data-words-exempt>/v1/metrics</code>).</>
              : "It takes JSON or NDJSON, and events arrive as soon as the producer posts."}
          </p>
        </div>

        {created.authKey && (
          <div className="grid gap-2 border border-border bg-muted px-4 py-3.5" role="status" data-testid="created-key">
            <p className="m-0 text-sm">
              <b>This cell requires a key, so the connection got its own.</b> Copy it now, it is not shown again.
              Under <Link to="/settings">Settings</Link> it is listed as <code data-words-exempt>ingest: {created.name}</code>.
            </p>
            <CopyLine text={created.authKey} what="key" data-testid="created-key-secret" />
          </div>
        )}
        {created.keyErr && (
          <Banner tone="error" data-testid="created-key-error">
            The connection was added, but Tares could not make its key: <span data-user-text>{created.keyErr}</span>.
            Make one that can send data under <Link to="/settings">Settings</Link>.
          </Banner>
        )}
        {!authOn && (
          <p className="m-0 text-[13px] text-muted-foreground" data-testid="created-open">
            This cell is open, so no key is needed and the address alone takes events.
            Start Tares with <code data-words-exempt>tares up --auth</code> to require a key for each producer.
          </p>
        )}

        <IngestSetup connector={connector} url={url} authKey={created.authKey} />

        <div className="flex flex-wrap gap-2">
          <Button asChild><Link to={`/sources/${encodeURIComponent(created.name)}`} data-testid="created-open-link">Open the connection</Link></Button>
          <Button variant="secondary" onClick={onAnother} data-testid="created-another">Add another</Button>
        </div>
      </div>
    </Panel>
  );
}

// A literal to paste somewhere else, with its copy button. A failed copy says so: the text stays
// selectable, so it can still be copied by hand.
function CopyLine({ text, what, ...props }: { text: string; what: string; "data-testid"?: string }) {
  const [copy, setCopy] = useState<"idle" | "copied" | "failed">("idle");
  const doCopy = async () => {
    try { await copyText(text); setCopy("copied"); setTimeout(() => setCopy("idle"), 1500); }
    catch { setCopy("failed"); }
  };
  return (
    <div className="grid gap-1.5">
      <div className="flex min-w-0 flex-wrap items-start gap-2">
        <code data-words-exempt {...props}
              className="min-w-0 basis-full border border-border bg-background px-3 py-2 font-mono text-[13px] [overflow-wrap:anywhere] sm:basis-0 sm:flex-1">
          {text}
        </code>
        <Button variant="secondary" onClick={() => void doCopy()}>{copy === "copied" ? "Copied" : `Copy ${what}`}</Button>
      </div>
      {copy === "failed" && <p className="m-0 text-[13px] text-err" role="alert">Could not copy. Select the {what} and copy it by hand.</p>}
    </div>
  );
}
