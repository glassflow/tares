import * as React from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";

import { api, ApiError } from "../../api";
import { licenceWordsOn, slugFromHost, useCloud, useWorkspaceOverview } from "../../cloud";
import LicencePanel from "../../components/LicencePanel";
import LinearSettings from "../../components/LinearSettings";
import { UsagePanels } from "../../components/UsagePanels";
import { EePage, useEeLoaded, useEePending } from "../../eeSlots";
import {
  AccessPanel, AgentLimitsPanel, ApiKeysPanel, CLOUD_BACK, GithubPanel, McpToolsPanel, PeopleBlock, ProvidersPanel,
  SlackCloudPanel, SlackSigningSecretPanel, SlackTokenPanel, TerseSettings, TracingPanel, useAccessData,
} from "../../pages/Security";
import WorkspaceSettings, { TeamPanel } from "../../pages/WorkspaceSettings";
import { Banner, Button, Dialog, ListRow, Skeleton, StatusDot, Textarea } from "../../components/edition";
import { Switch } from "../../components/ui/switch";
import { cn } from "../../lib/utils";
import { SettingsRefused, useSettingsRefused } from "../AdminOnly";
import { usePagePolling } from "../usePagePolling";
import { useSample } from "../sample";
import {
  addGrant, changeCrew, factorySettings, saveMorning, sendMorningNow, switchEdition,
  type Autonomy, type CrewChange, type MorningResult, type MorningStatus, type SettingsGrant, type SettingsPage, type SettingsTool,
} from "./FactorySettings.api";
import { isSample, SAMPLE_PEOPLE, sampleLoad } from "./settingsSamples";
import { MORNING_OFF, MorningStatusPane } from "./MorningStatus";
import type { Person } from "../../types";
import { FIRST_SETTING, settingFrom, shownGroups, type SettingGroup, type SettingId } from "./settingsNav";
import { placeBeforeSettings } from "../settingsBack";

// Settings has a sidebar of two levels and shows one item at a time. The crew's items and the tools'
// status come from one read of the page's data; a change goes through the route that already exists
// and the page reads again. The other items are the console's own panels, without their introductions.

const POLL_MS = 30_000;
const MAX_GRANT = 400;
const SAVE_FAILED = "That change was not saved. Try again in a moment.";

const ROW_TEXT = "min-w-0 flex-[1_1_320px]";
const HEAD = "flex items-center justify-between gap-3 border-b border-border bg-muted px-4 py-3";
const CHOICE = "flex! w-full items-start gap-3 border-0! border-b! border-border! bg-transparent! px-4! py-3.5! text-left font-normal! text-foreground! cursor-pointer disabled:cursor-not-allowed hover:bg-[var(--row-hover)]! last:border-b-0!";
const LINK_BTN = "border-0! bg-transparent! p-0! text-[13px]! font-normal! text-[color:var(--accent-deep)]! underline! cursor-pointer";

function Card({ dim, className, ...props }: { dim?: boolean } & React.HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("border border-border bg-card", dim && "opacity-50", className)} {...props} />;
}

function Heading({ title, why, children }: { title: string; why?: string; children?: React.ReactNode }) {
  return (
    <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2">
      <div className="min-w-0">
        <h2 className="m-0! text-xl!">{title}</h2>
        {why && <p className="m-0! text-[13px] text-muted-foreground">{why}</p>}
      </div>
      {children}
    </div>
  );
}

function Row({ title, text, user, children, ...props }: { title: string; text: string; user?: boolean; children?: React.ReactNode } & React.HTMLAttributes<HTMLDivElement>) {
  return (
    <ListRow {...props}>
      <div className={ROW_TEXT}>
        <div className="font-semibold">{title}</div>
        <div className="mt-0.5 text-[13px] text-muted-foreground" {...(user ? { "data-user-text": true } : {})}>{text}</div>
      </div>
      {children && <div className="flex flex-none items-center gap-2">{children}</div>}
    </ListRow>
  );
}

