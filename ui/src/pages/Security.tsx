import { useEffect, useState } from "react";

import { api, type TracingStatus } from "../api";
import ConfirmDialog from "../components/ConfirmDialog";
import { Close } from "../components/icons";
import { Picker, TimeAgo } from "../components/bits";
import type { ApiKey, GithubCredential, ModelProvider, ModelProviders } from "../types";

// Four distinct credential concepts, one box each:
//   · Access     — is this instance open, or does it require a login? (tares up --auth)
//   · API keys   — scoped, revocable, show-once credentials the operator mints for machines
//   · Providers  — the model providers Tares agents (and Ask) run on
//   · Slack      — the bot token behind slack:// trigger subscriptions (outbound), and the
//                  signing secret that authenticates the /tares slash command (inbound)
// The per-source ingest URL is an address, not a secret — it lives on the source page, not here.
type SettingsTab = "access" | "anthropic" | "github" | "slack" | "observability";
const TABS: { key: SettingsTab; label: string }[] = [
  { key: "access", label: "Access and API keys" },
  { key: "anthropic", label: "Model providers" },
  { key: "github", label: "GitHub" },
  { key: "slack", label: "Slack" },
  { key: "observability", label: "Observability" },
];

export default function Security() {
  // Cloud only (TR-142): the half of "settings" a user comes here looking for that lives in the
  // control plane, named and linked, so nobody has to know the control plane exists.
  const [workspaceUrl, setWorkspaceUrl] = useState<string>();
  useEffect(() => {
    api.health().then((h) => setWorkspaceUrl(h.workspace_url || undefined)).catch(() => {});
  }, []);
  const [tab, setTab] = useState<SettingsTab>(() => {
    const t = new URLSearchParams(window.location.search).get("tab");
    return (TABS.find((x) => x.key === t)?.key ?? "access");
  });
  const pick = (t: SettingsTab) => {
    setTab(t);
    const url = new URL(window.location.href); url.searchParams.set("tab", t);
    window.history.replaceState(null, "", url.toString());
  };
  return (
    <>
      <h1>Settings</h1>
      <p className="subtitle">access mode, API keys, model providers, the instance credentials (GitHub, Slack) and agent tracing</p>
      {workspaceUrl && (
        <div className="alert" style={{ marginBottom: 14 }}>
          <strong>Users, the Slack app, plan and storage</strong> are managed in your workspace, not
          here. The Slack <em>bot token</em> below is what this instance posts with; installing the
          app into your Slack happens in the workspace.{" "}
          <a href={workspaceUrl}>Open workspace ↗</a>
        </div>
      )}
      <div className="tabs">
        {TABS.map((t) => (
          <button key={t.key} className={tab === t.key ? "active" : ""} onClick={() => pick(t.key)}>{t.label}</button>
        ))}
      </div>
      {tab === "access" && <><AccessPanel /><ApiKeysPanel /></>}
      {tab === "anthropic" && <ProvidersPanel />}
      {tab === "github" && <GithubPanel />}
      {tab === "slack" && <><SlackTokenPanel /><SlackSigningSecretPanel /></>}
      {tab === "observability" && <TracingPanel />}
    </>
  );
}

