// Tares enterprise licence, see ee/LICENSE
import { useState } from "react";

import { api } from "../../api";
import ActionError from "../../components/ActionError";
import { Picker } from "../../components/bits";
import { extraEmails, personDraft, personLabel, type PersonDraft } from "../../personDraft";
import type { ApiKey, Person, Whoami } from "../../types";
import { createPerson, setKeyPerson } from "../api";

// Settings > Access, people and API keys, the team half (TR-930): adding and editing people, who a key
// belongs to, and the person picker of the new-key dialog. Core shows only your own row and the
// licence line; these replace them once the cell allows enterprise.

// Who is on the cell. Everyone who can read sees the list; an admin adds people and edits them.
// With auth on, the boot person `local` is not whoever is looking: it owns what was made while
// auth was off and acts for keys that belong to no one. It is called "You" only on an open cell.
export function PeoplePanel({ people, admin, err: loadErr, onChanged, me }: {
  people?: Person[]; admin: boolean; err?: string; onChanged: () => void; me?: Whoami;
}) {
  const authOn = !!me && me.kind !== "open";
  const shownName = (p: Person) => authOn && p.id === "local" && p.name === "You" ? "Local (no login)" : p.name;
  const [editing, setEditing] = useState<PersonDraft>();   // id "new": the add form
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<unknown>();

  const start = (p?: Person) => {
    setErr(undefined);
    setEditing(personDraft(p));
  };
  const save = async () => {
    if (!editing) return;
    setBusy(true); setErr(undefined);
    const body = { name: editing.name.trim(), email: editing.email.trim(), github: editing.github.trim(),
                   is_admin: editing.is_admin };
    try {
      if (editing.id === "new") {
        const made = await createPerson(body);
        // an email that is already someone's answers with that person (nothing was added): say so
        const known = people?.find((p) => p.id === made.id);
        if (known) {
          setErr(`${body.email} already belongs to ${known.name}.`);
          setBusy(false);
          return;
        }
      } else {
        await api.updatePerson(editing.id, { ...body, emails: extraEmails(editing) });
      }
      setEditing(undefined);
      onChanged();
    } catch (e) { setErr(e); }
    setBusy(false);
  };
  const form = (d: PersonDraft) => (
    <>
      <div className="row2">
        <label className="field">
          <span className="lbl">name</span>
          <input type="text" value={d.name} autoFocus onChange={(e) => setEditing({ ...d, name: e.target.value })} />
        </label>
        <label className="field">
          <span className="lbl">email</span>
          <input type="email" value={d.email} onChange={(e) => setEditing({ ...d, email: e.target.value })} />
        </label>
      </div>
      {d.id !== "new" && extraEmails(d).length > 0 && (
        <div className="field">
          <span className="lbl">other emails</span>
          <div style={{ display: "flex", flexWrap: "wrap", gap: 8, alignItems: "center" }}>
            {extraEmails(d).map((x) => (
              <span key={x} className="mono help" style={{ display: "inline-flex", gap: 6, alignItems: "center" }}>
                {x}
                <button type="button" aria-label={`Remove ${x}`}
                        onClick={() => setEditing({ ...d, dropped: [...d.dropped, x] })}>Remove</button>
              </span>
            ))}
          </div>
        </div>
      )}
      <div className="row2">
        <label className="field">
          <span className="lbl">GitHub login</span>
          <input type="text" className="mono" placeholder="e.g. octocat" value={d.github}
                 onChange={(e) => setEditing({ ...d, github: e.target.value })} />
        </label>
        <label className="field" style={{ display: "flex", gap: 10, alignItems: "baseline", cursor: "pointer" }}>
          <input type="checkbox" checked={d.is_admin} style={{ transform: "translateY(1px)" }}
                 onChange={(e) => setEditing({ ...d, is_admin: e.target.checked })} />
          <span>Admin</span>
        </label>
      </div>
      <div className="btnrow">
        <button className="primary" disabled={busy || !d.name.trim()} onClick={save}>
          {busy ? "…" : d.id === "new" ? "Add person" : "Save"}</button>
        <button disabled={busy} onClick={() => setEditing(undefined)}>Cancel</button>
      </div>
    </>
  );

  return (
    <div className="panel">
      <div className="pagehead" style={{ marginBottom: 6 }}>
        <h2 style={{ margin: 0 }}>People</h2>
        {admin && <button className="primary" onClick={() => start()}>Add person</button>}
      </div>
      <p className="help" style={{ marginTop: 0 }}>
        Everyone who works on this instance. Each API key belongs to a person, so you can tell whose
        agent or script is acting.
      </p>
      {(loadErr || err != null) && <ActionError error={err ?? loadErr}
                                                 what={editing && editing.id !== "new" ? "Changing other people" : "Adding people"} />}

      {editing?.id === "new" && <div className="panel" style={{ marginBottom: 12 }}>{form(editing)}</div>}

      {!people ? <div className="muted">loading…</div>
        : people.length === 0 ? <p className="help">No one yet.{admin && <> <strong>Add person</strong> to start.</>}</p>
        : (
          <table>
            <thead><tr><th>name</th><th>email</th><th>GitHub</th><th>admin</th><th aria-label="actions" /></tr></thead>
            <tbody>
              {people.map((p) => editing?.id === p.id ? (
                <tr key={p.id}><td colSpan={5}>{form(editing)}</td></tr>
              ) : (
                <tr key={p.id}>
                  <td>
                    <strong>{shownName(p)}</strong>
                    {authOn && p.id === me?.person?.id && <span className="badge" style={{ marginLeft: 8 }}>you</span>}
                  </td>
                  <td className="help">
                    {authOn && p.id === "local" && !p.email
                      ? "Owns what was made before login was turned on, and acts for keys with no person."
                      : p.email ?? ""}
                  </td>
                  <td className="mono help">{p.github ?? ""}</td>
                  <td className="help">{p.is_admin ? "yes" : "no"}</td>
                  <td style={{ textAlign: "right" }}>
                    {admin && <button disabled={!!editing} onClick={() => start(p)}>Edit</button>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
    </div>
  );
}

/** The person a key belongs to: an admin rebinds it here; everyone else just reads the name. */
export function KeyPersonCell({ k, people, admin, onChanged, onError }: {
  k: ApiKey; people: Person[]; admin: boolean; onChanged: () => void; onError: (message: string) => void;
}) {
  const names = Object.fromEntries([["", "No person"], ...people.map((p) => [p.id, personLabel(p)])]);
  // the key's own person is always a choice, named even before the people have loaded (or when
  // they failed to), so the picker never shows a raw id
  const ids = people.map((p) => p.id);
  const choices = !k.person ? { options: ["", ...ids], labels: names }
    : { options: ids.includes(k.person.id) ? ids : [k.person.id, ...ids],
        labels: { ...names, [k.person.id]: names[k.person.id] ?? k.person.name } };
  const bind = async (personId: string) => {
    if (!personId || personId === k.person?.id) return;
    try { await setKeyPerson(k.id, personId); } catch (e) { onError(String((e as Error).message ?? e)); }
    onChanged();
  };
  return admin && !k.revoked_at
    ? <Picker value={k.person?.id ?? ""} ariaLabel={`person for ${k.name}`} style={{ width: 200 }}
              {...choices} onChange={bind} />
    : k.person ? <>{k.person.name}</> : <span className="help">No person</span>;
}

/** The person field of the new-key dialog. */
export function KeyPersonPick({ people, value, onChange }: {
  people: Person[]; value: string; onChange: (personId: string) => void;
}) {
  return (
    <div className="field">
      <span className="lbl">person</span>
      <Picker value={value} ariaLabel="person" style={{ width: 280 }}
              options={["", ...people.map((p) => p.id)]}
              labels={Object.fromEntries([["", "No person"], ...people.map((p) => [p.id, personLabel(p)])])}
              onChange={onChange} />
      <span className="help">whose agent or script this is</span>
    </div>
  );
}