/** A switch with its own words; `label` names it for people who cannot see the row. */
function Toggle({ label, on, off, disabled, onChange, testid }: { label: string; on: boolean; off?: boolean; disabled: boolean; onChange: (v: boolean) => void; testid: string }) {
  return <Switch aria-label={label} checked={on} disabled={disabled || off} onCheckedChange={onChange} data-testid={testid} />;
}

/** Where a tool stands, above its own settings. */
function ToolStatus({ t, dim }: { t: SettingsTool; dim: boolean }) {
  return (
    <Card dim={dim}>
      <ListRow data-testid={`tool-${t.key}`}>
        <div className={ROW_TEXT}>
          <div className="font-semibold" data-user-text>
            <StatusDot tone={t.connected ? "ready" : "quiet"} className="mr-2 align-baseline" data-testid="tool-dot" data-on={t.connected} />
            {t.detail}
          </div>
          {t.covers && <div className="mt-0.5 text-[13px] text-muted-foreground" data-user-text data-testid="tool-covers">{t.covers}</div>}
        </div>
        {t.key === "claude_code" && (
          <div className="flex flex-none items-center gap-2">
            <Button asChild variant={t.connected ? "secondary" : "primary"} disabled={dim} data-ctl><Link to={t.href}>{t.connected ? "See how" : "Connect"}</Link></Button>
          </div>
        )}
      </ListRow>
    </Card>
  );
}

const CHOICES: { value: Autonomy; title: string; text: string }[] = [
  { value: "L3-review", title: "You approve each merge", text: "The crew builds and reviews, then waits for you." },
  { value: "L4-ship", title: "The crew merges on its own", text: "After the reviewer passes it and CI is green." },
];

function GrantRow({ g }: { g: SettingsGrant }) {
  return (
    <div className="border-b border-border px-4 py-3 last:border-b-0 [overflow-wrap:anywhere]" data-testid="grant">
      <div><span data-user-text data-testid="grant-text">{`“${g.text}”`}</span></div>
      {g.question && (
        <div className="mt-1 text-[13px]" data-testid="grant-question">Answer to: <span data-user-text>{g.question}</span></div>
      )}
      <div className="mt-1 text-xs text-muted-foreground" data-user-text data-testid="grant-meta">{[g.scope_text, g.state_text].filter(Boolean).join(", ")}</div>
    </div>
  );
}

function GrantDialog({ open, onOpenChange, onAdd }: { open: boolean; onOpenChange: (o: boolean) => void; onAdd: (words: string) => Promise<string | null> }) {
  const [words, setWords] = React.useState("");
  const [error, setError] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    const w = words.trim();
    if (!w) { setError("Write what the crew may do first."); return; }
    setBusy(true);
    const failed = await onAdd(w);
    setBusy(false);
    if (failed) setError(failed);
    else { setWords(""); setError(""); onOpenChange(false); }
  };
  return (
    <Dialog open={open} onOpenChange={(o) => { onOpenChange(o); if (!o) setError(""); }} data-testid="grant-dialog"
            title="Add a grant" description="Say what the crew may do without asking again. It applies to every project."
            footer={<>
              <Button variant="secondary" onClick={() => onOpenChange(false)}>Cancel</Button>
              <Button type="submit" form="grant-form" busy={busy} data-testid="grant-save">Add grant</Button>
            </>}>
      <form id="grant-form" onSubmit={submit}>
        <Textarea label="What the crew may do" value={words} maxLength={MAX_GRANT} rows={4} error={error || undefined}
                  hint={`Up to ${MAX_GRANT} characters.`} onChange={(e) => { setWords(e.target.value); if (error) setError(""); }} data-testid="grant-input" />
      </form>
    </Dialog>
  );
}

