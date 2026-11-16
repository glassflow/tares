import { createContext, Fragment, useContext, useEffect, useRef, useState } from "react";
import { useLocation } from "react-router-dom";

import { api, type AgentLimits } from "../api";
import ConfirmDialog from "../components/ConfirmDialog";
import { Close } from "../components/icons";
import { InternalName, Picker, TimeAgo, keyTitle } from "../components/bits";
import { licenceWordsOn, useCloud } from "../cloud";
import type { ApiKey, Edition, EditionSettings, ModelProvider, ModelProviders, Person, Whoami } from "../types";
import { UsagePanels } from "../components/UsagePanels";
import LinearSettings from "../components/LinearSettings";
import LicencePanel from "../components/LicencePanel";
import { EE_FAILED_WORDS, EePage, LICENCE_NO_ANSWER_WORDS, Slot, useEeFailed, useEeLoaded, useEePending, useLicenceNoAnswer } from "../eeSlots";
import LicenceNeeded from "../components/LicenceNeeded";
import { extraEmails, personDraft, type PersonDraft } from "../personDraft";
import { SlackCloudPanel, SlackSelfHostedPanels } from "../editions/settings/SlackPanels";
import { TracingPanel } from "../editions/settings/TracingPanel";
import { GithubPanel } from "../editions/settings/GithubPanel";
import AdvancedPanel from "../editions/AdvancedPanel";
import { useEdition } from "../editions/edition";
import WorkspaceSettings from "./WorkspaceSettings";
import ActionError from "../components/ActionError";
import { copyText } from "../editions/copyText";

// Four distinct credential concepts, one box each:
//   · Access     — is this instance open, or does it require a login? (tares up --auth)
//   · API keys   — scoped, revocable, show-once credentials the operator mints for machines
//   · Providers  — the model providers Tares agents (and Ask) run on
//   · Slack      — the bot token behind slack:// trigger subscriptions (outbound), and the
//                  signing secret that authenticates the /tares slash command (inbound)
// The per-source ingest URL is an address, not a secret — it lives on the source page, not here.
//   · Workspace  (Tares Cloud only) team, plan, storage, credit and delete, held by the control
//                  plane (TR-375); shown first, and the default tab, when /health has workspace_api_url
type SettingsTab = "workspace" | "usage" | "access" | "edition" | "licence" | "anthropic" | "agents" | "areas" | "github" | "linear" | "slack" | "observability" | "advanced";
// The tabs as a second sidebar (TR-521): the workspace itself on top, then one group per concern.
const TABS: { key: SettingsTab; label: string; group?: string }[] = [
  { key: "workspace", label: "Workspace" },
  { key: "usage", label: "Usage" },
  { key: "access", label: "Access, people and API keys" },
  { key: "edition", label: "Edition" },
  { key: "licence", label: "Enterprise licence" },
  { key: "anthropic", label: "Model providers", group: "Agents" },
  { key: "agents", label: "Limits", group: "Agents" },
  { key: "observability", label: "Observability", group: "Agents" },
  { key: "areas", label: "Areas" },
  { key: "github", label: "GitHub", group: "Integrations" },
  { key: "linear", label: "Linear", group: "Integrations" },
  { key: "slack", label: "Slack", group: "Integrations" },
  { key: "advanced", label: "Advanced" },   // factory and sre cells only
];

// Coming back from a Tares Cloud connect page (contract §3): `cloud` says what happened and
// `cloud_detail` may add a sentence to show as is. Shown once, on the tab it belongs to.
export const CLOUD_BACK: Record<string, { tab?: SettingsTab; text: string; kind: "ok" | "error" | "" }> = {
  "github-connected": { tab: "github", kind: "ok",
    text: "GitHub is connected. Events from the repositories you picked arrive here as they happen." },
  "github-repos": { tab: "github", kind: "ok",
    text: "Repositories saved. Events from them arrive here as they happen." },
  "github-disconnected": { tab: "github", kind: "",
    text: "GitHub is disconnected. Its events no longer reach this workspace." },
  "slack-connected": { tab: "slack", kind: "ok",
    text: "Slack is connected. Triggers and agents can post to its channels, and your team can ask Tares with /tares ask." },
  "slack-disconnected": { tab: "slack", kind: "",
    text: "Slack is disconnected. Nothing is posted to it any more." },
  "linear-connected": { tab: "linear", kind: "ok",
    text: "Linear is connected. Projects can keep their tickets in a Linear project." },
  "linear-disconnected": { tab: "linear", kind: "",
    text: "Linear is disconnected. Projects linked to it keep their tickets and stop syncing." },
  error: { kind: "error", text: "That did not work. Nothing was changed." },
  cancelled: { kind: "", text: "Cancelled. Nothing was changed." },
};

