"""Who a ticket is for (TR-631): the assignee, wherever the ticket lives.

A ticket has an assignee person, or none: then it shows as unowned together with the assignee as
the source named them (`assignee_raw`: an email, a name or a GitHub login). That is not the
ticket's `owner` column, which says where it lives (tares, linear or github), and not its
`holder`, the crew station working it.

Linear and GitHub name the assignee; the sync maps it to a person here. Tares tickets take an
assignee from the ticket routes and write_ticket.
"""
from __future__ import annotations

import logging

log = logging.getLogger("tares.ticket_assignee")

_warned: set = set()      # projects already warned about assignees without an email

Mapped = tuple[str | None, str | None]    # (person id, raw)


def _norm(v) -> str:
    return str(v or "").strip()


def from_linear(store, assignee: dict | None, cache: dict | None = None) -> Mapped:
    """(person id, raw) for the `assignee { email name }` of a Linear issue. A person is found by
    email (their main or an extra one); without a match, or when this Linear key cannot read
    emails, the raw value is the email, else the name. No assignee gives (None, None)."""
    if not isinstance(assignee, dict):
        return None, None
    email, name = _norm(assignee.get("email")), _norm(assignee.get("name"))
    if email:
        key = email.lower()
        if cache is not None and key in cache:
            person = cache[key]
        else:
            person = store.find_person_by_email(email)
            if cache is not None:
                cache[key] = person
        if person:
            return person["id"], None
    return None, email or name or None


def warn_nameless(project: dict, count: int) -> None:
    """Say once per project per daemon run that Linear sent assignees without an email."""
    if count and project["id"] not in _warned:
        _warned.add(project["id"])
        log.warning("Linear sent %d assignee(s) without an email in project %s (this key may not "
                    "read emails); those tickets are matched by nothing and show the assignee's "
                    "name", count, project.get("name") or project["id"])


def from_github(store, issue: dict) -> Mapped:
    """(person id, raw) for the first assignee of a GitHub issue, matched on the GitHub login."""
    nodes = ((issue.get("assignees") or {}).get("nodes")) or []
    login = _norm((nodes[0] or {}).get("login")) if nodes else ""
    if not login:
        return None, None
    person = store.find_person_by_github(login)
    return (person["id"], None) if person else (None, login)


def resolve(store, ref) -> Mapped:
    """(person id, raw) for what a caller typed: a person id or an email. An email nobody on the
    cell has comes back as raw with no person; empty clears. ValueError for text that is neither
    an id nor an email."""
    ref = _norm(ref)
    if not ref:
        return None, None
    person = store.get_person(ref)
    if person:
        return person["id"], None
    if "@" in ref:
        person = store.find_person_by_email(ref)
        return (person["id"], None) if person else (None, ref)
    raise ValueError(f"no person {ref!r}; give a person id or an email")


def assign(store, project: dict, tid: str, person_id: str | None, raw: str | None,
           added_by: str | None = None) -> None:
    """Set a ticket's assignee. A person assigned on a private project joins it as a member with
    the reason `ticket`; taking the assignee off removes nobody. The ticket's holder and stage
    stay as they are."""
    store.update_ticket(tid, assignee_person_id=person_id or None, assignee_raw=raw or None)
    join(store, project, person_id, added_by)


def join(store, project: dict, person_id: str | None, added_by: str | None = None) -> None:
    """A person assigned to a ticket of a private project is a member of it (reason `ticket`).
    The lead is a member already and gets no row."""
    if (person_id and project.get("scope") == "private"
            and person_id != project.get("lead_person_id")):
        store.add_project_member(project["id"], person_id, added_by, reason="ticket")


def sync_join(store, project: dict, person_id: str | None, status: str) -> None:
    """The join for an assignee a sync mapped: only for a ticket still open, so linking a project
    does not bring in everyone who ever finished or dropped a ticket."""
    if status not in ("done", "canceled"):
        join(store, project, person_id, "system")


def view(store, t: dict) -> dict:
    """The ticket's `assignee` ({id, name} or None) and `assignee_raw` (set only while nobody on
    the cell is the assignee)."""
    person = store.get_person(t["assignee_person_id"]) if t.get("assignee_person_id") else None
    if person:
        return {"assignee": {"id": person["id"], "name": person["name"]}, "assignee_raw": None}
    return {"assignee": None, "assignee_raw": t.get("assignee_raw")}


def note(t: dict) -> str:
    """One line for text output: "assignee: Kiran", or why nobody is."""
    a = t.get("assignee")
    if a:
        return f"assignee: {a['name']}"
    raw = t.get("assignee_raw")
    return f"unowned ({raw} is not on this cell)" if raw else "unowned"
