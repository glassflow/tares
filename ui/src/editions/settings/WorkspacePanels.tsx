import { type FormEvent, useEffect, useState } from "react";

import { api, type CloudMember, type CloudResult, type CloudWorkspaceOverview, cloudApi } from "../../api";
import { type Cloud, reloadList, signInLink, slugFromHost, useWorkspaceOverview, type WorkspaceOverview } from "../../cloud";
import { Banner, Button, Checkbox, Input, ListRow, Panel, ProgressBar, SkeletonRows, Switch } from "../../components/edition";
import LicenceNeeded from "../../components/LicenceNeeded";
import { UsagePanels } from "../../components/UsagePanels";
import { useLicence } from "../../licence";
import { cn } from "../../lib/utils";
import type { Person } from "../../types";

// Settings > Workspace on Tares Cloud (TR-375, TR-376), on the edition kit (TR-1393): the team, plan,
// storage, included credit, the sign-in pin and delete, read from and written to the control plane
// with the person's Tares Cloud session (contract §2). The data stays in the control plane; this
// console only shows it. Members see everything read-only; the control plane checks every write, and
// the console hides what the person cannot do. The platform Settings page shows it on its Workspace
// tab; Factory Settings shows it under General (without the team) and the team under People.

const TEXT = "m-0 text-[13px] leading-relaxed text-muted-foreground";
const ROW = "-mt-2 [--row-x:0px]";

const STATE_WORDS: Record<string, string> = {
  active: "Running",
  provisioning: "Setting up",
  suspended: "Suspended",
  error: "Not running",
};

const WARN_PCT = 80;   // the same threshold the control plane and this console's Overview use
const GIB = 1024 ** 3;

function fmtGiB(bytes: number): string {
  const gib = bytes / GIB;
  return gib < 10 ? gib.toFixed(1) : String(Math.round(gib));
}

const usd = (n: number) => `$${n.toFixed(2)}`;
/** The control plane's refusal, as a sentence. */
const sentence = (s: string) => (s ? s[0].toUpperCase() + s.slice(1) + (/[.!?]$/.test(s) ? "" : ".") : s);

/** `url` (relative to `base`) when it is http(s) on the same origin as `base`, else undefined:
 *  the console only follows or writes to addresses on the control plane it already talks to. */
function sameOrigin(url: string | undefined, base: string): string | undefined {
  if (!url) return undefined;
  try {
    const b = new URL(base, window.location.href);
    const u = new URL(url, b);
    return (u.protocol === "https:" || u.protocol === "http:") && u.origin === b.origin ? u.toString() : undefined;
  } catch {
    return undefined;
  }
}

/** What one call came to, for a panel: done, signed out, or the server's detail. */
type Outcome = { signedOut?: boolean; err?: string };
function outcome(r: CloudResult<unknown>): Outcome {
  return r.status === "signed_out" ? { signedOut: true } : r.status === "error" ? { err: r.detail } : {};
}

/** A write in one panel met a 401 or a refusal: that panel says so, the rest of the tab stays as it is. */
function PanelStatus({ o, loginUrl }: { o: Outcome; loginUrl?: string }) {
  if (o.signedOut) {
    return (
      <Banner tone="warn" data-testid="ws-signed-out"
              action={loginUrl ? <Button variant="secondary" asChild><a href={signInLink(loginUrl)}>Sign in</a></Button> : undefined}>
        You are signed out of Tares Cloud. Sign in to change this.
      </Banner>
    );
  }
  return o.err ? <Banner tone="error" data-testid="ws-error">{sentence(o.err)}</Banner> : null;
}

/** A confirmation under a control, read out when it appears. */
function Said({ children, ...props }: { children: React.ReactNode } & React.HTMLAttributes<HTMLParagraphElement>) {
  return <p className={TEXT} role="status" {...props}>{children}</p>;
}