// GitHub: a token stored once, referenced by name from `github` sources (`credential: <name>`)
// and from MCP servers (`credential:github/<name>`), so a rotation happens here and nowhere else.
// Same write-only contract as the other credentials: the token never comes back.
function GithubPanel() {
  const [creds, setCreds] = useState<GithubCredential[]>();
  const [err, setErr] = useState<string>();
  const [adding, setAdding] = useState(false);
  const [name, setName] = useState("");
  const [token, setToken] = useState("");
  const [apiUrl, setApiUrl] = useState("");
  const [busy, setBusy] = useState(false);
  const [tests, setTests] = useState<Record<string, { busy?: boolean; ok?: boolean; error?: string;
                                                     login?: string; scopes?: string[] }>>({});
  const [confirmDelete, setConfirmDelete] = useState<GithubCredential>();
  const [rotating, setRotating] = useState<string>();
  const [newToken, setNewToken] = useState("");

  const load = () =>
    api.githubCredentials().then((r) => setCreds(r.credentials))
      .catch((e) => setErr(String((e as Error).message ?? e)));
  useEffect(() => { load(); }, []);

  const add = async () => {
    setBusy(true); setErr(undefined);
    try {
      await api.createGithubCredential({ name: name.trim(), token: token.trim(), api_url: apiUrl.trim() });
      setName(""); setToken(""); setApiUrl(""); setAdding(false);
      await load();
    } catch (e) { setErr(String((e as Error).message ?? e)); }
    setBusy(false);
  };

  const rotate = async (n: string) => {
    setBusy(true); setErr(undefined);
    try {
      await api.updateGithubCredential(n, { name: n, token: newToken.trim() });
      setRotating(undefined); setNewToken("");
      await load();
    } catch (e) { setErr(String((e as Error).message ?? e)); }
    setBusy(false);
  };

  const test = async (n: string) => {
    setTests((t) => ({ ...t, [n]: { busy: true } }));
    try {
      const r = await api.testGithubCredential(n);
      setTests((t) => ({ ...t, [n]: { ok: r.ok, error: r.error, login: r.login, scopes: r.scopes } }));
      if (r.ok) load();
    } catch (e) {
      setTests((t) => ({ ...t, [n]: { ok: false, error: String((e as Error).message ?? e) } }));
    }
  };

  const remove = async (n: string) => {
    setBusy(true); setErr(undefined);
    try { await api.deleteGithubCredential(n); await load(); }
    catch (e) { setErr(String((e as Error).message ?? e)); }
    setBusy(false); setConfirmDelete(undefined);
  };

  return (
    <div className="panel">
      <div className="btnrow" style={{ justifyContent: "space-between", alignItems: "baseline" }}>
        <h2 style={{ margin: 0 }}>GitHub</h2>
        {!adding && <button className="primary" onClick={() => setAdding(true)}>Add credential</button>}
      </div>
      <p className="help">
        A GitHub token stored once. Pick it by name on a <em>GitHub</em> source instead of pasting a
        token per repository, and on an MCP server as its authentication; rotate it here and every
        source and server follows. Use a fine-grained token: the repositories you want, with{" "}
        <strong>Contents</strong> read (read/write on a repository an agent should update),{" "}
        <strong>Pull requests</strong> read/write, <strong>Metadata</strong> read. It is never
        returned by the API and never included in a catalog export.
      </p>

      {err && <div className="alert error">{err}</div>}

      {adding && (
        <div className="panel" style={{ marginBottom: 12 }}>
          <div className="row2">
            <label className="field">
              <span className="lbl">name</span>
              <input type="text" className="mono" placeholder="e.g. github" value={name}
                     onChange={(e) => setName(e.target.value)} />
            </label>
            <label className="field">
              <span className="lbl">token</span>
              <input type="password" className="mono" autoComplete="new-password"
                     placeholder="github_pat_… or ghp_…" value={token}
                     onChange={(e) => setToken(e.target.value)} />
            </label>
          </div>
          <label className="field">
            <span className="lbl">API URL <span className="help">(GitHub Enterprise only)</span></span>
            <input type="text" className="mono" placeholder="https://github.example.com/api/v3"
                   value={apiUrl} onChange={(e) => setApiUrl(e.target.value)} />
          </label>
          <div className="btnrow">
            <button className="primary" disabled={busy || !name.trim() || !token.trim()} onClick={add}>Save</button>
            <button onClick={() => { setAdding(false); setErr(undefined); }}>Cancel</button>
          </div>
        </div>
      )}

      {!creds ? <div className="muted">loading…</div>
        : creds.length === 0 ? (
          !adding && <div className="empty">no GitHub credential yet. Add one to pick it on sources and MCP servers.</div>
        ) : (
          <table>
            <thead><tr><th>name</th><th>account</th><th>used by</th><th>updated</th><th aria-label="actions" /></tr></thead>
            <tbody>
              {creds.map((c) => {
                const t = tests[c.name];
                const uses = c.sources.length + c.mcp_servers.length;
                return (
                  <>
                    <tr key={c.name}>
                      <td className="mono"><strong>{c.name}</strong>
                        {c.api_url && <span className="help" style={{ marginLeft: 6 }}>{c.api_url}</span>}</td>
                      <td>{c.account ? <span className="mono">{c.account}</span> : <span className="dim">unknown</span>}</td>
                      <td>{uses === 0 ? <span className="dim">nothing yet</span> : (
                        <span className="help" title={[...c.sources, ...c.mcp_servers].join("\n")}>
                          {c.sources.length > 0 && <>{c.sources.length} source{c.sources.length === 1 ? "" : "s"}</>}
                          {c.sources.length > 0 && c.mcp_servers.length > 0 && ", "}
                          {c.mcp_servers.length > 0 && <>{c.mcp_servers.length} MCP server{c.mcp_servers.length === 1 ? "" : "s"}</>}
                        </span>
                      )}</td>
                      <td style={{ whiteSpace: "nowrap" }}><TimeAgo ts={c.updated_at} /></td>
                      <td style={{ whiteSpace: "nowrap" }}>
                        <div className="btnrow" style={{ justifyContent: "flex-end", flexWrap: "nowrap" }}>
                          <button onClick={() => test(c.name)} disabled={t?.busy}>{t?.busy ? "testing…" : "Test"}</button>
                          <button onClick={() => { setRotating(rotating === c.name ? undefined : c.name); setNewToken(""); }}>Rotate</button>
                          <button className="danger" onClick={() => setConfirmDelete(c)}>Delete</button>
                        </div>
                      </td>
                    </tr>
                    {t && !t.busy && (
                      <tr key={c.name + "-test"}>
                        <td colSpan={5} style={{ background: "var(--wash)" }}>
                          {t.ok ? (
                            <div style={{ padding: "6px 4px" }}>
                              <span className="badge ok">token works</span>{" "}
                              <span className="help">signed in as <span className="mono">{t.login}</span>
                                {t.scopes && t.scopes.length > 0 && <> with scopes <span className="mono">{t.scopes.join(", ")}</span></>}</span>
                            </div>
                          ) : (
                            <div style={{ padding: "6px 4px" }}>
                              <span className="badge error">failed</span>{" "}
                              <span className="help mono">{t.error}</span>
                            </div>
                          )}
                        </td>
                      </tr>
                    )}
                    {rotating === c.name && (
                      <tr key={c.name + "-rotate"}>
                        <td colSpan={5} style={{ background: "var(--wash)" }}>
                          <div className="btnrow" style={{ alignItems: "center", maxWidth: 720, padding: "6px 4px" }}>
                            <input type="password" className="mono" style={{ flex: 1 }} autoComplete="new-password"
                                   placeholder="new token" value={newToken} onChange={(e) => setNewToken(e.target.value)} />
                            <button className="primary" disabled={busy || !newToken.trim()} onClick={() => rotate(c.name)}>Save new token</button>
                            <button onClick={() => setRotating(undefined)}>Cancel</button>
                          </div>
                        </td>
                      </tr>
                    )}
                  </>
                );
              })}
            </tbody>
          </table>
        )}

      {confirmDelete && (
        <ConfirmDialog
          title={`Delete GitHub credential ${confirmDelete.name}?`}
          message={confirmDelete.sources.length + confirmDelete.mcp_servers.length > 0
            ? `${confirmDelete.sources.length} source(s) and ${confirmDelete.mcp_servers.length} MCP server(s) reference it and will stop authenticating until you point them at another credential.`
            : "Nothing references it."}
          confirmLabel="Delete" danger
          onConfirm={() => remove(confirmDelete.name)}
          onCancel={() => setConfirmDelete(undefined)} />
      )}
    </div>
  );
}

