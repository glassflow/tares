import { type FormEvent, useEffect, useState } from "react";

import { api, type CloudMember, type CloudResult, type CloudWorkspaceOverview, cloudApi } from "../api";
import type { Person } from "../types";
import { type Cloud, reloadList, signInLink, slugFromHost, useWorkspaceOverview, type WorkspaceOverview } from "../cloud";
import { Switch } from "../components/setup/common";
import { UsagePanels } from "../components/UsagePanels";
import LicenceNeeded from "../components/LicenceNeeded";
import { useLicence } from "../licence";

// Settings > Workspace on Tares Cloud (TR-375, TR-376): team, plan, storage, included credit, the
// sign-in pin and delete, read from and written to the control plane with the person's Tares
// Cloud session (contract §2). The data stays in the control plane; this console only shows it.
// Members see everything read-only; the control plane checks every write, and the console hides
// what the person cannot do.

const STATE_WORDS: Record<string, string> = {
  active: "running",
  provisioning: "setting up",
  suspended: "suspended",
  error: "not running",
};

const WARN_PCT = 80;   // the same threshold the control plane and this console's Overview use
const GIB = 1024 ** 3;

function fmtGiB(bytes: number): string {
  const gib = bytes / GIB;
  return gib < 10 ? gib.toFixed(1) : String(Math.round(gib));
}

const usd = (n: number) => `$${n.toFixed(2)}`;

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

function Fail({ text }: { text?: string }) {
  return text ? <div className="alert error" role="alert">{text}</div> : null;
}

/** A write in one panel met a 401: that panel says so, the rest of the tab stays as it is. */
function SignedOutLine({ loginUrl }: { loginUrl?: string }) {
  return (
    <div className="alert" role="alert">
      You are signed out of Tares Cloud.{" "}
      {loginUrl ? <a href={signInLink(loginUrl)}>Sign in to change this.</a> : "Sign in to change this."}
    </div>
  );
}

/** What one call came to, for a panel: done, signed out, or the server's detail. */
type Outcome = { signedOut?: boolean; err?: string };
function outcome(r: CloudResult<unknown>): Outcome {
  return r.status === "signed_out" ? { signedOut: true } : r.status === "error" ? { err: r.detail } : {};
}

function PanelStatus({ o, loginUrl }: { o: Outcome; loginUrl?: string }) {
  if (o.signedOut) return <SignedOutLine loginUrl={loginUrl} />;
  return <Fail text={o.err} />;
}

export default function WorkspaceSettings({ cloud, apiUrl, onOpenTab, noTeam }: {
  cloud: Cloud; apiUrl: string; onOpenTab: (tab: "anthropic") => void;
  /** The team has its own page (a factory cell's Settings, Team, People). */
  noTeam?: boolean;
}) {
  const ov = useWorkspaceOverview(apiUrl);
  const r = ov.result;
  const loginUrl = cloud.health?.login_url;

  if (!r) return <div className="panel"><div className="muted">loading…</div></div>;
  if (r.status === "signed_out") {
    return (
      <div className="panel">
        <p className="help" style={{ marginTop: 0 }}>You are signed out of Tares Cloud.</p>
        {loginUrl
          ? <a className="btn primary" href={signInLink(loginUrl)}>Sign in to manage this workspace</a>
          : <p className="help" style={{ margin: 0 }}>Sign in to Tares Cloud to manage this workspace.</p>}
      </div>
    );
  }
  if (r.status === "error") {
    return (
      <div className="panel">
        <Fail text={r.code === 404 ? "You no longer have access to this workspace." : r.detail} />
        {r.code !== 404 && <button onClick={ov.reload}>Try again</button>}
      </div>
    );
  }

  const w = r.data;
  const slug = w.slug || slugFromHost();
  const isOwner = w.role === "owner";
  const showCredit = w.trial && (w.trial.state === "active" || w.trial.state === "exhausted");
  const p = { apiUrl, slug, ov, loginUrl };
  return (
    <>
      <OverviewPanel w={w} />
      {!noTeam && <TeamPanel {...p} isOwner={isOwner} you={w.you?.email} />}
      <StoragePanel {...p} w={w} isOwner={isOwner} />
      {showCredit && <CreditPanel w={w} onOpenTab={onOpenTab} />}
      {/* what this cell spent on model providers; storage is the panel above, from the control plane */}
      <UsagePanels storage={false} />
      <PinPanel {...p} w={w} />
      {isOwner && <DeletePanel {...p} />}
    </>
  );
}

