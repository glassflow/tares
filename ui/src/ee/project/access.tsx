// Tares enterprise licence, see ee/LICENSE
import { useState } from "react";

import type { Person, ProjectMember } from "../../types";
import { Picker } from "../../components/bits";
import ActionError from "../../components/ActionError";
import { Lock } from "../../components/icons";
import { usePeople } from "../../components/usePeople";
import { ViewHead, type Ctx } from "../../components/project/common";
import { addProjectMember, removeProjectMember, setProjectAccess } from "../api";

// Who leads a project, whether it is open to the team or private, and who is on it. The lead
// changes the scope and the members; an admin changes the lead only. The calls need the admin
// scope, so a key without it sees all of this read-only. With auth off everyone has the admin
// scope and the one person is `local`. The system credential (the root token) has no person: it
// sees every private project and may manage access, but it leads nothing.

/** The caller against this project: the lead, an admin, or neither. */
function useAccess(ctx: Ctx) {
  const { list, meId, isAdmin, kind } = usePeople();
  const leadsIt = !!meId && ctx.s.lead?.id === meId;
  return {
    list, isAdmin, isLead: isAdmin && leadsIt,
    // who the members routes accept (the backend's _lead_or_system): the lead, or the system
    // credential, which auth off counts as too; and only with the admin scope
    canMembers: isAdmin && (leadsIt || kind === "system" || kind === "open"),
    leadNeedsAdmin: leadsIt && !isAdmin,
  };
}

/** Pick a person and make them the project's lead. Shared by the project page and the list's
 *  row for a private project the admin is not on. */
function LeadPicker({ id, people, onDone, onCancel }: {
  id: string; people: Person[]; onDone: () => void; onCancel: () => void;
}) {
  const [pick, setPick] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<unknown>();
  const chosen = people.some((p) => p.id === pick) ? pick : "";
  const save = async () => {
    setBusy(true); setErr(undefined);
    try { await setProjectAccess(id, { lead_person_id: chosen }); onDone(); }
    catch (e) { setErr(e); }
    setBusy(false);
  };
  return (
    <>
      <div className="acc-row">
        <Picker value={chosen} onChange={setPick} ariaLabel="New lead"
                options={["", ...people.map((p) => p.id)]}
                labels={{ "": "Choose a person", ...Object.fromEntries(people.map((p) => [p.id, p.name])) }} />
        <button type="button" className="primary" disabled={busy || !chosen} onClick={save}>
          {busy ? "Saving…" : "Make lead"}
        </button>
        <button type="button" disabled={busy} onClick={onCancel}>Cancel</button>
      </div>
      {err != null && <ActionError error={err} what="Changing who leads a project" style={{ margin: "6px 0 0" }} />}
    </>
  );
}

/** A private project the admin is not on, in the Projects list: a "Change lead" button that opens
 *  the picker in place. `lead` is the current one, left out of the choices. */
export function ChangeLead({ id, lead, people, onDone }: {
  id: string; lead?: { id: string } | null; people: Person[]; onDone: () => void;
}) {
  const { isAdmin } = usePeople();
  const [open, setOpen] = useState(false);
  if (!isAdmin) return null;
  return (
    <div style={{ marginTop: 8 }}>
      {open ? (
        <LeadPicker id={id} people={people.filter((p) => p.id !== lead?.id)}
                    onDone={() => { setOpen(false); onDone(); }} onCancel={() => setOpen(false)} />
      ) : <button type="button" onClick={() => setOpen(true)}>Change lead</button>}
    </div>
  );
}

const REASON: Record<ProjectMember["reason"], string> = {
  lead: "lead", manual: "added by hand", ticket: "assignee of a ticket",
};

