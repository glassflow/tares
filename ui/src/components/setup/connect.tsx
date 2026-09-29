import { useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { api } from "../../api";
import IngestSetup from "../IngestSetup";
import { ServerForm } from "../../pages/McpServers";
import { TimeAgo } from "../bits";
import type { McpServer, Plan, SetupChecks, SetupConnect } from "../../types";
import { CopyLine, errText } from "./common";

// Step 3: only what only the person can do. Everything else is already set up and waiting. Each
// section says what to do and shows the live state from GET setup (polled every few seconds by
// the page), so "Waiting" turns into "Connected" without a reload. Continue works any time.

type ConnectSource = SetupConnect["sources"][number];
type ConnectTool = SetupConnect["tools"][number];

/** The connect list, from apply's answer; on a resumed setup (no answer in hand) from the plan,
 *  with each ingest URL read off the source the apply made. The key is not in the plan: it was
 *  shown once. */
function useConnect(projectId: string, plan: Plan, given: SetupConnect | undefined) {
  const [sources, setSources] = useState<ConnectSource[] | undefined>(given?.sources);
  // the plan is refetched every few seconds; only its sources decide what to look up
  const watchKey = plan.watches.map((w) => `${w.name}:${w.needs}:${w.existing}`).join(",");
  useEffect(() => {
    if (given) return;
    let live = true;
    const wanted = plan.watches.filter((w) => !w.existing && w.needs !== "none");
    Promise.all(wanted.map(async (w): Promise<ConnectSource> => {
      let url: string | null = null;
      if (w.needs === "send") {
        const origin = window.location.origin;
        if (w.connector === "otlp") url = `${origin}/v1/logs`;
        else {
          try { const s = await api.source(w.name); url = `${origin}/ingest/${s.ingest_key || s.name}`; }
          catch { url = `${origin}/ingest/${w.name}`; }
        }
      }
      return { name: w.name, needs: w.needs, ingest_url: url, sample: w.sample, credential_hint: null };
    })).then((r) => { if (live) setSources(r); });
    return () => { live = false; };
  }, [projectId, watchKey, given]);   // eslint-disable-line react-hooks/exhaustive-deps
  const tools: ConnectTool[] = given?.tools
    ?? (plan.who === "tares" ? plan.tools.filter((t) => t.enabled).map((t) => ({ name: t.name, url: t.url, needs_token: false })) : []);
  return { sources, tools, ownAgent: given?.own_agent ?? null };
}

function curlFor(url: string, sample: Record<string, unknown> | null, authRequired: boolean): string {
  const body = JSON.stringify(sample ?? { message: "hello from my service" }).replace(/'/g, "'\\''");
  return [
    `curl -X POST ${url} \\`,
    `  -H 'Content-Type: application/json' \\`,
    ...(authRequired ? [`  -H 'Authorization: Bearer <your ingest key>' \\`] : []),
    `  -d '${body}'`,
  ].join("\n");
}

function SourceSection({ projectId, src, connector, sentence, check, authRequired }: {
  projectId: string; src: ConnectSource; connector: string; sentence: string;
  check: SetupChecks["sources"][number] | undefined; authRequired: boolean;
}) {
  const [sending, setSending] = useState(false);
  const [sent, setSent] = useState(false);
  const [err, setErr] = useState<string>();
  const state = check?.state ?? "waiting";

  const sendTest = async () => {
    setSending(true); setErr(undefined);
    try { await api.sendSetupTestEvent(projectId, src.name); setSent(true); }
    catch (e) { setErr(errText(e)); }
    setSending(false);
  };

  const special = connector === "otlp" || connector === "alertmanager";
  return (
    <section className="su-box" aria-labelledby={`su-src-${src.name}`}>
      <h2 id={`su-src-${src.name}`} className="su-h2">{sentence}</h2>

      {src.needs === "send" && src.ingest_url && (
        <>
          <p>{connector === "otlp" ? "Point an OTLP/HTTP exporter at this address" : "Send each event to this address"}</p>
          <CopyLine text={src.ingest_url} what="the address" />
          {special ? (
            <IngestSetup connector={connector} url={src.ingest_url} />
          ) : (
            <>
              {src.sample && (
                <>
                  <p>As JSON, one event per request, for example:</p>
                  <pre className="su-pre">{JSON.stringify(src.sample, null, 2)}</pre>
                </>
              )}
              <p>Or try it from a terminal:</p>
              <CopyLine text={curlFor(src.ingest_url, src.sample, authRequired)} what="the curl command" multiline />
              {authRequired && (
                <p className="help">
                  This instance checks keys, so your service sends an ingest key with each event. Make one under{" "}
                  <Link to="/settings">Settings</Link>.
                </p>
              )}
            </>
          )}
        </>
      )}

      {src.needs === "credential" && (
        <>
          <p>{src.credential_hint ?? "Tares needs a credential to read this."}</p>
          <div>
            <Link className="btn" to={`/projects/${encodeURIComponent(projectId)}?view=source:${encodeURIComponent(src.name)}`}>
              Add the credential
            </Link>
          </div>
        </>
      )}

      <div className={`su-state ${state}`} role="status" aria-live="polite">
        <span className="su-dot" aria-hidden="true" />
        <span className="su-state-text">
          {state === "receiving" && (
            <>
              <strong>Connected.</strong>{" "}
              {check?.detail ?? "Events are arriving."}
              {check?.last_event_at && <> Last one <TimeAgo ts={check.last_event_at} />.</>}
              {check && check.fields_seen.length > 0 && (
                <> Tares read {check.fields_seen.map((f, i) => (
                  <span key={f}>{i > 0 && (i === check.fields_seen.length - 1 ? " and " : ", ")}<span className="mono">{f}</span></span>
                ))} from them.</>
              )}
            </>
          )}
          {state === "waiting" && (
            <><strong>{sent ? "Test event sent." : "Waiting for the first event."}</strong>{" "}
              {sent ? "It shows up here in a moment." : "It shows up here the moment it arrives."}</>
          )}
          {state === "error" && <><strong>Not connected.</strong> {check?.detail ?? "Tares could not read from it."}</>}
        </span>
        {state !== "receiving" && src.needs === "send" && src.sample && (
          <button type="button" onClick={sendTest} disabled={sending}>
            {sending ? "Sending…" : "Send a test event for me"}
          </button>
        )}
      </div>
      {err && <p className="su-err" role="alert">{err}</p>}
    </section>
  );
}

function ToolSection({ projectId, tool, check }: {
  projectId: string; tool: ConnectTool; check: SetupChecks["tools"][number] | undefined;
}) {
  const [url, setUrl] = useState(tool.url);
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<{ ok: boolean; text: string }>();
  const [more, setMore] = useState(false);
  const [server, setServer] = useState<McpServer>();

  const saveAndTest = async () => {
    setBusy(true); setResult(undefined);
    try {
      const v = token.trim();
      await api.updateMcpServer(tool.name, {
        name: tool.name, url: url.trim(),
        ...(v ? { auth_header: "", auth_value: /^bearer\s/i.test(v) ? v : `Bearer ${v}` } : {}),
        project: projectId,
      });
      setToken("");
      const t = await api.testMcpServer(tool.name);
      setResult(t.ok
        ? { ok: true, text: `Works. It offers ${t.tools.length} ${t.tools.length === 1 ? "tool" : "tools"}.` }
        : { ok: false, text: t.error ?? "It did not answer." });
    } catch (e) { setResult({ ok: false, text: errText(e) }); }
    setBusy(false);
  };
  const openMore = async () => {
    try {
      const r = await api.mcpServers();
      setServer(r.servers.find((s) => s.name === tool.name));
      setMore(true);
    } catch (e) { setResult({ ok: false, text: errText(e) }); }
  };

  const state = result ? (result.ok ? "ok" : "error") : check?.state ?? "untested";
  const text = result?.text ?? check?.detail
    ?? (state === "ok" ? "Works." : state === "error" ? "It did not answer." : "Not tested yet.");
  return (
    <section className="su-box" aria-labelledby={`su-tool-${tool.name}`}>
      <h2 id={`su-tool-${tool.name}`} className="su-h2">Tool <span className="mono">{tool.name}</span></h2>
      {more && server ? (
        <ServerForm initial={server} project={projectId}
                    onSaved={() => { setMore(false); setResult(undefined); }}
                    onCancel={() => setMore(false)} />
      ) : (
        <form className="su-tool-form" onSubmit={(e) => { e.preventDefault(); saveAndTest(); }}>
          <label className="field">
            <span className="lbl">Address</span>
            <input type="url" className="mono" value={url} required placeholder="https://mcp.example.com/mcp"
                   onChange={(e) => setUrl(e.target.value)} />
          </label>
          <label className="field">
            <span className="lbl">Token{tool.needs_token ? "" : " (if it asks for one)"}</span>
            <input type="password" className="mono" autoComplete="new-password" value={token}
                   onChange={(e) => setToken(e.target.value)} />
            <span className="help">Sent as Authorization: Bearer. Stored as a secret and not shown again. Leave it empty to keep the one saved.</span>
          </label>
          <div className="btnrow">
            <button type="submit" disabled={busy || !url.trim()}>{busy ? "Testing…" : "Save and test"}</button>
            <button type="button" className="linklike su-small" onClick={openMore}>More connection options</button>
          </div>
        </form>
      )}
      <div className={`su-state ${state === "ok" ? "receiving" : state === "error" ? "error" : "waiting"}`}
           role="status" aria-live="polite">
        <span className="su-dot" aria-hidden="true" />
        <span className="su-state-text">{text}</span>
      </div>
    </section>
  );
}

function OwnAgentSection({ projectId, own, name, check }: {
  projectId: string; own: SetupConnect["own_agent"]; name: string;
  check: SetupChecks["own_agent"] | undefined;
}) {
  const joined = check?.state === "joined";
  return (
    <section className="su-box" aria-labelledby="su-own-h">
      <h2 id="su-own-h" className="su-h2">Connect your agent</h2>
      {own ? (
        <>
          <p>Its key, <span className="mono">{name}</span>. It reads this project only and records findings in it.</p>
          <CopyLine text={own.key} what="the key" />
          <p className="su-warn">Shown once. Copy it now: Tares does not show it again.</p>
          <p>For Claude Code, run this once:</p>
          <CopyLine text={own.claude_command} what="the Claude Code command" multiline />
          <p>Any other MCP client connects to this address with the key as a bearer token:</p>
          <CopyLine text={own.mcp_url} what="the MCP address" />
          {own.subscribe_hint && <p className="help">{own.subscribe_hint}</p>}
        </>
      ) : (
        <p>
          The key for <span className="mono">{name}</span> was shown once, when this project was set up. If you did
          not keep it, <Link to={`/projects/${encodeURIComponent(projectId)}?view=settings:keys`}>make a new key</Link>{" "}
          and connect with that.
        </p>
      )}
      <div className={`su-state ${joined ? "receiving" : "waiting"}`} role="status" aria-live="polite">
        <span className="su-dot" aria-hidden="true" />
        <span className="su-state-text">
          {joined
            ? <><strong>Joined.</strong> {check?.detail ?? "Your agent connected."}</>
            : <><strong>Waiting for your agent.</strong> {check?.detail ?? "It shows up here once it uses the key."}</>}
        </span>
      </div>
    </section>
  );
}

export function ConnectStep({ projectId, plan, connect, checks, onContinue, onLater }: {
  projectId: string; plan: Plan; connect: SetupConnect | undefined; checks: SetupChecks | undefined;
  onContinue: () => void; onLater: () => void;
}) {
  const { sources, tools, ownAgent } = useConnect(projectId, plan, connect);
  const [authRequired, setAuthRequired] = useState(false);
  useEffect(() => { api.health().then((h) => setAuthRequired(h.auth_required)).catch(() => {}); }, []);
  const own = plan.who === "own" && plan.own_agent;
  const shown = (sources ?? []).filter((s) => s.needs !== "none");
  const nothing = sources !== undefined && shown.length === 0 && tools.length === 0 && !own;
  const watch = (name: string) => plan.watches.find((w) => w.name === name);

  const title = own && !shown.length && !tools.length ? "Connect your agent"
    : !own && !tools.length ? "Connect your data"
    : "Connect what only you can";

  return (
    <section className="su-section su-mid" aria-labelledby="su-connect-h">
      <div className="su-head">
        <h1 id="su-connect-h" className="su-title">{nothing ? "Nothing to connect" : title}</h1>
        <p className="su-sub">
          {nothing ? "Everything this project needs is already in Tares."
            : "The one step only you can do. The rest is set up and waiting."}
        </p>
      </div>

      {sources === undefined && <p className="help">Loading…</p>}
      {shown.map((s) => (
        <SourceSection key={s.name} projectId={projectId} src={s}
                       connector={watch(s.name)?.connector ?? ""} sentence={watch(s.name)?.sentence ?? s.name}
                       check={checks?.sources.find((c) => c.name === s.name)} authRequired={authRequired} />
      ))}
      {own && (
        <OwnAgentSection projectId={projectId} own={ownAgent} name={own.name} check={checks?.own_agent} />
      )}
      {tools.map((t) => (
        <ToolSection key={t.name} projectId={projectId} tool={t} check={checks?.tools.find((c) => c.name === t.name)} />
      ))}

      <div className="btnrow">
        <button type="button" className="primary su-cta" onClick={onContinue}>Continue</button>
        {!nothing && <button type="button" className="su-cta" onClick={onLater}>I will connect it later</button>}
      </div>
    </section>
  );
}
