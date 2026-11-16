import { createContext, Fragment, useContext, useEffect, useRef, useState } from "react";
import { useLocation } from "react-router-dom";

import { api, type AgentLimits } from "../api";
import ConfirmDialog from "../components/ConfirmDialog";
import { licenceWordsOn, useCloud } from "../cloud";
import type { Edition, EditionSettings, Person, Whoami } from "../types";
import { UsagePanels } from "../components/UsagePanels";
import LinearSettings from "../components/LinearSettings";
import LicencePanel from "../components/LicencePanel";
import { EE_FAILED_WORDS, EePage, LICENCE_NO_ANSWER_WORDS, Slot, useEeFailed, useEeLoaded, useEePending, useLicenceNoAnswer } from "../eeSlots";
import LicenceNeeded from "../components/LicenceNeeded";
import { extraEmails, personDraft, type PersonDraft } from "../personDraft";
import { SlackCloudPanel, SlackSelfHostedPanels } from "../editions/settings/SlackPanels";
import { TracingPanel } from "../editions/settings/TracingPanel";
import { GithubPanel } from "../editions/settings/GithubPanel";
import { ProvidersPanel } from "../editions/settings/ProvidersPanel";
import { ApiKeysPanel } from "../editions/settings/ApiKeysPanel";
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
      {tab === "anthropic" && <ProvidersPanel title="Model providers" intro />}
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
      <ApiKeysPanel title="API keys" intro people={access.people ?? []} admin={access.admin} me={access.me?.person?.id} />
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

export function CopySecret({ text }: { text: string }) {
  const [done, setDone] = useState(false);
  return (
    <button onClick={() => {
      copyText(text).then(() => { setDone(true); setTimeout(() => setDone(false), 1500); }, () => {});
    }}>{done ? "copied" : "copy"}</button>
  );
}