/** On the factory Settings page the sidebar already says where you are, so these panels drop their
 *  introductions and take the sidebar's words for their headings. */
export const TerseSettings = createContext(false);

export default function Security({ initialTab }: { initialTab?: SettingsTab } = {}) {
  // The Advanced tab holds the building blocks on the factory and sre editions; a platform cell has no such tab.
  const { edition } = useEdition();
  const hasAdvanced = edition !== undefined && edition !== "platform";
  const [cloudBack] = useState(() => {
    const q = new URLSearchParams(window.location.search);
    const m = CLOUD_BACK[q.get("cloud") ?? ""];
    return m ? { ...m, detail: q.get("cloud_detail") ?? "" } : undefined;
  });
  // the tab the address or a click asked for; none: Workspace on Tares Cloud, else Access
  const [picked, setPicked] = useState<SettingsTab | undefined>(() => {
    const t = new URLSearchParams(window.location.search).get("tab");
    return cloudBack?.tab ?? TABS.find((x) => x.key === t)?.key ?? initialTab;
  });
  const [backTab] = useState(picked);   // the tab the cloud message belongs to
  useEffect(() => {   // the message is shown once; a reload starts clean
    const url = new URL(window.location.href);
    if (!url.searchParams.has("cloud") && !url.searchParams.has("cloud_detail")) return;
    url.searchParams.delete("cloud"); url.searchParams.delete("cloud_detail");
    if (backTab) url.searchParams.set("tab", backTab);
    window.history.replaceState(null, "", url.toString());
  }, []);
  // an in-app link to another tab while Settings is open (the switcher's "Workspace settings")
  const loc = useLocation();
  const [firstKey] = useState(loc.key);   // the first render already read the address
  useEffect(() => {
    if (loc.key === firstKey) return;
    const t = TABS.find((x) => x.key === new URLSearchParams(loc.search).get("tab"))?.key;
    if (t) setPicked(t);
  }, [loc.key, loc.search]);
  const pick = (t: SettingsTab) => {
    setPicked(t);
    const url = new URL(window.location.href); url.searchParams.set("tab", t);
    window.history.replaceState(null, "", url.toString());
  };
  const cloud = useCloud();
  const eeLoaded = useEeLoaded();   // Areas is an enterprise tab
  const slackConnectUrl = cloud.health?.slack_connect_url;
  const workspaceApiUrl = cloud.health?.workspace_api_url || undefined;
  // Workspace exists only on Tares Cloud; asked for elsewhere (an old link), Access shows instead.
  // With no tab asked for, the default waits for /health (Workspace on Tares Cloud, else Access),
  // so a hard load does not show Access and then jump.
  // The licence tab is for self-hosted cells: Tares Cloud needs no licence, and until /health has
  // answered, or when it fails, the cell is not known to be self-hosted, so an asked-for licence tab falls back to the default.
  const want = picked === "advanced" && !hasAdvanced ? undefined
    : picked === "licence" && !licenceWordsOn() ? undefined : picked;
  const tab: SettingsTab | undefined = workspaceApiUrl ? (want ?? "workspace")
    : want === "workspace" ? (cloud.ready ? "access" : "workspace")
    : want ?? (cloud.ready ? "access" : undefined);
  // Tares Cloud: Workspace (team, plan, storage, credit, model spend). Self-hosted: Usage (model
  // spend and storage) in its place.
  const tabs = TABS.filter((t) => t.key === "workspace" ? !!workspaceApiUrl
    : t.key === "usage" ? !workspaceApiUrl
    : t.key === "advanced" ? hasAdvanced
    : t.key === "areas" ? eeLoaded
    : t.key === "licence" ? licenceWordsOn() : true);
  const groups = [...new Set(tabs.map((t) => t.group))];
  const link = (t: (typeof TABS)[number]) => (
    <button key={t.key} type="button" aria-current={tab === t.key ? "page" : undefined}
            className={`pnav-link${t.group ? " pnav-item pnav-plain" : ""}${tab === t.key ? " active" : ""}`}
            onClick={() => pick(t.key)}>
      <span className="pnav-label">{t.label}</span>
    </button>
  );
  return (
    <>
      <h1>Settings</h1>
      <p className="subtitle">What applies to the whole workspace, not to one project.</p>
      <div className="project-body settings-body">
      <nav className="pnav" aria-label="Settings">
        {tab !== undefined && groups.map((g) => g
          ? <div className="pnav-group" key={g}>
              <div className="pnav-head"><span className="pnav-title pnav-static">{g}</span></div>
              {tabs.filter((t) => t.group === g).map(link)}
            </div>
          : <Fragment key="top">{tabs.filter((t) => !t.group).map(link)}</Fragment>)}
      </nav>
      {tab !== undefined && (
        <label className="pnav-select">
          <span className="lbl">show</span>
          <select value={tab} onChange={(e) => pick(e.target.value as SettingsTab)}>
            {groups.map((g) => g
              ? <optgroup key={g} label={g}>
                  {tabs.filter((t) => t.group === g).map((t) => <option key={t.key} value={t.key}>{t.label}</option>)}
                </optgroup>
              : tabs.filter((t) => !t.group).map((t) => <option key={t.key} value={t.key}>{t.label}</option>))}
          </select>
        </label>
      )}
      <div className="pview">
      {cloudBack && (backTab ? tab === backTab : picked === undefined) && (
        <div className={"alert" + (cloudBack.kind ? ` ${cloudBack.kind}` : "")}
             role={cloudBack.kind === "error" ? "alert" : "status"}>
          {cloudBack.kind === "error" && cloudBack.detail ? cloudBack.detail
            : <>{cloudBack.text}{cloudBack.detail && <> {cloudBack.detail}</>}</>}
        </div>
      )}
      {tab === undefined && <div className="panel"><div className="muted">loading…</div></div>}
      {tab === "workspace" && (workspaceApiUrl
        ? <WorkspaceSettings cloud={cloud} apiUrl={workspaceApiUrl} onOpenTab={pick} />
        : <div className="panel"><div className="muted">loading…</div></div>)}
      {tab === "usage" && <UsagePanels />}
      {tab === "access" && <AccessTab />}
      {tab === "edition" && <EditionPanel />}
      {tab === "licence" && <LicencePanel />}
      {tab === "anthropic" && <ProvidersPanel />}
      {tab === "agents" && <AgentLimitsPanel agent={new URLSearchParams(loc.search).get("agent") ?? undefined} />}
      {tab === "areas" && <EePage slot="page.settings.areas" />}
      {tab === "github" && <GithubPanel cloud={cloud} title="GitHub" intro />}
      {tab === "linear" && <LinearSettings cloud={cloud} />}
      {/* Until /health answers (it can be slow on a busy instance) the self-host panels show, so
          a self-hosted Slack tab never waits on it; a cloud cell swaps to its panel once it does. */}
      {tab === "slack" && (slackConnectUrl ? <SlackCloudPanel cloud={cloud} connectUrl={slackConnectUrl} title="Slack" />
        : <SlackSelfHostedPanels />)}
      {tab === "observability" && <TracingPanel title="Agent tracing" intro />}
      {tab === "advanced" && <AdvancedPanel />}
      </div>
      </div>
    </>
  );
}