export interface ViewProps {
  page: SettingsPage;
  /** The sidebar item shown. */
  tab: SettingId;
  /** The agent a link to Budget came for: its limit field opens ready. */
  agent?: string;
  /** What a change that was not saved says; empty when there is none. */
  problem?: string;
  onCrew: (c: CrewChange) => Promise<void>;
  onGrant: (words: string) => Promise<string | null>;
  onSwitch: (edition: string) => Promise<void>;
  /** Saves Morning status; answers what went wrong, or null. */
  onMorning?: (m: MorningStatus) => Promise<string | null>;
  /** Sends one report on the saved settings; null when nothing was sent (a sample). */
  onMorningSend?: () => Promise<MorningResult | null>;
  /** The people Morning status can email; null until the list has loaded. */
  morningPeople?: Person[] | null;
}

// A console panel that is the whole page: its heading sits above its card, like the other pages.
const HEADED = "settings-headed min-w-0";

const TOOL_ITEMS = new Set<string>(["github", "linear", "slack", "claude_code"]);

function CellPeople() {
  return <PeopleBlock access={useAccessData()} />;
}

/** On Tares Cloud the workspace says who is on the team; this cell adds each person's GitHub login
 *  and admin flag. One list, with the workspace's invite and remove. */
function CloudPeople({ apiUrl }: { apiUrl: string }) {
  const cloud = useCloud();
  const ov = useWorkspaceOverview(apiUrl);
  const access = useAccessData();
  const r = ov.result;
  if (!r) return <div className="panel"><div className="muted">loading…</div></div>;
  if (r.status !== "ok") return <WorkspaceSettings cloud={cloud} apiUrl={apiUrl} onOpenTab={() => undefined} />;
  return (
    <TeamPanel apiUrl={apiUrl} slug={r.data.slug || slugFromHost()} loginUrl={cloud.health?.login_url}
               isOwner={r.data.role === "owner"} you={r.data.you?.email}
               cell={{ people: access.people, admin: access.admin, reload: access.reload }} />
  );
}

function PeoplePane() {
  const apiUrl = useCloud().health?.workspace_api_url || undefined;
  return apiUrl ? <CloudPeople apiUrl={apiUrl} /> : <CellPeople />;
}

function KeysPane() {
  const access = useAccessData();
  return (
    <>
      <AccessPanel me={access.me} />
      <ApiKeysPanel people={access.people ?? []} admin={access.admin} me={access.me?.person?.id} />
    </>
  );
}

/** The item's own settings. A tool's Connect and accounts are in the panel under its status. */
function ToolPanel({ id }: { id: string }) {
  const cloud = useCloud();
  const slackConnectUrl = cloud.health?.slack_connect_url;
  if (id === "github") return <GithubPanel cloud={cloud} />;
  // the page already says Linear or Slack above the panel
  if (id === "linear") return <div className="settings-tool min-w-0"><LinearSettings cloud={cloud} /></div>;
  if (id === "slack") return slackConnectUrl ? <div className="settings-tool min-w-0"><SlackCloudPanel cloud={cloud} connectUrl={slackConnectUrl} /></div> : <><SlackTokenPanel /><SlackSigningSecretPanel /></>;
  return null;
}

