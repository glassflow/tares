// The body of GET /api/factory/people on a factory cell (tares/screens/factory_people.py). The
// sentences (`state_words`, `add_text`) are worded for people and shown as they are. Names, emails,
// project names and repos are a person's own text, and the page shows every string as text.

import { request } from "../../api";

export interface PersonRow {
  id: string;
  name: string;
  email: string | null;
  admin: boolean;
  /** An https address of their picture (TR-1016); null shows their initials. */
  picture: string | null;
  /** The person this console is signed in as. */
  you: boolean;
  /** "active": has signed in; "invited": added, not signed in yet. */
  state: "active" | "invited";
  /** "Last active 3 days ago", "Invited, not signed in yet". */
  state_words: string;
  /** The open projects they lead. */
  leads: { id: string; name: string }[];
  /** The repos they own. */
  owns: string[];
}

export interface FactoryPeopleData {
  people: PersonRow[];
  /** Whether this console may add someone (an admin key). */
  can_add: boolean;
  /** What adding a person does here, or why this console cannot. */
  add_text: string;
  /** On Tares Cloud the invite goes through the workspace. */
  cloud: boolean;
  workspace_href: string | null;
  now: string;
}

export interface AddedPerson { id: string; name: string; email: string | null; invited: boolean }

export const factoryPeople = () => request<FactoryPeopleData>("/api/factory/people");

export const addPerson = (email: string, name?: string) =>
  request<AddedPerson>("/api/factory/people", { method: "POST", body: JSON.stringify({ email, name: name || undefined }) });
