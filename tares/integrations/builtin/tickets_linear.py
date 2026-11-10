"""Linear as the tickets integration (TR-845).

`sync` is today's Linear sync (tares/linear.py) behind the integration interface, and `set_status`
moves the Linear issue, so Tares does it and an agent no longer needs Linear's own tools for it.
Creating and changing issues is a later ticket: `create` and `update` say so.

A write Linear refuses, or cannot be reached for, never fails the caller. It leaves a row in
`pending_writeback` (the status wanted, the error, the attempts), which is tried again each time
`sync` runs. The fifth failure puts an item on the desk that names the ticket and the error. While
the row exists the sync leaves the ticket's status as Tares has it, so a move that has not reached
Linear yet is not undone by what Linear still shows.

The integration needs the cell's store: `TicketsLinear(store)`. Without one (the settings page
builds it bare to test the connection) only `check_connection` works.

`project` is a project dict or its id. `ticket` is a ticket dict as the store reads it (it holds
the project), never a bare id, because a ticket is looked up inside its project.
"""
from __future__ import annotations

import asyncio

from .. import tickets
from ..types import Field, Status, TicketRef
from ... import factory as factory_mod
from ... import linear
from ...envelope import now_utc

# Failures of one write before the desk hears of it.
DESK_AFTER = 5

_WORDS = {Status.todo: "to do", Status.in_progress: "in progress", Status.in_review: "in review",
          Status.done: "done"}

# a ticket's Linear issue: its team and the team's workflow states
_STATES = """query($id: String!) {
  issue(id: $id) { id team { id name states { nodes { id name type position } } } }
}"""

_MOVE = """mutation($id: String!, $state: String!) {
  issueUpdate(id: $id, input: {stateId: $state}) { success issue { id state { id name type } } }
}"""


def pick_state(states: list[dict], status: Status) -> dict | None:
    """The workflow state of a Linear team that `status` means, or None when the team has none:
    todo is the first state of type unstarted; in progress is the one named "In Progress", else the
    first of type started; in review is the one named "In Review", else the same as in progress;
    done is the first of type completed. "First" is by Linear's own order (position)."""
    def first(kind: str) -> dict | None:
        of = sorted((s for s in states if str(s.get("type") or "").lower() == kind),
                    key=lambda s: float(s.get("position") or 0))
        return of[0] if of else None

    def named(name: str) -> dict | None:
        return next((s for s in states if str(s.get("name") or "").strip().lower() == name), None)

    if status == Status.todo:
        return first("unstarted")
    if status == Status.in_progress:
        return named("in progress") or first("started")
    if status == Status.in_review:
        return named("in review") or named("in progress") or first("started")
    if status == Status.done:
        return first("completed")
    return None


