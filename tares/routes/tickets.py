"""A project's tickets: the ones Tares owns and the ones that live in Linear or GitHub Issues,
with the link to either and its sync. `ticket_out` and `tickets_out` are the shape every
route returns a ticket in (Deps.ticket_out, Deps.tickets_out)."""
from __future__ import annotations

import asyncio
from datetime import datetime
from functools import partial

from fastapi import APIRouter, Body, HTTPException, Request

from .. import docs as docs_mod
from .. import factory as factory_mod
from .. import factory_api
from .. import github_tickets as _gh_tickets
from .. import linear as _linear
from .. import team as team_mod
from ..deps import Deps
from ..envelope import now_utc
from ..integrations import registry as integrations_registry
from ..integrations import tickets as _tickets_integ


def plan_ctx(deps: Deps, uid: str, listing: bool = False) -> dict:
    """What every ticket's view needs from the rest of the plan: the tickets by id, the
    milestones' names, and which tickets are ready (TR-411, TR-412). `listing`: also every
    working doc's title and each staged ticket's last review, fetched once for the whole
    list instead of per ticket (TR-657)."""
    store = deps.store
    tickets = store.list_tickets(uid)
    ctx = {"by_id": {t["id"]: t for t in tickets},
           "ms": {m["id"]: m["name"] for m in store.list_milestones(uid)},
           "ready": factory_mod.readiness(tickets)}
    if listing:
        ctx["titles"] = store.doc_titles(uid, sorted({t["working_doc"] for t in tickets
                                                      if t["working_doc"]}))
        ctx["last_review"] = store.last_reviews([t["id"] for t in tickets if t["stage"]])
    return ctx


def assignee_view(deps: Deps, t: dict) -> dict:
    """The ticket's `assignee` ({id, name} or None) and `assignee_raw` (set only while nobody
    on the cell is the assignee). A read of what is stored: it shows whatever the licence."""
    person = deps.store.get_person(t["assignee_person_id"]) if t.get("assignee_person_id") else None
    if person:
        return {"assignee": {"id": person["id"], "name": person["name"]}, "assignee_raw": None}
    return {"assignee": None, "assignee_raw": t.get("assignee_raw")}


def ticket_out(deps: Deps, uid: str, t: dict, with_doc: bool = False, ctx: dict | None = None) -> dict:
    store = deps.store
    ctx = ctx or plan_ctx(deps, uid)
    out = {k: t[k] for k in ("id", "owner", "identifier", "url", "title", "status",
                             "position", "working_doc", "updated_at", "number", "milestone",
                             "holder", "stage", "stage_reason", "pr", "repo")}
    out.update(assignee_view(deps, t))   # assignee {id, name} | None, assignee_raw
    out["label"] = factory_mod.label(t)
    out["pr_note"] = factory_api.pr_note(t)
    out["milestone_name"] = ctx["ms"].get(t["milestone"]) if t["milestone"] else None
    out["depends_on"] = [factory_mod.label(ctx["by_id"][d]) for d in t["depends_on"]
                         if d in ctx["by_id"]]
    r = ctx["ready"].get(t["id"]) or {"ready": False, "blocked_by": []}
    out["ready"], out["blocked_by"] = r["ready"], r["blocked_by"]
    if "titles" in ctx:
        out["working_doc_title"] = ctx["titles"].get(t["working_doc"])
    else:
        doc = store.get_doc(uid, t["working_doc"]) if t["working_doc"] else None
        out["working_doc_title"] = doc["title"] if doc else None
    if with_doc:
        out["working_doc_body"] = doc["body"] if doc else None
        out["history"] = store.ticket_history(t["id"])
        by_id = ctx["by_id"]
        out["assumptions"] = [
            {**{k: a[k] for k in ("label", "question", "choice", "why", "state", "words",
                                  "made_by", "person", "by_label")},
             "tickets": [factory_mod.label(by_id[x]) for x in a["tickets"] if x in by_id]}
            for a in store.list_assumptions(uid, ticket=t["id"])]
        out.update(factory_api.review_state(deps, uid, t, deps.cc_source()))
        lab = out["label"]
        ds = store.list_decisions(uid, all_=True)
        out["decisions"] = ([d for d in ds if lab in d["tickets"]]
                            + [d for d in ds if lab not in d["tickets"]])[:10]
    elif "last_review" in ctx:
        out["verdict"] = factory_api.verdict_now(t, ctx["last_review"].get(t["id"]))
    else:
        rounds = store.ticket_reviews(t["id"]) if t["stage"] else []
        out["verdict"] = factory_api.verdict_now(t, factory_mod.standing_round(rounds))
    return out


