import { useState } from "react";

import { api } from "../../api";
import { Slot } from "../../eeSlots";
import ActionError from "../ActionError";
import ConfirmDialog from "../ConfirmDialog";
import { ErrorState, usePolling } from "../bits";
import { FACTORY_POLL_MS, ViewHead, type Ctx } from "./common";
import { usePeople } from "../usePeople";
import type { Grant, GrantKind } from "../../types";

// What the people on this project allow the crew to do, in their words (TR-630). A project grant
// that names a repo someone else owns waits for that owner to agree; one from a person who does
// not lead the project waits for the lead too. The daemon decides who may give, agree and end a
// grant, and its message shows here when it refuses.

const KIND_TEXT: Record<GrantKind, string> = { team: "Team", project: "Project", personal: "Your own" };

export function GrantsView({ ctx }: { ctx: Ctx }) {
  const { data, error, reload } = usePolling(() => api.grants(ctx.id), FACTORY_POLL_MS);
  const { meId, isAdmin } = usePeople();
  const [adding, setAdding] = useState(false);
  const [revoking, setRevoking] = useState<Grant>();
  const [busy, setBusy] = useState<string>();
  const [err, setErr] = useState<unknown>();

  if (error && !data) return <ErrorState error={error} what="the grants" onRetry={reload} />;
  const here = data?.grants ?? [];
  const leads = !!meId && ctx.s.lead?.id === meId;

  const mayRevoke = (g: Grant) => g.state !== "revoked" && (isAdmin || leads
    || (!!meId && (g.given_by?.id === meId || g.holder?.id === meId)));

  const revoke = async (g: Grant) => {
    setBusy(g.id); setErr(undefined);
    try { await api.revokeGrant(g.id); reload(); } catch (e) { setErr(e); }
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
      <Slot name="grants.agreedBy" part="cell" grant={g} />
      <td>{g.in_effect ? <span className="badge ok">In effect</span>
        : g.state === "waiting" ? <span className="badge paused">Waiting<Slot name="grants.waitingFor" grant={g} /></span> : g.state}</td>
      <td style={{ textAlign: "right", whiteSpace: "nowrap" }}>
        <Slot name="grants.consent" grant={g} list={data} busy={busy === g.id}
              onBusy={setBusy} reload={reload} onError={setErr} />
        {mayRevoke(g) && <> <button className="danger" disabled={busy === g.id} onClick={() => setRevoking(g)}>Revoke</button></>}
      </td>
    </tr>
  );
  const head = <thead><tr><th>grant</th><th>kind</th><th>given by</th><Slot name="grants.agreedBy" part="head" /><th>state</th><th aria-label="actions" /></tr></thead>;

  return (
    <>
      <ViewHead title="Grants"
                sub="What you allow the crew to do in this project, in your own words. Team grants hold in every project.">
        {!adding && <button type="button" className="primary" onClick={() => { setErr(undefined); setAdding(true); }}>Add a grant</button>}
      </ViewHead>
      {err != null && <ActionError error={err} what="Changing a grant" />}
      {adding && <GrantForm ctx={ctx} onCancel={() => setAdding(false)} onSaved={() => { setAdding(false); reload(); }} />}
      {data === undefined ? <div className="dim">loading…</div> : here.length ? (
        <table>{head}<tbody>{here.map(row)}</tbody></table>
      ) : !adding && <div className="empty">No grants yet. Add one, or tell a session what it may do.</div>}
      <Slot name="grants.waitingElsewhere" here={here} list={data} row={row} head={head} />
      {revoking && (
        <ConfirmDialog title="Revoke this grant?" danger confirmLabel="Revoke"
          message={`“${revoking.words}” stops applying and is never in effect again.`}
          onCancel={() => setRevoking(undefined)}
          onConfirm={() => { const g = revoking; setRevoking(undefined); revoke(g); }} />
      )}
    </>
  );
}

/** Add a project grant: the words, and on a licensed cell the repos it names. */
function GrantForm({ ctx, onSaved, onCancel }: { ctx: Ctx; onSaved: () => void; onCancel: () => void }) {
  const [words, setWords] = useState("");
  const [repos, setRepos] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<unknown>();
  const save = async () => {
    setBusy(true); setErr(undefined);
    try { await api.addGrant({ kind: "project", project: ctx.id, words: words.trim(), areas: repos }); onSaved(); }
    catch (e) { setErr(e); }
    setBusy(false);
  };
  return (
    <div className="panel" style={{ marginBottom: 16 }}>
      <h3 style={{ margin: "0 0 10px" }}>New grant for this project</h3>
      {err != null && <ActionError error={err} what="Adding a grant" />}
      <label className="field">
        <span className="lbl">your words</span>
        <input type="text" autoFocus maxLength={400} value={words} onChange={(e) => setWords(e.target.value)}
               placeholder="what the crew may do, like: builders may merge when the reviewer passes" />
      </label>
      <Slot name="grants.repos" repos={repos} onChange={setRepos} />
      <div className="btnrow">
        <button className="primary" disabled={busy || !words.trim()} onClick={save}>{busy ? "…" : "Add grant"}</button>
        <button disabled={busy} onClick={onCancel}>Cancel</button>
      </div>
    </div>
  );
}