// Access, people and keys share one load: who is asking (admin or not) and who is on the cell, so
// a person added in People is there to pick when a key is made. Only an admin credential manages
// people and keys (the daemon enforces it); the page just does not offer what it would refuse.
/** Who is asking and who is on the cell: what the people and key panels read. */
export function useAccessData() {
  const [me, setMe] = useState<Whoami>();
  const [people, setPeople] = useState<Person[]>();
  const [err, setErr] = useState<string>();
  const reload = () => api.people().then((r) => { setPeople(r.people); setErr(undefined); })
    .catch((e) => setErr(String((e as Error).message ?? e)));
  useEffect(() => {
    api.whoami().then(setMe).catch(() => undefined);
    reload();
  }, []);
  return { me, people, err, reload, admin: !!me?.scopes.includes("admin") };
}

export function PeopleBlock({ access }: { access: ReturnType<typeof useAccessData> }) {
  const pending = useEePending();
  const eeFailed = useEeFailed();
  const noAnswer = useLicenceNoAnswer();
  const { me, people, err, reload, admin } = access;
  return (
    <Slot name="settings.people" people={people} admin={admin} err={err} onChanged={reload} me={me}
          fallback={pending ? <div className="panel muted">loading…</div>
                            : eeFailed ? <div className="panel muted" data-testid="ee-failed">{EE_FAILED_WORDS}</div>
                            : noAnswer ? <div className="panel muted" data-testid="licence-no-answer">{LICENCE_NO_ANSWER_WORDS}</div>
                            : <YouPanel people={people} admin={admin} err={err} onChanged={reload} me={me} />} />
  );
}