/** "Lead: <name>" with its controls, under the project's name. */
export function ProjectAccess({ ctx }: { ctx: Ctx }) {
  const { list, isLead, isAdmin } = useAccess(ctx);
  const [changing, setChanging] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<unknown>();
  if (ctx.s.lead === undefined) return null;   // a daemon from before leads
  const lead = ctx.s.lead;
  const scope = ctx.s.scope ?? "team";

  const setScope = async (scope: "team" | "private") => {
    setBusy(true); setErr(undefined);
    try { await setProjectAccess(ctx.id, { scope }); ctx.refresh(); }
    catch (e) { setErr(e); }
    setBusy(false);
  };

  const candidates = list.filter((p) => p.id !== lead?.id);
  const canChange = (isLead || isAdmin) && candidates.length > 0;

  return (
    <div className="acc">
      <div className="acc-row">
        <span>{lead ? <>Lead: <strong>{lead.name}</strong></> : "No lead"}</span>
        {scope === "private" && !isLead && (
          <span className="acc-private" title="Only its lead and members can open it">
            <Lock /> Private
          </span>
        )}
        {isLead && (
          <span className="seg small" role="group" aria-label="Who can open this project">
            <button type="button" className={scope === "team" ? "active" : undefined} aria-pressed={scope === "team"}
                    disabled={busy} onClick={() => scope !== "team" && setScope("team")}>Team</button>
            <button type="button" className={scope === "private" ? "active" : undefined} aria-pressed={scope === "private"}
                    disabled={busy} onClick={() => scope !== "private" && setScope("private")}>Private</button>
          </span>
        )}
        {canChange && !changing && (
          <button type="button" className="linklike" onClick={() => { setErr(undefined); setChanging(true); }}>
            {lead ? "Change lead" : "Set a lead"}
          </button>
        )}
      </div>
      {changing && (
        <LeadPicker id={ctx.id} people={candidates}
                    onDone={() => { setChanging(false); ctx.refresh(); }} onCancel={() => setChanging(false)} />
      )}
      {isLead && (
        <p className="help">
          {scope === "private"
            ? "Private: only its lead and members can open this project."
            : "Team: everyone on this cell can open this project."}
        </p>
      )}
      {err != null && <ActionError error={err} what="Making a project private" />}
    </div>
  );
}

/** Settings, Members: who is on the project, and for its lead, who is added or removed. */
export function ProjectMembers({ ctx }: { ctx: Ctx }) {
  const { list, canMembers, leadNeedsAdmin } = useAccess(ctx);
  const [pick, setPick] = useState("");
  const [busy, setBusy] = useState<string>();
  const [err, setErr] = useState<unknown>();
  const members = ctx.s.members ?? [];
  const onIt = new Set(members.map((m) => m.id));
  const candidates = list.filter((p) => !onIt.has(p.id));
  const chosen = candidates.some((p) => p.id === pick) ? pick : "";

  const run = async (key: string, fn: () => Promise<unknown>) => {
    setBusy(key); setErr(undefined);
    try { await fn(); ctx.refresh(); }
    catch (e) { setErr(e); }
    setBusy(undefined);
  };

  return (
    <>
      <ViewHead title="Members"
                sub={ctx.s.scope === "private"
                  ? "Only the lead and these people can open this project."
                  : "Everyone on this cell can open this project. Members are the people it is set up for."} />
      {err != null && <ActionError error={err} what="Changing who is on a project" />}
      {members.length ? (
        <table>
          <thead><tr><th>person</th><th>why they are on it</th><th aria-label="actions" /></tr></thead>
          <tbody>
            {members.map((m) => (
              <tr key={m.id}>
                <td><strong>{m.name}</strong></td>
                <td className="help">{REASON[m.reason] ?? m.reason}</td>
                <td style={{ textAlign: "right" }}>
                  {canMembers && m.reason !== "lead" && (
                    <button type="button" className="danger" disabled={busy === m.id}
                            onClick={() => run(m.id, () => removeProjectMember(ctx.id, m.id))}>
                      {busy === m.id ? "Removing…" : "Remove"}
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : <div className="empty">Nobody is on this project yet. It has no lead.</div>}
      {canMembers ? (
        <div className="panel" style={{ marginTop: 12 }}>
          {candidates.length ? (
            <div className="field" style={{ marginBottom: 0 }}>
              <span className="lbl">Add a person</span>
              <div className="btnrow">
                <Picker value={chosen} onChange={setPick} ariaLabel="Person to add"
                        options={["", ...candidates.map((p) => p.id)]}
                        labels={{ "": "Choose a person", ...Object.fromEntries(candidates.map((p) => [p.id, p.name])) }} />
                <button type="button" className="primary" disabled={!!busy || !chosen}
                        onClick={() => run("add", () => addProjectMember(ctx.id, chosen))}>
                  {busy === "add" ? "Adding…" : "Add"}
                </button>
              </div>
            </div>
          ) : <p className="help" style={{ margin: 0 }}>Everyone on this cell is on this project already.</p>}
        </div>
      ) : (
        <p className="help">
          {leadNeedsAdmin
            ? "You lead this project, but this key needs the admin scope to change who is on it."
            : "Only the project's lead changes who is on it."}
        </p>
      )}
    </>
  );
}
