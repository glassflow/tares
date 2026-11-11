"""Linear as the tickets integration (TR-845, TR-847).

`sync` is today's Linear sync (tares/linear.py) behind the integration interface, and `set_status`
moves the Linear issue, so Tares does it and an agent no longer needs Linear's own tools for it.
`create` makes a milestone or an issue of the project's Linear project (with its "blocks"
relations), `update` changes an issue's title, order or milestone.

A write Linear refuses, or cannot be reached for, never fails the caller. It leaves a row in
`pending_writeback` (the status wanted, the error, the attempts), which is tried again each time
`sync` runs. The fifth failure puts an item on the desk that names the ticket and the error. While
the row exists the sync leaves the ticket's status as Tares has it, so a move that has not reached
Linear yet is not undone by what Linear still shows.

`create` and `update` raise when Linear refuses; they are not kept for a retry. A retry of `create` is
safe: a milestone or issue with the same name or title is found first and returned, and the
relations still missing are added. Linear limits how fast a connection may call it; `linear.query`
waits that out for every call here (see MAX_WAIT and RATE_RETRIES in tares/linear.py).

The integration needs the cell's store: `TicketsLinear(store)`. Without one (the settings page
builds it bare to test the connection) only `check_connection` works.

`project` is a project dict or its id. `ticket` is a ticket dict as the store reads it (it holds
the project), never a bare id, because a ticket is looked up inside its project.
"""
from __future__ import annotations

import asyncio
import logging

from .. import tickets
from ..types import Field, Status, TicketRef
from ... import factory as factory_mod
from ... import linear
from ...envelope import now_utc

log = logging.getLogger("tares.integrations.tickets_linear")

# Failures of one write before the desk hears of it.
DESK_AFTER = 5

_WORDS = {Status.todo: "to do", Status.in_progress: "in progress", Status.in_review: "in review",
          Status.done: "done"}

# a ticket's Linear issue: its team and the team's workflow states
_STATES = """query($id: String!) {
  issue(id: $id) { id state { id name type }
    team { id name states { nodes { id name type position } } } }
}"""

# the team a Linear project belongs to (an issue is made in a team)
_TEAMS = """query($id: String!) {
  project(id: $id) { id teams(first: 10) { nodes { id name } } }
}"""

# the project's issues with exactly this title, any state; the server filters, `_same` checks
_FIND_ISSUE = """query($id: String!, $title: String!, $after: String) {
  project(id: $id) {
    issues(first: 50, after: $after, filter: { title: { eq: $title } }) {
      nodes { id identifier title url sortOrder createdAt state { name type } }
      pageInfo { hasNextPage endCursor }
    }
  }
}"""

# the issues that block this one: a "blocks" relation points at it, the way the sync reads them
_BLOCKERS = """query($id: String!, $after: String) {
  issue(id: $id) {
    id
    inverseRelations(first: 50, after: $after) {
      nodes { type issue { id } }
      pageInfo { hasNextPage endCursor }
    }
  }
}"""

_ISSUE_ID = "query($id: String!) { issue(id: $id) { id } }"

_CREATE_ISSUE = """mutation($input: IssueCreateInput!) {
  issueCreate(input: $input) {
    success
    issue { id identifier title url sortOrder state { name type } }
  }
}"""

_CREATE_MILESTONE = """mutation($input: ProjectMilestoneCreateInput!) {
  projectMilestoneCreate(input: $input) { success projectMilestone { id name description } }
}"""

# issueId blocks relatedIssueId: `blockers_of` reads it back on the blocked issue
_CREATE_RELATION = """mutation($input: IssueRelationCreateInput!) {
  issueRelationCreate(input: $input) { success }
}"""

