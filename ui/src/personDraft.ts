import type { Person } from "./types";

/** A person as the pickers name them: "Name (email)", or just the name. */
export const personLabel = (p: Person) => p.email ? `${p.name} (${p.email})` : p.name;

// `oldEmail` and `extras` are the person's main and extra addresses as loaded: an edit that changes
// the main email keeps the old one among the extras, so the person is still found by it. `dropped`
// are the extras the admin removed in this edit.
export type PersonDraft = { id: string; name: string; email: string; github: string; is_admin: boolean;
                            oldEmail: string; extras: string[]; dropped: string[] };

/** A draft of an existing person, or a blank one (id "new") for the add form. */
export function personDraft(p?: Person): PersonDraft {
  return p ? { id: p.id, name: p.name, email: p.email ?? "", github: p.github ?? "", is_admin: p.is_admin,
               oldEmail: p.email ?? "", extras: p.emails, dropped: [] }
           : { id: "new", name: "", email: "", github: "", is_admin: false, oldEmail: "", extras: [], dropped: [] };
}

// The extra addresses this edit leaves the person with: the loaded ones, plus the old main email when
// the main one changed, minus the new main email and whatever the admin removed.
export function extraEmails(d: PersonDraft): string[] {
  const email = d.email.trim().toLowerCase();
  const old = d.oldEmail.toLowerCase();
  const all = [...d.extras, ...(old && email !== old ? [old] : [])];
  return all.filter((x, i) => x !== email && !d.dropped.includes(x) && all.indexOf(x) === i);
}