export function FactorySettingsView({ page, tab, agent, problem, onCrew, onGrant, onSwitch, onMorning, onMorningSend, morningPeople = null }: ViewProps) {
  const { crew, models, edition, tools } = page;
  const cloud = useCloud();
  const dim = !page.can_change;
  const [confirm, setConfirm] = React.useState(false);
  const [target, setTarget] = React.useState<{ value: string; label: string } | null>(null);
  const [grantOpen, setGrantOpen] = React.useState(false);
  const [switching, setSwitching] = React.useState(false);
  const level = crew.how_far.value;
  const tool = TOOL_ITEMS.has(tab) ? tools.find((t) => t.key === tab) : undefined;
  const workspaceApiUrl = cloud.health?.workspace_api_url || undefined;

  return (
    <div className="grid min-w-0 grid-cols-[minmax(0,1fr)] gap-4" data-testid={`pane-${tab}`}>
      {dim && <Banner tone="warn" data-testid="read-only">You can look but not change anything here. Ask an admin.</Banner>}
      {problem && <Banner tone="error" data-testid="problem">{problem}</Banner>}

      {tab === "crew" && (
        <>
          <Heading title="How it works" />
          <Card dim={dim}>
            <div className={HEAD}><b className="text-[13px]">Who merges</b></div>
            <div role="radiogroup" aria-label="Who merges">
              {CHOICES.map((c) => (
                <button key={c.value} type="button" role="radio" aria-checked={level === c.value} disabled={dim} data-testid={`how-${c.value}`} data-ctl
                        onClick={() => { if (c.value === level) return; if (c.value === "L4-ship") setConfirm(true); else void onCrew({ autonomy: c.value }); }}
                        className={CHOICE}>
                  <span aria-hidden className={cn("mt-[3px] grid size-4 flex-none place-items-center rounded-full border-[1.5px] border-foreground bg-[var(--input-bg)]", level === c.value && "after:size-2 after:rounded-full after:bg-foreground")} />
                  <span className="grid gap-0.5">
                    <span className="font-semibold">{c.title}</span>
                    <span className="text-[13px] text-muted-foreground">{c.text}</span>
                  </span>
                </button>
              ))}
            </div>
            {crew.release_profile && (
              <Row title="Releases" data-testid="row-releases"
                   text={crew.how_far.releases ? "The crew may ship a finished milestone when you say so." : "The crew does not ship. Merged work waits."}>
                <Toggle label="Releases" on={crew.how_far.releases} disabled={dim} testid="toggle-releases" onChange={(v) => void onCrew({ releaser: v })} />
              </Row>
            )}
          </Card>
          <Card dim={dim}>
            <Row title="Builders at once" text={crew.builders_text} data-testid="row-builders">
              <div className="flex items-center border border-foreground bg-[var(--input-bg)]">
                <button type="button" aria-label="One builder fewer" disabled={dim || crew.builders_max <= 1} data-ctl data-testid="builders-less"
                        onClick={() => void onCrew({ builders_max: crew.builders_max - 1 })}
                        className="size-8 border-0! bg-transparent! p-0! text-lg! text-foreground! cursor-pointer disabled:cursor-not-allowed">{"−"}</button>
                <b className="min-w-9 text-center" data-testid="builders-count" aria-live="polite">{crew.builders_max}</b>
                <button type="button" aria-label="One builder more" disabled={dim || crew.builders_max >= 20} data-ctl data-testid="builders-more"
                        onClick={() => void onCrew({ builders_max: crew.builders_max + 1 })}
                        className="size-8 border-0! bg-transparent! p-0! text-lg! text-foreground! cursor-pointer disabled:cursor-not-allowed">+</button>
              </div>
            </Row>
            <Row title="Second check" data-testid="row-challenger"
                 text={crew.challenger ? "A second model checks every commit before the review. Findings land on the ticket." : "Off. Only the reviewer checks a pull request."}>
              <Toggle label="Second check" on={crew.challenger} disabled={dim} testid="toggle-challenger" onChange={(v) => void onCrew({ challenger: v })} />
            </Row>
            <Row title="Pacing" text={crew.pace_text} data-testid="row-pace">
              <Toggle label="Pacing" on={crew.pace} disabled={dim} testid="toggle-pace" onChange={(v) => void onCrew({ pace: v })} />
            </Row>
          </Card>
        </>
      )}

      {tab === "allowed" && (
        <>
          <Heading title="What you allowed" why="What the crew may do without asking again.">
            <Button variant="primary" disabled={dim} data-ctl data-testid="grant-add" onClick={() => setGrantOpen(true)}>Add a grant</Button>
          </Heading>
          <Card dim={dim} data-testid="grants">
            {crew.grants.length === 0 ? (
              <div className="p-4 text-muted-foreground" data-testid="grants-empty">Nothing allowed yet. The crew asks when it needs something.</div>
            ) : crew.grants.map((g) => <GrantRow key={g.id} g={g} />)}
            {crew.grants_total > crew.grants.length && (
              <div className="border-t border-border px-4 py-2.5 text-xs text-muted-foreground">Showing the newest {crew.grants.length} of {crew.grants_total}.</div>
            )}
          </Card>
        </>
      )}

      {tab === "morning" && (
        <MorningStatusPane value={page.morning ?? MORNING_OFF} dim={dim}
                           slackConnected={tools.some((t) => t.key === "slack" && t.connected)}
                           people={morningPeople}
                           onSave={onMorning ?? (async () => null)}
                           onSendNow={onMorningSend ?? (async () => null)} />
      )}

      {tool && (
        <>
          <Heading title={tool.name} />
          <ToolStatus t={tool} dim={dim} />
          <ToolPanel id={tab} />
        </>
      )}

      {tab === "providers" && <div className={HEADED}><ProvidersPanel /></div>}

      {tab === "budget" && (
        <>
          <Heading title="Budget" />
          <Card dim={dim}><div className="px-4 py-3.5" data-testid="budget-text">{models.budget_text}</div></Card>
          <AgentLimitsPanel agent={agent} />
        </>
      )}

      {tab === "people" && <div className={HEADED}><PeoplePane /></div>}
      {tab === "keys" && <div className={HEADED}><KeysPane /></div>}
      {tab === "general" && workspaceApiUrl && <WorkspaceSettings cloud={cloud} apiUrl={workspaceApiUrl} onOpenTab={() => undefined} noTeam />}
      {tab === "usage" && <UsagePanels />}
      {tab === "licence" && <LicencePanel />}
      {tab === "agent_tools" && <div className={HEADED}><McpToolsPanel /></div>}
      {tab === "observability" && <div className={HEADED}><TracingPanel /></div>}
      {tab === "areas" && <EePage slot="page.settings.areas" />}

      {tab === "edition" && (
        <>
          <Heading title="Edition" />
          <Card>
            <ListRow data-testid="row-edition">
              <div className={ROW_TEXT}>
                <div className="font-semibold" data-testid="edition-name">{edition.label}</div>
                <div className="mt-0.5 text-[13px] text-muted-foreground">{edition.note}</div>
                {!edition.can_switch && <div className="mt-0.5 text-[13px] text-muted-foreground" data-testid="edition-readonly">Only an admin can switch the edition.</div>}
              </div>
              {edition.can_switch && (
                <div className="flex flex-none flex-wrap items-center gap-2">
                  {edition.options.map((o) => (
                    <Button key={o.value} variant="secondary" data-testid={`edition-${o.value}`} onClick={() => setTarget(o)}>{`Switch to ${o.label}`}</Button>
                  ))}
                </div>
              )}
            </ListRow>
          </Card>
        </>
      )}

      <Dialog open={confirm} onOpenChange={setConfirm} data-testid="confirm-ship"
              title="Let the crew merge on its own?"
              description="It will merge a pull request once the reviewer passed it and CI is green, without waiting for you."
              footer={<>
                <Button variant="secondary" onClick={() => setConfirm(false)}>Keep approving</Button>
                <Button data-testid="confirm-ship-yes" onClick={() => { setConfirm(false); void onCrew({ autonomy: "L4-ship" }); }}>Let it merge</Button>
              </>} />
      <Dialog open={target !== null} onOpenChange={(o) => { if (!o) setTarget(null); }} data-testid="confirm-edition"
              title={`Switch to ${target?.label ?? ""}?`} description={edition.note}
              footer={<>
                <Button variant="secondary" onClick={() => setTarget(null)}>Cancel</Button>
                <Button busy={switching} data-testid="confirm-edition-yes"
                        onClick={async () => { if (!target) return; setSwitching(true); await onSwitch(target.value); setSwitching(false); setTarget(null); }}>
                  {`Switch to ${target?.label ?? ""}`}
                </Button>
              </>} />
      <GrantDialog open={grantOpen} onOpenChange={setGrantOpen} onAdd={onGrant} />
    </div>
  );
}

