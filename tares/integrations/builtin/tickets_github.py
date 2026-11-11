"""GitHub Issues as the tickets integration (TR-846).

`sync` is today's GitHub Issues sync (tares/github_tickets.py) behind the integration interface.
On top of it Tares now writes to GitHub itself, through the cell's GitHub credential:
`set_status` closes, reopens and labels the issue, `create` makes a milestone or an issue (with
its "blocked by" links), `update` changes an issue's title, body or milestone. An agent no longer
needs `gh` for any of it.

A status GitHub shows by its own rules: an open issue is to do, an open issue with the label
`in progress` is in progress, a closed issue is done. GitHub has no "in review", so in review
keeps the `in progress` label. Done closes the issue directly (state closed, reason completed);
nothing depends on a `Closes` keyword in a pull request.

A status write GitHub refuses, or cannot be reached for, never fails the caller. It leaves a row
in `pending_writeback` (the status wanted, the error, the attempts), which is tried again each
time `sync` runs. The fifth failure puts an item on the desk that names the ticket and the error.
While the row exists the sync leaves the ticket's status as Tares has it, so a move that has not
reached GitHub yet is not undone by what GitHub still shows.

`create` and `update` raise when GitHub refuses; they are not kept for a retry. A retry of
`create` is safe: an issue or milestone with the same title is found first and returned.

GitHub limits how fast a credential may write. A 403 or 429 that carries `Retry-After` (or says no
requests are left) is waited out, at most `MAX_WAIT` seconds each time, and tried again up to
`RATE_RETRIES` times; then the call fails with a plain message.

The integration needs the cell's store: `TicketsGithub(store)`. Without one (the integrations
screen builds it bare to test the connection) only `check_connection` can say anything useful.

`project` is a project dict or its id. `ticket` is a ticket dict as the store reads it (it holds
the project), never a bare id, because a ticket is looked up inside its project. The project must
be linked to a repo first (its `github` link); the label, milestones and issues go into that repo.
"""
from __future__ import annotations

import asyncio
import logging
import time
from urllib.parse import quote

import httpx

from .. import tickets
from ..types import Field, Status, TicketRef
from ... import factory as factory_mod
from ... import github_tickets as gt
from ...connectors.github import _normalize_repo
from ...envelope import now_utc
from ...github_credentials import API_DEFAULT, credential_name, get_token, resolve_api_url

log = logging.getLogger("tares.integrations.tickets_github")

# Failures of one write before the desk hears of it.
DESK_AFTER = 5
# A rate limit is waited out at most this long (seconds) each time, and tried again this often.
MAX_WAIT = 60
RATE_RETRIES = 3
MAX_PAGES = 30               # 100 per page: 3,000 issues or milestones is far past a factory project
TITLE_MAX = 256              # GitHub cuts issue titles here

_WORDS = {Status.todo: "to do", Status.in_progress: "in progress", Status.in_review: "in review",
          Status.done: "done"}

PROJECT_LABEL_COLOR = "0052cc"
IN_PROGRESS_COLOR = "fbca04"

_BLOCKED_BY = """query($id: ID!) {
  node(id: $id) { ... on Issue { id blockedBy(first: 100) { nodes { id } } } }
}"""

_BLOCK = """mutation($issue: ID!, $blocker: ID!) {
  addBlockedBy(input: {issueId: $issue, blockingIssueId: $blocker}) { clientMutationId }
}"""


async def _sleep(seconds: float) -> None:
    """The wait before a rate limited call is tried again. Tests replace it."""
    await asyncio.sleep(seconds)


def _wait_of(headers) -> float:
    """Seconds to wait after a rate limit answer: `Retry-After`, else until the limit resets,
    else a minute; never more than MAX_WAIT."""
    raw = headers.get("retry-after")
    try:
        if raw is not None:
            return min(float(MAX_WAIT), max(1.0, float(raw)))
    except (TypeError, ValueError):
        return float(MAX_WAIT)
    try:
        return min(float(MAX_WAIT), max(1.0, float(headers.get("x-ratelimit-reset")) - time.time()))
    except (TypeError, ValueError):
        return float(MAX_WAIT)