export default function WorkspaceSettings({ cloud, apiUrl, onOpenTab, noTeam }: {
  cloud: Cloud; apiUrl: string;
  /** Opens the model providers' tab; without it the credit panel names the page in words. */
  onOpenTab?: (tab: "anthropic") => void;
  /** The team has its own page (a factory cell's Settings, Team, People). */
  noTeam?: boolean;
}) {
  const ov = useWorkspaceOverview(apiUrl);
  const r = ov.result;
  const loginUrl = cloud.health?.login_url;

  if (!r) return <Panel data-testid="ws-loading"><SkeletonRows count={2} /></Panel>;
  if (r.status === "signed_out") {
    return (
      <Panel title="Workspace" data-testid="ws-signed-out-page">
        <div className="grid justify-items-start gap-4">
          <p className={TEXT}>You are signed out of Tares Cloud.</p>
          {loginUrl
            ? <Button asChild><a href={signInLink(loginUrl)}>Sign in to manage this workspace</a></Button>
            : <p className={TEXT}>Sign in to Tares Cloud to manage this workspace.</p>}
        </div>
      </Panel>
    );
  }
  if (r.status === "error") {
    const gone = r.code === 404;
    return (
      <Banner tone="error" data-testid="ws-load-error"
              action={gone ? undefined : <Button variant="secondary" onClick={ov.reload}>Try again</Button>}>
        {gone ? "You no longer have access to this workspace." : sentence(r.detail)}
      </Banner>
    );
  }

  const w = r.data;
  const slug = w.slug || slugFromHost();
  const isOwner = w.role === "owner";
  const showCredit = w.trial && (w.trial.state === "active" || w.trial.state === "exhausted");
  const p = { apiUrl, slug, ov, loginUrl };
  return (
    // UsagePanels is still in the legacy dress (its own margin); the grid's gap spaces it here
    <div className="grid min-w-0 gap-4 [&>.panel]:m-0" data-testid="workspace">
      <OverviewPanel w={w} />
      {!noTeam && <TeamPanel {...p} isOwner={isOwner} you={w.you?.email} />}
      <StoragePanel {...p} w={w} isOwner={isOwner} />
      {showCredit && <CreditPanel w={w} onOpenTab={onOpenTab} />}
      {/* what this cell spent on model providers; storage is the panel above, from the control plane */}
      <UsagePanels storage={false} />
      <PinPanel {...p} w={w} />
      {isOwner && <DeletePanel {...p} />}
    </div>
  );
}

type PanelProps = { apiUrl: string; slug: string; ov: WorkspaceOverview; loginUrl?: string };

function OverviewPanel({ w }: { w: CloudWorkspaceOverview }) {
  const facts: [string, React.ReactNode, string?][] = [
    ["Plan", w.plan],
    ["Tares version", w.image_tag || "Not known", w.image_tag ? "font-mono" : undefined],
    ["State", STATE_WORDS[w.state] ?? w.state],
  ];
  return (
    <Panel title={w.slug} data-testid="ws-overview">
      <dl className="m-0 grid grid-cols-1 gap-x-8 gap-y-3 sm:grid-cols-3">
        {facts.map(([k, v, cls]) => (
          <div key={k} className="min-w-0">
            <dt className="text-[12px] text-muted-foreground">{k}</dt>
            <dd className={cn("m-0 text-[14px]", cls)}>{v}</dd>
          </div>
        ))}
      </dl>
    </Panel>
  );
}

/** What this cell knows about the people on the team: their GitHub login and whether they are admins. */
export type CellTeam = { people?: Person[]; admin: boolean; reload: () => void };

const hasEmail = (p: Person, email: string) => [p.email, ...p.emails].some((e) => e?.toLowerCase() === email.toLowerCase());