class TicketsLinear(tickets.TicketBackend):
    name = "linear"
    title = "Linear"
    settings_schema = [
        Field("api_key", "API key", "secret", False,
              "A Linear personal API key (in Linear: Settings, Security and access, Personal API "
              "keys). Left empty, the connection already saved on this cell is used.")]

    def __init__(self, store=None):
        self.store = store

    # ── the connection ────────────────────────────────────────────────────────
    async def check_connection(self, settings: dict) -> tuple[bool, str]:
        """Asks Linear who the key belongs to. With no key in `settings`, the cell's saved
        connection is tried; there is nothing to check against without a store."""
        key = str((settings or {}).get("api_key") or "").strip()
        conn = {"kind": "api_key", "token": key} if key else None
        if conn is None and self.store is None:
            return False, "Paste a Linear API key to test it."
        try:
            who = await linear._account(self.store, conn or linear.get_connection(self.store))
        except linear.LinearError as e:
            return False, str(e)
        except Exception as e:  # noqa: BLE001 - a connection test returns its answer, never raises
            return False, f"Linear could not be tested: {type(e).__name__}"
        name = who.get("name") or who.get("email") or "an account"
        org = f" in {who['org']}" if who.get("org") else ""
        return True, f"Connected to Linear as {name}{org}."

    # ── the interface ─────────────────────────────────────────────────────────
    async def sync(self, project) -> int:
        """Retries the status changes waiting to reach Linear, reads the project's issues into
        its tickets (and the change events onto the timeline, as the polling connector does), and
        returns how many tickets Linear owns in the project now. Raises LinearError when Linear
        cannot be read; the tickets stay as they were."""
        uid = self._project(project)
        await self.retry_pending(uid)
        envelopes = await linear.sync_project(self.store, uid)
        if envelopes:
            await asyncio.to_thread(self.store.append, envelopes)
        rows = await asyncio.to_thread(self.store.list_tickets, uid)
        return sum(1 for t in rows if t["owner"] == "linear")

    async def set_status(self, ticket, status: Status) -> None:
        """Moves the ticket's Linear issue to the state `status` means in its team. A failure is
        kept for a retry, not raised (see the top of this file); a ticket that is not a Linear one
        and an unknown status are mistakes of the caller and still raise."""
        try:
            status = Status(status)
        except ValueError:
            raise ValueError(f"unknown status {status!r}; one of: "
                             + ", ".join(s.value for s in Status)) from None
        t = await asyncio.to_thread(self._ticket, ticket)
        await self._write(t, status)

    async def retry_pending(self, project) -> int:
        """Tries each waiting status change of the project's tickets once more. Returns how many
        landed."""
        uid = self._project(project)
        rows = await asyncio.to_thread(self.store.list_pending_writebacks, self.name, uid)
        landed = 0
        for row in rows:
            t = await asyncio.to_thread(self._find, uid, row["ticket"])
            if t is None:
                await asyncio.to_thread(self.store.pending_writeback_clear, row["ticket"],
                                        self.name)
                continue
            if await self._write(t, Status(row["status"])):
                landed += 1
        return landed

    async def create(self, project, milestones: list, tickets: list) -> list[TicketRef]:
        raise NotImplementedError("creating Linear issues is not built yet")

    async def update(self, ticket, fields: dict) -> None:
        raise NotImplementedError("changing Linear issues is not built yet")

    async def link(self, project, link_fields: dict) -> dict:
        raise NotImplementedError("linking a project to Linear is done from the project page")

    # ── the write ─────────────────────────────────────────────────────────────
    async def _write(self, t: dict, status: Status) -> bool:
        """One try. True when it landed (the ticket's row is gone), False when it did not (the row
        holds the status wanted and the error)."""
        try:
            await self._move(t, status)
        except linear.LinearError as e:
            await asyncio.to_thread(self._failed, t, status, str(e))
            return False
        await asyncio.to_thread(self.store.pending_writeback_clear, t["id"], self.name)
        return True

    async def _move(self, t: dict, status: Status) -> None:
        data = await linear.query(self.store, _STATES, {"id": t["external_id"]})
        issue = data.get("issue")
        if not issue:
            raise linear.LinearError(f"Linear has no issue {factory_mod.label(t)}, or this "
                                     "connection cannot see it")
        states = (((issue.get("team") or {}).get("states") or {}).get("nodes")) or []
        state = pick_state(states, status)
        if state is None:
            team = (issue.get("team") or {}).get("name") or "its team"
            raise linear.LinearError(f"{team} in Linear has no state for {_WORDS[status]}")
        data = await linear.query(self.store, _MOVE, {"id": t["external_id"], "state": state["id"]})
        if not (data.get("issueUpdate") or {}).get("success"):
            raise linear.LinearError(f"Linear did not move {factory_mod.label(t)} to "
                                     f"{state.get('name') or _WORDS[status]}")

    def _failed(self, t: dict, status: Status, error: str) -> None:
        attempts = self.store.pending_writeback_fail(t["id"], self.name, status.value,
                                                     error[:300], now_utc())
        if attempts == DESK_AFTER:       # once: the attempts only grow
            self._desk(t, status, error, attempts)

    def _desk(self, t: dict, status: Status, error: str, attempts: int) -> None:
        """The desk item, made the way the desk route makes one: the same checks on its words,
        addressed to the ticket's assignee when it has one."""
        ref = factory_mod.label(t)
        q, rec, why, opts, ctx, _, _ = factory_mod.desk_ok(
            f"{ref} could not be moved to {_WORDS[status]} in Linear",
            "Fix the Linear connection, or move the issue in Linear yourself. Tares keeps trying "
            "each time it reads Linear, and stops when the issue shows the status.",
            f"Tares tried {attempts} times and Linear refused or could not be reached each time. "
            f"Until it works, Tares keeps {ref} as it is and does not copy Linear's status over it.",
            [], f"Linear said: {error}")
        self.store.add_desk_item(t["project"], q, ctx, opts, rec, why, "", [ref], False, "tares",
                                 [], now_utc(), to_person_id=t.get("assignee_person_id"))

    # ── the ticket and the store ──────────────────────────────────────────────
    def _store(self):
        if self.store is None:
            raise RuntimeError("the Linear tickets integration needs the cell's store")
        return self.store

    def _project(self, project) -> str:
        self._store()
        uid = project.get("id") if isinstance(project, dict) else project
        if not uid:
            raise ValueError("a project is required: its dict or its id")
        return str(uid)

    def _find(self, uid: str, ref: str) -> dict | None:
        t = self.store.get_ticket(uid, ref)
        return t if t and t["owner"] == "linear" and t["external_id"] else None

    def _ticket(self, ticket) -> dict:
        self._store()
        if not isinstance(ticket, dict) or not ticket.get("id") or not ticket.get("project"):
            raise ValueError("a ticket is the dict the store gives, with its id and project")
        t = self.store.get_ticket(ticket["project"], ticket["id"])
        if t is None:
            raise KeyError(f"project has no ticket {ticket['id']!r}")
        if t["owner"] != "linear" or not t["external_id"]:
            raise ValueError(f"{factory_mod.label(t)} does not live in Linear")
        return t
