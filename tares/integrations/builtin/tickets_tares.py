"""Tares tickets as the built-in tickets integration (TR-844).

A project whose tickets live in Tares keeps them in the same tables as before: this class is a
thin front over the store calls the ticket and milestone routes already make, so a Tares-only
project behaves the same when it goes through the integration. It adds no storage.

The integration needs the cell's store: `TicketsTares(store)`. Without one (the settings page
builds it bare to test the connection) only `check_connection` works.

`project` is a project dict or its id. `ticket` is a ticket dict as the store reads it (it holds
the project), never a bare id, because a ticket is looked up inside its project.
"""
from __future__ import annotations

import asyncio

from .. import tickets
from ..types import Status, TicketRef
from ... import docs as docs_mod
from ... import factory as factory_mod

# Tares tickets have no in-review status; a ticket under review is in progress.
_TARES_STATUS = {Status.todo: "todo", Status.in_progress: "in_progress",
                 Status.in_review: "in_progress", Status.done: "done"}


class TicketsTares(tickets.TicketBackend):
    name = "tares"
    title = "Tares"
    settings_schema: list = []

    def __init__(self, store=None):
        self.store = store

    async def check_connection(self, settings: dict) -> tuple[bool, str]:
        return True, "Tickets live in Tares: nothing to connect."

    # ── the interface ─────────────────────────────────────────────────────────
    async def sync(self, project) -> int:
        """Tares is where the tickets are written: there is nothing to read in."""
        return 0

    async def link(self, project, link_fields: dict) -> dict:
        """Nothing to link a project to: Tares tickets belong to the project itself."""
        return {}

    async def set_status(self, ticket, status: Status) -> None:
        try:
            value = _TARES_STATUS[Status(status)]
        except ValueError:
            raise ValueError(f"unknown status {status!r}; one of: "
                             + ", ".join(s.value for s in Status)) from None
        t = self._ticket(ticket)
        await asyncio.to_thread(self.store.update_ticket, t["id"],
                                why=f"marked {value.replace('_', ' ')} in Tares", status=value)

    async def create(self, project, milestones: list, tickets: list) -> list[TicketRef]:
        milestones, tickets = list(milestones or []), list(tickets or [])
        if len(milestones) + len(tickets) != 1:
            raise ValueError("create takes one milestone or one ticket per call")
        uid = self._project(project)
        if milestones:
            return [await asyncio.to_thread(self._create_milestone, uid, milestones[0])]
        return [await asyncio.to_thread(self._create_ticket, uid, tickets[0])]

    async def update(self, ticket, fields: dict) -> None:
        t = self._ticket(ticket)
        await asyncio.to_thread(self._update, t, dict(fields or {}))

    # ── the store calls, the same the routes make ─────────────────────────────
    def _store(self):
        if self.store is None:
            raise RuntimeError("the Tares tickets integration needs the cell's store")
        return self.store

    def _project(self, project) -> str:
        self._store()
        uid = project.get("id") if isinstance(project, dict) else project
        if not uid:
            raise ValueError("a project is required: its dict or its id")
        return str(uid)

    def _ticket(self, ticket) -> dict:
        self._store()
        if not isinstance(ticket, dict) or not ticket.get("id") or not ticket.get("project"):
            raise ValueError("a ticket is the dict the store gives, with its id and project")
        t = self.store.get_ticket(ticket["project"], ticket["id"])
        if t is None:
            raise KeyError(f"project has no ticket {ticket['id']!r}")
        if t["owner"] != "tares":
            raise ValueError(f"{factory_mod.label(t)} lives in {t['owner']}: change it there")
        return t

    def _milestone_id(self, uid: str, ref) -> str | None:
        ref = str(ref or "").strip()
        if not ref:
            return None
        m = self.store.get_milestone(uid, ref)
        if m is None:
            names = [x["name"] for x in self.store.list_milestones(uid)]
            raise KeyError(f"project has no milestone {ref!r}"
                           + ("; one of: " + ", ".join(names) if names else ""))
        return m["id"]

    def _depends_ids(self, uid: str, tid: str | None, refs) -> list[str]:
        ids: list[str] = []
        for ref in factory_mod.depends_ok(refs):
            d = self.store.get_ticket(uid, ref)
            if d is None:
                raise KeyError(f"project has no ticket {ref!r} to depend on")
            if d["id"] == tid:
                raise ValueError("a ticket cannot depend on itself")
            if d["id"] not in ids:
                ids.append(d["id"])
        if tid and ids:
            all_t = self.store.list_tickets(uid)
            deps = {x["id"]: x["depends_on"] for x in all_t}
            deps[tid] = ids
            loop = factory_mod.find_loop(deps, tid)
            if loop:
                by_id = {x["id"]: x for x in all_t}
                raise ValueError("that makes a dependency loop: " + " -> ".join(
                    factory_mod.label(by_id[x]) for x in loop if x in by_id))
        return ids

    def _create_milestone(self, uid: str, item: dict) -> TicketRef:
        """A milestone with the same name is changed, not added twice (the route does the same),
        so a retry finds the one already made."""
        name, goal, checks = factory_mod.milestone_ok(item.get("name"), item.get("goal"),
                                                       item.get("checks"))
        pos = item.get("position")
        cur = self.store.get_milestone(uid, name)
        if cur:
            fields = {k: v for k, v in (("goal", goal), ("checks", checks)) if k in item}
            if pos is not None and cur["owner"] == "tares":
                fields["position"] = float(pos)
            self.store.update_milestone(cur["id"], **fields)
            return TicketRef(cur["id"])
        return TicketRef(self.store.create_milestone(
            uid, name, goal, checks, float(pos) if pos is not None else None))

    def _create_ticket(self, uid: str, item: dict) -> TicketRef:
        """A ticket Tares owns with the same title in the same milestone is the one a retry
        made: it is returned, and no second copy is added."""
        title = docs_mod.title_ok(item.get("title"))
        status = docs_mod.status_ok(item.get("status"))
        milestone = self._milestone_id(uid, item.get("milestone"))
        for t in self.store.list_tickets(uid):
            if t["owner"] == "tares" and t["title"] == title and t["milestone"] == milestone:
                return TicketRef(t["id"], t["url"] or "", t["status"])
        pos = item.get("position")
        working_doc = item.get("working_doc")
        if working_doc and self.store.get_doc(uid, str(working_doc)) is None:
            raise KeyError(f"project has no doc {working_doc!r}; write the working doc first")
        tid = self.store.create_ticket(
            uid, "tares", title, status, float(pos) if pos is not None else None,
            working_doc=str(working_doc) if working_doc else None, milestone=milestone,
            depends_on=self._depends_ids(uid, None, item.get("depends_on")),
            repo=factory_mod.repo_ok(item.get("repo")))
        return TicketRef(tid, "", status)

    def _update(self, t: dict, fields: dict) -> None:
        uid = t["project"]
        out: dict = {}
        if "title" in fields:
            out["title"] = docs_mod.title_ok(fields["title"])
        order = fields.get("order", fields.get("position"))
        if order is not None:
            out["position"] = float(order)
        if "milestone" in fields:
            out["milestone"] = self._milestone_id(uid, fields["milestone"])
        if "depends_on" in fields:
            out["depends_on"] = self._depends_ids(uid, t["id"], fields["depends_on"])
        if out:
            self.store.update_ticket(t["id"], **out)