export function TeamPanel({ apiUrl, slug, loginUrl, isOwner, you, cell }: Omit<PanelProps, "ov"> & { isOwner: boolean; you?: string; cell?: CellTeam }) {
  const [members, setMembers] = useState<CloudMember[]>();
  const [email, setEmail] = useState("");
  const [busy, setBusy] = useState<string>();   // "invite" or the user_id being removed
  const [o, setO] = useState<Outcome>({});
  const [msg, setMsg] = useState<string>();
  const { eeAllowed, loading } = useLicence();
  // The form stays while the licence read is on its way or cannot be answered, as it was before the licence existed;
  // the workspace answers an invite it will not take with licence_required, which shows the line below.
  const canInvite = eeAllowed || loading;
  const [noLicence, setNoLicence] = useState(false);   // the workspace answered the invite with licence_required

  const load = async () => {
    const r = await cloudApi.members(apiUrl);
    if (r.status === "ok") setMembers(r.data.members ?? []);
    else setO(outcome(r));
  };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { load(); }, [apiUrl]);

  const invite = async (e: FormEvent) => {
    e.preventDefault();
    const to = email.trim();
    setBusy("invite"); setO({}); setMsg(undefined); setNoLicence(false);
    const r = await cloudApi.invite(apiUrl, slug, to);
    if (r.status === "ok") {
      cell?.reload();
      setEmail("");
      setMsg(`Invited ${to}. They get access when they sign in with that email.`);
      await load();
    } else if (r.status === "error" && r.code === 402) setNoLicence(true);
    else setO(outcome(r));
    setBusy(undefined);
  };

  // One list: the workspace says who is on the team, the cell adds what it knows about each.
  const [edit, setEdit] = useState<{ id: string; github: string; is_admin: boolean }>();
  const [cellErr, setCellErr] = useState<string>();
  const personOf = (m: CloudMember) => cell?.people?.find((p) => hasEmail(p, m.email));
  // on the cell but not in the workspace: added by hand, or left before leaving reached the cell
  const strays = members ? (cell?.people ?? []).filter((p) => p.id !== "local" && !members.some((m) => hasEmail(p, m.email))) : [];
  const saveEdit = async (e: FormEvent) => {
    e.preventDefault();
    if (!edit) return;
    setBusy(edit.id); setCellErr(undefined);
    try {
      await api.updatePerson(edit.id, { github: edit.github.trim() || null, is_admin: edit.is_admin });
      setEdit(undefined);
      cell?.reload();
    } catch (err) { setCellErr(String((err as Error).message ?? err)); }
    setBusy(undefined);
  };
  const removeStray = async (p: Person) => {
    setBusy(p.id); setCellErr(undefined);
    try { await api.removePerson(p.id); cell?.reload(); }
    catch (err) { setCellErr(String((err as Error).message ?? err)); }
    setBusy(undefined);
  };

  const remove = async (m: CloudMember) => {
    setBusy(m.user_id); setO({}); setMsg(undefined);
    const r = await cloudApi.removeMember(apiUrl, slug, m.user_id);
    if (r.status === "ok") { setMsg(`${m.email} no longer has access.`); await load(); cell?.reload(); }
    else setO(outcome(r));
    setBusy(undefined);
  };

  const me = (m: CloudMember) => !!you && m.email.toLowerCase() === you.toLowerCase();
  const failed = !!o.err || !!o.signedOut;
  return (
    <Panel title={cell ? "People" : "Team"} data-testid="ws-team">
      <div className="grid gap-4">
        {!cell && (
          <p className={TEXT}>
            {isOwner
              ? "People with access to this workspace. Invited teammates get access when they sign in with that email."
              : "People with access to this workspace."}
          </p>
        )}
        <PanelStatus o={o} loginUrl={loginUrl} />
        {msg && <Said data-testid="team-said">{msg}</Said>}
        {cellErr && <Banner tone="error" data-testid="team-cell-error">{sentence(cellErr)}</Banner>}
        {!members ? (!failed && <SkeletonRows count={2} />) : (
          <div className={ROW}>
            {members.map((m) => {
              const person = personOf(m);
              const owner = m.role === "owner";
              if (edit && person?.id === edit.id) return (
                <ListRow key={m.user_id} className="block hover:bg-transparent" data-testid="team-edit">
                  <form className="grid gap-4" onSubmit={saveEdit}>
                    <p className="m-0 font-semibold [overflow-wrap:anywhere]" data-user-text>{m.email}</p>
                    <Input label="GitHub login" className="font-mono" placeholder="octocat" autoFocus value={edit.github}
                           onChange={(e) => setEdit({ ...edit, github: e.target.value })} />
                    <Checkbox label="Admin" hint="An admin manages people, keys and settings on this cell."
                              checked={edit.is_admin} onCheckedChange={(v) => setEdit({ ...edit, is_admin: v === true })} />
                    <div className="flex flex-wrap gap-2">
                      <Button type="submit" busy={busy === edit.id} disabled={!!busy}>Save</Button>
                      <Button type="button" variant="secondary" disabled={!!busy} onClick={() => setEdit(undefined)}>Cancel</Button>
                    </div>
                  </form>
                </ListRow>
              );
              return (
                <ListRow key={m.user_id} className="grid grid-cols-1 gap-x-4 gap-y-2 hover:bg-transparent md:grid-cols-[1fr_auto]"
                         data-testid="team-row">
                  <div className="min-w-0">
                    <div className="[overflow-wrap:anywhere]">
                      <span className="font-semibold" data-user-text>{m.email}</span>
                      {m.name && <span className="text-muted-foreground" data-user-text> {m.name}</span>}
                      {me(m) && <span className="text-muted-foreground"> (you)</span>}
                    </div>
                    <p className={cn(TEXT, "flex flex-wrap gap-x-3")}>
                      <span data-testid="team-role">{owner ? "Owner" : "Member"}</span>
                      {!owner && m.status === "invited" && <span className="text-foreground" data-testid="team-invited">Invited, not signed in yet</span>}
                      {cell && person?.github && <span>GitHub <span className="font-mono text-foreground" data-user-text>{person.github}</span></span>}
                      {cell && person?.is_admin && <span data-testid="team-admin">Admin on this cell</span>}
                    </p>
                  </div>
                  {(isOwner || cell?.admin) && (
                    <div className="flex flex-wrap gap-2 md:justify-end">
                      {cell?.admin && person && (
                        <Button variant="secondary" disabled={!!busy || !!edit} aria-label={`Edit ${m.email}`}
                                onClick={() => setEdit({ id: person.id, github: person.github ?? "", is_admin: person.is_admin })}>Edit</Button>
                      )}
                      {isOwner && !owner && (
                        <Button variant="secondary" busy={busy === m.user_id} disabled={!!busy} onClick={() => remove(m)}
                                aria-label={`Remove ${m.email}`}>Remove</Button>
                      )}
                    </div>
                  )}
                </ListRow>
              );
            })}
          </div>
        )}
        {strays.length > 0 && (
          <div className="grid gap-2" data-testid="team-strays">
            <p className={TEXT}>On this cell's records, but not on the team:</p>
            <div className={ROW}>
              {strays.map((p) => (
                <ListRow key={p.id} className="justify-between hover:bg-transparent" data-testid="team-stray">
                  <span className="min-w-0 [overflow-wrap:anywhere]">
                    <span className="font-semibold" data-user-text>{p.name}</span>
                    {p.email && <span className="text-muted-foreground" data-user-text> {p.email}</span>}
                  </span>
                  {cell?.admin && <Button variant="secondary" disabled={!!busy} aria-label={`Remove ${p.name}`} onClick={() => removeStray(p)}>Remove</Button>}
                </ListRow>
              ))}
            </div>
          </div>
        )}
        {isOwner && (canInvite ? (
          <form className="grid max-w-[560px] gap-2 sm:grid-cols-[1fr_auto] sm:items-end" onSubmit={invite} data-testid="team-invite">
            <Input id="ws-invite" type="email" label="Invite a teammate" placeholder="teammate@company.com"
                   autoComplete="off" value={email} onChange={(e) => setEmail(e.target.value)} />
            <Button type="submit" busy={busy === "invite"} disabled={!!busy || !email.trim()}>Invite</Button>
          </form>
        ) : <LicenceNeeded what="Inviting people" />)}
        {isOwner && canInvite && noLicence && <LicenceNeeded what="Inviting people" />}
      </div>
    </Panel>
  );
}

