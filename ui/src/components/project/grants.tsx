import { useState } from "react";
import { Link } from "react-router-dom";

import { api } from "../../api";
import ConfirmDialog from "../ConfirmDialog";
import { ErrorState, Picker, usePolling } from "../bits";
import { FACTORY_POLL_MS, ViewHead, type Ctx } from "./common";
import { usePeople } from "./access";
import type { Grant, GrantKind } from "../../types";

// What the people on this project allow the crew to do, in their words (TR-630). A project grant
// that names a repo someone else owns waits for that owner to agree; one from a person who does
// not lead the project waits for the lead too. The daemon decides who may give, agree and end a
// grant, and its message shows here when it refuses.

const KIND_TEXT: Record<GrantKind, string> = { team: "Team", project: "Project", personal: "Your own" };

const waitingNames = (g: Grant) => g.waiting_for.map((w) => w.name ?? w.id).join(", ");

export function GrantsView({ ctx }: { ctx: Ctx }) {
  const { data, error, reload } = usePolling(() => api.grants(ctx.id), FACTORY_POLL_MS);
  const { meId, isAdmin } = usePeople();
  const [adding, setAdding] = useState(false);
  const [revoking, setRevoking] = useState<Grant>();
  const [busy, setBusy] = useState<string>();
  const [err, setErr] = useState<string>();

  if (error && !data) return <ErrorState error={error} what="the grants" onRetry={reload} />;
  const here = data?.grants ?? [];
  // grants of other projects that wait for the caller: they may not see those projects
  const elsewhere = (data?.waiting_for_me ?? []).filter((g) => !here.some((h) => h.id === g.id));
  const mine = new Set((data?.waiting_for_me ?? []).map((g) => g.id));
  const leads = !!meId && ctx.s.lead?.id === meId;

  const mayConsent = (g: Grant) => g.state === "waiting" && (mine.has(g.id)
    || (!!meId && g.waiting_for.some((w) => w.id === meId))
    || (isAdmin && g.waiting_for.some((w) => w.id === "admin")));
  const mayRevoke = (g: Grant) => g.state !== "revoked" && (isAdmin || leads
    || (!!meId && (g.given_by?.id === meId || g.holder?.id === meId)));

  const act = async (g: Grant, what: "consent" | "revoke") => {
    setBusy(g.id); setErr(undefined);
    try {
      await (what === "consent" ? api.consentGrant(g.id) : api.revokeGrant(g.id));
      reload();
    } catch (e) { setErr(String((e as Error).message ?? e)); }
    setBusy(undefined);
  };

  const row = (g: Grant) => (
    <tr key={g.id}>
      <td style={{ overflowWrap: "anywhere" }}>
        “{g.words}”
        {g.areas.length > 0 && <div className="help mono">{g.areas.join(", ")}</div>}
      </td>
      <td>{g.project && g.project !== ctx.id ? `${KIND_TEXT[g.kind]}, ${ctx.projectName(g.project)}` : KIND_TEXT[g.kind]}</td>
      <td>{g.given_by?.name ?? <span className="dim">not recorded</span>}</td>
      <td className="help">{g.consents.map((c) => c.name).join(", ")}</td>
      <td>{g.in_effect ? <span className="badge ok">In effect</span>
        : g.state === "waiting" ? <span className="badge paused">Waiting for {waitingNames(g) || "no one"}</span> : g.state}</td>
      <td style={{ textAlign: "right", whiteSpace: "nowrap" }}>
        {mayConsent(g) && <button className="primary" disabled={busy === g.id} onClick={() => act(g, "consent")}>Consent</button>}
        {mayRevoke(g) && <> <button className="danger" disabled={busy === g.id} onClick={() => setRevoking(g)}>Revoke</button></>}
      </td>
    </tr>
  );
  const head = <thead><tr><th>grant</th><th>kind</th><th>given by</th><th>agreed by</th><th>state</th><th aria-label="actions" /></tr></thead>;

  return (
    <>
      <ViewHead title="Grants"
                sub="What you allow the crew to do in this project, in your own words. Team grants hold in every project.">
        {!adding && <button type="button" className="primary" onClick={() => { setErr(undefined); setAdding(true); }}>Add a grant</button>}
      </ViewHead>
      {err && <div className="alert error">{err}</div>}
      {adding && <GrantForm ctx={ctx} onCancel={() => setAdding(false)} onSaved={() => { setAdding(false); reload(); }} />}
      {data === undefined ? <div className="dim">loading…</div> : here.length ? (
        <table>{head}<tbody>{here.map(row)}</tbody></table>
      ) : !adding && <div className="empty">No grants yet. Add one, or tell a session what it may do.</div>}
      {elsewhere.length > 0 && (
        <>
          <h3 style={{ margin: "22px 0 8px" }}>Waiting for your yes in other projects</h3>
          <table>{head}<tbody>{elsewhere.map(row)}</tbody></table>
        </>
      )}
      {revoking && (
        <ConfirmDialog title="Revoke this grant?" danger confirmLabel="Revoke"
          message={`“${revoking.words}” stops applying and is never in effect again.`}
          onCancel={() => setRevoking(undefined)}
          onConfirm={() => { const g = revoking; setRevoking(undefined); act(g, "revoke"); }} />
      )}
    </>
  );
}