def tickets_out(deps: Deps, uid: str) -> list[dict]:
    ctx = plan_ctx(deps, uid, listing=True)
    return [ticket_out(deps, uid, t, ctx=ctx) for t in ctx["by_id"].values()]


def home_title(owner: str) -> str:
    """Where a ticket or milestone Tares does not own lives, in a word: Linear, GitHub."""
    cls = integrations_registry.get("tickets", owner)
    return (getattr(cls, "title", None) or owner).split()[0]


def linear_err(deps: Deps, e: Exception):
    deps.err(e, 502 if "reach" in str(e) or "answered" in str(e) else 400)


async def linear_sync_now(deps: Deps, uid: str) -> None:
    # the same lock the sync runner holds, so the two never write one project's tickets at once
    from ..integrations.sync import project_lock
    async with project_lock("linear", uid):
        await _linear.sync_project(deps.store, uid)   # its change events are not stored (TR-1806)


def gh_err(deps: Deps, e: Exception):
    if getattr(e, "rate_limited", False):
        deps.err(e, 429)
    deps.err(e, 502 if "reach" in str(e) or "answered" in str(e) else 400)


async def github_sync_now(deps: Deps, uid: str) -> None:
    # the same lock the sync runner holds, so the two never write one project's tickets at once
    from ..integrations.sync import project_lock
    async with project_lock("github", uid):
        await _gh_tickets.sync_project(deps.store, uid)   # its change events are not stored (TR-1806)