function StoragePanel({ w, apiUrl, slug, ov, loginUrl, isOwner }: PanelProps & {
  w: CloudWorkspaceOverview; isOwner: boolean;
}) {
  const [size, setSize] = useState(String(w.storage_gb));
  const [busy, setBusy] = useState(false);
  const [o, setO] = useState<Outcome>({});
  const [ok, setOk] = useState(false);
  const [bad, setBad] = useState(false);
  useEffect(() => { setSize(String(w.storage_gb)); }, [w.storage_gb]);
  const atMax = w.storage_gb >= w.storage_max_gb;
  const pct = w.storage_pct_used;
  const near = pct != null && pct >= WARN_PCT;
  const n = Number(size);
  const valid = /^\d+$/.test(size.trim()) && n >= w.storage_gb && n <= w.storage_max_gb;

  const grow = async (e: FormEvent) => {
    e.preventDefault();
    if (!valid) { setBad(true); return; }
    setBusy(true); setO({}); setOk(false); setBad(false);
    const r = await cloudApi.growStorage(apiUrl, slug, n);
    if (r.status === "ok") {
      // keep only the storage fields the answer carries; the rest of the overview stays as it is
      const d = r.data ?? {};
      ov.update({
        storage_gb: typeof d.storage_gb === "number" ? d.storage_gb : n,
        ...(typeof d.storage_max_gb === "number" ? { storage_max_gb: d.storage_max_gb } : {}),
        ...("storage_used_bytes" in d ? { storage_used_bytes: d.storage_used_bytes ?? null } : {}),
        ...("storage_pct_used" in d ? { storage_pct_used: d.storage_pct_used ?? null } : {}),
      });
      setOk(true);
    } else {
      setSize(String(w.storage_gb));   // the field goes back to what the workspace has
      setO(outcome(r));
    }
    setBusy(false);
  };

  return (
    <Panel title="Storage" data-testid="ws-storage-panel">
      <div className="grid gap-4">
        {w.storage_used_bytes == null || pct == null ? (
          <p className={TEXT} data-testid="ws-usage-unknown">Usage is unavailable right now. It appears here once the workspace reports in.</p>
        ) : (
          <div className="grid gap-2">
            <p className="m-0 flex justify-between gap-3 text-[14px]">
              <span><b>{fmtGiB(w.storage_used_bytes)} GiB</b> of {w.storage_gb} GiB used</span>
              <span className={near ? "font-semibold text-foreground" : "text-muted-foreground"} data-testid="ws-pct">{pct}%</span>
            </p>
            <ProgressBar value={pct} aria-label="Storage used" data-testid="ws-usage-bar" />
            {near && (
              <Banner tone="warn" data-testid="ws-near-full">
                This workspace is {pct}% full.{" "}
                {isOwner ? "Grow the storage below to keep room for new events."
                  : "The workspace owner can grow the storage to keep room for new events."}
              </Banner>
            )}
          </div>
        )}
        <p className={TEXT}>
          Now <b className="text-foreground">{w.storage_gb} GiB</b>, up to {w.storage_max_gb} GiB on
          the {w.plan} plan. Storage grows online and cannot be shrunk.
        </p>
        {isOwner && (atMax ? (
          <p className={TEXT} data-testid="ws-at-max">At the plan maximum.</p>
        ) : (
          <form className="grid max-w-[560px] gap-2 sm:grid-cols-[1fr_auto] sm:items-start" noValidate onSubmit={grow} data-testid="ws-grow">
            <Input id="ws-storage" type="number" inputMode="numeric" label="New size in GiB" min={w.storage_gb} max={w.storage_max_gb} step={1}
                   value={size} disabled={busy}
                   hint={bad ? undefined : `From ${w.storage_gb} to ${w.storage_max_gb} GiB.`}
                   error={bad ? `Give a whole number from ${w.storage_gb} to ${w.storage_max_gb}.` : undefined}
                   onChange={(e) => { setSize(e.target.value); setOk(false); setBad(false); }} />
            {/* the label's height, so the button sits level with the field */}
            <Button type="submit" className="sm:mt-[26px]" busy={busy} disabled={busy || size.trim() === String(w.storage_gb)}>Grow</Button>
          </form>
        ))}
        {ok && <Said data-testid="ws-grown">Storage updated. It grows in the background.</Said>}
        <PanelStatus o={o} loginUrl={loginUrl} />
      </div>
    </Panel>
  );
}

