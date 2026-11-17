import { useEffect, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { api, auth } from "../api";
import { usePolling } from "../components/bits";
import {
  Banner, Button, EmptyState, Input, ListRow, ListSection, Panel, Select, SkeletonLines, SkeletonRows, Tabs,
} from "../components/edition";
import { copyText } from "../editions/copyText";
import { useEdition } from "../editions/edition";

// Connect (TR-1395): how an agent of your own hooks up to this Tares, one tab per way in. The tab keys
// are the address's (/connect?tab=push is linked from a project's Subscribers), not page words.
type ConnectTab = "mcp" | "push" | "rest" | "tools";
const TABS: { value: ConnectTab; label: string }[] = [
  { value: "mcp", label: "MCP" },
  { value: "push", label: "Webhook" },
  { value: "rest", label: "REST API" },
  { value: "tools", label: "Tools" },
];

// The MCP tab is a client picker, mirroring the docs page (docs.glassflow.ai/tares/agents): the person
// picks their client and gets the exact command with this Tares's endpoint and token filled in.
type McpClient = "Claude Code" | "Codex CLI" | "Cursor" | "Claude Desktop" | "Other (JSON)" | "stdio";
const MCP_CLIENTS: { value: McpClient; label: string }[] = [
  { value: "Claude Code", label: "Claude Code" },
  { value: "Codex CLI", label: "Codex CLI" },
  { value: "Cursor", label: "Cursor" },
  { value: "Claude Desktop", label: "Claude Desktop" },
  { value: "Other (JSON)", label: "Another client (JSON)" },
  { value: "stdio", label: "On this machine (stdio)" },
];

/** A name off the wire (a scope, a tool, a path): not a page word. */
const Code = ({ children }: { children: string }) => (
  <code className="font-mono text-[12.5px]" data-words-exempt>{children}</code>
);

/** A snippet with a copy button. `copy` lets the screen show the masked token (••••••••) while the
 *  button carries the real one: without it a snippet either shows the credential or copies the mask. */
function Snippet({ title, code, copy }: { title: string; code: string; copy?: string }) {
  const [copied, setCopied] = useState(false);
  const onCopy = () => copyText(copy ?? code).then(() => { setCopied(true); setTimeout(() => setCopied(false), 1500); }, () => {});
  return (
    <Panel title={title} data-testid="connect-snippet"
           action={<Button variant="secondary" onClick={onCopy} aria-label={`${copied ? "Copied" : "Copy"}: ${title}`}
                           data-testid="connect-copy">{copied ? "Copied" : "Copy"}</Button>}>
      <pre className="m-0 whitespace-pre-wrap font-mono text-xs text-foreground [overflow-wrap:anywhere]" data-words-exempt data-testid="connect-code">{code}</pre>
    </Panel>
  );
}

const prose = "m-0 max-w-3xl text-sm text-muted-foreground";
// Wire names a person types or reads in the docs, so they stay as the API spells them.
const INGEST_SCOPE = "ingest"; // words-ok
const GUIDE = "docs.glassflow.ai/tares/guides/triggers"; // words-ok

export function ConnectPage() {
  // Only the first tab comes from the address (a project's Subscribers links to ?tab=push); after that
  // the tab is local, so arrowing across the tabs does not fill the history.
  const [params] = useSearchParams();
  const initial = params.get("tab");
  const [tab, setTab] = useState<ConnectTab>(TABS.some((t) => t.value === initial) ? (initial as ConnectTab) : "mcp");
  const { edition } = useEdition();
  const rule = edition === "sre" ? "check rule" : "rule";

  const origin = window.location.origin;
  const [authReq, setAuthReq] = useState<boolean>();
  const [status, setStatus] = useState<"checking" | "live" | "absent">("checking");
  const [url, setUrl] = useState(`${origin}/mcp`);
  const [reveal, setReveal] = useState(false);
  const [mcpClient, setMcpClient] = useState<McpClient>("Claude Code");
  const token = auth.get();
  const tools = usePolling(() => api.mcpTools(), 60000);
  const rules = usePolling(() => api.triggers(), 60000);
  const firstRule = rules.data?.[0]?.name;

  useEffect(() => {
    api.health().then((h) => setAuthReq(h.auth_required)).catch(() => setAuthReq(false));

    // The MCP endpoint sits in one of two places: same host and port behind a reverse proxy (the
    // compose deployment: Caddy routes /mcp to the MCP server), or on its own port when `tares mcp`
    // is a separate process locally (the documented default, :8788).
    const loc = window.location;
    const sameOrigin = `${origin}/mcp`;
    const localPort = `${loc.protocol}//${loc.hostname}:8788/mcp`;

    const probe = async (target: string, ms = 2500): Promise<boolean> => {
      const ctl = new AbortController();
      const timer = setTimeout(() => ctl.abort(), ms);
      try {
        if (target === sameOrigin) {
          // Same origin: a real MCP server answers with something other than HTML; the SPA catch-all
          // answers with HTML.
          const r = await fetch(target, { headers: { Accept: "application/json, text/event-stream" }, signal: ctl.signal });
          return !(r.headers.get("content-type") || "").includes("text/html");
        }
        // Cross origin: the MCP server sends no CORS headers, so the answer is unreadable, but a no-cors
        // fetch that resolves (rather than failing on a refused connection) means something listens there.
        await fetch(target, { mode: "no-cors", signal: ctl.signal });
        return true;
      } catch {
        return false;
      } finally {
        clearTimeout(timer);
      }
    };

    (async () => {
      if (await probe(sameOrigin)) { setUrl(sameOrigin); setStatus("live"); return; }
      if (localPort !== sameOrigin && (await probe(localPort))) { setUrl(localPort); setStatus("live"); return; }
      setStatus("absent");
    })();
  }, [origin]);

  const tok = authReq ? (token || "<your-access-token>") : "";
  const shownTok = authReq && !reveal && token ? "••••••••" : tok;
  const bearer = (t: string) => (authReq ? { headers: { Authorization: `Bearer ${t}` } } : {});

  const claudeCode = (t: string) =>
    `claude mcp add --transport http tares ${url}` + (authReq ? ` --header "Authorization: Bearer ${t}"` : "");
  const codexCmd = (t: string) => authReq
    ? `export TARES_API_KEY=${t}\ncodex mcp add tares --url ${url} --bearer-token-env-var TARES_API_KEY`
    : `codex mcp add tares --url ${url}`;
  const httpJson = (t: string) => JSON.stringify({ mcpServers: { tares: { type: "http", url, ...bearer(t) } } }, null, 2);
  // Cursor and Claude Desktop take the bare-url shape (no "type" field).
  const urlJson = (t: string) => JSON.stringify({ mcpServers: { tares: { url, ...bearer(t) } } }, null, 2);
  const stdioJson = (t: string) => JSON.stringify({
    mcpServers: { tares: { command: "tares-mcp", env: { TARESD_URL: origin, ...(authReq ? { TARES_AUTH_TOKEN: t } : {}) } } },
  }, null, 2);
  const curl = (path: string, t: string, body: string) =>
    `curl -X POST ${origin}${path} \\\n` +
    `  -H 'Content-Type: application/json' \\\n` +
    (authReq ? `  -H 'Authorization: Bearer ${t}' \\\n` : "") +
    `  -d '${body}'`;
  const subscribeCurl = (t: string) =>
    curl("/subscribe", t, `{"trigger": "${firstRule ?? "<trigger>"}", "url": "https://your-agent.example.com/hook"}`); // words-ok
  const readCurl = (t: string) => curl("/read", t, `{"selector": {"service": "<entity>"}, "window": "15m"}`); // words-ok
  /** A snippet that carries the token: masked on screen, real on the clipboard. */
  const withToken = (title: string, make: (t: string) => string) => <Snippet title={title} code={make(shownTok)} copy={make(tok)} />;

  const mcp = (
    <div className="grid min-w-0 gap-4">
      {status === "checking" && <Banner data-testid="connect-endpoint">Looking for the MCP endpoint…</Banner>}
      {status === "live" && (
        <Banner data-testid="connect-endpoint">The MCP endpoint is live at <Code>{url}</Code></Banner>
      )}
      {status === "absent" && (
        <Banner tone="warn" data-testid="connect-endpoint">
          <span>
            No MCP endpoint answers, at <Code>{`${origin}/mcp`}</Code> (behind a proxy) or on port <Code>8788</Code> (on its
            own). Start <Code>tares mcp</Code> beside the daemon (the compose deployment includes it), or pick
            &ldquo;On this machine (stdio)&rdquo; below.
          </span>
        </Banner>
      )}

      {authReq && (
        <Panel title="Your access token" data-testid="connect-token"
               action={
                 <div className="flex flex-wrap gap-2">
                   {token && <Button variant="quiet" onClick={() => setReveal((r) => !r)}>{reveal ? "Hide" : "Show"}</Button>}
                   {token && <Button variant="secondary" onClick={() => { copyText(token).catch(() => {}); }}>Copy token</Button>}
                 </div>
               }>
          <div className="grid gap-2">
            <code className="font-mono text-[13px] text-foreground [overflow-wrap:anywhere]" data-words-exempt
                  data-testid="connect-token-value">{shownTok}</code>
            <p className={prose}>
              It works in the snippets below, but it carries full admin rights. For an agent, make a key with
              the <Code>read</Code> scope in <Link to="/settings">Settings</Link> (add <Code>{INGEST_SCOPE}</Code> if it
              also writes memories) and paste it in place of the token.
            </p>
          </div>
        </Panel>
      )}

      <p className={prose}>
        The agent connects as an MCP client and reads when it needs to (<Code>read</Code>, <Code>query</Code>,{" "}
        <Code>catalog_*</Code>): one correlated timeline per call, instead of a search across every system. It
        needs a key with the <Code>read</Code> scope.
      </p>
      <div className="max-w-xs">
        <Select label="Your client" value={mcpClient} onValueChange={(v) => setMcpClient(v as McpClient)}
                options={MCP_CLIENTS} data-testid="connect-client" />
      </div>

      {mcpClient === "Claude Code" && (
        <>
          {withToken("Paste in your terminal", claudeCode)}
          <p className={prose}>
            Or install the{" "}
            <a href="https://docs.glassflow.ai/tares/connectors/claude-code" target="_blank" rel="noreferrer">Tares plugin</a>{" "}
            instead: the same connection, and it also records Claude Code sessions into Tares.
          </p>
          <Snippet title="Install the plugin, inside Claude Code" code={"/plugin marketplace add glassflow/tares\n/plugin install tares@tares"} />
        </>
      )}
      {mcpClient === "Codex CLI" && withToken("Paste in your terminal (keep --url: without it Codex adds a stdio server)", codexCmd)}
      {mcpClient === "Cursor" && withToken("Add to ~/.cursor/mcp.json (or .cursor/mcp.json in a project), then approve it under Settings → MCP", urlJson)}
      {mcpClient === "Claude Desktop" && withToken("Add to claude_desktop_config.json (Settings → Developer → Edit Config), then restart", urlJson)}
      {mcpClient === "Other (JSON)" && (
        <>
          {withToken("The shape most clients accept", httpJson)}
          <p className={prose}>
            Each client&rsquo;s config file and field names (VS Code, Windsurf, Zed, Gemini CLI and more):{" "}
            <a href="https://docs.glassflow.ai/tares/agents" target="_blank" rel="noreferrer">docs.glassflow.ai/tares/agents</a>.
          </p>
        </>
      )}
      {mcpClient === "stdio" && (
        <>
          <p className={prose}>
            For an agent on the same machine, or when no <Code>/mcp</Code> endpoint answers. It needs{" "}
            <Code>pip install tares</Code> where the agent is, and passes everything on to this daemon.
          </p>
          {withToken("MCP config for stdio", stdioJson)}
        </>
      )}

      <p className={prose}>
        Then check it works: ask the client &ldquo;Use tares: what are you receiving right now?&rdquo; It should
        call <Code>catalog_list</Code> and answer with your connections, {rule}s and projects.
      </p>
    </div>
  );

  const push = (
    <div className="grid min-w-0 gap-4">
      <p className={prose}>
        Turn it around: Tares watches and the agent waits. When a {rule}&rsquo;s condition is met, Tares sends
        the correlated timeline to an address your agent listens on, so the work starts with the evidence
        attached.
      </p>

      <h2 className="m-0 mt-2 text-[17px]">1. Give your agent an address</h2>
      <p className={prose}>
        This is for your own agent, on your own infrastructure. It needs an HTTP endpoint that takes a POST,
        is reachable from this Tares server, and starts the agent with the body as its context. For example:
      </p>
      <Snippet title="A minimal receiver (FastAPI, on your side)" code={[
        "from fastapi import FastAPI, Request",
        "app = FastAPI()",
        "",
        "@app.post(\"/hook\")",
        "async def hook(req: Request):",
        "    dispatch = await req.json()", // words-ok
        "    run_my_agent(dispatch[\"payload\"])   # the correlated timeline", // words-ok
        "    return {\"ok\": True}",
      ].join("\n")} />
      <p className={prose}>
        No agent of your own? A <Link to="/projects">Tares agent</Link> works inside Tares when a {rule}{" "}
        matches, with no endpoint. Each notification is JSON shaped like this:
      </p>
      <Snippet title="What your endpoint receives each time" code={JSON.stringify({
        dispatch_id: "9f2c81d4…",
        trigger: firstRule ?? "error_spike",
        kind: "alert",
        key: "api-server",
        fired_at: "2026-07-17T09:14:03+00:00",
        payload: "…the correlated timeline for this key, ready to hand to the model…",
      }, null, 2)} />
      <p className={prose} data-testid="connect-retries">
        Tares sends each notification at least once, so dedupe on <Code>dispatch_id</Code>. Answer with a 2xx
        to acknowledge it. Tares tries a 5xx or a dropped connection again, waiting longer each time, and
        records a 4xx as failed without trying again.
      </p>

      <h2 className="m-0 mt-2 text-[17px]">2. Add the endpoint to a {rule}</h2>
      {rules.error && !rules.data ? (
        <Banner tone="error" data-testid="connect-rules-failed"
                action={<Button variant="secondary" onClick={rules.reload}>Try again</Button>}>
          Tares could not list its {rule}s: {rules.error}
        </Banner>
      ) : !rules.data ? (
        <SkeletonLines widths={["70%", "45%"]} />
      ) : rules.data.length > 0 ? (
        <>
          <p className={prose}>
            Add it in the {rule}&rsquo;s project: open <Link to="/projects">the project</Link>, then Setup, Advanced
            setup, Settings, Subscribers. From a script it is one call, with a key that has the{" "}
            <Code>read</Code> scope:
          </p>
          {withToken("Add your agent's endpoint", subscribeCurl)}
        </>
      ) : (
        <EmptyState data-testid="connect-no-rules">
          This Tares has no {rule}s yet, so there is nothing to add it to. Make one in{" "}
          <Link to="/projects">a project</Link>, then come back.
        </EmptyState>
      )}
      <p className={prose}>
        A project&rsquo;s Subscribers lists every endpoint its {rule}s notify. More in{" "}
        <a href={`https://${GUIDE}`} target="_blank" rel="noreferrer">the guide to {rule}s</a>.
      </p>
    </div>
  );

  const rest = (
    <div className="grid min-w-0 gap-4">
      <p className={prose}>
        No MCP client? The same reads are plain HTTP: one call returns the correlated timeline. An agent can
        also write what it concluded back with <Code>POST /remember</Code> (with the <Code>{INGEST_SCOPE}</Code> scope),
        so the next timeline arrives with it attached.
      </p>
      {withToken("Read a correlated timeline", readCurl)}
    </div>
  );

  return (
    <div className="grid min-w-0 gap-5" data-testid="connect-page">
      <header className="min-w-0">
        <h1 className="m-0 mb-1.5 text-[22px] md:text-[28px]">Connect</h1>
        <p className="m-0 max-w-2xl text-sm text-muted-foreground">
          Give an agent of your own access to this Tares. It can read over MCP, have events sent to its own
          address, or call the REST API.
        </p>
      </header>
      <Tabs aria-label="Ways to connect" value={tab} onValueChange={(v) => setTab(v as ConnectTab)}
            items={TABS.map((t) => ({
              value: t.value,
              label: t.label,
              content: <div className="pt-4">{
                t.value === "mcp" ? mcp : t.value === "push" ? push : t.value === "rest" ? rest
                  : <ToolList tools={tools.data} error={tools.error} reload={tools.reload} />
              }</div>,
            }))} />
    </div>
  );
}

function ToolList({ tools, error, reload }: {
  tools?: { name: string; description: string }[]; error?: string; reload: () => void;
}) {
  const [q, setQ] = useState("");
  const shown = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return (tools ?? []).filter((t) =>
      !needle || t.name.toLowerCase().includes(needle) || t.description.toLowerCase().includes(needle));
  }, [tools, q]);

  return (
    <div className="grid min-w-0 gap-4">
      <p className={prose}>
        Exactly what a connected agent can call, read from this Tares&rsquo;s MCP server, so it always matches
        what MCP serves. The same reads back the REST API.
      </p>
      {error && !tools ? (
        <Banner tone="error" data-testid="connect-tools-failed"
                action={<Button variant="secondary" onClick={reload}>Try again</Button>}>
          Tares could not list its tools: {error}
        </Banner>
      ) : !tools ? (
        <SkeletonRows count={4} />
      ) : !tools.length ? (
        <EmptyState title="No tools" data-testid="connect-tools-empty">This Tares serves no MCP tools.</EmptyState>
      ) : (
        <>
          <div className="max-w-sm">
            <Input label="Search the tools" placeholder="A tool, or what it does" value={q}
                   onChange={(e) => setQ(e.target.value)} data-testid="connect-tools-search" />
          </div>
          <ListSection paper title="Tools the agent gets"
                       aside={<span data-testid="connect-tools-count">{shown.length} of {tools.length}</span>}>
            {shown.map((t) => (
              <ListRow key={t.name} data-testid="connect-tool"
                       className="grid grid-cols-1 gap-x-6 gap-y-1 md:grid-cols-[minmax(0,16rem)_minmax(0,1fr)]">
                <code className="font-mono text-[13px] font-medium text-foreground [overflow-wrap:anywhere]" data-words-exempt>{t.name}</code>
                <span className="text-[13px] text-muted-foreground" data-words-exempt>{t.description}</span>
              </ListRow>
            ))}
            {!shown.length && <EmptyState data-testid="connect-tools-none">No tool matches.</EmptyState>}
          </ListSection>
        </>
      )}
    </div>
  );
}