type PanelProps = { apiUrl: string; slug: string; ov: WorkspaceOverview; loginUrl?: string };

function OverviewPanel({ w }: { w: CloudWorkspaceOverview }) {
  return (
    <div className="panel">
      <h2 style={{ marginTop: 0 }}>{w.slug}</h2>
      <dl className="ws-facts">
        <div><dt>Plan</dt><dd>{w.plan}</dd></div>
        <div><dt>Tares version</dt><dd className="mono">{w.image_tag || "not known"}</dd></div>
        <div><dt>State</dt><dd>{STATE_WORDS[w.state] ?? w.state}</dd></div>
      </dl>
    </div>
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
  const saveEdit = async () => {
    if (!edit) return;
    setBusy(edit.id); setCellErr(undefined);
    try {
      await api.updatePerson(edit.id, { github: edit.github.trim() || null, is_admin: edit.is_admin });
      setEdit(undefined);
      cell?.reload();
    } catch (e) { setCellErr(String((e as Error).message ?? e)); }
    setBusy(undefined);
  };
  const removeStray = async (p: Person) => {
    setBusy(p.id); setCellErr(undefined);
    try { await api.removePerson(p.id); cell?.reload(); }
    catch (e) { setCellErr(String((e as Error).message ?? e)); }
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
  return (
    <div className="panel">
      <h2 style={{ marginTop: 0 }}>{cell ? "People" : "Team"}</h2>
      {!cell && <p className="help" style={{ marginTop: 0 }}>
        {isOwner
          ? "People with access to this workspace. Invited teammates get access when they sign in with that email."
          : "People with access to this workspace."}
      </p>}
      <PanelStatus o={o} loginUrl={loginUrl} />
      {msg && <p className="help" role="status">{msg}</p>}
      {cellErr && <Fail text={cellErr} />}
      {!members ? (!o.err && !o.signedOut && <div className="muted">loading…</div>) : (
        <div className="table-scroll">
        <table>
          <thead><tr><th>email</th><th>role</th>{cell && <><th>GitHub</th><th>admin</th></>}{(isOwner || cell?.admin) && <th aria-label="actions" />}</tr></thead>
          <tbody>
            {members.map((m) => {
              const person = personOf(m);
              if (edit && person?.id === edit.id) return (
                <tr key={m.user_id} data-testid="team-edit">
                  <td>{m.email}</td>
                  <td>{m.role === "owner" ? "owner" : "member"}</td>
                  <td><input type="text" className="mono" aria-label="GitHub login" placeholder="e.g. octocat" value={edit.github}
                             onChange={(e) => setEdit({ ...edit, github: e.target.value })} /></td>
                  <td><input type="checkbox" aria-label="Admin" checked={edit.is_admin} onChange={(e) => setEdit({ ...edit, is_admin: e.target.checked })} /></td>
                  <td style={{ textAlign: "right" }}>
                    <div className="btnrow" style={{ justifyContent: "flex-end" }}>
                      <button className="primary" disabled={!!busy} onClick={saveEdit}>Save</button>
                      <button disabled={!!busy} onClick={() => setEdit(undefined)}>Cancel</button>
                    </div>
                  </td>
                </tr>
              );
              return (
              <tr key={m.user_id} data-testid="team-row">
                <td>
                  {m.email}{m.name && <span className="help"> {m.name}</span>}
                  {me(m) && <span className="help"> (you)</span>}
                </td>
                <td>
                  {m.role === "owner" ? "owner" : "member"}
                  {m.role !== "owner" && m.status === "invited"
                    && <span className="badge paused" style={{ marginLeft: 8 }}>invited</span>}
                </td>
                {cell && <>
                  <td className="mono help">{person?.github ?? ""}</td>
                  <td className="help">{person ? (person.is_admin ? "yes" : "no") : ""}</td>
                </>}
                {(isOwner || cell?.admin) && (
                  <td style={{ textAlign: "right" }}>
                    <div className="btnrow" style={{ justifyContent: "flex-end" }}>
                      {cell?.admin && person && (
                        <button disabled={!!busy || !!edit} aria-label={`Edit ${m.email}`}
                                onClick={() => setEdit({ id: person.id, github: person.github ?? "", is_admin: person.is_admin })}>Edit</button>
                      )}
                      {isOwner && m.role !== "owner" && (
                        <button className="danger" disabled={!!busy} onClick={() => remove(m)}
                                aria-label={`Remove ${m.email}`}>
                          {busy === m.user_id ? "removing…" : "Remove"}
                        </button>
                      )}
                    </div>
                  </td>
                )}
              </tr>
              );
            })}
          </tbody>
        </table>
        </div>
      )}
      {strays.length > 0 && (
        <div style={{ marginTop: 14 }} data-testid="team-strays">
          <p className="help" style={{ margin: "0 0 6px" }}>Listed on this workspace's records, but not on the team:</p>
          {strays.map((p) => (
            <div key={p.id} className="btnrow" style={{ justifyContent: "space-between", padding: "6px 0" }} data-testid="team-stray">
              <span>{p.name}{p.email && <span className="help"> {p.email}</span>}</span>
              {cell?.admin && <button className="danger" disabled={!!busy} aria-label={`Remove ${p.name}`} onClick={() => removeStray(p)}>Remove</button>}
            </div>
          ))}
        </div>
      )}
      {isOwner && (canInvite ? (
        <form className="btnrow" style={{ marginTop: 14, maxWidth: 560 }} onSubmit={invite}>
          <label htmlFor="ws-invite" className="sr-only">Teammate's email</label>
          <input id="ws-invite" type="email" style={{ flex: 1 }} placeholder="teammate@company.com"
                 autoComplete="off" value={email} onChange={(e) => setEmail(e.target.value)} />
          <button className="primary" type="submit" disabled={!!busy || !email.trim()}>
            {busy === "invite" ? "inviting…" : "Invite"}
          </button>
        </form>
      ) : <LicenceNeeded what="Inviting people" />)}
      {isOwner && canInvite && noLicence && <LicenceNeeded what="Inviting people" />}
    </div>
  );
}

function StoragePanel({ w, apiUrl, slug, ov, loginUrl, isOwner }: PanelProps & {
  w: CloudWorkspaceOverview; isOwner: boolean;
}) {
  const [size, setSize] = useState(w.storage_gb);
  const [busy, setBusy] = useState(false);
  const [o, setO] = useState<Outcome>({});
  const [ok, setOk] = useState(false);
  useEffect(() => { setSize(w.storage_gb); }, [w.storage_gb]);
  const atMax = w.storage_gb >= w.storage_max_gb;
  const pct = w.storage_pct_used;
  const near = pct != null && pct >= WARN_PCT;

  const grow = async () => {
    setBusy(true); setO({}); setOk(false);
    const r = await cloudApi.growStorage(apiUrl, slug, size);
    if (r.status === "ok") {
      // keep only the storage fields the answer carries; the rest of the overview stays as it is
      const d = r.data ?? {};
      ov.update({
        storage_gb: typeof d.storage_gb === "number" ? d.storage_gb : size,
        ...(typeof d.storage_max_gb === "number" ? { storage_max_gb: d.storage_max_gb } : {}),
        ...("storage_used_bytes" in d ? { storage_used_bytes: d.storage_used_bytes ?? null } : {}),
        ...("storage_pct_used" in d ? { storage_pct_used: d.storage_pct_used ?? null } : {}),
      });
      setOk(true);
    } else {
      setSize(w.storage_gb);   // the slider goes back to what the workspace has
      setO(outcome(r));
    }
    setBusy(false);
  };

  return (
    <div className="panel">
      <h2 style={{ marginTop: 0 }}>Storage</h2>
      {w.storage_used_bytes == null || pct == null ? (
        <p className="help" style={{ marginTop: 0 }}>
          Usage is unavailable right now. It appears here once the workspace reports in.
        </p>
      ) : (
        <>
          <p style={{ margin: "0 0 8px", display: "flex", justifyContent: "space-between", gap: 12 }}>
            <span><strong>{fmtGiB(w.storage_used_bytes)} GiB</strong> of {w.storage_gb} GiB used</span>
            <span className={near ? "ws-hot" : "help"}>{pct}%</span>
          </p>
          <div className="usage-bar" role="progressbar" aria-label="Storage used"
               aria-valuenow={pct} aria-valuemin={0} aria-valuemax={100}>
            <span className={near ? "hot" : undefined} style={{ width: `${Math.min(100, Math.max(0, pct))}%` }} />
          </div>
          {near && (
            <div className="alert warn" style={{ margin: "12px 0 0" }}>
              This workspace is {pct}% full.{" "}
              {isOwner ? "Grow the storage below to keep room for new events."
                : "The workspace owner can grow the storage to keep room for new events."}
            </div>
          )}
        </>
      )}
      <p className="help">
        Currently <strong>{w.storage_gb} GiB</strong>, up to {w.storage_max_gb} GiB on
        the {w.plan} plan. Storage grows online and cannot be shrunk.
      </p>
      {isOwner && (atMax ? (
        <p className="help" style={{ marginBottom: 0 }}>At the plan maximum.</p>
      ) : (
        <div className="btnrow" style={{ maxWidth: 560 }}>
          <label htmlFor="ws-storage" className="sr-only">New storage size in GiB</label>
          {/* step 1, so the plan maximum is reachable whatever size the workspace starts at */}
          <input id="ws-storage" type="range" className="ws-range" min={w.storage_gb} max={w.storage_max_gb}
                 step={1} value={size} disabled={busy}
                 aria-valuetext={`${size} GiB`}
                 onChange={(e) => { setSize(Number(e.target.value)); setOk(false); }} />
          <span className="mono" style={{ minWidth: 64, textAlign: "right" }} aria-hidden="true">{size} GiB</span>
          <button className="primary" disabled={busy || size === w.storage_gb} onClick={grow}>
            {busy ? "growing…" : "Grow"}
          </button>
        </div>
      ))}
      {ok && <p className="help" role="status">Storage updated. It grows in the background.</p>}
      <PanelStatus o={o} loginUrl={loginUrl} />
    </div>
  );
}

function CreditPanel({ w, onOpenTab }: { w: CloudWorkspaceOverview; onOpenTab: (tab: "anthropic") => void }) {
  const t = w.trial!;
  const providers = (
    <button type="button" className="linklike" onClick={() => onOpenTab("anthropic")}>Model providers</button>
  );
  return (
    <div className="panel">
      <h2 style={{ marginTop: 0 }}>Included credit</h2>
      {t.state === "exhausted" ? (
        <p className="help" style={{ margin: 0 }}>
          The included <strong>{usd(t.credit_usd)}</strong> Anthropic credit is used up. Add your
          own API key under {providers} and your agents keep running on it.
        </p>
      ) : (
        <p className="help" style={{ margin: 0 }}>
          <strong>{usd(t.spend_usd ?? 0)} of {usd(t.credit_usd)}</strong> included Anthropic credit
          used. Agent runs are on us until it is used up. Adding your own API key under {providers}{" "}
          takes over immediately.
        </p>
      )}
    </div>
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
    <div className="panel">
      <Switch checked={w.is_default} disabled={busy || !w.default_url} onChange={change}>
        <strong>Open this workspace when I sign in</strong>
        <span className="help" style={{ display: "block" }}>
          {w.is_default ? "Signing in to Tares Cloud brings you straight here."
            : "Right now you land in the workspace you opened last."}
        </span>
      </Switch>
      <PanelStatus o={o} loginUrl={loginUrl} />
    </div>
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
    <div className="panel ws-danger">
      <h2 style={{ marginTop: 0 }}>Delete workspace</h2>
      <p className="help" style={{ marginTop: 0 }}>
        Permanently deletes <strong>{slug}</strong> and all its data: sources, events, projects,
        agents and settings. This cannot be undone. Afterwards you land in your next workspace.
      </p>
      <form className="btnrow" style={{ maxWidth: 560 }} onSubmit={del}>
        <label htmlFor="ws-delete" className="sr-only">Type {slug} to confirm</label>
        <input id="ws-delete" type="text" className="mono" style={{ flex: 1 }} autoComplete="off"
               placeholder={`type ${slug} to confirm`} value={typed} disabled={busy}
               onChange={(e) => setTyped(e.target.value)} />
        <button className="danger" type="submit" disabled={busy || typed !== slug}>
          {busy ? "deleting…" : "Delete workspace"}
        </button>
      </form>
      <PanelStatus o={o} loginUrl={loginUrl} />
    </div>
  );
}