function CreditPanel({ w, onOpenTab }: { w: CloudWorkspaceOverview; onOpenTab?: (tab: "anthropic") => void }) {
  const t = w.trial!;
  return (
    <Panel title="Included credit" data-testid="ws-credit"
           action={onOpenTab ? <Button variant="secondary" onClick={() => onOpenTab("anthropic")}>Add your API key</Button> : undefined}>
      {t.state === "exhausted" ? (
        <p className={TEXT}>
          The included <b className="text-foreground">{usd(t.credit_usd)}</b> Anthropic credit is used up. Add your
          own API key under Model providers and your agents keep working on it.
        </p>
      ) : (
        <p className={TEXT}>
          <b className="text-foreground">{usd(t.spend_usd ?? 0)} of {usd(t.credit_usd)}</b> included Anthropic credit
          used. Agent work is on us until it is used up. Your own API key under Model providers takes over at once.
        </p>
      )}
    </Panel>
  );
}

function PinPanel({ w, apiUrl, slug, ov, loginUrl }: PanelProps & { w: CloudWorkspaceOverview }) {
  const [busy, setBusy] = useState(false);
  const [o, setO] = useState<Outcome>({});
  const change = async (on: boolean) => {
    setO({});
    // only ever written to the control plane this workspace already talks to
    const url = sameOrigin(w.default_url, apiUrl);
    if (!url) {
      setO({ err: "This setting cannot be changed from here: Tares Cloud gave an address it does not own. Nothing was changed." });
      return;
    }
    setBusy(true);
    const r = await cloudApi.setDefault(url, slug, on ? slug : null);
    if (r.status === "ok") { ov.update({ is_default: on }); reloadList(); }
    else setO(outcome(r));   // the switch stays where the control plane has it
    setBusy(false);
  };
  return (
    <Panel title="Sign-in" data-testid="ws-pin">
      <div className="grid gap-4">
        <Switch label="Open this workspace when I sign in" data-testid="ws-pin-switch"
                hint={w.is_default ? "Signing in to Tares Cloud brings you straight here."
                  : "Right now you land in the workspace you opened last."}
                checked={w.is_default} disabled={busy || !w.default_url} onCheckedChange={change} />
        <PanelStatus o={o} loginUrl={loginUrl} />
      </div>
    </Panel>
  );
}