function AccessTab() {
  const access = useAccessData();
  return (
    <>
      <AccessPanel me={access.me} />
      <PeopleBlock access={access} />
      <McpToolsPanel />
      <ApiKeysPanel people={access.people ?? []} admin={access.admin} me={access.me?.person?.id} />
    </>
  );
}

// Without the enterprise licence the cell has one person for you to manage: yourself. The row shows
// the name, email and GitHub login, and Edit changes them (an admin changes all three, anyone else
// only the name; the daemon decides). Adding anyone else is the licence's, so the line says so.
function YouPanel({ people, admin, err: loadErr, onChanged, me }: {
  people?: Person[]; admin: boolean; err?: string; onChanged: () => void; me?: Whoami;
}) {
  const mine = people?.find((p) => p.id === me?.person?.id);
  const [editing, setEditing] = useState<PersonDraft>();
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<unknown>();
  const save = async () => {
    if (!editing) return;
    setBusy(true); setErr(undefined);
    try {
      await api.updatePerson(editing.id, admin
        ? { name: editing.name.trim(), email: editing.email.trim(), github: editing.github.trim(), emails: extraEmails(editing) }
        : { name: editing.name.trim() });
      setEditing(undefined);
      onChanged();
    } catch (e) { setErr(e); }
    setBusy(false);
  };
  return (
    <div className="panel">
      <h2 style={{ marginTop: 0, marginBottom: 6 }}>People</h2>
      <p className="help" style={{ marginTop: 0 }}>
        You are the one person on this instance. Each API key belongs to a person, so you can tell whose
        agent or script is acting.
      </p>
      {(loadErr || err != null) && <ActionError error={err ?? loadErr} what="Changing your details" />}
      {!people ? <div className="muted">loading…</div> : mine && (editing ? (
        <>
          <div className="row2">
            <label className="field">
              <span className="lbl">name</span>
              <input type="text" value={editing.name} autoFocus onChange={(e) => setEditing({ ...editing, name: e.target.value })} />
            </label>
            {admin && (
              <label className="field">
                <span className="lbl">email</span>
                <input type="email" value={editing.email} onChange={(e) => setEditing({ ...editing, email: e.target.value })} />
              </label>
            )}
          </div>
          {admin && (
            <label className="field">
              <span className="lbl">GitHub login</span>
              <input type="text" className="mono" placeholder="e.g. octocat" value={editing.github}
                     onChange={(e) => setEditing({ ...editing, github: e.target.value })} />
            </label>
          )}
          <div className="btnrow">
            <button className="primary" disabled={busy || !editing.name.trim()} onClick={save}>{busy ? "…" : "Save"}</button>
            <button disabled={busy} onClick={() => setEditing(undefined)}>Cancel</button>
          </div>
        </>
      ) : (
        <div className="table-scroll">
        <table>
          <thead><tr><th>name</th><th>email</th><th>GitHub</th><th aria-label="actions" /></tr></thead>
          <tbody>
            <tr data-testid="you-row">
              <td><strong>{mine.id === "local" && mine.name === "You" && me?.kind !== "open" ? "Local (no login)" : mine.name}</strong></td>
              <td className="help">{mine.email ?? ""}</td>
              <td className="mono help">{mine.github ?? ""}</td>
              <td style={{ textAlign: "right" }}><button onClick={() => { setErr(undefined); setEditing(personDraft(mine)); }}>Edit</button></td>
            </tr>
          </tbody>
        </table>
        </div>
      ))}
      <LicenceNeeded what="Adding people" />
    </div>
  );
}

