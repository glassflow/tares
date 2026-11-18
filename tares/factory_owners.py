"""Who a thing waits for on a factory cell: THE rule, the one every screen reads.

Every waiting item (a pull request to merge, a milestone to check, calls to confirm, a desk
question, a spec ready to hand over, a project ready to close, a spec question, a grant) names the
people it waits for in `for_people`. Home, a project's page, Projects, Tickets and Reviews then
only split: what is the caller's ("Waiting for you"), and what is someone else's ("Waiting for
others", named for them). Nothing else decides it.

The owner of:
  - a ticket: its assignee, else its project's owner;
  - a project: its lead, else the cell's admins (the item then says the project has no lead);
  - a desk item: the person it is addressed to, else its project's owner, else the admins.

Somebody who left the cell, or the built-in person `local` nobody claimed, owns nothing: their work
falls to the next in line. On a cell with no admin person an orphaned item falls to the unclaimed
`local`, which only the cell's own credential (the root token) acts as: it is on no person's home,
its project page lists it under "Waiting for others" as for no one, with "No lead, set one", and
the root token (and a crew it runs) has it. It is never shown to everyone as theirs.
On a cell of one person everything is theirs.

Licence-independent by design (TR-915): the rule is core, so a team cell without a licence shows
each person their own. The team code only adds kinds of item and names the others.
"""
from __future__ import annotations


def claimed(person: dict) -> bool:
    """A person someone is: anyone but the built-in `local` before an owner claimed it (no email)."""
    return not (person.get("id") == "local" and not person.get("email"))


class Owners:
    """The owners on one read of the people. Cheap to make: one query of the people."""

    def __init__(self, store):
        self.store = store
        self._members: dict[str, bool] = {}
        people = [p for p in store.list_people() if not p.get("removed_at")]
        self.single = store.only_local_person()
        self.names = {p["id"]: p.get("name") or p["id"] for p in people}
        self.alive = {p["id"] for p in people if claimed(p)}
        admins = sorted(p["id"] for p in people if p["id"] in self.alive and p.get("is_admin"))
        # an orphan lands on the admins; with none, on the unclaimed `local` (only the cell's own
        # credential acts as it), so the root token still has it; never on everyone
        self.unclaimed_local = any(p["id"] == "local" and not claimed(p) for p in people)
        self.fallback = tuple(admins) or (("local",) if self.unclaimed_local else ())

    def owns_all(self, pid: str | None) -> bool:
        """`pid` owns every item: the one person of a cell of one."""
        return self.single

    def here(self, pid: str | None) -> tuple[str, ...] | None:
        """(pid,) when it names someone here: a person on the cell, or the unclaimed `local`
        an item was addressed to (the root credential's)."""
        if pid == "local" and self.unclaimed_local:
            return (pid,)
        return (pid,) if pid and pid in self.alive else None

    def _sees(self, pid: str, project: dict | None) -> bool:
        """On a private project only its lead and members own anything (TR-629): an item addressed
        to someone who cannot see it falls to the project's owner instead."""
        if not project or project.get("scope") != "private" or pid == project.get("lead_person_id"):
            return True
        key = f"{project['id']}\n{pid}"
        if key not in self._members:
            self._members[key] = self.store.is_project_member(project["id"], pid)
        return self._members[key]

    def no_lead(self, project: dict | None) -> bool:
        """The project has nobody to lead it (none set, or the lead left): its work goes to the admins."""
        return not self.single and bool(project) and self.here(project.get("lead_person_id")) is None

    def project(self, project: dict | None) -> tuple[str, ...]:
        if self.single:
            return ("local",)
        lead = self.here((project or {}).get("lead_person_id"))
        if lead:
            return lead
        # the admins who can open it: on a private project, only those on it
        return tuple(i for i in self.fallback
                     if (i == "local" and self.unclaimed_local) or self._sees(i, project))

    def orphan(self, ids) -> bool:
        """Nobody a person knows by name has it: no one, or only the unclaimed `local`. It is on
        no person's list; the root credential has it, and any admin key may act on it."""
        return not [i for i in ids if not (i == "local" and self.unclaimed_local)]

    def ticket(self, ticket: dict, project: dict | None) -> tuple[str, ...]:
        if self.single:
            return ("local",)
        a = ticket.get("assignee_person_id")
        return (self.here(a) if self._sees(a, project) else None) or self.project(project)

    def desk(self, item: dict, project: dict | None) -> tuple[str, ...]:
        if self.single:
            return ("local",)
        to = item.get("to_person_id")
        return (self.here(to) if self._sees(to, project) else None) or self.project(project)

    def who(self, ids) -> list[dict]:
        """[{id, name}] of the people `ids` names, in their order; the unclaimed `local` is no
        person anyone knows by name, so it is left out ("for no one")."""
        return [{"id": i, "name": self.names.get(i) or i} for i in ids
                if not (i == "local" and self.unclaimed_local)]


# ── the split every screen makes ──────────────────────────────────────────────

def mine(items: list[dict], me: str | None, *, everything: bool = False,
         admin: bool = False, orphans: bool = False) -> list[dict]:
    """The items that wait for `me`. `everything` is a cell of one person. `admin` is a credential
    with the admin scope: it also has a grant that waits for any admin (`for_admin`). `orphans`
    is the root credential: it works as `local` and also has what nobody a person knows has (no
    lead, no admin), so its list is what it may act on (desk_can_act). A caller with no person
    gets none."""
    if everything:
        return list(items)
    if not me:
        return []
    return [i for i in items if me in (i.get("for_people") or ())
            or (admin and i.get("for_admin"))
            or (orphans and not [p for p in (i.get("for_people") or ()) if p != "local"])]


def _and(names: list[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def for_others(owners: Owners, items: list[dict]) -> list[dict]:
    """`items` (none of them the caller's) said for the people they wait for: "Question for you"
    becomes "Question for Kiran", "Milestone to check" becomes "Milestone to check for Kiran", a
    grant for any admin is "for an admin". Nothing to press: only they may act on it."""
    out = []
    for i in items:
        names = [w["name"] for w in owners.who(i.get("for_people") or ())]
        if i.get("for_admin"):
            names.append("an admin")
        who = _and(names) if names else "no one"
        label = i["label"]
        base = label[: -len(" for you")] if label.endswith(" for you") else label
        first = (i.get("for_people") or [None])[0]
        out.append({**i, "label": f"{base} for {who}", "actions": [], "question": None,
                    "for_person": (owners.who([first]) or [None])[0] if first else None})
    return out


NO_LEAD = "No lead, set one"


def tag(item: dict, owners: Owners, ids, project: dict | None = None) -> dict:
    """`item` with the people it waits for; an item of a project with no lead says so in its meta,
    so whoever it fell to knows why and can set one."""
    item["for_people"] = list(dict.fromkeys(ids))
    if project is not None and owners.no_lead(project):
        item["no_lead"] = True
        item["meta"] = f"{NO_LEAD} · {item['meta']}" if item.get("meta") else NO_LEAD
    return item