_UPDATE = """mutation($id: String!, $input: IssueUpdateInput!) {
  issueUpdate(id: $id, input: $input) { success issue { id } }
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
        # a state of that name counts when Linear files it as started: the sync reads a status
        # back from the state's type, so a "In Progress" of another type would read as something else
        return next((s for s in states if str(s.get("name") or "").strip().lower() == name
                     and str(s.get("type") or "").lower() == "started"), None)

    if status == Status.todo:
        return first("unstarted")
    if status == Status.in_progress:
        return named("in progress") or first("started")
    if status == Status.in_review:
        return named("in review") or named("in progress") or first("started")
    if status == Status.done:
        return first("completed")
    return None


def _shows(now: dict, wanted: dict, status: Status, states: list[dict], retry: bool) -> bool:
    """True when the issue's state `now` already is what `status` means, so nothing is written.
    A first write moves the issue unless it sits in the very state picked. A retry (the person may
    have moved the issue by hand since) also accepts another state of the same type, a team's own
    "Doing" for in progress; but never a state that means a different status here: In Review is
    not in progress, though Linear files both as started."""
    if not now:
        return False
    if now.get("id") and now.get("id") == wanted.get("id"):
        return True
    if not retry or status == Status.in_review or not now.get("type"):
        return False
    if str(now["type"]).lower() != str(wanted.get("type") or "").lower():
        return False
    others = (pick_state(states, o) for o in Status if o != status)
    return not any(o and o.get("id") == now.get("id") for o in others)


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
        await self._write(t, status, retry=False)

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
            if await self._write(t, Status(row["status"]), retry=True):
                landed += 1
        return landed

    async def create(self, project, milestones: list, tickets: list) -> list[TicketRef]:
        """One milestone or one ticket per call. A milestone of the Linear project with the same
        name, or an issue of the project (any state, archived ones not looked at) with the same
        title, is found and returned instead of made again, so a retry gives one. A milestone item
        holds `name` and `goal` (its description); a ticket item holds `title`, `body`, `milestone`
        (its name in Linear) and `depends_on` (tickets of the project, refs this method returned,
        or Linear issue ids; each becomes a "blocks" relation from the dependency to the new
        issue). Relations that exist are left alone, and a found issue still gets the ones it
        lacks, so a retry after a failure in between finishes the job.

        "The same" is exact: the name or title as Linear returns it and the one sent are compared
        after cutting the spaces at the start and end of both, and nothing else is changed. Inner
        spaces, case and punctuation count, so "A  B" and "A B" are two titles.

        The ref's id is the Linear id of the issue (or milestone), the `external_id` the sync
        stores. The issue is made in the first team of the Linear project."""
        milestones, tickets = _items(milestones), _items(tickets)
        if len(milestones) + len(tickets) != 1:
            raise ValueError("create takes one milestone or one ticket per call")
        uid = self._project(project)
        if milestones:
            return [await self._create_milestone(uid, milestones[0])]
        return [await self._create_ticket(uid, tickets[0])]

    async def update(self, ticket, fields: dict) -> None:
        """Changes the ticket's issue: `title`, `order` (Linear's sort order, a number) and
        `milestone` (its name in Linear; empty or None takes the issue out of its milestone). Only
        the fields given are sent, in one write. A milestone Linear does not have is an error and
        nothing is sent. Any other field is refused."""
        if fields is not None and not isinstance(fields, dict):
            raise ValueError("fields is a dict of what to change")
        fields = dict(fields or {})
        if "depends_on" in fields:
            raise ValueError("a Linear issue's blocking relations are set in Linear; Tares reads "
                             "them on the next sync")
        unknown = sorted(str(k) for k in set(fields) - {"title", "order", "milestone"})
        if unknown:
            raise ValueError("a Linear issue can change its title, order and milestone, not: "
                             + ", ".join(unknown))
        t = await asyncio.to_thread(self._ticket, ticket)
        patch: dict = {}
        if "title" in fields:
            patch["title"] = _exact(fields["title"], "a ticket needs a title")
        if "order" in fields:
            patch["sortOrder"] = _number(fields["order"])
        if "milestone" in fields:
            name = _exact(fields["milestone"])
            patch["projectMilestoneId"] = None
            if name:
                pid = await asyncio.to_thread(self._linear_id, t["project"])
                patch["projectMilestoneId"] = await self._milestone_id(pid, name)
        if not patch:
            return
        data = await linear.query(self.store, _UPDATE, {"id": t["external_id"], "input": patch})
        if not (data.get("issueUpdate") or {}).get("success"):
            raise linear.LinearError(f"Linear did not change {factory_mod.label(t)}")

    async def link(self, project, link_fields: dict) -> dict:
        """Links the project to the Linear project `link_fields["project"]` names (its id, URL or
        name). Takes no key: the cell's own connection reads Linear. Returns the stored link (its
        id, name and URL, and the state of the last sync). Raises tickets.LinkConflict when the
        project's tickets live in GitHub, or are Tares's own, or it is linked to another Linear
        project already; LinearError when Linear does not have the project. The first sync and the
        `linear` source are the caller's."""
        fields = tickets.check_link_fields(link_fields, ("project",))
        uid = self._project(project)
        await asyncio.to_thread(self._link_refusals, uid)
        found = await linear.find_project(self.store, str(fields.get("project") or ""))
        # Linear was asked in between: look again, and save with no await after the look
        cur = await asyncio.to_thread(self._link_refusals, uid)
        if cur and cur.get("id") != found["id"]:
            raise tickets.LinkConflict(
                f"this project is linked to Linear project {cur.get('name')!r}; unlink it first")
        link = {**(cur or {}), **found}
        await asyncio.to_thread(self.store.set_project_linear, uid, link)
        return await asyncio.to_thread(self.store.get_project_linear, uid)

    def _link_refusals(self, uid: str) -> dict | None:
        """The 409s; the project's link as it stands."""
        if self.store.get_project_github(uid):
            raise tickets.LinkConflict("this project's tickets live in GitHub; stop using GitHub "
                                       "first")
        if any(t["owner"] == "tares" for t in self.store.list_tickets(uid)):
            raise tickets.LinkConflict("this project already has its own tickets in Tares; a "
                                       "project's tickets live in one place")
        return self.store.get_project_linear(uid)

    # ── the write ─────────────────────────────────────────────────────────────
    async def _write(self, t: dict, status: Status, retry: bool) -> bool:
        """One try. True when it landed (the ticket's row is gone), False when it did not (the row
        holds the status wanted and the error)."""
        try:
            await self._move(t, status, retry)
        except linear.LinearError as e:
            await asyncio.to_thread(self._failed, t, status, str(e))
            return False
        except Exception as e:  # noqa: BLE001 - a write that fails for any reason is kept, never raised
            await asyncio.to_thread(self._failed, t, status,
                                    f"the move failed: {type(e).__name__}")
            return False
        await asyncio.to_thread(self.store.pending_writeback_clear, t["id"], self.name)
        return True

    async def _move(self, t: dict, status: Status, retry: bool) -> None:
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
        if _shows(issue.get("state") or {}, state, status, states, retry):
            return          # the issue is there already (on a retry: moved by hand): nothing to write
        data = await linear.query(self.store, _MOVE, {"id": t["external_id"], "state": state["id"]})
        if not (data.get("issueUpdate") or {}).get("success"):
            raise linear.LinearError(f"Linear did not move {factory_mod.label(t)} to "
                                     f"{state.get('name') or _WORDS[status]}")

    def _failed(self, t: dict, status: Status, error: str) -> None:
        error = error[:300]     # what the row keeps and the desk shows (its limit is 1500)
        attempts = self.store.pending_writeback_fail(t["id"], self.name, status.value,
                                                     error, now_utc())
        if attempts == DESK_AFTER:       # once per status wanted
            try:
                self._desk(t, status, error, attempts)
            except Exception:  # noqa: BLE001 - the desk is a courtesy; the write's failure is kept
                log.exception("the desk item for %s could not be added", t.get("id"))

    def _desk(self, t: dict, status: Status, error: str, attempts: int) -> None:
        """The desk item, made the way the desk route makes one: the same checks on its words,
        addressed to the ticket's assignee when it has one."""
        ref = factory_mod.label(t)
        q, rec, why, opts, ctx, _, _ = factory_mod.desk_ok(
            f"{ref} could not be moved to {_WORDS[status]} in Linear",
            "Fix the Linear connection, add the missing state to the team in Linear, or move the "
            "issue in Linear yourself. Tares tries again each time this integration syncs, and "
            "stops when the issue shows the status.",
            f"Tares tried {attempts} times and Linear refused or could not be reached each time. "
            f"Until it works, Tares keeps {ref} as it is and does not copy Linear's status over it.",
            [], f"Linear said: {error}")
        to = (t.get("assignee_person_id")
              or (self.store.get_project(t["project"]) or {}).get("lead_person_id"))
        self.store.add_desk_item(t["project"], q, ctx, opts, rec, why, "", [ref], False, "tares",
                                 [], now_utc(), to_person_id=to)

    # ── creating ──────────────────────────────────────────────────────────────
    async def _create_milestone(self, uid: str, item) -> TicketRef:
        if not isinstance(item, dict):
            raise ValueError("a milestone is a dict with a name and a goal")
        name = _exact(item.get("name"), "a milestone needs a name")
        pid = await asyncio.to_thread(self._linear_id, uid)
        found = await self._find_milestone(pid, name)
        if found is None:
            goal = _exact(item.get("goal"))
            data = await linear.query(self.store, _CREATE_MILESTONE, {"input": {
                "projectId": pid, "name": name, **({"description": goal} if goal else {})}})
            made = data.get("projectMilestoneCreate") or {}
            found = made.get("projectMilestone") if made.get("success") else None
            if not (found or {}).get("id"):
                raise linear.LinearError(f"Linear did not create the milestone {name!r}")
        return TicketRef(str(found["id"]), "", "")

    async def _create_ticket(self, uid: str, item) -> TicketRef:
        if not isinstance(item, dict):
            raise ValueError("a ticket is a dict with a title")
        title = _exact(item.get("title"), "a ticket needs a title")
        pid = await asyncio.to_thread(self._linear_id, uid)
        # everything that can be refused for what the caller sent comes before anything is made
        blockers = await self._blockers(uid, item.get("depends_on"))
        issue = await self._find_issue(pid, title)
        fresh = issue is None
        if fresh:
            body = _exact(item.get("body") or item.get("description"))
            ms = _exact(item.get("milestone"))
            ms_id = await self._milestone_id(pid, ms) if ms else None
            data = await linear.query(self.store, _CREATE_ISSUE, {"input": {
                "teamId": await self._team_id(pid), "projectId": pid, "title": title,
                **({"projectMilestoneId": ms_id} if ms_id else {}),
                **({"description": body} if body else {})}})
            made = data.get("issueCreate") or {}
            issue = made.get("issue") if made.get("success") else None
            if not (issue or {}).get("id"):
                raise linear.LinearError(f"Linear did not create the issue {title!r}")
        iid = str(issue["id"])
        blockers = [b for b in blockers if b != iid]
        if blockers:
            # a retry finds the relations of the first try: only the missing ones are added
            have = set() if fresh else await self._blocked_by(iid)
            for blocker in blockers:
                if blocker in have:
                    continue
                data = await linear.query(self.store, _CREATE_RELATION, {"input": {
                    "issueId": blocker, "relatedIssueId": iid, "type": "blocks"}})
                if not (data.get("issueRelationCreate") or {}).get("success"):
                    raise linear.LinearError(
                        f"Linear did not make {title!r} depend on {blocker}; try again")
        return TicketRef(iid, str(issue.get("url") or ""), linear.status_of(issue.get("state")))

    async def _blockers(self, uid: str, refs) -> list[str]:
        """The Linear ids of what an issue is blocked by. A ref is a ticket of the project (its
        label, id or identifier), or a Linear issue id; either may come as the ref `create`
        returned. A ticket of the project must be one Linear owns. A Linear id Tares has no
        ticket for is asked of Linear first, so a wrong one is refused before anything is made."""
        known, bare = await asyncio.to_thread(self._dep_refs, uid, refs)
        for ref in bare:
            no_issue = (f"Linear has no issue {ref!r} to depend on, or this connection "
                        "cannot see it")
            try:
                data = await linear.query(self.store, _ISSUE_ID, {"id": ref})
            except linear.LinearError as e:
                # Linear answers an id it does not know with "Entity not found"
                if "not found" in str(e).lower():
                    raise linear.LinearError(no_issue) from None
                raise
            found = str((data.get("issue") or {}).get("id") or "")
            if not found:
                raise linear.LinearError(no_issue)
            if found not in known:
                known.append(found)
        return known

    def _dep_refs(self, uid: str, refs) -> tuple[list[str], list[str]]:
        """(Linear ids of the dependencies that are tickets of the project, refs of the rest).
        Empty refs and repeats are dropped."""
        if refs is None:
            return [], []
        if isinstance(refs, (str, dict, TicketRef)):
            refs = [refs]
        elif not isinstance(refs, (list, tuple)):
            raise ValueError("depends_on is a list of tickets")
        known: list[str] = []
        bare: list[str] = []
        for ref in refs:
            if isinstance(ref, (dict, TicketRef)):
                ref = ref.get("id") if isinstance(ref, dict) else ref.id
                if not str(ref or "").strip():
                    raise ValueError("a dependency given as a ticket needs its id")
            ref = str(ref or "").strip()
            if not ref:
                continue
            t = self.store.get_ticket(uid, ref)
            if t is None:
                if ref not in bare:
                    bare.append(ref)
            elif t["owner"] != "linear" or not t["external_id"]:
                raise ValueError(f"{factory_mod.label(t)} does not live in Linear, so it cannot "
                                 "be a dependency of a Linear issue")
            elif t["external_id"] not in known:
                known.append(t["external_id"])
        return known, bare

    async def _find_issue(self, pid: str, title: str) -> dict | None:
        """The oldest issue of the Linear project with exactly this title, in any state. Linear
        filters by title; the title it returns is compared here too (see `create`), and every
        page is read."""
        found: list[dict] = []
        async for nodes in linear.pages(self.store, _FIND_ISSUE, {"id": pid, "title": title},
                                        ("project", "issues"), missing=_GONE):
            found += [i for i in nodes if isinstance(i, dict) and i.get("id")
                      and _same(i.get("title"), title)]
        # oldest first; an issue with no creation time is placed last, the first seen wins a tie
        return min(found, key=lambda i: str(i.get("createdAt") or "\uffff"), default=None)

    async def _find_milestone(self, pid: str, name: str) -> dict | None:
        for m in await linear.fetch_milestones(self.store, pid):
            if isinstance(m, dict) and m.get("id") and _same(m.get("name"), name):
                return m
        return None

    async def _milestone_id(self, pid: str, name: str) -> str:
        found = await self._find_milestone(pid, name)
        if found is None:
            raise linear.LinearError(f"the Linear project has no milestone {name!r}; "
                                     "create it first")
        return str(found["id"])

    async def _blocked_by(self, iid: str) -> set[str]:
        """The ids of the issues that already block this one, every page."""
        have: set[str] = set()
        async for nodes in linear.pages(self.store, _BLOCKERS, {"id": iid},
                                        ("issue", "inverseRelations"),
                                        missing=f"Linear has no issue {iid}, or this connection "
                                                "cannot see it"):
            have.update(linear.blockers_of({"inverseRelations": {"nodes": nodes}}))
        return have

    async def _team_id(self, pid: str) -> str:
        """The team a new issue goes to: the first team of the Linear project (a project may have
        several; this one is not asked which)."""
        data = await linear.query(self.store, _TEAMS, {"id": pid})
        proj = data.get("project")
        if not isinstance(proj, dict):
            raise linear.LinearError(_GONE)
        teams = [t for t in ((proj.get("teams") or {}).get("nodes") or [])
                 if isinstance(t, dict) and t.get("id")]
        if not teams:
            raise linear.LinearError("the Linear project has no team; add a team to the project "
                                     "in Linear")
        return str(teams[0]["id"])

    def _linear_id(self, uid: str) -> str:
        """The id of the Linear project the project is linked to."""
        if self.store.get_project(uid) is None:
            raise KeyError(f"no project {uid!r}")
        link = self.store.get_project_linear(uid)
        if not link or not link.get("id"):
            raise linear.LinearError("this project is not linked to a Linear project yet")
        return str(link["id"])

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


_GONE = "the linked Linear project is gone, or this connection can no longer see it"


def _items(value) -> list:
    """The items of `create`'s milestones or tickets: a list, None as none, a lone dict as one."""
    if value is None:
        return []
    if isinstance(value, dict):
        return [value]
    return list(value)


def _exact(value, missing: str | None = None) -> str:
    """`value` as text with the spaces at both ends cut and nothing else changed. Raises
    ValueError(`missing`) when that is empty and a message is given."""
    text = "" if value is None else str(value).strip()
    if not text and missing:
        raise ValueError(missing)
    return text


def _same(theirs, ours: str) -> bool:
    """A title or name Linear returned against the one sent (already cut): equal after cutting
    the spaces at both ends of Linear's, and nothing else."""
    return _exact(theirs) == ours


def _number(value) -> float:
    """An order for Linear's sortOrder: a finite number (not True or False, not text)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value \
            or value in (float("inf"), float("-inf")):
        raise ValueError("order is a number")
    return float(value)