/** Add a project grant: the words, and the repos it names. */
function GrantForm({ ctx, onSaved, onCancel }: { ctx: Ctx; onSaved: () => void; onCancel: () => void }) {
  const { data: areas } = usePolling(() => api.areas(), 60000);
  const [words, setWords] = useState("");
  const [repos, setRepos] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string>();
  const owner = (repo: string) => areas?.find((a) => a.repo === repo)?.person.name;
  const free = (areas ?? []).map((a) => a.repo).filter((r) => !repos.includes(r));
  const save = async () => {
    setBusy(true); setErr(undefined);
    try { await api.addGrant({ kind: "project", project: ctx.id, words: words.trim(), areas: repos }); onSaved(); }
    catch (e) { setErr(String((e as Error).message ?? e)); }
    setBusy(false);
  };
  return (
    <div className="panel" style={{ marginBottom: 16 }}>
      <h3 style={{ margin: "0 0 10px" }}>New grant for this project</h3>
      {err && <div className="alert error">{err}</div>}
      <label className="field">
        <span className="lbl">your words</span>
        <input type="text" autoFocus maxLength={400} value={words} onChange={(e) => setWords(e.target.value)}
               placeholder="what the crew may do, like: builders may merge when the reviewer passes" />
      </label>
      <div className="field">
        <span className="lbl">repos it names</span>
        {repos.length > 0 && (
          <div className="btnrow" style={{ marginBottom: 8 }}>
            {repos.map((r) => (
              <span key={r} className="chip mono">
                {r}{owner(r) && <span className="dim"> · {owner(r)}</span>}
                <button type="button" className="chip-x" aria-label={`remove ${r}`}
                        onClick={() => setRepos(repos.filter((x) => x !== r))}>×</button>
              </span>
            ))}
          </div>
        )}
        {free.length > 0 && (
          <Picker value="" onChange={(v) => v && setRepos([...repos, v])} ariaLabel="Add a repo"
                  options={["", ...free]}
                  labels={{ "": "Add a repo", ...Object.fromEntries(free.map((r) => [r, `${r} (${owner(r)})`])) }} />
        )}
        <span className="help">
          {areas && areas.length === 0
            ? <>No repo has an owner yet, so nobody else has to agree. Owners are set under <Link to="/settings?tab=areas">Settings, Areas</Link>.</>
            : "A repo that someone else owns makes the grant wait for that owner to agree."}
        </span>
      </div>
      <div className="btnrow">
        <button className="primary" disabled={busy || !words.trim()} onClick={save}>{busy ? "…" : "Add grant"}</button>
        <button disabled={busy} onClick={onCancel}>Cancel</button>
      </div>
    </div>
  );
}