// Auth is set at launch: `tares up` is open; `tares up --auth` requires a login (a root token
// printed to the terminal). This box states which mode you're in — there's no runtime toggle, so
// it's status, not a control.
function AccessPanel() {
  const [authRequired, setAuthRequired] = useState<boolean>();
  useEffect(() => {
    api.health().then((h) => setAuthRequired(h.auth_required)).catch(() => setAuthRequired(undefined));
  }, []);

  return (
    <div className="panel">
      <h2 style={{ marginTop: 0 }}>Access</h2>
      {authRequired === undefined ? <div className="muted">loading…</div>
        : authRequired ? (
          <p style={{ margin: 0 }}>
            <span className="badge ok">auth on</span>{" "}
            <span className="help">
              the console and API require a login. You signed in with the root token printed by{" "}
              <code>tares up --auth</code>. Hand machines their own scoped <strong>API keys</strong>{" "}
              below; never the root token.
            </span>
          </p>
        ) : (
          <div className="alert">
            <span className="badge">auth off</span> · this instance is <strong>open</strong>: anyone
            who can reach it can read and write, and API keys are not enforced. Restart with{" "}
            <code>tares up --auth</code> to require a login.
          </div>
        )}
    </div>
  );
}

// The model providers a cell holds (TR-301): Anthropic, OpenAI, or any OpenAI-compatible
// endpoint (LiteLLM, OpenRouter, Ollama, vLLM). One is the default, what Ask and the builder
// use and what an agent without a provider of its own runs on. Credentials are write-only.
// The Anthropic entry doubles as the old key + gateway settings, so a deployment that fills
// those (a hosted trial cell, a compose file) shows up here unchanged.
type ProviderKind = ModelProvider["kind"];
const KIND_HELP: Record<ProviderKind, string> = {
  anthropic: "Claude models with your Anthropic API key. Base URL only if calls go through a gateway that speaks the Anthropic format.",
  openai: "GPT models with your OpenAI API key. Base URL only for Azure or a proxy.",
  openai_compatible: "Anything that speaks OpenAI chat completions: LiteLLM, OpenRouter, Ollama, vLLM. The models it lists are what the picker offers.",
};
const PRESETS: { id: string; label: string; kind: ProviderKind; name: string; base_url: string; key: boolean }[] = [
  { id: "anthropic", label: "Anthropic", kind: "anthropic", name: "Anthropic", base_url: "", key: true },
  { id: "openai", label: "OpenAI", kind: "openai", name: "OpenAI", base_url: "", key: true },
  { id: "litellm", label: "LiteLLM", kind: "openai_compatible", name: "LiteLLM", base_url: "http://localhost:4000/v1", key: true },
  { id: "openrouter", label: "OpenRouter", kind: "openai_compatible", name: "OpenRouter", base_url: "https://openrouter.ai/api/v1", key: true },
  { id: "ollama", label: "Ollama", kind: "openai_compatible", name: "Ollama", base_url: "http://localhost:11434/v1", key: false },
  { id: "other", label: "Other OpenAI-compatible", kind: "openai_compatible", name: "", base_url: "", key: true },
];