def _limited(r: httpx.Response) -> bool:
    return r.status_code == 429 or (r.status_code == 403 and (
        r.headers.get("retry-after") is not None or r.headers.get("x-ratelimit-remaining") == "0"))


def _shape(issue: dict) -> dict:
    """A REST issue in the form github_tickets.status_of reads (GraphQL's)."""
    labels = []
    for lab in issue.get("labels") or []:
        labels.append({"name": lab if isinstance(lab, str) else (lab or {}).get("name")})
    reason = str(issue.get("state_reason") or "").upper()
    return {"state": "CLOSED" if issue.get("state") == "closed" else "OPEN",
            "stateReason": reason or None, "labels": {"nodes": labels}}


def _has_label(issue: dict) -> bool:
    return any(str(l.get("name") or "").strip().lower() == gt.IN_PROGRESS
               for l in _shape(issue)["labels"]["nodes"])


def _shows(issue: dict, status: Status) -> bool:
    """True when the issue already shows `status`, so nothing is written: the status
    github_tickets.status_of reads from it is the one wanted. A closed issue shows done whatever
    its reason: it is closed, and a person's "not planned" is not overridden. In review is
    in progress on GitHub."""
    now = gt.status_of(_shape(issue))
    if status == Status.done:
        return now in ("done", "canceled")
    if status == Status.todo:
        return now == "todo"
    return now == "in_progress"


def _number(t: dict) -> int:
    """The issue number of a ticket: GH-12 is issue 12."""
    ident = str(t.get("identifier") or "")
    tail = ident.rsplit("-", 1)[-1] if ident.upper().startswith("GH-") else \
        str(t.get("url") or "").rstrip("/").rsplit("/", 1)[-1]
    if not tail.isdigit():
        raise gt.GitHubTicketsError(f"{factory_mod.label(t)} has no GitHub issue number; "
                                    "sync the project first")
    return int(tail)


class _Ctx:
    """What one run of calls needs: where GitHub is, the token, the repo and the issue label."""
    def __init__(self, api: str, token: str, repo: str, label: str):
        self.api, self.token, self.repo, self.label = api.rstrip("/"), token, repo, label

    @property
    def graphql(self) -> str:
        if self.api == API_DEFAULT:
            return gt.API_URL
        return (self.api[:-len("/api/v3")] + "/api/graphql") if self.api.endswith("/api/v3") \
            else self.api + "/graphql"