const BACK_ARROW = (
  <svg aria-hidden="true" width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1.5"><path d="M8 2L4 6l4 4" /></svg>
);
const NAV_ITEM = "flex! w-full min-h-8 items-center justify-between gap-2 border-0! py-1! pr-2.5! pl-[22px]! text-left text-sm! no-underline! cursor-pointer hover:bg-muted!";

function SettingsBack() {
  return (
    <Link to={placeBeforeSettings()} data-testid="settings-back"
          className="inline-flex! h-8 w-fit items-center gap-1.5 px-2.5 text-[13px] text-muted-foreground! no-underline! hover:text-foreground!">{BACK_ARROW}Back</Link>
  );
}

/** The two levels: every group by name, the open group's items under it. Narrow, one select. */
function SettingsNav({ groups, tab, onPick, tools, grants }: {
  groups: SettingGroup[]; tab: SettingId; onPick: (id: string) => void; tools?: SettingsTool[]; grants?: number;
}) {
  const navigate = useNavigate();
  const go = (id: string) => {
    const to = groups.flatMap((g) => g.items).find((i) => i.id === id)?.to;
    if (to) navigate(to); else onPick(id);
  };
  const back = <SettingsBack />;
  return (
    <>
      <div className="md:hidden">
        {back}
        <h1 className="m-0! mb-3! px-2.5 text-[22px]!">Settings</h1>
        <label className="block">
          <span className="sr-only">Settings section</span>
          <select value={tab} data-testid="settings-select" onChange={(e) => go(e.target.value)}>
            {groups.map((g) => (
              <optgroup key={g.id} label={g.label}>
                {g.items.map((i) => <option key={i.id} value={i.id}>{i.label}</option>)}
              </optgroup>
            ))}
          </select>
        </label>
      </div>
      <nav aria-label="Settings" data-testid="settings-nav" className="hidden w-[216px] flex-none flex-col gap-0.5 md:flex">
        {back}
        <h1 className="m-0! mb-3.5! px-2.5 text-2xl!">Settings</h1>
        {groups.map((g) => {
          const open = g.items.some((i) => i.id === tab);
          return (
            <div key={g.id} data-testid={`group-${g.id}`} className={g.id === "advanced" ? "mt-2.5 border-t border-border pt-2.5" : ""}>
              <button type="button" aria-expanded={open} data-testid={`group-open-${g.id}`} onClick={() => go(g.items[0].id)}
                      className="flex! h-9 w-full items-center justify-between border-0! bg-transparent! px-2.5! text-left text-sm! font-semibold! text-foreground! cursor-pointer hover:bg-muted!">
                <span>{g.label}</span>
                <svg aria-hidden="true" width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1.5" className={cn("opacity-70", open && "rotate-90")}><path d="M4 2l4 4-4 4" /></svg>
              </button>
              {open && (
                <div className="mb-2 mt-0.5 flex flex-col">
                  {g.items.map((i) => {
                    const on = i.id === tab;
                    const t = tools?.find((x) => x.key === i.id);
                    const inner = (
                      <>
                        <span>{i.label}</span>
                        {t && <StatusDot tone={t.connected ? "ready" : "quiet"} data-testid="nav-dot" data-on={t.connected} />}
                        {i.id === "allowed" && grants ? <span className="font-mono text-xs text-muted-foreground" data-testid="nav-grants">{grants}</span> : null}
                      </>
                    );
                    const cls = cn(NAV_ITEM, on ? "bg-muted! font-semibold! text-foreground!" : "bg-transparent! font-normal! text-muted-foreground!");
                    return i.to
                      ? <Link key={i.id} to={i.to} data-testid={`nav-${i.id}`} className={cls}>{inner}</Link>
                      : <button key={i.id} type="button" aria-current={on ? "page" : undefined} data-testid={`nav-${i.id}`} onClick={() => onPick(i.id)} className={cls}>{inner}</button>;
                  })}
                </div>
              )}
            </div>
          );
        })}
      </nav>
    </>
  );
}