// Auth is set at launch: `tares up` is open; `tares up --auth` requires a login (a root token
// printed to the terminal). This box states which mode you're in — there's no runtime toggle, so
// it's status, not a control.
export function AccessPanel({ me }: { me?: Whoami }) {
  const terse = useContext(TerseSettings);
  const [authRequired, setAuthRequired] = useState<boolean>();
  useEffect(() => {
    api.health().then((h) => setAuthRequired(h.auth_required)).catch(() => setAuthRequired(undefined));
  }, []);

  if (terse && authRequired !== false) return null;   // only an open cell has something to say here
  return (
    <div className="panel">
      <h2 style={{ marginTop: 0 }}>Access</h2>
      {authRequired === undefined ? <div className="muted">loading…</div>
        : authRequired ? (
          <p style={{ margin: 0 }}>
            <span className="badge ok">auth on</span>{" "}
            <span className="help">
              the console and API require a login.{" "}
              {!me ? null
                : me.kind === "system" ? <>You signed in with the root token printed by <code>tares up --auth</code>.{" "}</>
                : me.kind === "project" ? <>You signed in with a project key for {me.project_name ?? me.project}.{" "}</>
                : <>You signed in with the API key <strong>{me.name}</strong>
                    {me.person ? <>, which belongs to {me.person.name}</> : <>, which belongs to no one</>}.{" "}</>}
              Hand machines their own scoped <strong>API keys</strong> below; never the root token.
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

// Whether agents that connect over MCP see the building-block tools (TR-757). On by default on a
// platform cell; off on a factory or AI SRE cell until an admin turns it on. Only an admin sees it.
export function McpToolsPanel() {
  const [on, setOn] = useState<boolean>();
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string>();
  useEffect(() => {
    api.whoami().then((w) => w.scopes.includes("admin") ? api.mcpSettings().then((s) => setOn(s.object_tools)) : undefined)
      .catch((e) => setErr(String((e as Error).message ?? e)));
  }, []);
  if (on === undefined && !err) return null;
  const toggle = async (next: boolean) => {
    setBusy(true); setErr(undefined);
    try { setOn((await api.setMcpObjectTools(next)).object_tools); }
    catch (e) { setErr(String((e as Error).message ?? e)); }
    setBusy(false);
  };
  return (
    <div className="panel">
      <h2 style={{ marginTop: 0 }}>Agent tools</h2>
      {err && <div className="alert error">{err}</div>}
      <label className="field" style={{ display: "flex", flexDirection: "row", gap: 10, alignItems: "flex-start" }}>
        <input type="checkbox" checked={!!on} disabled={busy || on === undefined} style={{ marginTop: 3 }}
               onChange={(e) => toggle(e.target.checked)} />
        <span>
          <strong>Let agents use the building blocks (connections, raw data, rules)</strong><br />
          <span className="help">
            Agents that connect over MCP get the low-level tools as well as the ones named for what you want done.
            Crew sessions always have them. Project keys lose these tools unless this is on. Agents that are
            already connected need to reconnect after a change. This only shortens the list; what a key may do
            is set by its scopes.
          </span>
        </span>
      </label>
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
  { id: "xai", label: "xAI (Grok)", kind: "openai_compatible", name: "xAI", base_url: "https://api.x.ai/v1", key: true },
  { id: "ollama", label: "Ollama", kind: "openai_compatible", name: "Ollama", base_url: "http://localhost:11434/v1", key: false },
  { id: "other", label: "Other OpenAI-compatible", kind: "openai_compatible", name: "", base_url: "", key: true },
];

export function ProvidersPanel() {
  const terse = useContext(TerseSettings);
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
        ? `✓ saved, but ${saved.models_problem || "the endpoint did not list its models"}. Agents can still name a model by hand.`
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
        <h2 style={{ margin: 0 }}>{terse ? "Providers" : "Model providers"}</h2>
        {!editing && <button className="primary" onClick={startAdd}>Add provider</button>}
      </div>
      {!terse && (<p className="help">
        What <strong>Tares agents</strong> and Ask run on. Add the providers you hold keys for; each
        agent picks a provider and a model, and the <strong>default</strong> is what Ask and any agent
        without its own choice use. Values saved here win over the daemon's environment
        (<code>ANTHROPIC_API_KEY</code>, <code>OPENAI_API_KEY</code>, <code>TARES_MODEL_PROVIDER</code>);
        credentials are never returned by the API and never included in a catalog export.
      </p>)}

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
          <div className="table-scroll">
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
                    {!p.configured
                      ? <span className="badge error">no key</span>
                      : p.models_problem?.startsWith("the endpoint rejected the key")
                        ? <><span className="badge error">key rejected</span><span className="help"> by the endpoint; edit and save a valid key</span></>
                        : <><span className="badge ok">configured</span>{p.source && <span className="help"> from <span className="mono">{p.source}</span></span>}</>}
                  </td>
                  <td style={{ minWidth: 220 }}>
                    {p.models_error
                      ? <span className="help" title={p.models_error}><span className="badge error">not listed</span> {p.models_problem}{p.models.length > 0 ? `; the picker offers ${p.models.length} ${p.discovers && p.models_at ? "from the last read" : "built in"}` : ""}</span>
                      : <span className="help" title={p.models.slice(0, 40).join("\n")}>{p.models.length}{p.discovers ? " from the endpoint" : " built in"}</span>}
                  </td>
                  <td style={{ width: 270 }}>
                    <div className="btnrow" style={{ justifyContent: "flex-end", flexWrap: "wrap" }}>
                      {p.discovers && p.configured && <button style={{ whiteSpace: "nowrap" }} disabled={busy} onClick={() => refresh(p.id)} title="re-read the models this endpoint serves">Refresh models</button>}
                      {!p.default && p.configured && <button style={{ whiteSpace: "nowrap" }} disabled={busy} onClick={() => makeDefault(p.id)}>Make default</button>}
                      <button disabled={busy} onClick={() => startEdit(p)}>Edit</button>
                      {(p.stored || p.kind !== "anthropic") && <button className="danger" disabled={busy} onClick={() => setConfirmDelete(p)}>Remove</button>}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          </div>
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
// Runs per agent per day (TR-325). A guard against a runaway trigger, not a bill: spend is bounded
// by each agent's budget. Saved here it replaces the deployment's value; cleared, that comes back.
// `agent` is who the person came for (a project's "Raise the limit", TR-670): the field is scrolled
// into view and marked, since the one number applies to every agent without its own limit.
export function AgentLimitsPanel({ agent }: { agent?: string }) {
  const terse = useContext(TerseSettings);
  const [st, setSt] = useState<AgentLimits>();
  const field = useRef<HTMLLabelElement>(null);
  const [value, setValue] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string>();
  const [msg, setMsg] = useState<string>();

  const load = () => api.agentLimits()
    .then((s) => { setSt(s); setValue(String(s.daily_cap)); })
    .catch((e) => setErr(String((e as Error).message ?? e)));
  useEffect(() => { load(); }, []);
  useEffect(() => {
    if (agent && st) field.current?.scrollIntoView({ block: "center" });
  }, [agent, !!st]);

  const save = async (daily_cap: string | null, done: string) => {
    setBusy(true); setErr(undefined); setMsg(undefined);
    try { await api.setAgentLimits({ daily_cap }); setMsg(done); await load(); }
    catch (e) { setErr(String((e as Error).message ?? e)); }
    setBusy(false);
  };

  if (!st) return <div className="panel"><h2 style={{ marginTop: 0 }}>Agent limits</h2>
    {err ? <div className="alert error">{err}</div> : <div className="muted">loading…</div>}</div>;

  const from = st.daily_cap_source === "console" ? "saved here"
    : st.daily_cap_source === "env" ? `from ${st.env}` : "the default";

  return (
    <div className="panel">
      <h2 style={{ marginTop: 0 }}>Agent limits</h2>
      {!terse && (<p className="help" style={{ marginTop: 0 }}>
        How many times each Tares agent may run in 24 hours. When an agent reaches it, further
        firings are recorded as capped and cost nothing until the window moves on. An agent on a
        schedule every 10 minutes runs 144 times a day. Spend is bounded separately by each
        agent's budget.
      </p>)}
      {err && <div className="alert error">{err}</div>}
      {msg && <p className="help">{msg}</p>}
      <div className="row2">
        <label className={`field${agent ? " field-focus" : ""}`} ref={field}
               data-testid="daily-cap-field">
          <span className="lbl">runs per agent per day</span>
          <input type="number" min={1} max={10000} value={value} disabled={busy}
                 onChange={(e) => setValue(e.target.value)} />
          <span className="help">now {st.daily_cap}, {from}</span>
          {agent && (
            <span className="help">{agent} stopped at this limit. Raise it here and {agent} runs
              again on its next trigger; the number applies to every agent
              without its own limit.</span>
          )}
        </label>
      </div>
      <div className="btnrow">
        <button className="primary" disabled={busy || value.trim() === String(st.daily_cap)}
                onClick={() => save(value.trim(), "✓ saved; applies from the next run")}>Save</button>
        {st.daily_cap_source === "console" && (
          <button disabled={busy} onClick={() => save(null, "✓ cleared")}>
            Use the deployment's value or the default
          </button>
        )}
      </div>
    </div>
  );
}

// The cell's edition (TR-724): which set of screens the console shows. Anyone can see it; only an
// admin can switch it. The data, projects and crew stay as they are; the console reloads after.
const EDITION_BLURBS: Record<Edition, string> = {
  factory: "Projects, tickets and reviews, with a crew of Claude Code sessions building them.",
  sre: "Services, incidents and runbooks, watched by agents that tell you what needs you.",
  platform: "Every building block on its own: sources, triggers, agents and projects.",
};

function EditionPanel() {
  const [st, setSt] = useState<EditionSettings>();
  const [admin, setAdmin] = useState<boolean>();
  const [now, setNow] = useState<string>();
  const [pick, setPick] = useState<Edition>();
  const [ask, setAsk] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string>();

  // whoami carries the current edition for every key; the choices come from the admin-only route.
  useEffect(() => {
    api.whoami().then((w) => {
      const isAdmin = w.scopes.includes("admin");
      setAdmin(isAdmin);
      setNow(w.edition_name);
      if (!isAdmin) return;
      return api.edition().then((s) => { setSt(s); setPick(s.edition); });
    }).catch((e) => { setAdmin(false); setErr(String((e as Error).message ?? e)); });
  }, []);

  if (admin === false && now) return (
    <div className="panel">
      <h2 style={{ marginTop: 0 }}>Edition</h2>
      <p className="help" style={{ marginTop: 0 }}>Which screens this workspace shows. Now: {now}.</p>
      <p className="help">Only an admin can change the edition.</p>
    </div>
  );
  if (!st || pick === undefined) return <div className="panel"><h2 style={{ marginTop: 0 }}>Edition</h2>
    {err ? <div className="alert error">{err}</div> : <div className="muted">loading…</div>}</div>;

  const nameOf = (e: Edition) => st.choices.find((c) => c.id === e)?.name ?? e;
  const save = async () => {
    setBusy(true); setErr(undefined);
    try { await api.setEdition(pick); window.location.reload(); }
    catch (e) { setErr(String((e as Error).message ?? e)); setAsk(false); setBusy(false); }
  };

  return (
    <div className="panel">
      <h2 style={{ marginTop: 0 }}>Edition</h2>
      <p className="help" style={{ marginTop: 0 }}>
        Which screens this workspace shows. Your projects, data and crew are the same in every edition.
      </p>
      {err && <div className="alert error">{err}</div>}
      <div role="radiogroup" aria-label="Edition">
        {st.choices.map((c) => (
          <label key={c.id} className="field" style={{ display: "flex", flexDirection: "row", gap: 8, alignItems: "flex-start" }}>
            <input type="radio" name="edition" value={c.id} checked={pick === c.id}
                   disabled={busy} onChange={() => setPick(c.id)} />
            <span><strong>{c.name}</strong><br /><span className="help">{EDITION_BLURBS[c.id]}</span></span>
          </label>
        ))}
      </div>
      <p className="help">Now: {st.edition_name}</p>
      <div className="btnrow">
        <button className="primary" disabled={busy || pick === st.edition} onClick={() => setAsk(true)}>Save</button>
      </div>
      {ask && (
        <ConfirmDialog title={`Switch to ${nameOf(pick)}?`} confirmLabel="Switch"
          message={`Your projects, data and crew stay as they are. The screens change from ${nameOf(st.edition)} to ${nameOf(pick)}; what only ${nameOf(st.edition)} shows stays reachable under Settings, Advanced.`}
          onConfirm={save} onCancel={() => setAsk(false)} />
      )}
    </div>
  );
}

// Scope semantics (docs/design/api-keys.md): read = consume (queries, catalog reads, an agent's
// own derive/subscribe) · ingest = contribute events · admin = configure the instance.
const SCOPE_HELP: Record<string, string> = {
  read: "consume: reads, timelines, catalog; agents' own subscriptions",
  ingest: "contribute: POST events to /ingest and /v1/*, write memories",
  admin: "configure: sources, triggers, projects, credentials, keys (implies the rest)",
};

export function ApiKeysPanel({ people, admin, me }: { people: Person[]; admin: boolean; me?: string }) {
  const terse = useContext(TerseSettings);
  const [keys, setKeys] = useState<ApiKey[]>();
  const [enforced, setEnforced] = useState(true);
  const [err, setErr] = useState<string>();
  const [minted, setMinted] = useState<{ name: string; secret: string }>();
  const [revoking, setRevoking] = useState<ApiKey>();
  const [creating, setCreating] = useState(false);
  const [showRevoked, setShowRevoked] = useState(false);
  const load = () => api.keys().then((r) => { setKeys(r.keys); setEnforced(r.enforced); })
    .catch((e) => setErr(String((e as Error).message ?? e)));
  useEffect(() => { load(); }, []);
  // a project key reads one project; the table names it (they are made on the project's page)
  const [projectNames, setProjectNames] = useState<Record<string, string>>({});
  useEffect(() => {
    api.projectsWithHidden().then((r) => setProjectNames(Object.fromEntries(r.projects.map((p) => [p.id, p.name]))))
      .catch(() => undefined);
  }, []);

  return (
    <div className="panel">
      <div className="pagehead" style={{ marginBottom: 6 }}>
        <h2 style={{ margin: 0 }}>{terse ? "Access keys" : "API keys"}</h2>
        <button className="primary" onClick={() => setCreating(true)}>Create key</button>
      </div>
      {!terse && (<><p className="help" style={{ marginTop: 0, marginBottom: 6 }}>
        Give each agent, script or service its own key, so you can revoke one without touching the others.
        What a key can do:
      </p>
      <ul className="help" style={{ marginTop: 0, paddingLeft: 18 }}>
        <li><strong>read</strong>: look things up, for an agent connecting over MCP</li>
        <li><strong>ingest</strong>: send events in, for a script or service that pushes logs or webhooks</li>
        <li><strong>read + ingest</strong>: both, for the Claude Code plugin</li>
        <li><strong>admin</strong>: everything, settings included</li>
      </ul></>)}
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
        <>
        <div className="table-scroll" style={{ marginBottom: 14 }}>
        <table>
          <thead><tr><th>name</th><th>person</th><th>scopes</th><th>key</th><th>created</th><th>last used</th><th></th></tr></thead>
          <tbody>
            {/* live keys first; revoked ones only on request, they are history */}
            {[...keys.filter((k) => !k.revoked_at), ...(showRevoked ? keys.filter((k) => k.revoked_at) : [])].map((k) => (
              <tr key={k.id} style={k.revoked_at ? { opacity: 0.45 } : undefined}>
                <td>{keyTitle(k.name) ? <>{keyTitle(k.name)}<InternalName name={k.name} /></> : k.name}</td>
                <td><Slot name="settings.keyPerson" k={k} people={people} admin={admin} onChanged={load}
                          onError={setErr}
                          fallback={k.person ? k.person.name : <span className="help">No person</span>} /></td>
                <td>{k.scopes.map((s) => <span className="chip" key={s} title={SCOPE_HELP[s]} data-words-exempt>{s}</span>)}
                  {k.project && <span className="help"> · project {projectNames[k.project] ?? k.project} only</span>}</td>
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
        </div>
        {keys.some((k) => k.revoked_at) && (
          <button type="button" className="linklike" onClick={() => setShowRevoked((v) => !v)}>
            {showRevoked ? "Hide revoked keys" : `Show ${keys.filter((k) => k.revoked_at).length} revoked`}
          </button>
        )}
        </>
      ) : (
        <p className="help">No keys yet. <strong>Create key</strong> to make one for an agent, script or service.</p>
      )}

      {creating && (
        <KeyModal
          people={people} defaultPerson={me}
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
function KeyModal({ people, defaultPerson, onClose, onCreated }: {
  people: Person[]; defaultPerson?: string;
  onClose: () => void;
  onCreated: (m: { name: string; secret: string }) => void;
}) {
  const [name, setName] = useState("");
  const [scopes, setScopes] = useState<string[]>(["read"]);
  // the licensed picker may change it (or pick no one); without it the key is made for you
  const [personId, setPersonId] = useState(defaultPerson ?? "");
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
      const r = await api.createKey(name.trim(), scopes, personId || undefined);
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
            <span className="help">who uses it: one key per agent, script or service</span>
          </label>
          <Slot name="settings.keyPersonPick" people={people} value={personId} onChange={setPersonId} />
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

export function CopySecret({ text }: { text: string }) {
  const [done, setDone] = useState(false);
  return (
    <button onClick={() => {
      copyText(text).then(() => { setDone(true); setTimeout(() => setDone(false), 1500); }, () => {});
    }}>{done ? "copied" : "copy"}</button>
  );
}