class TicketsGithub(tickets.TicketBackend):
    name = "github"
    title = "GitHub Issues"
    settings_schema = [
        Field("repo", "Repository", "text", True,
              "The repository that holds the issues, as owner/name, for example glassflow/tares. "
              "A pasted GitHub address works too."),
        Field("credential", "GitHub credential", "text", False,
              "The name of a GitHub credential stored on this cell. It needs to write issues. "
              "Left empty, every stored credential is tried."),
        Field("issue_label", "Issue label", "text", False,
              "The label every issue of the project carries. Left empty it is tares: followed by "
              "the project's name.")]

    def __init__(self, store=None):
        self.store = store

    # ── the connection ────────────────────────────────────────────────────────
    async def check_connection(self, settings: dict) -> tuple[bool, str]:
        """Asks GitHub about the repo with the credential. It passes only when the credential can
        write to the repo (the `push` permission): a read only credential would sync but every
        write would fail. With no credential named in the connection details, each stored one is
        tried."""
        s = settings or {}
        try:
            repo = _normalize_repo(s.get("repo"))
        except ValueError:
            return False, "Enter the repository as owner/name, for example glassflow/tares."
        if self.store is None:
            return False, "The cell's stored GitHub credentials are needed to test the connection."
        given = credential_name(str(s.get("credential") or "").strip())
        try:
            names = [given] if given else [c["name"] for c in self.store.list_github_credentials()]
        except Exception as e:  # noqa: BLE001 - a connection test returns its answer, never raises
            return False, f"GitHub could not be tested: {type(e).__name__}"
        if not names:
            return False, "No GitHub credential is stored on this cell. Add one first."
        said: list[str] = []
        for name in names:
            try:
                token = await get_token(self.store, name, repo)
                api = resolve_api_url(self.store, name) or API_DEFAULT
                ok, msg = await self._probe(api, token, repo, name)
            except ValueError as e:
                ok, msg = False, str(e)
            except Exception as e:  # noqa: BLE001 - never raises
                ok, msg = False, f"GitHub could not be tested: {type(e).__name__}"
            if ok:
                return True, msg
            said.append(msg)
        return False, " ".join(dict.fromkeys(said))

    async def _probe(self, api: str, token: str, repo: str, name: str) -> tuple[bool, str]:
        try:
            async with httpx.AsyncClient(timeout=15, transport=gt._transport) as cx:
                r = await cx.get(f"{api.rstrip('/')}/repos/{repo}", headers=_headers(token))
        except httpx.HTTPError as e:
            return False, f"Could not reach GitHub: {type(e).__name__}."
        if r.status_code == 401:
            return False, f"GitHub did not accept the credential {name}."
        if r.status_code == 404:
            return False, f"GitHub cannot find {repo}, or the credential {name} cannot see it."
        if _limited(r):
            return False, "GitHub rate limit reached. Try again in a few minutes."
        if r.status_code != 200:
            return False, f"GitHub answered {r.status_code} for {repo}."
        try:
            body = r.json()
        except ValueError:
            return False, "GitHub sent a reply that is not JSON."
        perms = body.get("permissions") if isinstance(body, dict) else None
        if not (isinstance(perms, dict) and perms.get("push")):
            return False, (f"The credential {name} can see {repo} but cannot write issues. Give it "
                           "write access to the repository (a token: repo scope or Issues read and "
                           "write; an App: the Issues permission).")
        return True, f"Connected to {body.get('full_name') or repo} with {name}; it can write issues."

    # ── the interface ─────────────────────────────────────────────────────────
    async def sync(self, project) -> int:
        """Retries the status changes waiting to reach GitHub, reads the project's issues into
        its tickets (and the change events onto the timeline, as the polling connector does), and
        returns how many tickets GitHub owns in the project now. Raises GitHubTicketsError when
        GitHub cannot be read; the tickets stay as they were."""
        uid = self._project(project)
        await self.retry_pending(uid)
        envelopes = await gt.sync_project(self.store, uid)
        if envelopes:
            await asyncio.to_thread(self.store.append, envelopes)
        rows = await asyncio.to_thread(self.store.list_tickets, uid)
        return sum(1 for t in rows if t["owner"] == "github")

    async def set_status(self, ticket, status: Status) -> None:
        """Makes the ticket's GitHub issue show `status` (see the top of this file). A failure is
        kept for a retry, not raised; a ticket that is not a GitHub one and an unknown status are
        mistakes of the caller and still raise."""
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
        """One milestone or one ticket per call. An open milestone with the same title, or an
        issue of the project (open or closed) with the same title, is found and returned instead
        of made again, so a retry gives one. A milestone item holds `name` and `goal`; a ticket
        item holds `title`, `body`, `milestone` (its title in GitHub) and `depends_on` (tickets of
        the project, or GitHub node ids, each becoming a "blocked by" link). The ref's id is the
        issue's (or milestone's) GitHub node id, the `external_id` the sync stores."""
        milestones, tickets = list(milestones or []), list(tickets or [])
        if len(milestones) + len(tickets) != 1:
            raise ValueError("create takes one milestone or one ticket per call")
        uid = self._project(project)
        ctx = await self._ctx(uid)
        if milestones:
            return [await self._create_milestone(ctx, milestones[0])]
        return [await self._create_ticket(ctx, uid, tickets[0])]

    async def update(self, ticket, fields: dict) -> None:
        """Changes the ticket's issue: `title`, `body` and `milestone` (its title in GitHub; empty
        takes the issue out of its milestone). Only the fields given are sent. `order` is ignored
        (GitHub orders issues by number). A milestone GitHub does not have is an error."""
        fields = dict(fields or {})
        fields.pop("order", None)
        if "depends_on" in fields:
            raise ValueError("a GitHub issue's blocked-by links are set in GitHub; Tares reads "
                             "them on the next sync")
        unknown = sorted(set(fields) - {"title", "body", "milestone"})
        if unknown:
            raise ValueError("a GitHub issue can change its title, body and milestone, not: "
                             + ", ".join(unknown))
        t = await asyncio.to_thread(self._ticket, ticket)
        ctx = await self._ctx(t["project"])
        patch: dict = {}
        if "title" in fields:
            patch["title"] = _title(fields["title"])
        if "body" in fields:
            patch["body"] = str(fields["body"] or "")
        if "milestone" in fields:
            ref = fields["milestone"]
            patch["milestone"] = None if ref in (None, "") else await self._milestone_number(ctx, ref)
        if patch:
            await self._rest(ctx, "PATCH", f"/issues/{_number(t)}", body=patch)

    async def link(self, project, link_fields: dict) -> dict:
        """Links the project to the repo `link_fields["repo"]` names (owner/name or a pasted
        address) and the issue label `link_fields["issue_label"]` (default `tares:<project name>`).
        Reads the repo once with the cell's GitHub credentials to check access; takes no key.
        Returns the stored link (repo, label, URL and the credential's name). Raises
        tickets.LinkConflict when the project's tickets live in Linear or are Tares's own, when it
        is linked to another repo or label, or when another project holds this repo and label;
        ValueError for a repo or label that is not valid; GitHubTicketsError when GitHub cannot be
        read. The first sync and the `github_issues` source are the caller's."""
        fields = tickets.check_link_fields(link_fields, ("repo", "issue_label"))
        uid = self._project(project)
        repo = _normalize_repo(fields.get("repo"))
        name = project.get("name") if isinstance(project, dict) else None
        if not name:
            row = await asyncio.to_thread(self.store.get_project, uid)
            name = (row or {}).get("name") or uid
        issue_label = str(fields.get("issue_label") or gt.default_issue_label(name)).strip()
        if not issue_label or len(issue_label) > 50:
            raise ValueError("the issue label must be 1 to 50 characters")
        cur = await asyncio.to_thread(self._link_refusals, uid, repo, issue_label)
        info, _milestones, _issues = await gt.fetch(
            self.store, {**(cur or {}), "repo": repo, "issue_label": issue_label})
        # GitHub was asked in between: look again, and save with no await after the look
        cur = await asyncio.to_thread(self._link_refusals, uid, repo, issue_label)
        await asyncio.to_thread(
            self.store.set_project_github, uid,
            {**(cur or {}), "repo": repo, "issue_label": issue_label,
             "url": f"https://github.com/{repo}", "credential": info["credential"]})
        return await asyncio.to_thread(self.store.get_project_github, uid)

    def _link_refusals(self, uid: str, repo: str, issue_label: str) -> dict | None:
        """The 409s; the project's link as it stands."""
        if self.store.get_project_linear(uid):
            raise tickets.LinkConflict("this project's tickets live in Linear; stop using Linear "
                                       "first")
        if any(t["owner"] == "tares" for t in self.store.list_tickets(uid)):
            raise tickets.LinkConflict("this project already has its own tickets in Tares; a "
                                       "project's tickets live in one place")
        cur = self.store.get_project_github(uid)
        if cur and (cur.get("repo"), cur.get("issue_label")) != (repo, issue_label):
            raise tickets.LinkConflict(
                f"this project is linked to {cur.get('repo')} with issue label "
                f"{cur.get('issue_label')}; unlink it first")
        for other, link in self.store.github_projects():
            if other != uid and (str(link.get("repo")).lower(),
                                 str(link.get("issue_label")).lower()
                                 ) == (repo.lower(), issue_label.lower()):
                owner = self.store.get_project(other)
                raise tickets.LinkConflict(
                    f"{repo} with issue label {issue_label} is already the ticket home of "
                    f"project {owner['name'] if owner else other}")
        return cur

    # ── the status write ──────────────────────────────────────────────────────
    async def _write(self, t: dict, status: Status, retry: bool) -> bool:
        """One try. True when it landed (the ticket's row is gone), False when it did not (the row
        holds the status wanted and the error). `retry` is kept so both ticket integrations share
        one shape; GitHub needs no other rule for a retry."""
        try:
            await self._move(t, status)
        except gt.GitHubTicketsError as e:
            await asyncio.to_thread(self._failed, t, status, str(e))
            return False
        except Exception as e:  # noqa: BLE001 - a write that fails for any reason is kept, never raised
            await asyncio.to_thread(self._failed, t, status,
                                    f"the move failed: {type(e).__name__}")
            return False
        await asyncio.to_thread(self.store.pending_writeback_clear, t["id"], self.name)
        return True

    async def _move(self, t: dict, status: Status) -> None:
        ctx = await self._ctx(t["project"])
        n = _number(t)
        issue = await self._rest(ctx, "GET", f"/issues/{n}", allow=(404,))
        if not issue:
            raise gt.GitHubTicketsError(f"GitHub has no issue {factory_mod.label(t)}, or this "
                                        "credential cannot see it")
        if _shows(issue, status):
            return          # the issue is there already: nothing to write
        if status == Status.done:
            await self._rest(ctx, "PATCH", f"/issues/{n}",
                             body={"state": "closed", "state_reason": "completed"})
            return
        if issue.get("state") == "closed":
            await self._rest(ctx, "PATCH", f"/issues/{n}", body={"state": "open"})
        has = _has_label(issue)
        if status == Status.todo:
            if has:
                await self._rest(ctx, "DELETE", f"/issues/{n}/labels/{quote(gt.IN_PROGRESS, safe='')}",
                                 allow=(404,))
        elif not has:
            await self._ensure_label(ctx, gt.IN_PROGRESS, IN_PROGRESS_COLOR)
            await self._rest(ctx, "POST", f"/issues/{n}/labels", body={"labels": [gt.IN_PROGRESS]})

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
            f"{ref} could not be moved to {_WORDS[status]} in GitHub",
            "Give the GitHub credential write access to the repository's issues, or change the "
            "issue in GitHub yourself. Tares tries again each time this integration syncs, and "
            "stops when the issue shows the status.",
            f"Tares tried {attempts} times and GitHub refused or could not be reached each time. "
            f"Until it works, Tares keeps {ref} as it is and does not copy GitHub's status over it.",
            [], f"GitHub said: {error}")
        to = (t.get("assignee_person_id")
              or (self.store.get_project(t["project"]) or {}).get("lead_person_id"))
        self.store.add_desk_item(t["project"], q, ctx, opts, rec, why, "", [ref], False, "tares",
                                 [], now_utc(), to_person_id=to)

    # ── creating ──────────────────────────────────────────────────────────────
    async def _create_milestone(self, ctx: _Ctx, item: dict) -> TicketRef:
        title = _title((item or {}).get("name"), "a milestone needs a name")
        found = await self._find_milestone(ctx, title)
        if found is None:
            found = await self._rest(ctx, "POST", "/milestones", body={
                "title": title, "description": str(item.get("goal") or "")})
        return TicketRef(str(found.get("node_id") or ""), str(found.get("html_url") or ""), "")

    async def _create_ticket(self, ctx: _Ctx, uid: str, item: dict) -> TicketRef:
        item = item or {}
        title = _title(item.get("title"))
        deps = await asyncio.to_thread(self._dep_ids, uid, item.get("depends_on"))
        await self._ensure_label(ctx, ctx.label, PROJECT_LABEL_COLOR)
        issue = await self._find_issue(ctx, title)
        fresh = issue is None
        if fresh:
            body = {"title": title, "body": str(item.get("body") or item.get("description") or ""),
                    "labels": [ctx.label]}
            ms = item.get("milestone")
            if ms not in (None, ""):
                body["milestone"] = await self._milestone_number(ctx, ms)
            issue = await self._rest(ctx, "POST", "/issues", body=body)
        node = str(issue.get("node_id") or "")
        deps = [d for d in deps if d != node]
        if deps:
            # a retry finds the links of the first try: only the missing ones are added
            have: set[str] = set()
            if not fresh:
                data = await self._graphql(ctx, _BLOCKED_BY, {"id": node})
                if not data.get("node"):
                    raise gt.GitHubTicketsError("GitHub did not return the issue's blocking links")
                have = {str(b.get("id")) for b in
                        (((data["node"].get("blockedBy") or {}).get("nodes")) or [])}
            for dep in deps:
                if dep in have:
                    continue
                try:
                    await self._graphql(ctx, _BLOCK, {"issue": node, "blocker": dep})
                except gt.GitHubTicketsError as e:
                    if "already" not in str(e).lower():     # linked a moment ago: that is the goal
                        raise
        return TicketRef(node, str(issue.get("html_url") or ""), gt.status_of(_shape(issue)))

    def _dep_ids(self, uid: str, refs) -> list[str]:
        """The node ids of what an issue is blocked by. A ref is a ticket of the project (its
        label, id or GH-n), or a GitHub node id; either may come as the ref `create` returned."""
        out: list[str] = []
        for ref in refs or []:
            ref = getattr(ref, "id", None) or (ref.get("id") if isinstance(ref, dict) else ref)
            ref = str(ref or "").strip()
            if not ref:
                continue
            t = self.store.get_ticket(uid, ref)
            node = t["external_id"] if t and t["owner"] == "github" and t["external_id"] else ref
            if node not in out:
                out.append(node)
        return out

    async def _ensure_label(self, ctx: _Ctx, name: str, color: str) -> None:
        """The label is in the repo. An already existing one (also one made a moment ago by
        someone else: GitHub answers 422) counts as there."""
        if await self._rest(ctx, "GET", f"/labels/{quote(name, safe='')}", allow=(404,)):
            return
        await self._rest(ctx, "POST", "/labels", body={"name": name, "color": color},
                         allow=(422,))

    async def _find_issue(self, ctx: _Ctx, title: str) -> dict | None:
        """The oldest issue with the project's label and exactly this title, open or closed."""
        async for batch in self._pages(ctx, "/issues", {"labels": ctx.label, "state": "all",
                                                         "sort": "created", "direction": "asc"}):
            for i in batch:
                if "pull_request" not in i and str(i.get("title") or "").strip() == title:
                    return i
        return None

    async def _find_milestone(self, ctx: _Ctx, title: str) -> dict | None:
        async for batch in self._pages(ctx, "/milestones", {"state": "open"}):
            for m in batch:
                if str(m.get("title") or "").strip() == title:
                    return m
        return None

    async def _milestone_number(self, ctx: _Ctx, ref) -> int:
        if isinstance(ref, int) and not isinstance(ref, bool):
            return ref
        title = str(ref).strip()
        found = await self._find_milestone(ctx, title)
        if found is None:
            raise ValueError(f"GitHub has no open milestone {title!r} in {ctx.repo}; create it first")
        return int(found["number"])

    # ── talking to GitHub ─────────────────────────────────────────────────────
    async def _pages(self, ctx: _Ctx, path: str, params: dict):
        """The pages of a list, 100 at a time, until a page comes back short."""
        for page in range(1, MAX_PAGES + 1):
            batch = await self._rest(ctx, "GET", path, params={**params, "per_page": 100,
                                                                "page": page})
            if not isinstance(batch, list):
                raise gt.GitHubTicketsError("GitHub sent a list that is not a list")
            yield batch
            if len(batch) < 100:
                return
        raise gt.GitHubTicketsError(f"{ctx.repo} has more than {MAX_PAGES * 100} entries to look "
                                    "through")

    async def _rest(self, ctx: _Ctx, method: str, path: str, body=None, params=None, allow=()):
        """One call to the repo's REST API; its JSON. A status in `allow` gives None instead of an
        error. Raises GitHubTicketsError with a message for a person."""
        r = await self._send(ctx, method, f"{ctx.api}/repos/{ctx.repo}{path}", body, params)
        if r.status_code in allow:
            return None
        self._check(r, ctx)
        try:
            return r.json() if r.content else {}
        except ValueError as e:
            raise gt.GitHubTicketsError("GitHub sent a reply that is not JSON") from e

    async def _graphql(self, ctx: _Ctx, query: str, variables: dict) -> dict:
        r = await self._send(ctx, "POST", ctx.graphql, {"query": query, "variables": variables})
        self._check(r, ctx)
        try:
            data = r.json()
        except ValueError as e:
            raise gt.GitHubTicketsError("GitHub sent a reply that is not JSON") from e
        errors = (data or {}).get("errors") or []
        if errors:
            raise gt.GitHubTicketsError("GitHub: " + "; ".join(str(e.get("message")) for e in errors))
        return (data or {}).get("data") or {}

    async def _send(self, ctx: _Ctx, method: str, url: str, body, params=None) -> httpx.Response:
        """One request, waiting out GitHub's rate limit: a 403 or 429 with a wait named is slept
        through (at most MAX_WAIT seconds) and tried again, RATE_RETRIES times at most."""
        for attempt in range(RATE_RETRIES + 1):
            try:
                async with httpx.AsyncClient(timeout=20, transport=gt._transport) as cx:
                    r = await cx.request(method, url, json=body, params=params,
                                         headers=_headers(ctx.token))
            except httpx.HTTPError as e:
                raise gt.GitHubTicketsError(f"cannot reach GitHub: {type(e).__name__}") from e
            if not _limited(r):
                return r
            if attempt == RATE_RETRIES:
                break
            await _sleep(_wait_of(r.headers))
        raise gt.GitHubTicketsError("GitHub rate limit reached; try again in a few minutes",
                                    rate_limited=True)

    @staticmethod
    def _check(r: httpx.Response, ctx: _Ctx) -> None:
        if r.status_code < 400:
            return
        what = {401: "GitHub did not accept the credential",
                403: f"the credential cannot write to {ctx.repo}; give it write access to issues",
                404: f"GitHub cannot find that in {ctx.repo}, or the credential cannot see it",
                }.get(r.status_code)
        if r.status_code == 422:
            try:
                what = "GitHub refused it: " + "; ".join(
                    str(e.get("message") or e.get("code") or "invalid")
                    for e in (r.json().get("errors") or [])) or "GitHub refused it"
            except (ValueError, AttributeError):
                what = "GitHub refused it"
        raise gt.GitHubTicketsError(what or f"GitHub answered {r.status_code}",
                                    kind={401: "unauthorized", 403: "forbidden",
                                          404: "not_found"}.get(r.status_code))

    async def _ctx(self, uid: str) -> _Ctx:
        """The repo, issue label, token and API address of the project's GitHub link. The
        credential the link remembers is used first; without one, the first stored credential that
        gives a token."""
        link, label = await asyncio.to_thread(self._link, uid)
        repo = _normalize_repo(link["repo"])
        name = link.get("credential")
        if name and self.store.get_github_credential(credential_name(name)):
            token = await get_token(self.store, name, repo)
            return _Ctx(resolve_api_url(self.store, name) or API_DEFAULT, token, repo, label)
        for cred_name, token in await gt._candidates(self.store, link, repo):
            return _Ctx(API_DEFAULT, token, repo, label)
        raise gt.GitHubTicketsError(f"no stored GitHub credential can be used for {repo}; "
                                    "add one in the GitHub credentials")

    def _link(self, uid: str) -> tuple[dict, str]:
        project = self.store.get_project(uid)
        if project is None:
            raise KeyError(f"no project {uid!r}")
        link = self.store.get_project_github(uid)
        if not link or not link.get("repo"):
            raise gt.GitHubTicketsError("this project is not linked to a GitHub repository yet")
        return link, str(link.get("issue_label") or gt.default_issue_label(project["name"]))

    # ── the ticket and the store ──────────────────────────────────────────────
    def _store(self):
        if self.store is None:
            raise RuntimeError("the GitHub tickets integration needs the cell's store")
        return self.store

    def _project(self, project) -> str:
        self._store()
        uid = project.get("id") if isinstance(project, dict) else project
        if not uid:
            raise ValueError("a project is required: its dict or its id")
        return str(uid)

    def _find(self, uid: str, ref: str) -> dict | None:
        t = self.store.get_ticket(uid, ref)
        return t if t and t["owner"] == "github" and t["external_id"] else None

    def _ticket(self, ticket) -> dict:
        self._store()
        if not isinstance(ticket, dict) or not ticket.get("id") or not ticket.get("project"):
            raise ValueError("a ticket is the dict the store gives, with its id and project")
        t = self.store.get_ticket(ticket["project"], ticket["id"])
        if t is None:
            raise KeyError(f"project has no ticket {ticket['id']!r}")
        if t["owner"] != "github" or not t["external_id"]:
            raise ValueError(f"{factory_mod.label(t)} does not live in GitHub Issues")
        return t


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28"}


def _title(value, missing: str = "a ticket needs a title") -> str:
    title = " ".join(str(value or "").split())
    if not title:
        raise ValueError(missing)
    if len(title) > TITLE_MAX:
        raise ValueError(f"a title is at most {TITLE_MAX} characters")
    return title