def router(deps: Deps) -> APIRouter:
    r = APIRouter()
    store = deps.store
    _err = deps.err
    _current_person = deps.current_person
    _is_system = deps.is_system
    _project_or_404 = deps.project_or_404
    _ticket_out = deps.ticket_out
    _tickets_out = deps.tickets_out
    _home_title = home_title
    _linear_err = partial(linear_err, deps)
    _gh_err = partial(gh_err, deps)
    _linear_sync_now = partial(linear_sync_now, deps)
    _github_sync_now = partial(github_sync_now, deps)

    def _milestone_ref(uid: str, ref) -> str | None:
        """The id of the milestone `ref` names (id or name), None for "" or None; 404 otherwise."""
        ref = str(ref or "").strip()
        if not ref:
            return None
        m = store.get_milestone(uid, ref)
        if m is None:
            names = [x["name"] for x in store.list_milestones(uid)]
            _err(KeyError(f"project has no milestone {ref!r}"
                          + ("; one of: " + ", ".join(names) if names
                             else "; write it first with write_milestone")), 404)
        return m["id"]

    def _depends_ref(uid: str, tid: str | None, refs) -> list[str]:
        """The ids of the tickets `refs` name, refusing an unknown ticket, the ticket itself and
        a dependency loop."""
        try:
            refs = factory_mod.depends_ok(refs)
        except docs_mod.DocError as e:
            _err(e)
        ids: list[str] = []
        for ref in refs:
            d = store.get_ticket(uid, ref)
            if d is None:
                _err(KeyError(f"project has no ticket {ref!r} to depend on"), 404)
            if d["id"] == tid:
                _err(ValueError("a ticket cannot depend on itself"))
            if d["id"] not in ids:
                ids.append(d["id"])
        if tid and ids:
            deps = {t["id"]: t["depends_on"] for t in store.list_tickets(uid)}
            deps[tid] = ids
            loop = factory_mod.find_loop(deps, tid)
            if loop:
                by_id = {t["id"]: t for t in store.list_tickets(uid)}
                _err(ValueError("that makes a dependency loop: " + " -> ".join(
                    factory_mod.label(by_id[x]) for x in loop if x in by_id)), 409)
        return ids

    def _repo_ok(v) -> str | None:
        try:
            return factory_mod.repo_ok(v)
        except docs_mod.DocError as e:
            _err(e)

    def _assignee_ref(v) -> tuple[str | None, str | None]:
        """(person id, raw) for the `assignee` a caller sent; 404 for text that is neither a
        person id nor an email."""
        try:
            return team_mod.get().assignee_resolve(store, v)
        except ValueError as e:
            _err(KeyError(str(e)), 404)

    def _assign_join(request, project: dict, prev: str | None, who: str | None) -> bool:
        """May this caller give the ticket `who` as its assignee, and does that add them to the
        project? Only a change joins anyone, and on a private project only the lead or the system
        credential may bring in someone who is not on it (403 otherwise, like the members route);
        any member may assign a person already on it."""
        if (not who or who == prev or project.get("scope") != "private"
                or who == project.get("lead_person_id")
                or store.is_project_member(project["id"], who)):
            return False
        me = _current_person(request)
        if not _is_system(request) and not (me and me["id"] == project.get("lead_person_id")):
            raise HTTPException(status_code=403, detail=(
                "that person is not on this private project: assign someone who is, or ask the "
                "project lead to add them"))
        return True

    def _added_by(request) -> str:
        """Who a project member added by assigning a ticket is recorded as: the person behind the
        credential, from the credential alone."""
        by = _current_person(request)
        return by["id"] if by else "system"

    def _ticket_or_404(uid: str, ref: str) -> dict:
        t = store.get_ticket(uid, ref)
        if t is None:
            _err(KeyError(f"project has no ticket {ref!r}"), 404)
        return t

    def _working_doc_ok(uid: str, doc_id) -> str | None:
        if not doc_id:
            return None
        if store.get_doc(uid, str(doc_id)) is None:
            _err(KeyError(f"project has no doc {doc_id!r}; write the working doc first"), 404)
        return str(doc_id)

    @r.get("/api/projects/{uid}/tickets")
    async def list_project_tickets(uid: str):
        """The project's tickets in order, each with the title of its working doc, and the
        Linear project they live in when the project uses Linear (`linear`, with the last
        sync's time and error), or the GitHub repo and issue label when it uses GitHub Issues
        (`github`, the same)."""
        await asyncio.to_thread(_project_or_404, uid)
        # no GitHub call (TR-1474): the pull request state kept so far, which the background sync
        # and a ticket's own read keep current; the store work runs in a thread, so other
        # requests are served meanwhile
        return await asyncio.to_thread(lambda: {
            "tickets": _tickets_out(uid), "linear": store.get_project_linear(uid),
            "github": store.get_project_github(uid)})

    @r.post("/api/projects/{uid}/tickets", status_code=201)
    async def create_project_ticket(uid: str, request: Request, body: dict = Body(...)):
        """{title, status?, position?, working_doc?, milestone?, depends_on?, repo?, assignee?}: a
        ticket Tares owns. `repo` is the GitHub repo (owner/name) it changes, for a project that
        spans several. `assignee` is a person id or an email (an email nobody on the cell has is
        kept as written, and the ticket shows as unowned). When the project's tickets live in
        Linear or GitHub the ticket is created there, through the tickets integration, and Tares
        keeps the one row for it (TR-851): it takes title, body, milestone (a milestone already
        written), depends_on (tickets already written), working_doc and repo; its status, assignee
        and position come from Linear or GitHub (409). A repeat of the same title finds the issue
        again."""
        project = await asyncio.to_thread(_project_or_404, uid)
        home = await asyncio.to_thread(store.project_ticket_home, uid)
        if home:
            return await _create_foreign_ticket(uid, home, body)
        return await asyncio.to_thread(_create_tares_ticket, uid, project, request, body)

    def _create_tares_ticket(uid: str, project: dict, request: Request, body: dict) -> dict:
        try:
            title = docs_mod.title_ok(body.get("title"))
            status = docs_mod.status_ok(body.get("status"))
        except docs_mod.DocError as e:
            _err(e)
        who, raw = _assignee_ref(body.get("assignee"))
        joins = _assign_join(request, project, None, who)
        pos = body.get("position")
        milestone = _milestone_ref(uid, body.get("milestone"))
        depends = _depends_ref(uid, None, body.get("depends_on"))
        tid = store.create_ticket(uid, "tares", title, status,
                                  float(pos) if pos is not None else None,
                                  working_doc=_working_doc_ok(uid, body.get("working_doc")),
                                  milestone=milestone, depends_on=depends,
                                  repo=_repo_ok(body.get("repo")),
                                  assignee_person_id=who, assignee_raw=raw)
        if joins:
            team_mod.get().assignee_join(store, project, who, _added_by(request))
        return _ticket_out(uid, _ticket_or_404(uid, tid))

    def _foreign_ticket_plan(uid: str, home: str, body: dict):
        """What creating a ticket in Linear or GitHub needs, checked before anything is made:
        (the integration class, the item for its `create`, the milestone's Tares id, the
        dependencies' Tares ids, repo, working doc). Every 4xx is raised here."""
        held = sorted({"status", "assignee", "position"} & set(body))
        if held:
            _err(ValueError(f"this project's tickets live in {_home_title(home)}: a ticket's "
                            + ", ".join(held) + " changes in " + _home_title(home)
                            + ", not when it is created"), 409)
        cls = integrations_registry.get("tickets", home)
        if cls is None:
            _err(ValueError(f"this project's tickets live in {_home_title(home)}; add the ticket "
                            "there and it shows up here on the next sync"), 409)
        try:
            title = docs_mod.title_ok(body.get("title"))
        except docs_mod.DocError as e:
            _err(e)
        mid = _milestone_ref(uid, body.get("milestone"))
        m = store.get_milestone(uid, mid) if mid else None
        deps = _depends_ref(uid, None, body.get("depends_on"))
        item = {"title": title, "body": str(body.get("body") or ""),
                "milestone": m["name"] if m else "", "depends_on": deps}
        return cls, item, mid, deps, _repo_ok(body.get("repo")), \
            _working_doc_ok(uid, body.get("working_doc"))

    async def _create_foreign_ticket(uid: str, home: str, body: dict) -> dict:
        cls, item, mid, deps, repo, doc = await asyncio.to_thread(_foreign_ticket_plan, uid,
                                                                  home, body)
        try:
            refs = await cls(store).create(uid, [], [item])
        except (_linear.LinearError, _gh_tickets.GitHubTicketsError) as e:
            (_linear_err if home == "linear" else _gh_err)(e)
        except (ValueError, KeyError) as e:
            _err(e)
        if len(refs) != 1:
            _err(ValueError(f"{_home_title(home)} did not answer with the ticket"), 502)
        ref = refs[0]

        def _adopt() -> dict:
            tid = store.adopt_ticket(uid, home, ref.id, item["title"], ref.status or "todo",
                                     ref.identifier, ref.url, milestone=mid, depends_on=deps,
                                     repo=repo, working_doc=doc)
            return _ticket_out(uid, _ticket_or_404(uid, tid))
        return await asyncio.to_thread(_adopt)

    @r.get("/api/projects/{uid}/tickets/{ref}")
    async def get_project_ticket(uid: str, ref: str):
        """One ticket by its id, number or Linear identifier, with its working doc's body and
        its history (who moved it, when)."""
        _project_or_404(uid)
        t = _ticket_or_404(uid, ref)
        if await factory_api.refresh_pr(store, uid, t):   # its own pull request only (TR-1474)
            t = _ticket_or_404(uid, ref)
        return _ticket_out(uid, t, with_doc=True)

    @r.put("/api/projects/{uid}/tickets/link")
    async def link_project_tickets(uid: str, body: dict = Body(...)):
        """{integration, link_fields?}: link the project's tickets to Linear or GitHub Issues
        through that integration's `link`, then read them once. `link_fields` are the non-secret
        fields the integration names: `project` for Linear; `repo` and `issue_label` for GitHub.
        A body that carries a key or a token is refused (400): the cell's own credentials read the
        system. 409 when the project's tickets already live somewhere else. Answers
        {integration, tickets, linear | github}, the link with the state of its first sync (a
        sync that failed is on the link; the sync button tries again). DELETE /api/projects/{uid}/linear (or /github) unlinks."""
        project = await asyncio.to_thread(_project_or_404, uid)
        name = body.get("integration")
        if not isinstance(name, str) or name not in ("linear", "github"):
            _err(ValueError("integration is 'linear' or 'github'"))
        extra = sorted(str(k) for k in set(body) - {"integration", "link_fields"})
        if extra:
            _err(ValueError("this takes integration and link_fields, not: " + ", ".join(extra)))
        cls = integrations_registry.get("tickets", name)
        if cls is None:
            _err(KeyError(f"no tickets integration {name!r}"), 404)
        try:
            link = await cls(store).link(project, body.get("link_fields") or {})
        except _tickets_integ.LinkConflict as e:
            _err(e, 409)
        except _linear.LinearError as e:
            _linear_err(e)
        except _gh_tickets.GitHubTicketsError as e:
            _err(e, 429 if e.rate_limited else 400)
        except (ValueError, KeyError) as e:
            _err(e)
        # no source is made: the sync runner keeps the tickets in step (TR-1806)
        try:
            await (_linear_sync_now if name == "linear" else _github_sync_now)(uid)
        except (_linear.LinearError, _gh_tickets.GitHubTicketsError):
            pass   # recorded on the link; the answer shows it
        return await asyncio.to_thread(lambda: {
            "integration": name, "tickets": _tickets_out(uid),
            name: store.get_project_linear(uid) if name == "linear" else store.get_project_github(uid)})

    _TICKET_EDIT_FIELDS = {"title", "status", "position", "milestone", "depends_on", "assignee",
                           "working_doc", "repo"}

    @r.put("/api/projects/{uid}/tickets/{ref}")
    async def update_project_ticket(uid: str, ref: str, request: Request,
                                    body: dict = Body(...)):
        """Change a ticket. A Tares ticket takes title, status, position, assignee, milestone,
        depends_on, working_doc and repo. A Linear or GitHub ticket takes title, position (its
        order) and milestone, written to where it lives through the tickets integration (a slow
        write carries on in the background and the answer says `edit_pending`), plus working_doc
        and repo; its status, assignee and blocking relations change where it lives (409). Nothing
        is saved when Linear or GitHub refuses the edit. `assignee` is a person id or an email; ""
        clears it. Changing it leaves the holder and stage alone."""
        project = await asyncio.to_thread(_project_or_404, uid)
        t = await asyncio.to_thread(_ticket_or_404, uid, ref)
        unknown = sorted(str(k) for k in set(body) - _TICKET_EDIT_FIELDS)
        if unknown:
            _err(ValueError("a ticket takes " + ", ".join(sorted(_TICKET_EDIT_FIELDS))
                            + ", not: " + ", ".join(unknown)))
        foreign = t["owner"] != "tares"
        if foreign:
            # GitHub orders issues by number, so only Linear has an order to write
            home_only = {"status", "assignee", "depends_on"}
            if t["owner"] == "github":
                home_only.add("position")
            held = sorted(home_only & set(body))
            if held:
                _err(ValueError(f"{t['identifier'] or t['id']} lives in {_home_title(t['owner'])}: "
                                "change its " + ", ".join(held) + " there"), 409)
        fields, remote, mirror, joins = await asyncio.to_thread(
            _ticket_edit_plan, uid, t, project, request, body, foreign)
        pending = False
        if remote:
            try:
                landed = await factory_api.edit_ticket(store, t, t["owner"], remote, mirror)
            except (_linear.LinearError, _gh_tickets.GitHubTicketsError) as e:
                (_linear_err if t["owner"] == "linear" else _gh_err)(e)
            except (ValueError, KeyError) as e:
                _err(e)
            pending = not landed

        def _save() -> dict:
            if fields:
                why = (f"marked {fields['status'].replace('_', ' ')} in Tares"
                       if "status" in fields else None)
                store.update_ticket(t["id"], why=why, **fields)
            if joins:
                team_mod.get().assignee_join(store, project, fields["assignee_person_id"],
                                             _added_by(request))
            return _ticket_out(uid, _ticket_or_404(uid, t["id"]), with_doc=True)
        out = await asyncio.to_thread(_save)
        if pending:
            out["edit_pending"] = True
        return out

    def _ticket_edit_plan(uid: str, t: dict, project: dict, request, body: dict, foreign: bool):
        """What a ticket edit changes: (columns saved in Tares now, fields for the integration's
        `update`, the columns that mirror them once it lands, whether the assignee joins the
        project). Every 4xx is raised here, before anything is written."""
        fields: dict = {}
        remote: dict = {}
        mirror: dict = {}
        joins = False
        if "working_doc" in body:
            fields["working_doc"] = _working_doc_ok(uid, body.get("working_doc"))
        if "repo" in body:
            fields["repo"] = _repo_ok(body.get("repo"))
        try:
            if "title" in body:
                title = docs_mod.title_ok(body["title"])
                (remote if foreign else fields)["title"] = title
                if foreign:
                    mirror["title"] = title
            if "status" in body:
                fields["status"] = docs_mod.status_ok(body["status"])
        except docs_mod.DocError as e:
            _err(e)
        if "position" in body and body["position"] is not None:
            try:
                pos = float(body["position"])
            except (TypeError, ValueError):
                _err(ValueError("position is a number"))
            if foreign:
                remote["order"] = pos
                mirror["position"] = pos
            else:
                fields["position"] = pos
        if "milestone" in body:
            mid = _milestone_ref(uid, body.get("milestone"))
            if foreign:
                m = store.get_milestone(uid, mid) if mid else None
                if mid and m is None:
                    _err(KeyError(f"project has no milestone {body.get('milestone')!r}"), 404)
                remote["milestone"] = m["name"] if m else None
                mirror["milestone"] = mid
            else:
                fields["milestone"] = mid
        if "depends_on" in body:
            fields["depends_on"] = _depends_ref(uid, t["id"], body.get("depends_on"))
        if "assignee" in body:
            who, raw = _assignee_ref(body.get("assignee"))
            joins = _assign_join(request, project, t.get("assignee_person_id"), who)
            fields.update(assignee_person_id=who, assignee_raw=raw)
        return fields, remote, mirror, joins

    @r.delete("/api/projects/{uid}/tickets/{ref}")
    def delete_project_ticket(uid: str, ref: str):
        """Delete a Tares ticket (its working doc stays). A Linear or GitHub ticket is removed
        where it lives."""
        _project_or_404(uid)
        t = _ticket_or_404(uid, ref)
        if t["owner"] != "tares":
            _err(ValueError(f"{t['identifier'] or t['id']} lives in {_home_title(t['owner'])}: "
                            "remove it there"), 409)
        store.delete_ticket(t["id"])
        return {"ok": True, "deleted": t["id"]}

    @r.post("/api/projects/{uid}/linear/sync")
    async def sync_linear_project(uid: str):
        """Sync the project's tickets with Linear now."""
        _project_or_404(uid)
        if not store.get_project_linear(uid):
            _err(ValueError("this project does not use Linear"), 409)
        try:
            await _linear_sync_now(uid)
        except _linear.LinearError as e:
            _linear_err(e)
        return {"tickets": _tickets_out(uid),
                "linear": store.get_project_linear(uid)}

    @r.delete("/api/projects/{uid}/linear")
    def unlink_linear(uid: str):
        """The project stops using Linear. Its tickets stay, now owned by Tares, with their
        working docs."""
        _project_or_404(uid)
        store.set_project_linear(uid, None)
        for t in store.list_tickets(uid):
            if t["owner"] == "linear":
                store.update_ticket_owner(t["id"], "tares")
        for m in store.list_milestones(uid):
            if m["owner"] == "linear":
                store.update_milestone(m["id"], owner="tares", external_id=None)
        return {"tickets": _tickets_out(uid),
                "linear": None}

    # ── GitHub Issues (TR-569): the repo + issue label a project's tickets live in. Tares only
    # reads from GitHub; sessions write with the `gh` CLI ────────────────────────────────────
    @r.post("/api/projects/{uid}/github/sync")
    async def sync_github_issues(uid: str):
        """Sync the project's tickets with GitHub now."""
        _project_or_404(uid)
        link = store.get_project_github(uid)
        if not link:
            _err(ValueError("this project does not use GitHub Issues"), 409)
        if _gh_tickets._backing_off(link):
            # a sync now would be skipped, so say so instead of answering 200 with old tickets
            wait = (datetime.fromisoformat(link["backoff_until"]) - now_utc()).total_seconds()
            raise HTTPException(status_code=429, detail="GitHub rate limit reached",
                                headers={"Retry-After": str(max(1, int(wait) + 1))})
        try:
            await _github_sync_now(uid)
        except _gh_tickets.GitHubTicketsError as e:
            _gh_err(e)
        return {"tickets": _tickets_out(uid), "github": store.get_project_github(uid)}

    @r.delete("/api/projects/{uid}/github")
    def unlink_github(uid: str):
        """The project stops using GitHub Issues. Its tickets stay, now owned by Tares, with their
        working docs."""
        _project_or_404(uid)
        store.set_project_github(uid, None)
        for t in store.list_tickets(uid):
            if t["owner"] == "github":
                store.update_ticket_owner(t["id"], "tares")
        for m in store.list_milestones(uid):
            if m["owner"] == "github":
                store.update_milestone(m["id"], owner="tares", external_id=None)
        return {"tickets": _tickets_out(uid), "github": None}

    return r