function Loading() {
  return (
    <Card role="status" aria-busy="true" aria-label="Loading" className="grid gap-2 p-4" data-testid="settings-loading">
      {[0, 1, 2, 3, 4].map((i) => (
        <div key={i} className="grid gap-2 pb-4"><Skeleton width="35%" /><Skeleton width="60%" /><Skeleton width="80%" /></div>
      ))}
    </Card>
  );
}

function SettingsPage() {
  const named = useSample();
  const sample = isSample(named) ? named : null;
  const loc = useLocation();
  const navigate = useNavigate();
  const cloud = useCloud();
  const eeLoaded = useEeLoaded();
  const eePending = useEePending();
  const { data, error, reload } = usePagePolling<SettingsPage>(() => (sample ? sampleLoad(sample) : factorySettings()), POLL_MS);
  // What was just changed shows at once; the next read replaces it.
  const [over, setOver] = React.useState<CrewChange>({});
  const [problem, setProblem] = React.useState("");
  React.useEffect(() => { setOver({}); }, [data]);

  // What a cell has decides what the sidebar lists: Tares Cloud has General, a self-hosted cell has
  // Usage and a licence, and Areas comes with the enterprise build.
  const workspaceApiUrl = cloud.health?.workspace_api_url || undefined;
  const off = new Set<string>();
  off.add(workspaceApiUrl ? "usage" : "general");
  if (!licenceWordsOn()) off.add("licence");
  if (!eeLoaded) off.add("areas");
  const groups = shownGroups(off);

  // Coming back from a Tares Cloud connect page: said once, on the item it belongs to.
  const [cloudBack] = React.useState(() => {
    const q = new URLSearchParams(window.location.search);
    const m = CLOUD_BACK[q.get("cloud") ?? ""];
    return m ? { ...m, detail: q.get("cloud_detail") ?? "", item: settingFrom(`?tab=${m.tab ?? q.get("tab") ?? ""}`, "") } : undefined;
  });
  React.useEffect(() => {
    const q = new URLSearchParams(window.location.search);
    if (!q.has("cloud") && !q.has("cloud_detail")) return;
    q.delete("cloud"); q.delete("cloud_detail");
    if (cloudBack?.item) q.set("tab", cloudBack.item);
    navigate({ pathname: "/settings", search: `?${q}` }, { replace: true });
  }, []);

  const wanted = cloudBack?.item ?? settingFrom(loc.search, loc.hash);
  const known = !!wanted && groups.some((g) => g.items.some((i) => i.id === wanted));
  // an item this cell may not have waits until the cell has said
  const waiting = !!wanted && !known && (!cloud.ready || eePending);
  const tab: SettingId = wanted && known ? wanted : FIRST_SETTING;
  const pick = (id: string) => {
    const q = new URLSearchParams();
    if (named) q.set("sample", named);
    q.set("tab", id);
    navigate({ pathname: "/settings", search: `?${q}` }, { replace: true });
    window.scrollTo(0, 0);
  };

  const failed = (e: unknown) => (e instanceof ApiError && e.status === 403 ? "Only an admin can change this." : SAVE_FAILED);
  const onCrew = async (c: CrewChange) => {
    setProblem("");
    if (sample) return;
    setOver((o) => ({ ...o, ...c }));
    try { await changeCrew(c); } catch (e) { setOver({}); setProblem(failed(e)); }
    reload();
  };
  const onGrant = async (words: string) => {
    if (sample) return null;
    try { await addGrant(words); reload(); return null; } catch (e) { return e instanceof ApiError && e.status !== 403 && e.message ? e.message : failed(e); }
  };
  const onSwitch = async (edition: string) => {
    if (sample) return;
    try { await switchEdition(edition); window.location.reload(); } catch (e) { setProblem(failed(e)); }
  };
  const onMorning = async (m: MorningStatus) => {
    if (sample) return null;
    try { await saveMorning(m); reload(); return null; } catch (e) { return e instanceof ApiError && e.status !== 403 && e.message ? e.message : failed(e); }
  };
  const onMorningSend = async (): Promise<MorningResult | null> => {
    if (sample) return null;
    const result = await sendMorningNow();
    reload();
    return result;
  };

  // The people Morning status can email; a sample reads no cell.
  const [people, setPeople] = React.useState<Person[] | null>(null);
  const wantsPeople = !sample && tab === "morning";
  React.useEffect(() => {
    if (!wantsPeople) return;
    let stop = false;
    api.people().then((r) => { if (!stop) setPeople(r.people); }).catch(() => { if (!stop) setPeople([]); });
    return () => { stop = true; };
  }, [wantsPeople]);

  const page = data && {
    ...data,
    crew: {
      ...data.crew,
      how_far: { ...data.crew.how_far, value: over.autonomy ?? data.crew.how_far.value, releases: over.releaser ?? data.crew.how_far.releases },
      builders_max: over.builders_max ?? data.crew.builders_max,
      builders_text: over.builders_max ? `Up to ${over.builders_max} ${over.builders_max === 1 ? "builder" : "builders"} at once` : data.crew.builders_text,
      challenger: over.challenger ?? data.crew.challenger,
      pace: over.pace ?? data.crew.pace,
    },
  };

  return (
    <div className="flex min-w-0 flex-col gap-4 md:flex-row md:items-start md:gap-10" data-testid="factory-settings">
      <SettingsNav groups={groups} tab={tab} onPick={pick} tools={page?.tools} grants={page?.crew.grants_total} />
      <div className="min-w-0 max-w-[900px] flex-1">
        {cloudBack && cloudBack.item === tab && (
          <div className={"alert" + (cloudBack.kind ? ` ${cloudBack.kind}` : "")} role={cloudBack.kind === "error" ? "alert" : "status"}>
            {cloudBack.kind === "error" && cloudBack.detail ? cloudBack.detail
              : <>{cloudBack.text}{cloudBack.detail && <> {cloudBack.detail}</>}</>}
          </div>
        )}
        {error && (
          <Banner tone="error" action={<Button variant="secondary" onClick={reload}>Retry</Button>}>
            <b>Settings did not load.</b> <span className="text-foreground">The cell did not answer. Try again in a moment.</span>
          </Banner>
        )}
        {(!page || waiting) && !error && <Loading />}
        {page && !waiting && (
          <TerseSettings.Provider value={true}>
            <FactorySettingsView page={page} tab={tab} agent={new URLSearchParams(loc.search).get("agent") ?? undefined}
                                 problem={problem} onCrew={onCrew} onGrant={onGrant} onSwitch={onSwitch}
                                 onMorning={onMorning} onMorningSend={onMorningSend} morningPeople={sample ? SAMPLE_PEOPLE : people} />
          </TerseSettings.Provider>
        )}
      </div>
    </div>
  );
}

export default function FactorySettings() {
  return <SettingsGate />;
}

// A key that can only read is refused by the cell, so the page does not ask.
function SettingsGate() {
  const refused = useSettingsRefused(isSample(useSample()));
  const { search, hash } = useLocation();
  if (!refused) return <SettingsPage />;
  // The licence is the one thing a person with such a key may still look at.
  if (settingFrom(search, hash) === "licence") return <div className="grid gap-3"><SettingsBack /><LicencePanel /></div>;
  return (
    <div className="grid gap-3"><SettingsBack /><SettingsRefused testId="factory-settings-refused" /></div>
  );
}