function DeletePanel({ apiUrl, slug, loginUrl }: PanelProps) {
  const [typed, setTyped] = useState("");
  const [busy, setBusy] = useState(false);
  const [o, setO] = useState<Outcome>({});
  const del = async (e: FormEvent) => {
    e.preventDefault();
    if (typed !== slug) return;
    setBusy(true); setO({});
    const r = await cloudApi.deleteWorkspace(apiUrl, slug);
    if (r.status === "ok") {
      // the page that asked is going away: the control plane says where to land, and only an
      // address on that same control plane is followed
      const next = sameOrigin(r.data?.next, apiUrl) || loginUrl;
      if (next) { window.location.assign(next); return; }
      setO({ err: "The workspace is deleted. Sign in to Tares Cloud again to continue." });
    } else setO(outcome(r));
    setBusy(false);
  };
  return (
    <Panel title="Delete workspace" className="border-err-border" data-testid="ws-delete-panel">
      <div className="grid gap-4">
        <p className={TEXT}>
          Permanently deletes <b className="text-foreground">{slug}</b> and all its data: connections, events, projects,
          agents and settings. This cannot be undone. Afterwards you land in your next workspace.
        </p>
        <form className="grid max-w-[560px] gap-2 sm:grid-cols-[1fr_auto] sm:items-end" onSubmit={del}>
          <Input id="ws-delete" className="font-mono" autoComplete="off" label={`Type ${slug} to confirm`}
                 value={typed} disabled={busy} onChange={(e) => setTyped(e.target.value)} />
          <Button type="submit" variant="secondary" className="text-err" busy={busy} disabled={busy || typed !== slug}>Delete workspace</Button>
        </form>
        <PanelStatus o={o} loginUrl={loginUrl} />
      </div>
    </Panel>
  );
}
