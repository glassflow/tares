import { Fragment, useEffect, useState } from "react";
import { useLocation } from "react-router-dom";

import { api } from "../api";
import ConfirmDialog from "../components/ConfirmDialog";
import { licenceWordsOn, useCloud } from "../cloud";
import type { Edition, EditionSettings } from "../types";
import { UsagePanels } from "../components/UsagePanels";
import LinearSettings from "../components/LinearSettings";
import LicencePanel from "../components/LicencePanel";
import { EePage, useEeLoaded } from "../eeSlots";
import { SlackCloudPanel, SlackSelfHostedPanels } from "../editions/settings/SlackPanels";
import { TracingPanel } from "../editions/settings/TracingPanel";
import { GithubPanel } from "../editions/settings/GithubPanel";
import { ProvidersPanel } from "../editions/settings/ProvidersPanel";
import { ApiKeysPanel } from "../editions/settings/ApiKeysPanel";
import { McpToolsPanel } from "../editions/settings/McpToolsPanel";
import { AgentLimitsPanel } from "../editions/settings/AgentLimitsPanel";
import { AccessPanel, PeopleBlock, useAccessData } from "../editions/settings/AccessPanels";
import AdvancedPanel from "../editions/AdvancedPanel";
import { useEdition } from "../editions/edition";
import WorkspaceSettings from "./WorkspaceSettings";
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

function AccessTab() {
  const access = useAccessData();
  // Kit panels carry no margin of their own, so the tab stacks them with a gap.
  return (
    <div className="grid min-w-0 gap-4">
      <AccessPanel me={access.me} />
      <PeopleBlock access={access} />
      <McpToolsPanel title="Agent tools" />
      <ApiKeysPanel title="API keys" intro people={access.people ?? []} admin={access.admin} me={access.me?.person?.id} />
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
