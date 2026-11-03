import { useState } from "react";

import { api } from "../api";
import type { Area } from "../types";
import ConfirmDialog from "./ConfirmDialog";
import { ErrorState, Picker, usePolling } from "./bits";

// Who owns each GitHub repo (TR-627). A project grant that names a repo waits for its owner to
// agree. An admin sets, changes and removes owners; anyone else may take a repo nobody owns yet,
// for themselves, and edit the note of a repo they own. The daemon decides and its message shows
// here when it refuses.

type Draft = { repo: string; person: string; note: string; isNew: boolean };

export default function AreasPanel() {
  const { data: areas, error, reload } = usePolling(() => api.areas(), 30000);
  const { data: me } = usePolling(() => api.whoami(), 60000);
  const { data: people } = usePolling(() => api.people(), 60000);
  const [draft, setDraft] = useState<Draft>();
  const [removing, setRemoving] = useState<Area>();
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string>();

  // the admin scope, not the person's display flag: it is what the daemon checks
  const admin = !!me?.scopes.includes("admin");
  const meId = me?.person?.id;
  const list = people?.people ?? [];

  const start = (a?: Area) => {
    setErr(undefined);
    setDraft(a ? { repo: a.repo, person: a.person.id, note: a.note ?? "", isNew: false }
               : { repo: "", person: meId ?? "", note: "", isNew: true });
  };
  const save = async () => {
    if (!draft) return;
    setBusy(true); setErr(undefined);
    try {
      await api.setArea(draft.repo.trim(), draft.person, draft.note.trim());
      setDraft(undefined);
      reload();
    } catch (e) { setErr(String((e as Error).message ?? e)); }
    setBusy(false);
  };
  const remove = async (a: Area) => {
    setRemoving(undefined); setErr(undefined);
    try { await api.deleteArea(a.repo); reload(); }
    catch (e) { setErr(String((e as Error).message ?? e)); }
  };

  const form = (d: Draft) => (
    <>
      <div className="row2">
        <label className="field">
          <span className="lbl">repo</span>
          <input type="text" className="mono" placeholder="owner/name, like glassflow/tares" value={d.repo}
                 autoFocus={d.isNew} disabled={!d.isNew} onChange={(e) => setDraft({ ...d, repo: e.target.value })} />
        </label>
        <div className="field">
          <span className="lbl">owner</span>
          {admin ? (
            <Picker value={d.person} onChange={(v) => setDraft({ ...d, person: v })} ariaLabel="Owner"
                    options={["", ...list.map((p) => p.id)]}
                    labels={{ "": "Choose a person", ...Object.fromEntries(list.map((p) => [p.id, p.name])) }} />
          ) : <span>{me?.person?.name}</span>}
        </div>
      </div>
      <label className="field">
        <span className="lbl">note</span>
        <input type="text" placeholder="optional: what this repo is, who to ask" value={d.note}
               onChange={(e) => setDraft({ ...d, note: e.target.value })} />
      </label>
      <div className="btnrow">
        <button className="primary" disabled={busy || !d.repo.trim() || !d.person} onClick={save}>
          {busy ? "…" : admin ? "Save" : d.isNew ? "Take this repo" : "Save"}</button>
        <button disabled={busy} onClick={() => setDraft(undefined)}>Cancel</button>
      </div>
    </>
  );

  if (error && !areas) return <ErrorState error={error} what="the areas" onRetry={reload} />;
  const canAdd = admin || !!meId;
  return (
    <div className="panel">
      <div className="pagehead" style={{ marginBottom: 6 }}>
        <h2 style={{ margin: 0 }}>Areas</h2>
        {canAdd && !draft && <button className="primary" onClick={() => start()}>{admin ? "Add a repo" : "Take a repo"}</button>}
      </div>
      <p className="help" style={{ marginTop: 0 }}>
        The person who owns each GitHub repo. A project grant that names a repo waits for its owner to
        agree.{!admin && " Take a repo nobody owns yet; an admin changes the rest."}
      </p>
      {err && <div className="alert error">{err}</div>}

      {draft?.isNew && <div className="panel" style={{ marginBottom: 12 }}>{form(draft)}</div>}

      {!areas ? <div className="muted">loading…</div>
        : areas.length === 0 ? <p className="help">No repo has an owner yet.</p>
        : (
          <table>
            <thead><tr><th>repo</th><th>owner</th><th>note</th><th aria-label="actions" /></tr></thead>
            <tbody>
              {areas.map((a) => draft && !draft.isNew && draft.repo === a.repo ? (
                <tr key={a.repo}><td colSpan={4}>{form(draft)}</td></tr>
              ) : (
                <tr key={a.repo}>
                  <td className="mono">{a.repo}</td>
                  <td>{a.person.name}</td>
                  <td className="help">{a.note ?? ""}</td>
                  <td style={{ textAlign: "right" }}>
                    {(admin || a.person.id === meId) && <button disabled={!!draft} onClick={() => start(a)}>Edit</button>}
                    {admin && <> <button className="danger" disabled={!!draft} onClick={() => setRemoving(a)}>Remove</button></>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      {removing && (
        <ConfirmDialog title={`Remove the owner of ${removing.repo}?`} danger confirmLabel="Remove"
          message="The repo has no owner afterwards. A grant that was waiting only for this owner takes effect."
          onCancel={() => setRemoving(undefined)} onConfirm={() => remove(removing)} />
      )}
    </div>
  );
}