function ProvidersPanel() {
  const [data, setData] = useState<ModelProviders>();
  const [err, setErr] = useState<string>();
  const [msg, setMsg] = useState<string>();
  const [busy, setBusy] = useState(false);
  const [editing, setEditing] = useState<{ id: string; preset: string; kind: ProviderKind; name: string; base_url: string; key: string }>();
  const [confirmDelete, setConfirmDelete] = useState<ModelProvider>();

  const load = () => api.providers().then(setData).catch((e) => setErr(String((e as Error).message ?? e)));
  useEffect(() => { load(); }, []);

  const startAdd = () => {
    const p = PRESETS[0];
    setEditing({ id: "new", preset: p.id, kind: p.kind, name: p.name, base_url: p.base_url, key: "" });
    setMsg(undefined); setErr(undefined);
  };
  const startEdit = (p: ModelProvider) => {
    const preset = p.kind === "openai_compatible" ? (PRESETS.find((x) => x.base_url && p.base_url.startsWith(x.base_url))?.id ?? "other") : p.kind;
    setEditing({ id: p.id, preset, kind: p.kind, name: p.name, base_url: p.kind === "anthropic" && !p.gateway_stored && !p.base_source ? "" : p.base_url, key: "" });
    setMsg(undefined); setErr(undefined);
  };
  const pickPreset = (id: string) => {
    const p = PRESETS.find((x) => x.id === id)!;
    setEditing((e) => e && { ...e, preset: id, kind: p.kind, name: p.name, base_url: p.base_url });
  };
  const save = async () => {
    if (!editing) return;
    setBusy(true); setErr(undefined); setMsg(undefined);
    try {
      const r = await api.saveProvider(editing.id, { kind: editing.kind, name: editing.name, key: editing.key, base_url: editing.base_url });
      setData(r); setEditing(undefined);
      const saved = r.providers.find((p) => p.id === r.id);
      setMsg(saved?.models_error
        ? `✓ saved, but the endpoint did not list its models: ${saved.models_error}. Agents can still name a model by hand.`
        : saved?.discovers ? `✓ saved; ${saved.models.length} model${saved.models.length === 1 ? "" : "s"} listed by the endpoint` : "✓ saved; in use from the next call");
    } catch (e) { setErr(String((e as Error).message ?? e)); }
    setBusy(false);
  };
  const remove = async (id: string) => {
    setBusy(true); setErr(undefined);
    try { setData(await api.deleteProvider(id)); }
    catch (e) { setErr(String((e as Error).message ?? e)); }
    setBusy(false); setConfirmDelete(undefined);
  };
  const makeDefault = async (id: string) => {
    setBusy(true); setErr(undefined);
    try { setData(await api.setDefaultProvider(id)); }
    catch (e) { setErr(String((e as Error).message ?? e)); }
    setBusy(false);
  };
  const refresh = async (id: string) => {
    setBusy(true); setErr(undefined); setMsg(undefined);
    try {
      const r = await api.refreshProviderModels(id);
      setData(r);
      setMsg(r.error ? undefined : `✓ ${r.models.length} model${r.models.length === 1 ? "" : "s"} listed`);
    } catch (e) { setErr(String((e as Error).message ?? e)); }
    setBusy(false);
  };

  const preset = editing && PRESETS.find((x) => x.id === editing.preset);
  const existing = editing && data?.providers.find((p) => p.id === editing.id);
  const isNew = editing?.id === "new";
  const needsUrl = editing?.kind === "openai_compatible";
  const canSave = !!editing && (!needsUrl || !!editing.base_url.trim()) && (editing.kind !== "openai_compatible" || !!editing.name.trim())
    && (!!editing.key.trim() || !!existing?.configured || (editing.kind === "openai_compatible" && preset?.key === false));

  return (
    <div className="panel">
      <div className="btnrow" style={{ justifyContent: "space-between", alignItems: "baseline" }}>
        <h2 style={{ margin: 0 }}>Model providers</h2>
        {!editing && <button className="primary" onClick={startAdd}>Add provider</button>}
      </div>
      <p className="help">
        What <strong>Tares agents</strong> and Ask run on. Add the providers you hold keys for; each
        agent picks a provider and a model, and the <strong>default</strong> is what Ask and any agent
        without its own choice use. Values saved here win over the daemon's environment
        (<code>ANTHROPIC_API_KEY</code>, <code>OPENAI_API_KEY</code>, <code>TARES_MODEL_PROVIDER</code>);
        credentials are never returned by the API and never included in a catalog export.
      </p>

      {err && <div className="alert error">{err}</div>}
      {msg && <p className="help">{msg}</p>}

      {editing && (
        <div className="panel" style={{ marginBottom: 12 }}>
          {isNew && (
            <div className="field">
              <span className="lbl">provider</span>
              <Picker value={editing.preset} options={PRESETS.map((p) => p.id)} style={{ width: 280 }}
                      labels={Object.fromEntries(PRESETS.map((p) => [p.id, p.label]))}
                      ariaLabel="provider" onChange={pickPreset} />
              <span className="help">{KIND_HELP[editing.kind]}</span>
            </div>
          )}
          {!isNew && <p className="help" style={{ marginTop: 0 }}><strong>{existing?.name}</strong>: {KIND_HELP[editing.kind]}</p>}
          <div className="row2">
            {editing.kind === "openai_compatible" && (
              <label className="field">
                <span className="lbl">name</span>
                <input type="text" placeholder="e.g. LiteLLM" value={editing.name} disabled={!isNew}
                       onChange={(e) => setEditing({ ...editing, name: e.target.value })} />
                {isNew && <span className="help">agents refer to it by this name</span>}
              </label>
            )}
            <label className="field">
              <span className="lbl">API key {(!needsUrl || preset?.key !== false) ? "" : <span className="help">(optional)</span>}</span>
              <input type="password" className="mono" autoComplete="new-password" value={editing.key}
                     placeholder={existing?.configured ? "leave blank to keep the stored key" : editing.kind === "anthropic" ? "sk-ant-…" : editing.kind === "openai" ? "sk-…" : "the endpoint's key, if it wants one"}
                     onChange={(e) => setEditing({ ...editing, key: e.target.value })} />
              {existing?.source.startsWith("env:") && <span className="help">the environment's key ({existing.source}) is in use; a key saved here takes over</span>}
              {existing?.gateway_token_stored && <span className="help">a gateway token is stored and sent as a bearer header</span>}
            </label>
          </div>
          <label className="field">
            <span className="lbl">base URL {!needsUrl && <span className="help">(optional)</span>}</span>
            <input type="text" className="mono" value={editing.base_url}
                   placeholder={editing.kind === "anthropic" ? "https://api.anthropic.com, or a gateway that speaks the Anthropic format" : editing.kind === "openai" ? "https://api.openai.com/v1" : "http://localhost:4000/v1"}
                   onChange={(e) => setEditing({ ...editing, base_url: e.target.value })} />
            {editing.kind === "anthropic" && existing?.base_source?.startsWith("env:") && !editing.base_url && (
              <span className="help">the deployment routes calls through <span className="mono">{existing.base_url}</span> ({existing.base_source}); a URL saved here takes over</span>
            )}
          </label>
          <div className="btnrow">
            <button className="primary" disabled={busy || !canSave} onClick={save}>Save</button>
            <button onClick={() => { setEditing(undefined); setErr(undefined); }}>Cancel</button>
          </div>
        </div>
      )}

      {!data ? <div className="muted">loading…</div>
        : data.providers.filter((p) => p.configured || p.stored).length === 0 && !editing ? (
          <div className="empty">no model provider yet. Add one: agents cannot be enabled and Ask cannot answer until then.</div>
        ) : (
          <table>
            <thead><tr><th>provider</th><th>kind</th><th>endpoint</th><th>status</th><th>models</th><th aria-label="actions" /></tr></thead>
            <tbody>
              {data.providers.filter((p) => p.configured || p.stored).map((p) => (
                <tr key={p.id}>
                  <td><strong>{p.name}</strong>
                    {p.default && <span className="badge agent" style={{ marginLeft: 8 }}>default</span>}</td>
                  <td className="help">{data.kinds.find((k) => k.id === p.kind)?.label ?? p.kind}</td>
                  <td className="mono help">{p.base_url || (p.kind === "anthropic" ? "api.anthropic.com" : "")}</td>
                  <td>
                    {p.configured
                      ? <><span className="badge ok">configured</span>{p.source && <span className="help"> from <span className="mono">{p.source}</span></span>}</>
                      : <span className="badge error">no key</span>}
                  </td>
                  <td>
                    {p.models_error
                      ? <span className="help" title={p.models_error}><span className="badge error">not listed</span> {p.models.length > 0 ? `${p.models.length} kept from last time` : "the endpoint did not answer"}</span>
                      : <span className="help" title={p.models.slice(0, 40).join("\n")}>{p.models.length}{p.discovers ? " from the endpoint" : " built in"}</span>}
                  </td>
                  <td style={{ whiteSpace: "nowrap" }}>
                    <div className="btnrow" style={{ justifyContent: "flex-end", flexWrap: "nowrap" }}>
                      {p.discovers && p.configured && <button disabled={busy} onClick={() => refresh(p.id)} title="re-read the models this endpoint serves">Refresh models</button>}
                      {!p.default && p.configured && <button disabled={busy} onClick={() => makeDefault(p.id)}>Make default</button>}
                      <button disabled={busy} onClick={() => startEdit(p)}>Edit</button>
                      {(p.stored || p.kind !== "anthropic") && <button className="danger" disabled={busy} onClick={() => setConfirmDelete(p)}>Remove</button>}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}

      {confirmDelete && (
        <ConfirmDialog
          title={`Remove ${confirmDelete.name}?`}
          message={confirmDelete.kind === "anthropic"
            ? "The stored key and gateway are removed. If the deployment's environment carries a key, that one is used again."
            : "Agents that name this provider fall back to the default until you point them elsewhere."}
          confirmLabel="Remove" danger
          onConfirm={() => remove(confirmDelete.id)}
          onCancel={() => setConfirmDelete(undefined)} />
      )}
    </div>
  );
}

// Agent tracing: every agent run and Ask turn exported as an OpenTelemetry trace. Rius is a
// preset (a key is enough); any OTLP/HTTP endpoint works. Same precedence as the Anthropic key:
// a value saved here wins over the environment, so a cloud cell shows "from env" everywhere and
// the switch is the one live control. The key and headers are write-only.
function TracingPanel() {
  const [st, setSt] = useState<TracingStatus>();
  const [provider, setProvider] = useState<string>();
  const [endpoint, setEndpoint] = useState<string>();
  const [apiKey, setApiKey] = useState("");
  const [headers, setHeaders] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string>();
  const [msg, setMsg] = useState<string>();

  const load = () =>
    api.tracingStatus().then((s) => { setSt(s); setProvider(undefined); setEndpoint(undefined); })
      .catch((e) => setErr(String((e as Error).message ?? e)));
  useEffect(() => { load(); }, []);

  const apply = async (body: Parameters<typeof api.setTracing>[0], done?: string) => {
    setBusy(true); setErr(undefined); setMsg(undefined);
    try {
      const r = await api.setTracing(body);
      setMsg(r.note ?? done ?? "✓ saved");
      setApiKey(""); setHeaders("");
      await load();
    } catch (e) { setErr(String((e as Error).message ?? e)); }
    setBusy(false);
  };

  const from = (source: string) =>
    source === "console" ? "saved here" : source.startsWith("env:") ? `from ${source}` :
    source === "preset" ? "the provider's default" : source === "default" ? "default" : "";

  if (!st) return <div className="panel"><h2 style={{ marginTop: 0 }}>Agent tracing</h2>
    {err ? <div className="alert error">{err}</div> : <div className="muted">loading…</div>}</div>;

  const prov = provider ?? st.provider;
  const ep = endpoint ?? (st.endpoint_source === "preset" ? "" : st.endpoint);
  const envOnly = st.provider_source.startsWith("env:") || st.key_source.startsWith("env:");

  return (
    <div className="panel">
      <h2 style={{ marginTop: 0 }}>Agent tracing</h2>
      <p className="help" style={{ marginTop: 0 }}>
        Every agent run and every Ask turn becomes an OpenTelemetry trace: the run, each model
        call with its prompt, answer, tokens and cost, and each tool call. Traces go to the
        backend below; each agent is its own service, named <code>{st.instance}/&lt;agent&gt;</code>.
        The key and headers are never returned by the API.
      </p>

      {err && <div className="alert error">{err}</div>}
      {msg && <p className="help">{msg}</p>}

      <p style={{ margin: "0 0 12px" }}>
        <label style={{ display: "inline-flex", alignItems: "center", gap: 8 }}>
          <input type="checkbox" checked={st.enabled} disabled={busy}
                 onChange={(e) => apply({ enabled: e.target.checked },
                                        e.target.checked ? "✓ tracing on" : "✓ tracing off")} />
          <strong>Send agent traces</strong>
        </label>{" "}
        {st.enabled
          ? (st.active
              ? <span className="badge ok">on</span>
              : <span className="badge error">on, but no endpoint resolves</span>)
          : <span className="badge">off</span>}
        {st.enabled_source && <span className="help"> {from(st.enabled_source)}</span>}
      </p>

      {envOnly && (
        <p className="help" style={{ margin: "0 0 10px" }}>
          The provider and key come from the deployment's environment. Saving a value here
          replaces it for this instance; clearing it falls back to the environment.
        </p>
      )}

      <div style={{ display: "grid", gap: 10, maxWidth: 720 }}>
        <label className="help">Provider{" "}
          <select value={prov} disabled={busy} onChange={(e) => setProvider(e.target.value)}>
            <option value="rius">Rius (GlassFlow)</option>
            <option value="otlp">Any OpenTelemetry endpoint (OTLP/HTTP)</option>
          </select>{" "}
          <span className="muted">{from(st.provider_source)}</span>
        </label>

        {prov === "rius" ? (
          <>
            <p className="help" style={{ margin: 0 }}>
              Create an API key in the Rius console under Settings, API keys and paste it here.{" "}
              <a href={st.rius_console_url} target="_blank" rel="noreferrer">Open Rius ↗</a>
              {" "}{st.key_configured
                ? <><span className="badge ok">key configured</span> <span className="muted">{from(st.key_source)}</span></>
                : <span className="badge error">no key</span>}
            </p>
            <div className="btnrow" style={{ alignItems: "center" }}>
              <input type="password" className="mono" style={{ flex: 1 }} placeholder="gf_…"
                     value={apiKey} onChange={(e) => setApiKey(e.target.value)} />
              <button className="primary" disabled={busy || !apiKey.trim()}
                      onClick={() => apply({ provider: "rius", api_key: apiKey.trim() })}>Save</button>
              {st.key_stored && (
                <button className="danger" disabled={busy}
                        onClick={() => apply({ api_key: "" }, "✓ stored key cleared")}>Clear stored key</button>
              )}
            </div>
          </>
        ) : (
          <>
            <label className="help">Endpoint <span className="muted">{from(st.endpoint_source)}</span>
              <input className="mono" style={{ width: "100%" }} placeholder="https://collector:4318"
                     value={ep} onChange={(e) => setEndpoint(e.target.value)} />
            </label>
            <label className="help">Headers, <code>name=value</code> separated by commas{" "}
              {st.headers_configured && <><span className="badge ok">configured</span> <span className="muted">{from(st.headers_source)}</span></>}
              <input type="password" className="mono" style={{ width: "100%" }}
                     placeholder="authorization=Bearer …, x-other=…"
                     value={headers} onChange={(e) => setHeaders(e.target.value)} />
            </label>
            <div className="btnrow" style={{ alignItems: "center" }}>
              <button className="primary" disabled={busy || !ep.trim()}
                      onClick={() => apply({ provider: "otlp", endpoint: ep.trim(),
                                             ...(headers.trim() ? { headers: headers.trim() } : {}) })}>Save</button>
              {st.headers_configured && st.headers_source === "console" && (
                <button className="danger" disabled={busy}
                        onClick={() => apply({ headers: "" }, "✓ stored headers cleared")}>Clear stored headers</button>
              )}
            </div>
          </>
        )}
      </div>
    </div>
  );
}

// One bot token per instance, behind every `slack://channel/<id>` trigger subscription. Same
// contract as the Anthropic key — env wins, the value is never returned — so this panel is a
// deliberate near-copy rather than a variation the reader has to diff in their head.
function SlackTokenPanel() {
  const [st, setSt] = useState<{ configured: boolean; source: string; stored: boolean; env_overrides: boolean }>();
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string>();
  const [msg, setMsg] = useState<string>();

  const load = () =>
    api.slackTokenStatus().then(setSt).catch((e) => setErr(String((e as Error).message ?? e)));
  useEffect(() => { load(); }, []);

  const save = async () => {
    setBusy(true); setErr(undefined); setMsg(undefined);
    try {
      const r = await api.setSlackToken(token.trim());
      setToken("");
      setMsg(r.note ?? "✓ saved");
      await load();
    } catch (e) { setErr(String((e as Error).message ?? e)); }
    setBusy(false);
  };

  return (
    <div className="panel">
      <h2 style={{ marginTop: 0 }}>Slack bot token</h2>
      <p className="help" style={{ marginTop: 0 }}>
        Lets a trigger post to a channel: subscribe{" "}
        <code>slack://channel/C0123456789</code> on any trigger and every firing is delivered,
        retried and logged like a webhook. Create a Slack app with the <code>chat:write</code>{" "}
        scope, invite it to the channel, and paste its <strong>Bot User OAuth Token</strong> here —
        or set <code>TARES_SLACK_BOT_TOKEN</code> in the daemon's environment. It is never
        returned by the API and never included in a catalog export.
      </p>

      {err && <div className="alert error">{err}</div>}
      {msg && <p className="help">{msg}</p>}

      {!st ? <div className="muted">loading…</div> : (
        <>
          <p style={{ margin: "0 0 10px" }}>
            {st.configured
              ? <><span className="badge ok">configured</span>{" "}
                  <span className="help">from <span className="mono">{st.source}</span></span></>
              : <><span className="badge">not configured</span>{" "}
                  <span className="help">channels cannot be subscribed until one is set</span></>}
          </p>
          {st.env_overrides && st.stored && (
            <div className="alert">
              A token is set in the environment and takes precedence; the one stored here is not in
              use. Remove the environment variable, or clear the stored token to avoid confusion.
            </div>
          )}
          <div className="btnrow" style={{ alignItems: "center", maxWidth: 720 }}>
            <input type="password" className="mono" style={{ flex: 1 }} placeholder="xoxb-…"
                   value={token} onChange={(e) => setToken(e.target.value)} />
            <button className="primary" disabled={busy || !token.trim()} onClick={save}>Save</button>
            {st.stored && (
              <button className="danger" disabled={busy} onClick={async () => {
                setBusy(true);
                try { await api.clearSlackToken(); await load(); }
                catch (e) { setErr(String((e as Error).message ?? e)); }
                setBusy(false);
              }}>Clear stored</button>
            )}
          </div>
        </>
      )}
    </div>
  );
}

// The other half of Slack: the signing secret that authenticates requests coming IN. Same
// write-only contract again, and the same panel shape — but this one is the security boundary for
// the only route that is public to the auth middleware, so the copy says what happens without it.
function SlackSigningSecretPanel() {
  const [st, setSt] = useState<{ configured: boolean; source: string; stored: boolean; env_overrides: boolean }>();
  const [secret, setSecret] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string>();
  const [msg, setMsg] = useState<string>();

  const load = () =>
    api.slackSigningSecretStatus().then(setSt).catch((e) => setErr(String((e as Error).message ?? e)));
  useEffect(() => { load(); }, []);

  const save = async () => {
    setBusy(true); setErr(undefined); setMsg(undefined);
    try {
      const r = await api.setSlackSigningSecret(secret.trim());
      setSecret("");
      setMsg(r.note ?? "✓ saved");
      await load();
    } catch (e) { setErr(String((e as Error).message ?? e)); }
    setBusy(false);
  };

  return (
    <div className="panel">
      <h2 style={{ marginTop: 0 }}>Slack signing secret</h2>
      <p className="help" style={{ marginTop: 0 }}>
        Lets your team ask Tares from Slack: point the Slack app's <code>/tares</code> slash
        command at <code>/api/slack/events</code> on this instance and run{" "}
        <code>/tares ask what happened to checkout-svc?</code> in any channel. Paste the app's{" "}
        <strong>Signing Secret</strong> (Basic Information) here, or set{" "}
        <code>TARES_SLACK_SIGNING_SECRET</code>. Every inbound request is verified against it and
        replays older than 5 minutes are refused; with no secret configured the endpoint answers
        503 rather than trusting anything. The secret is never returned by the API.
      </p>

      {err && <div className="alert error">{err}</div>}
      {msg && <p className="help">{msg}</p>}

      {!st ? <div className="muted">loading…</div> : (
        <>
          <p style={{ margin: "0 0 10px" }}>
            {st.configured
              ? <><span className="badge ok">configured</span>{" "}
                  <span className="help">from <span className="mono">{st.source}</span></span></>
              : <><span className="badge">not configured</span>{" "}
                  <span className="help">inbound Slack requests are refused until one is set</span></>}
          </p>
          {st.env_overrides && st.stored && (
            <div className="alert">
              A signing secret is set in the environment and takes precedence; the one stored here
              is not in use. Remove the environment variable, or clear the stored secret.
            </div>
          )}
          <div className="btnrow" style={{ alignItems: "center", maxWidth: 720 }}>
            <input type="password" className="mono" style={{ flex: 1 }} placeholder="signing secret"
                   value={secret} onChange={(e) => setSecret(e.target.value)} />
            <button className="primary" disabled={busy || !secret.trim()} onClick={save}>Save</button>
            {st.stored && (
              <button className="danger" disabled={busy} onClick={async () => {
                setBusy(true);
                try { await api.clearSlackSigningSecret(); await load(); }
                catch (e) { setErr(String((e as Error).message ?? e)); }
                setBusy(false);
              }}>Clear stored</button>
            )}
          </div>
        </>
      )}
    </div>
  );
}

// Scope semantics (docs/design/api-keys.md): read = consume (queries, catalog reads, an agent's
// own derive/subscribe) · ingest = contribute events · admin = configure the instance.
const SCOPE_HELP: Record<string, string> = {
  read: "consume: queries, timelines, catalog; agents' own views & subscriptions",
  ingest: "contribute: POST events to /ingest and /v1/*, write memories",
  admin: "configure: sources/views/triggers, credentials, keys (implies the rest)",
};

function ApiKeysPanel() {
  const [keys, setKeys] = useState<ApiKey[]>();
  const [enforced, setEnforced] = useState(true);
  const [err, setErr] = useState<string>();
  const [minted, setMinted] = useState<{ name: string; secret: string }>();
  const [revoking, setRevoking] = useState<ApiKey>();
  const [creating, setCreating] = useState(false);

  const load = () => api.keys().then((r) => { setKeys(r.keys); setEnforced(r.enforced); })
    .catch((e) => setErr(String((e as Error).message ?? e)));
  useEffect(() => { load(); }, []);

  return (
    <div className="panel">
      <div className="pagehead" style={{ marginBottom: 6 }}>
        <h2 style={{ margin: 0 }}>API keys</h2>
        <button className="primary" onClick={() => setCreating(true)}>Create key</button>
      </div>
      <p className="help" style={{ marginTop: 0 }}>
        Scoped, revocable credentials; hand each producer and agent its own key instead of a
        shared token. <strong>read</strong>: agents over MCP · <strong>ingest</strong>: producers ·{" "}
        <strong>read + ingest</strong>: the Claude Code plugin · <strong>admin</strong>: full control.
      </p>
      {!enforced && keys && (
        <div className="alert">
          No <code>TARES_AUTH_TOKEN</code> is set, so this instance is open and keys are not
          enforced. Keys become meaningful once the daemon has a root auth token.
        </div>
      )}
      {err && <div className="alert error">{err}</div>}

      {minted && (
        <div className="alert ok">
          Key <strong>{minted.name}</strong> created; copy the secret now; it is not shown again:
          <div className="ingest-url" style={{ marginTop: 8 }}>
            <code className="mono">{minted.secret}</code>
            <CopySecret text={minted.secret} />
          </div>
          <button style={{ marginTop: 8 }} onClick={() => setMinted(undefined)}>done</button>
        </div>
      )}

      {keys && keys.length > 0 ? (
        <table style={{ marginBottom: 14 }}>
          <thead><tr><th>name</th><th>scopes</th><th>key</th><th>created</th><th>last used</th><th></th></tr></thead>
          <tbody>
            {keys.map((k) => (
              <tr key={k.id} style={k.revoked_at ? { opacity: 0.45 } : undefined}>
                <td>{k.name}</td>
                <td>{k.scopes.map((s) => <span className="chip" key={s} title={SCOPE_HELP[s]}>{s}</span>)}</td>
                <td className="mono">{k.prefix}…</td>
                <td className="help"><TimeAgo ts={k.created_at} /></td>
                <td className="help">{k.revoked_at ? "revoked" : k.last_used_at ? <TimeAgo ts={k.last_used_at} /> : "never"}</td>
                <td style={{ textAlign: "right" }}>
                  {!k.revoked_at && (
                    <button className="danger" onClick={() => setRevoking(k)}>revoke</button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <p className="help">no keys yet; <strong>Create key</strong> to issue one for a producer or agent.</p>
      )}

      {creating && (
        <KeyModal
          onClose={() => setCreating(false)}
          onCreated={(m) => { setCreating(false); setMinted(m); load(); }}
        />
      )}

      {revoking && (
        <ConfirmDialog
          title={`Revoke ${revoking.name}?`}
          message="The key stops working immediately and its subscriptions are removed. Producers or agents still using it will start failing."
          confirmLabel="Revoke"
          danger
          onCancel={() => setRevoking(undefined)}
          onConfirm={async () => {
            try { await api.revokeKey(revoking.id); } catch (e) { setErr(String((e as Error).message ?? e)); }
            setRevoking(undefined);
            load();
          }}
        />
      )}
    </div>
  );
}

// Create-key modal — name + scope checkboxes. The secret only exists in the create response, so it
// surfaces once (in the panel's "created" alert) and never again; this dialog just collects inputs.
function KeyModal({ onClose, onCreated }: {
  onClose: () => void;
  onCreated: (m: { name: string; secret: string }) => void;
}) {
  const [name, setName] = useState("");
  const [scopes, setScopes] = useState<string[]>(["read"]);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string>();

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const create = async () => {
    setBusy(true); setErr(undefined);
    try {
      const r = await api.createKey(name.trim(), scopes);
      onCreated({ name: r.name, secret: r.secret });
    } catch (e) { setErr(String((e as Error).message ?? e)); setBusy(false); }
  };

  return (
    <>
      <div className="sheet-overlay" style={{ zIndex: 100 }} onClick={onClose} />
      <div className="modal" role="dialog" aria-modal="true" aria-label="Create API key">
        <div className="sheet-head">
          <div className="sheet-title"><h2 style={{ margin: 0 }}>Create API key</h2></div>
          <button className="sheet-close" onClick={onClose} aria-label="Close"><Close /></button>
        </div>
        <div className="modal-body">
          {err && <div className="alert error">{err}</div>}
          <label className="field">
            <span className="lbl">name</span>
            <input type="text" placeholder="e.g. otel-prod, my-agent" value={name} autoFocus
                   onChange={(e) => setName(e.target.value)} />
            <span className="help">who holds it; one key per producer or agent</span>
          </label>
          <div className="field">
            <span className="lbl">scopes</span>
            {Object.entries(SCOPE_HELP).map(([s, help]) => (
              <label key={s} style={{ display: "flex", gap: 10, alignItems: "baseline",
                                       padding: "5px 0", cursor: "pointer" }}>
                <input type="checkbox" checked={scopes.includes(s)}
                       style={{ transform: "translateY(1px)" }}
                       onChange={(e) => setScopes(e.target.checked
                         ? [...scopes, s] : scopes.filter((x) => x !== s))} />
                <span className="mono" style={{ minWidth: 56 }}>{s}</span>
                <span className="help" style={{ margin: 0 }}>{help}</span>
              </label>
            ))}
          </div>
        </div>
        <div className="sheet-foot">
          <button className="primary" disabled={busy || !name.trim() || scopes.length === 0}
                  onClick={create}>{busy ? "…" : "Create key"}</button>
          <button onClick={onClose}>Cancel</button>
        </div>
      </div>
    </>
  );
}

function CopySecret({ text }: { text: string }) {
  const [done, setDone] = useState(false);
  return (
    <button onClick={() => {
      navigator.clipboard?.writeText(text);
      setDone(true);
      setTimeout(() => setDone(false), 1500);
    }}>{done ? "copied" : "copy"}</button>
  );
}
