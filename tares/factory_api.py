"""HTTP routes for the software factory's crew (P-TR-176, M3 onward), registered by the daemon.

One always-on crew (orchestrator, reviewer, releaser) serves every project; builders are started
per project. Tares runs none of it: it keeps the crew's settings and grants on the cell, groups
the Claude Code sessions it receives into stations, and records when a project was handed to the
crew. Who is calling comes from the X-Tares-Station / -Role / -Parent headers the MCP proxy sets
from the station's environment (tares/factory.py `may`).
"""
from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime
from types import SimpleNamespace

from fastapi import Body, Request

from . import docs as docs_mod
from . import factory as factory_mod
from . import factory_github
from .envelope import now_utc

CREW_SETTING = "crew_settings"
GRANTS_SETTING = "global_grants"   # the id of the cell's "Grants (all projects)" doc
QUIET_MIN = 10   # a working station that sent nothing for this long shows as quiet
# a waiting station silent this long is most likely gone: Claude Code retires idle background
# sessions after about an hour, and a killed one never says so
WAITING_QUIET_MIN = 90


def caller(request: Request) -> dict:
    """{station, role, parent} the request says it comes from ("" each when unlabeled)."""
    h = request.headers
    return {"station": (h.get("x-tares-station") or "").strip()[:factory_mod.MAX_NAME],
            "role": (h.get("x-tares-role") or "").strip().lower(),
            "parent": (h.get("x-tares-parent") or "").strip()[:factory_mod.MAX_NAME]}


_REFRESHING: dict[str, asyncio.Task] = {}
REFRESH_WAIT = 2.5     # seconds a ticket read waits for GitHub before serving what it has
_READS_AT_ONCE = 4


async def refresh_prs(store, uid: str) -> None:
    """Bring a project's pull requests up to date before its tickets are read (TR-410), without
    making the read wait on GitHub: one refresh per project at a time, shared by every read that
    asks meanwhile; a read waits at most REFRESH_WAIT seconds, then gets the cached state while
    the refresh finishes in the background."""
    task = _REFRESHING.get(uid)
    if task is None or task.done():
        task = asyncio.create_task(_refresh(store, uid))
        _REFRESHING[uid] = task
    try:
        await asyncio.wait_for(asyncio.shield(task), REFRESH_WAIT)
    except asyncio.TimeoutError:
        pass                     # the refresh carries on; this read serves what is cached
    except Exception as e:       # a refresh must never fail a read, but say why it did not run
        print(f"[tares] pull request refresh for {uid} failed: {type(e).__name__}: {e}",
              flush=True)


def merged_fields(t: dict, pr: dict) -> dict:
    """What a merge seen on GitHub changes on the ticket (nothing when it already says so)."""
    if not pr.get("merged") or t["stage"] in ("merged", "shipped"):
        return {}
    out = {"stage": "merged"}
    if t["owner"] != "linear":
        out["status"] = "done"
    return out


async def _refresh(store, uid: str) -> None:
    """A linked PR read again when its last read is stale; a ticket in the work with no PR linked
    by a `factory/<ref>` branch in the project's repos, only when no other project has a ticket
    in the work with that ref in that repo; a merge seen on GitHub marks the ticket merged even
    when the builder did not."""
    tickets = store.list_tickets(uid)
    repos = {t["pr"]["repo"] for t in tickets if t["pr"] and t["pr"].get("repo")}
    if repos:
        seen = store.factory_branch_prs(repos)
        for t in tickets:
            if t["pr"] or t["stage"] not in ("doing", "review", "changes", "blocked"):
                continue
            lab = factory_mod.label(t).upper()
            hit = next(((r, n) for (r, ref), (_, n) in seen.items() if ref == lab), None)
            if hit and not store.factory_ref_elsewhere(uid, hit[0], factory_mod.label(t)):
                t["pr"] = {"repo": hit[0], "number": hit[1], "checked_at": None,
                           "linked_by": "branch"}
                store.update_ticket(t["id"], pr=t["pr"])
    sem = asyncio.Semaphore(_READS_AT_ONCE)

    async def one(t: dict) -> None:
        pr = t["pr"]
        if factory_github.stale(pr):
            async with sem:
                pr = await factory_github.read_pr(store, pr["repo"], int(pr["number"]), pr)
        cur = store.get_ticket(uid, t["id"])   # re-read: a set_stage may have run meanwhile
        if cur is None or not cur["pr"] or (cur["pr"].get("repo"), cur["pr"].get("number")) != (
                pr.get("repo"), pr.get("number")):
            return
        fields: dict = {"pr": pr} if pr is not t["pr"] else {}
        fields.update(merged_fields(cur, pr))
        # new commits after the reviewer asked for changes: it is back in review (TR-444)
        last = store.ticket_reviews(cur["id"])[-1:]
        if (not fields.get("stage") and cur["stage"] == "changes" and last and pr.get("head")
                and not verdict_now({"pr": pr}, last[0])["current"]):
            fields["stage"] = "review"
            store.add_ticket_history(cur["id"], uid, "stage", "review",
                                     "new commits pushed after the verdict", "github", "github",
                                     now_utc())
        if fields:
            store.update_ticket(cur["id"], **fields)
        if fields.get("stage") == "merged":
            store.add_ticket_history(cur["id"], uid, "stage", "merged",
                                     "GitHub shows the pull request merged", "github", "github",
                                     now_utc())

    await asyncio.gather(*(one(t) for t in tickets if t["pr"] and t["pr"].get("repo")),
                         return_exceptions=True)


def verdict_now(t: dict, last: dict | None) -> dict | None:
    """The reviewer's last verdict on the ticket and whether it still counts (TR-444): it was
    given on a commit; once the pull request's head moves on, it is for an older commit."""
    if not last:
        return None
    head = ((t.get("pr") or {}).get("head") or "").lower()
    mine = (last["head"] or "").lower()
    current = not head or head.startswith(mine) or mine.startswith(head)
    return {"verdict": last["verdict"], "head": last["head"], "round": last["round"],
            "at": last["at"], "current": bool(current),
            "note": None if current else
            f"verdict is for an older commit ({last['head'][:7]}); the PR is now at {head[:7]}"}


def review_state(store, uid: str, t: dict, source: str) -> dict:
    """What a ticket's full view adds for review (TR-440..444): its checks matched against the
    recording, the challenger's layer, the review rounds, and whether the last verdict counts."""
    raw = store.ticket_checks(t["id"], 50)
    checks = []
    if raw:
        holder = t.get("holder")
        sessions = (store.station_session_ids(holder) if holder
                    else [s["session"] for s in store.project_sessions(uid)])
        commands = store.recorded_commands(source, sessions) if sessions else []
        # only what ran since the ticket went into the work counts for its checks
        since = next((x["at"] for x in store.ticket_history(t["id"])
                      if x["field"] == "stage" and x["value"] == "doing"), None)
        if since is not None:
            from datetime import timedelta
            since = since - timedelta(minutes=5)
        for c in raw:
            state, recorded = (factory_mod.match_check(c, commands, since) if sessions
                               else (None, None))
            checks.append({**c, "match": state, "recorded": recorded})
    reviews = store.ticket_reviews(t["id"])
    layer = factory_mod.challenger_layer(
        store.challenge_events(source, uid, factory_mod.label(t)))
    return {"checks": checks, "reviews": reviews, "challenger": layer,
            "verdict": verdict_now(t, reviews[-1] if reviews else None)}


def pr_note(t: dict) -> str | None:
    """When the ticket's stage and its pull request disagree, the sentence that says so."""
    pr = t.get("pr") or {}
    if not pr.get("repo"):
        return None
    if t.get("stage") in ("merged", "shipped") and pr.get("state") == "open":
        return "the ticket says merged, but GitHub shows the pull request still open"
    if t.get("stage") in ("merged", "shipped") and pr.get("state") == "closed" and not pr.get("merged"):
        return "the ticket says merged, but GitHub shows the pull request closed without merging"
    if t.get("stage") in ("todo", "doing", "review", "changes") and pr.get("state") == "closed" \
            and not pr.get("merged"):
        return "GitHub shows the pull request closed without merging"
    return None


def register(app, store, h: SimpleNamespace) -> None:
    """`h` carries the daemon's helpers: err(exc, code), project_or_404(uid), by(request, body),
    cc_source(), global_ids(create)."""

    def deny(request: Request, action: str) -> None:
        why = factory_mod.may(caller(request), action)
        if why:
            h.err(PermissionError(why), 403)

    # ── crew settings (TR-419) ────────────────────────────────────────────────
    def settings() -> dict:
        try:
            cur = json.loads(store.get_setting(CREW_SETTING) or "{}")
        except ValueError:
            cur = {}
        return {**factory_mod.CREW_DEFAULTS, **{k: cur.get(k) for k in factory_mod.CREW_DEFAULTS}}

    @app.get("/api/crew/settings")
    async def get_crew_settings():
        """The crew's settings for every project: autonomy (L3-review, L4-ship, L5-dark),
        release profile, prod pattern, challenger on builders, most builders at once. Unset
        ones are null."""
        return {**settings(), "autonomy_levels": factory_mod.AUTONOMY}

    @app.put("/api/crew/settings")
    async def put_crew_settings(request: Request, body: dict = Body(...)):
        """Change the fields given; the rest keep their value."""
        deny(request, "grants")
        try:
            out = factory_mod.crew_settings_ok(body, settings())
        except docs_mod.DocError as e:
            h.err(e)
        store.set_setting(CREW_SETTING, json.dumps(out))
        return {**out, "autonomy_levels": factory_mod.AUTONOMY}

    # ── grants (TR-419): the person's words, dated; shared, and per project ───
    def shared_grants_id(create: bool = False) -> str | None:
        """The cell's "Grants (all projects)" doc, made with the first grant."""
        gid = store.get_setting(GRANTS_SETTING)
        if gid and store.get_doc(None, gid) is not None:
            return gid
        if not create:
            return None
        title, body = docs_mod.GRANTS_DOC
        gid = store.create_doc(None, "grants", title, body, by="tares")
        store.set_setting(GRANTS_SETTING, gid)
        for uid in store.handed_over_projects():   # the crew's projects show it from now on
            store.use_doc(uid, gid)
        return gid

    def include_shared_grants(uid: str) -> None:
        gid = shared_grants_id()
        if gid:
            store.use_doc(uid, gid)

    def project_grants_doc(uid: str) -> dict | None:
        gid = shared_grants_id()
        return next((d for d in store.list_docs(uid, "grants") if d["id"] != gid), None)

    def grants_out(uid: str | None) -> dict:
        gid = shared_grants_id()
        shared = store.get_doc(None, gid) if gid else None
        own = None
        if uid:
            d = project_grants_doc(uid)
            own = store.get_doc(uid, d["id"]) if d else None
        return {"all": shared["body"] if shared else "",
                "project": own["body"] if own else "",
                "all_id": shared["id"] if shared else None,
                "project_id": own["id"] if own else None}

    @app.get("/api/grants")
    async def get_grants(project: str = ""):
        """The grants for every project (`all`) and, with `project`, that project's own, as
        markdown."""
        uid = None
        if project.strip():
            uid = h.project_or_404(h.resolve_project(project.strip()))["id"]
        return grants_out(uid)

    @app.post("/api/grants", status_code=201)
    async def add_grant(request: Request, body: dict = Body(...)):
        """{words, scope: all|project, project?}: one grant, quoting the person, dated today.
        Only the orchestrator among the crew may add one; a person's own session may."""
        deny(request, "grants")
        scope = str(body.get("scope") or "all").strip().lower()
        if scope not in ("all", "project"):
            h.err(ValueError("scope is all or project"))
        try:
            line = factory_mod.grant_line(body.get("words"), now_utc().date().isoformat())
        except docs_mod.DocError as e:
            h.err(e)
        uid = None
        if scope == "project":
            ref = str(body.get("project") or "").strip()
            if not ref:
                h.err(ValueError("name the project the grant is for"))
            uid = h.project_or_404(h.resolve_project(ref))["id"]
        added = append_grant(uid, line, h.by(request, body))
        return {**grants_out(uid), "added": added, "line": line}

    def append_grant(uid: str | None, line: str, by: str) -> bool:
        """One grant line into the shared grants (uid None) or the project's own."""
        if uid is None:
            doc_id = shared_grants_id(create=True)
        else:
            d = project_grants_doc(uid)
            if d is None:
                p = store.get_project(uid)
                doc_id = store.create_doc(uid, "grants", f"Grants: {p['name']}",
                                          f"# Grants for {p['name']}\n\nWhat the person allows "
                                          "the crew in this project, on top of the grants for "
                                          "every project. Their words, dated.\n\n", by=by)
            else:
                doc_id = d["id"]
            include_shared_grants(uid)
        text, added = docs_mod.append_line(store.get_doc(None, doc_id)["body"], line)
        if added:
            store.update_doc(doc_id, body=text, by=by)
        return added

    # ── stations (TR-420): sessions grouped by the station they play ──────────
    def _ts(v) -> float:
        return v.timestamp() if isinstance(v, datetime) else 0.0

    def stations(project: str | None = None) -> list[dict]:
        rows = store.station_sessions(h.cc_source())
        names = {p["id"]: p["name"] for p in store.list_projects()}
        now = now_utc()
        by: dict[str, list[dict]] = {}
        for s in rows:
            by.setdefault(s["station"], []).append(s)
        out = []
        for name, ss in by.items():
            # the newest session plays the station now; an older one that never reported its
            # end (killed) does not outrank it
            ss.sort(key=lambda s: _ts(s["last_at"]), reverse=True)
            cur = ss[0]
            quiet = (int((now - cur["last_at"]).total_seconds() // 60)
                     if isinstance(cur["last_at"], datetime) else None)
            state = cur["state"] or "working"
            if quiet is not None and (
                    (state == "working" and quiet >= QUIET_MIN)
                    or (state == "waiting" and quiet >= WAITING_QUIET_MIN)):
                state = "quiet"
            elif state == "replaced":
                state = "ended"
            # the sessions that played the station before the current one: replaced by it
            earlier = [{"session": s["session"], "started_at": s["started_at"],
                        "last_at": s["last_at"], "lines": s["lines"], "state": "replaced"}
                       for s in ss if s is not cur]
            out.append({"name": name, "role": cur["role"], "parent": cur["parent"],
                        "project": cur["project"], "project_name": names.get(cur["project"]),
                        "state": state, "state_reason": cur["state_reason"],
                        "state_at": cur["state_at"], "quiet_minutes": quiet,
                        "session": cur["session"], "started_at": cur["started_at"],
                        "last_at": cur["last_at"], "lines": cur["lines"], "earlier": earlier,
                        "projects": sorted({s["project"] for s in ss if s["project"]})})
        for st in out:
            st["children"] = sorted(x["name"] for x in out if x["parent"] == st["name"])
        if project:
            out = [s for s in out if project in s["projects"]]
        order = {r: i for i, r in enumerate(factory_mod.ROLES)}
        out.sort(key=lambda s: (order.get(s["role"] or "", 99), s["name"]))
        return out

    @app.get("/api/crew")
    async def get_crew():
        """The crew: one row per station (its current session and state, the earlier sessions
        that played it, the stations it started), the crew's settings, and the shared grants."""
        return {"stations": stations(), "settings": settings(), "grants": grants_out(None)["all"],
                "now": now_utc()}

    @app.get("/api/crew/{name}")
    async def get_station(name: str):
        st = next((s for s in stations() if s["name"] == name), None)
        if st is None:
            h.err(KeyError(f"no station {name!r} has reported to Tares"), 404)
        return st

    # ── the crew on a project, and the hand-over (TR-422, TR-425) ─────────────
    @app.get("/api/projects/{uid}/crew")
    async def get_project_crew(uid: str):
        """The stations that worked on this project (builders started for it among them), and
        when it was handed to the crew."""
        h.project_or_404(uid)
        return {"stations": stations(uid), "handover": store.get_handover(uid),
                "now": now_utc()}

    # ── the ledger (TR-427, TR-428, TR-410, TR-429) ───────────────────────────
    def ticket_or_404(uid: str, ref: str) -> dict:
        t = store.get_ticket(uid, ref)
        if t is None:
            h.err(KeyError(f"project has no ticket {ref!r}"), 404)
        return t

    def who(request: Request, body: dict | None = None) -> tuple[str, str]:
        c = caller(request)
        return (c["station"] or h.by(request, body) or "a session"), c["role"]

    @app.post("/api/projects/{uid}/tickets/{ref}/assign")
    async def assign_ticket(uid: str, ref: str, request: Request, body: dict = Body(...)):
        """{holder}: the station that works the ticket. Only the orchestrator (or a person's own
        session) assigns. A ticket with no stage yet starts at todo."""
        h.project_or_404(uid)
        deny(request, "assign")
        t = ticket_or_404(uid, ref)
        holder = str(body.get("holder") or "").strip()[:factory_mod.MAX_NAME]
        if not holder:
            h.err(ValueError("name the station that holds it (holder)"))
        fields = {"holder": holder}
        if not t["stage"]:
            fields["stage"] = "todo"
        store.update_ticket(t["id"], **fields)
        by, role = who(request, body)
        store.add_ticket_history(t["id"], uid, "holder", holder, None, by, role, now_utc())
        return h.ticket_out(uid, ticket_or_404(uid, t["id"]), with_doc=False)

    @app.post("/api/projects/{uid}/tickets/{ref}/stage")
    async def set_stage(uid: str, ref: str, request: Request, body: dict = Body(...)):
        """{stage, reason?, pr?}: move the ticket. Only the station holding it (or a helper it
        started) moves it; only the releaser marks it shipped; a person's own session may do
        anything. `pr` (a PR URL or owner/repo#12) links the ticket to its pull request, read
        from GitHub from then on. A Tares ticket's status follows its stage; a Linear ticket's
        status stays Linear's."""
        h.project_or_404(uid)
        t = ticket_or_404(uid, ref)
        try:
            stage, reason = factory_mod.stage_ok(body.get("stage"), body.get("reason"))
        except docs_mod.DocError as e:
            h.err(e)
        why = factory_mod.may_move(caller(request), t, stage)
        if why:
            h.err(PermissionError(why), 403)
        fields: dict = {"stage": stage, "stage_reason": reason or None}
        if t["owner"] != "linear":
            fields["status"] = factory_mod.STATUS_OF_STAGE[stage]
        pr_ref = str(body.get("pr") or "").strip()
        if pr_ref:
            parsed = factory_github.parse_pr(pr_ref)
            if parsed is None:
                h.err(ValueError("pr is the pull request's URL (https://github.com/o/r/pull/12) "
                                 "or o/r#12"))
            prev = t["pr"] if t["pr"] and (t["pr"].get("repo"), t["pr"].get("number")) == parsed else None
            fields["pr"] = await factory_github.read_pr(store, parsed[0], parsed[1], prev)
        store.update_ticket(t["id"], **fields)
        by, role = who(request, body)
        store.add_ticket_history(t["id"], uid, "stage", stage, reason, by, role or None, now_utc())
        if fields.get("pr"):   # a PR GitHub already shows merged makes the ticket merged
            after = ticket_or_404(uid, t["id"])
            merged = merged_fields(after, fields["pr"])
            if merged:
                store.update_ticket(t["id"], **merged)
                store.add_ticket_history(t["id"], uid, "stage", "merged",
                                         "GitHub shows the pull request merged", "github",
                                         "github", now_utc())
        return h.ticket_out(uid, ticket_or_404(uid, t["id"]), with_doc=False)

    @app.get("/api/projects/{uid}/tickets/{ref}/messages")
    async def ticket_messages(uid: str, ref: str):
        """The crew's protocol messages ([TF:<TYPE>]) that name this ticket, oldest first, from
        the recorded SendMessage calls (TR-429)."""
        p = h.project_or_404(uid)
        t = ticket_or_404(uid, ref)
        return {"messages": messages_for(p, t)}

    def messages_for(project: dict, t: dict) -> list[dict]:
        builders = {s["station"] for s in store.station_sessions(h.cc_source())
                    if s["project"] == project["id"]}
        lab = factory_mod.label(t)
        name = re.compile(rf"(?<![\w-]){re.escape(project['name'])}(?![\w-])", re.I)
        out = []
        for m in store.protocol_messages(h.cc_source()):
            text = m["text"]
            if not re.search(rf"(?<![\w-]){re.escape(lab)}(?![\w-])", text):
                continue
            # a builder of this project took part, or the message names the project (the
            # crew's own stations serve every project)
            if not (m["to"] in builders or m["from"] in builders or name.search(text)):
                continue
            out.append(m)
        return out

    # ── the build, recorded (TR-433, TR-435, TR-436) ──────────────────────────
    def ticket_ids(uid: str, refs) -> list[str]:
        try:
            refs = factory_mod.depends_ok(refs)
        except docs_mod.DocError as e:
            h.err(e)
        return [ticket_or_404(uid, r)["id"] for r in refs]

    def assumption_out(uid: str, a: dict) -> dict:
        by_id = {t["id"]: t for t in store.list_tickets(uid)}
        return {**a, "tickets": [factory_mod.label(by_id[t]) for t in a["tickets"] if t in by_id]}

    @app.post("/api/projects/{uid}/assumptions", status_code=201)
    async def add_assumption(uid: str, request: Request, body: dict = Body(...)):
        """{question, choice, why, affects?, tickets?}: an assumption made so the work did not
        block. Numbered A1, A2... per project; open until the person keeps or overturns it."""
        h.project_or_404(uid)
        try:
            q, choice, why, affects = factory_mod.assumption_ok(
                body.get("question"), body.get("choice"), body.get("why"), body.get("affects"))
        except docs_mod.DocError as e:
            h.err(e)
        by, role = who(request, body)
        a = store.add_assumption(uid, q, choice, why, affects, ticket_ids(uid, body.get("tickets")),
                                 by, role, now_utc())
        return assumption_out(uid, a)

    @app.get("/api/projects/{uid}/assumptions")
    async def list_assumptions(uid: str, state: str = ""):
        h.project_or_404(uid)
        st = state.strip().lower() or None
        if st and st not in factory_mod.ASSUMPTION_STATES:
            h.err(ValueError("state is one of: " + ", ".join(factory_mod.ASSUMPTION_STATES)))
        return {"assumptions": [assumption_out(uid, a) for a in store.list_assumptions(uid, st)]}

    @app.post("/api/projects/{uid}/assumptions/{ref}/state")
    async def decide_assumption(uid: str, ref: str, request: Request, body: dict = Body(...)):
        """{state: kept|overturned|open, words?, batch?}: what the person said about it. Only the
        orchestrator (or a person's own session) records it; an overturned one keeps the
        person's words."""
        h.project_or_404(uid)
        deny(request, "assumption_state")
        a = store.get_assumption(uid, ref)
        if a is None:
            h.err(KeyError(f"project has no assumption {ref!r}"), 404)
        st = str(body.get("state") or "").strip().lower()
        if st not in factory_mod.ASSUMPTION_STATES:
            h.err(ValueError("state is one of: " + ", ".join(factory_mod.ASSUMPTION_STATES)))
        words = " ".join(str(body.get("words") or "").split())[:1000] or None
        if st == "overturned" and not words:
            h.err(ValueError("an overturned assumption keeps the person's words: say them"))
        store.decide_assumption(a["id"], st, words, str(body.get("batch") or "") or None,
                                now_utc() if st != "open" else None)
        return assumption_out(uid, store.get_assumption(uid, a["id"]))

    @app.post("/api/projects/{uid}/tickets/{ref}/checks", status_code=201)
    async def add_check(uid: str, ref: str, request: Request, body: dict = Body(...)):
        """{command, result, commit?, broke_test?}: a check run on the ticket. Kept, and added as
        one line to the Checks part of the ticket's working doc (made when there is none)."""
        p = h.project_or_404(uid)
        t = ticket_or_404(uid, ref)
        try:
            cmd, result, commit, broke = factory_mod.check_ok(
                body.get("command"), body.get("result"), body.get("commit"), body.get("broke_test"))
        except docs_mod.DocError as e:
            h.err(e)
        by, _ = who(request, body)
        store.add_check(uid, t["id"], cmd, result, commit, broke, caller(request)["station"],
                        "", now_utc())
        line = factory_mod.check_line(cmd, result, commit, broke)
        doc_id = t["working_doc"]
        if doc_id and store.get_doc(uid, doc_id):
            body_now = store.get_doc(uid, doc_id)["body"]
            store.update_doc(doc_id, body=factory_mod.add_check_to_doc(body_now, line), by=by)
        else:
            doc_id = store.create_doc(uid, "working", f"Working: {t['title']}",
                                      factory_mod.add_check_to_doc(f"# {t['title']}\n", line), by=by)
            store.update_ticket(t["id"], working_doc=doc_id)
        return {"line": line, "working_doc": doc_id,
                "checks": store.ticket_checks(t["id"], 10), "project": p["name"]}

    # the project's own memory (TR-436): what builders learned about this project
    def memory_doc(uid: str, create: bool = False) -> str | None:
        gid = (h.global_ids(False) or {}).get("memory")
        d = next((x for x in store.list_docs(uid, "memory") if x["id"] != gid), None)
        if d or not create:
            return d["id"] if d else None
        p = store.get_project(uid)
        return store.create_doc(uid, "memory", f"Memory: {p['name']}",
                                f"# Memory for {p['name']}\n\nWhat the builders learned about "
                                "this project, one line each, newest last.\n\n", by="tares")

    @app.post("/api/projects/{uid}/memory/lines", status_code=201)
    async def remember_for_project(uid: str, request: Request, body: dict = Body(...)):
        """{text}: one dated line in the project's memory (made with the first line)."""
        h.project_or_404(uid)
        doc_id = memory_doc(uid, create=True)
        by, _ = who(request, body)
        cur = store.get_doc(uid, doc_id)["body"]
        try:
            text, added = docs_mod.append_line(
                cur, f"{str(body.get('text') or '').strip()} ({now_utc().date().isoformat()}, {by})"
                if str(body.get("text") or "").strip() else "")
        except docs_mod.DocError as e:
            h.err(e)
        if added:
            store.update_doc(doc_id, body=text, by=by)
        h.ensure_globals(uid)
        return {"id": doc_id, "added": added, "body": store.get_doc(uid, doc_id)["body"]}

    # ── review and merge (TR-441..445) ────────────────────────────────────────
    @app.post("/api/projects/{uid}/tickets/{ref}/reviews", status_code=201)
    async def record_review(uid: str, ref: str, request: Request, body: dict = Body(...)):
        """{head, verdict, verified, not_verified?, findings?, resolved?}: one review round, the
        verdict on the commit it was given on. `resolved` marks earlier findings ({F1: fixed}).
        Only the reviewer (or a person's own session) records one."""
        h.project_or_404(uid)
        deny(request, "review")
        t = ticket_or_404(uid, ref)
        try:
            verdict, verified, not_verified, findings, head = factory_mod.review_ok(
                body.get("verdict"), body.get("verified"), body.get("not_verified"),
                body.get("findings"), body.get("head"))
        except docs_mod.DocError as e:
            h.err(e)
        raw = body.get("resolved") or {}
        if not isinstance(raw, dict) or any(v not in ("fixed", "open") for v in raw.values()):
            h.err(ValueError("resolved is {finding: fixed|open}, e.g. {\"F1\": \"fixed\"}"))
        # one spelling for every finding ref (F1, f1, 1, its id); an unknown one is a mistake
        known = {f["id"]: f["label"] for r in store.ticket_reviews(t["id"]) for f in r["findings"]}
        resolved = {}
        for k, v in raw.items():
            m = re.match(r"^[Ff]?(\d{1,6})$", str(k).strip())
            lab = f"F{int(m.group(1))}" if m else known.get(str(k))
            if lab not in known.values():
                h.err(KeyError(f"no finding {k!r} on {factory_mod.label(t)}; it has: "
                               + (", ".join(sorted(known.values())) or "none")), 404)
            resolved[lab] = v
        if verdict == "pass" and any(
                f["blocking"] and resolved.get(f["label"]) != "fixed"
                for f in store.open_findings(t["id"])):
            h.err(ValueError("a pass leaves no blocking finding open: mark the earlier ones "
                             "fixed in `resolved`, or ask for changes"))
        by, _ = who(request, body)
        r = store.add_review(uid, t["id"], (t["pr"] or {}).get("url"), head, verdict, verified,
                             not_verified, by, findings, resolved, factory_mod.finding_kind,
                             now_utc())
        repo = (t["pr"] or {}).get("repo")
        if repo:
            offer_rules(repo)
        rv = next(x for x in store.ticket_reviews(t["id"]) if x["id"] == r["id"])
        return {**rv, "open": [f["label"] for f in store.open_findings(t["id"])]}

    def queue() -> list[dict]:
        names = {p["id"]: p["name"] for p in store.list_projects()}
        out = []
        for uid, pname in names.items():
            tickets = store.list_tickets(uid)
            if not any(t["stage"] == "review" for t in tickets):
                continue
            for t in tickets:
                if t["stage"] != "review":
                    continue
                # it blocks its milestone when it is the last of the milestone's tickets open
                others = [x for x in tickets if x["milestone"] and x["milestone"] == t["milestone"]
                          and x["id"] != t["id"]]
                blocks = bool(t["milestone"]) and all(
                    x["status"] in factory_mod.DONE_STATUSES for x in others)
                since = next((x["at"] for x in reversed(store.ticket_history(t["id"]))
                              if x["field"] == "stage" and x["value"] == "review"), t["updated_at"])
                layer = factory_mod.challenger_layer(
                    store.challenge_events(h.cc_source(), uid, factory_mod.label(t)))
                last = store.ticket_reviews(t["id"])[-1:]
                v = verdict_now(t, last[0] if last else None)
                out.append({"project": uid, "project_name": pname, "ticket": factory_mod.label(t),
                            "title": t["title"], "holder": t["holder"],
                            "pr": (t["pr"] or {}).get("url"), "head": (t["pr"] or {}).get("head"),
                            "waiting_since": since,
                            "blocks_milestone": blocks,
                            "layer1_clean": layer["clean"],
                            "verdict": v})
        now = now_utc()
        for q in out:
            q["waiting_minutes"] = (int((now - q["waiting_since"]).total_seconds() // 60)
                                    if isinstance(q["waiting_since"], datetime) else None)
        out.sort(key=lambda q: (not q["blocks_milestone"], -(q["waiting_minutes"] or 0)))
        return out

    @app.get("/api/review-queue")
    async def review_queue():
        """Tickets in review across every project: those blocking their milestone first, then
        the longest waiting (TR-444)."""
        return {"queue": queue(), "now": now_utc()}

    @app.get("/api/projects/{uid}/tickets/{ref}/brief")
    async def review_brief(uid: str, ref: str, head: str = ""):
        """Everything the reviewer needs for one review, as markdown (TR-445)."""
        p = h.project_or_404(uid)
        await refresh_prs(store, uid)
        t = ticket_or_404(uid, ref)
        return {"brief": brief_md(p, t, head.strip())}

    def brief_md(p: dict, t: dict, head: str) -> str:
        full = h.ticket_out(p["id"], t, with_doc=True)
        pr = t["pr"] or {}
        lab = factory_mod.label(t)
        L = [f"# Review brief: {lab} {t['title']} ({p['name']})", ""]
        # 1. GitHub's facts first: stop early when there is nothing to review
        L += ["## The pull request, as GitHub has it"]
        if not pr.get("url"):
            L += ["No pull request is linked to this ticket yet (set_stage review with pr=...).", ""]
        else:
            L += [f"- {pr['url']}, head {str(pr.get('head') or '?')[:12]}, CI {pr.get('ci') or 'unknown'}"
                  + (f", read {pr.get('checked_at')}" if pr.get("checked_at") else "")]
            if pr.get("error"):
                L += [f"- Could not read it fresh: {pr['error']}"]
            if pr.get("merged"):
                L += ["", "STOP: the pull request is already merged. There is nothing to review."]
                return "\n".join(L)
            hd, now_head = head.lower(), str(pr.get("head") or "").lower()
            if len(hd) >= 7 and now_head and not (now_head.startswith(hd) or hd.startswith(now_head)):
                L += ["", f"STOP: the head moved. You were asked about {head[:12]}, the PR is now "
                          f"at {pr['head'][:12]}. Review the new head and say so."]
                return "\n".join(L)
            L.append("")
        # 2. what was asked
        L += ["## The ticket and its working doc",
              f"Milestone: {full.get('milestone_name') or 'none'}. Held by {t['holder'] or 'nobody'}.",
              "", (full.get("working_doc_body") or "(no working doc)").strip(), ""]
        # 3. the builder's claims against the recording, unmatched first
        checks = sorted(full.get("checks") or [], key=lambda c: c.get("match") == "matched")
        L += ["## The builder's checks, against the recording"]
        if not checks:
            L += ["The builder recorded no checks: verify everything yourself."]
        for c in checks:
            mark = {"matched": "matched", "not_found": "NOT IN THE RECORDING",
                    "differs": "RECORDING DIFFERS"}.get(c.get("match") or "", "not checked")
            L += [f"- [{mark}] `{c['command']}` -> {c['result']}"
                  + (f" (broke {c['broke_test']})" if c.get("broke_test") else "")
                  + (f". Recording: {c['recorded']}" if c.get("recorded") else "")]
        L.append("")
        # 4. layer 1: Codex
        layer = full.get("challenger") or {}
        L += ["## Codex (the builder's challenger)"]
        if not layer.get("rounds"):
            L += ["No Codex review is recorded for this ticket's branch."]
        else:
            L += [f"{layer['rounds']} rounds, last: {layer.get('verdict')}"
                  + (f" on {str(layer.get('sha'))[:7]}" if layer.get("sha") else "")]
            L += [f"- open [{f.get('priority')}] {f.get('title')}" for f in layer.get("open") or []]
            L += [f"- fixed [{f.get('priority')}] {f.get('title')}" for f in layer.get("fixed") or []]
            L += [f"- WAIVED [{f.get('priority')}] {f.get('title')}: "
                  f"{f.get('reason') or 'no reason given'} (judge this waiver)"
                  for f in layer.get("waived") or []]
        L.append("")
        # 5. earlier rounds
        reviews = full.get("reviews") or []
        L += ["## Earlier review rounds"]
        if not reviews:
            L += ["This is the first review."]
        for r in reviews:
            L += [f"- Round {r['round']} on {r['head'][:7]}: {r['verdict']}"]
            L += [f"  - {f['label']} [{f['severity']}{', blocking' if f['blocking'] else ''}] "
                  f"{f['state']}: {f['text']}" + (f" ({f['file']}:{f['line']})" if f.get("file") else "")
                  for f in r["findings"]]
        L.append("")
        # 6. assumptions and decisions on it
        if full.get("assumptions"):
            L += ["## Assumptions made on this ticket"]
            L += [f"- {a['label']} ({a['state']}): {a['question']} -> {a['choice']}"
                  for a in full["assumptions"]] + [""]
        ds = [d for d in store.list_decisions(p["id"], all_=True)
              if lab in (d.get("tickets") or []) or not d.get("tickets")][:10]
        if ds:
            L += ["## Decisions the person made on this project"]
            L += [f"- {d.get('question') or ''}: \"{d.get('words') or ''}\"" for d in ds] + [""]
        # 7. recurring findings in this repo: check these first
        if pr.get("repo"):
            rec = recurring(pr["repo"])
            if rec:
                L += [f"## What keeps coming back in {pr['repo']} (check these first)"]
                L += [f"- {r['kind']}: {r['count']} tickets ({', '.join(r['tickets'][:5])})" for r in rec]
                L.append("")
        # 8. links, not inlined
        L += ["## Read as needed",
              "- The spec, AGENTS.md (the project's and the shared one) and the project memory: "
              "list_docs, then get_doc(id).", "- The crew's messages about it: get_ticket."]
        return "\n".join(L)

    # ── recurring findings and rules offered from them (TR-443) ───────────────
    def recurring(repo: str) -> list[dict]:
        from datetime import timedelta
        rows = store.repo_findings(repo, now_utc() - timedelta(days=factory_mod.RECUR_DAYS))
        names = {p["id"]: p["name"] for p in store.list_projects()}
        by: dict[str, dict] = {}
        for f in rows:
            k = by.setdefault(f["kind"], {"kind": f["kind"], "refs": set(), "examples": []})
            t = store.get_ticket(f["project"], f["ticket"])
            k["refs"].add((f["project"], factory_mod.label(t) if t else "?"))
            if len(k["examples"]) < 3:
                k["examples"].append(f["text"][:200])
        out = [{"kind": k["kind"], "count": len(k["refs"]),
                "projects": sorted({p for p, _ in k["refs"]}),
                "tickets": sorted(f"{names.get(p, '?')} {lab}" for p, lab in k["refs"]),
                "examples": k["examples"]} for k in by.values()
               if len(k["refs"]) >= factory_mod.RECUR_MIN]
        return sorted(out, key=lambda r: -r["count"])

    def offer_rules(repo: str) -> None:
        for r in recurring(repo):
            scope = "all" if len(r["projects"]) > 1 else "project"
            pid = r["projects"][0] if scope == "project" else None
            text = (f"In {repo}, check for {r['kind']} before asking for review: it came back in "
                    f"{r['count']} tickets.")
            store.upsert_rule_proposal(repo, r["kind"], pid, scope, text, r["tickets"], now_utc())

    @app.get("/api/recurring-findings")
    async def recurring_findings(repo: str):
        return {"repo": repo, "recurring": recurring(repo.strip())}

    @app.get("/api/rule-proposals")
    async def list_rule_proposals(state: str = "open"):
        return {"proposals": store.list_rule_proposals(state.strip() or None)}

    @app.post("/api/rule-proposals/{pid}/{decision}")
    async def decide_rule(pid: str, decision: str, request: Request,
                          body: dict = Body(default={})):
        """accept (optionally with the text edited) adds the rule to AGENTS.md (the project's,
        or the shared one when it came back across projects); reject keeps it from being
        offered again. A person decides: refused from any crew station."""
        if caller(request)["station"]:
            h.err(PermissionError("a rule is the person's call: accept or reject it in the "
                                  "console"), 403)
        rp = store.get_rule_proposal(pid)
        if rp is None:
            h.err(KeyError(f"no rule proposal {pid!r}"), 404)
        if rp["state"] != "open":
            h.err(ValueError(f"already {rp['state']}"), 409)
        if decision == "reject":
            store.decide_rule_proposal(pid, "rejected", None, now_utc())
            return store.get_rule_proposal(pid)
        if decision != "accept":
            h.err(KeyError("accept or reject"), 404)
        text = " ".join(str(body.get("text") or rp["text"]).split())
        if rp["scope"] == "project" and not (rp["project"] and store.get_project(rp["project"])):
            h.err(ValueError("the project this rule was for is gone; reject it"), 409)
        if rp["scope"] == "project":
            doc = next((d for d in store.list_docs(rp["project"], "agents")
                        if d["id"] not in h.global_ids(False).values()), None)
            doc_id = doc["id"] if doc else store.create_doc(
                rp["project"], "agents", "AGENTS.md", "# AGENTS.md\n\n", by="console")
        else:
            doc_id = h.global_ids(True)["agents"]
        cur = store.get_doc(None, doc_id)["body"]
        new, _ = docs_mod.append_line(cur, text)
        store.update_doc(doc_id, body=new, by="console")
        store.decide_rule_proposal(pid, "accepted", text, now_utc())
        return {**store.get_rule_proposal(pid), "doc": doc_id}

    # ── the desk: questions for the person, and their answers (TR-449..452) ────
    def desk_out(d: dict) -> dict:
        p = store.get_project(d["project"]) if d["project"] else None
        return {**d, "project_name": p["name"] if p else None}

    @app.post("/api/desk", status_code=201)
    async def desk_add(request: Request, body: dict = Body(...)):
        """{question, recommendation, why, options?, context?, blocks?, tickets?, blocking?,
        assumptions?, project?}: a question for the person, numbered D1, D2... for the whole
        cell. Only the orchestrator (or a person's own session) writes the desk."""
        deny(request, "desk")
        try:
            q, rec, why, opts, ctx = factory_mod.desk_ok(
                body.get("question"), body.get("recommendation"), body.get("why"),
                body.get("options"), body.get("context"))
        except docs_mod.DocError as e:
            h.err(e)
        uid = None
        if str(body.get("project") or "").strip():
            uid = h.project_or_404(h.resolve_project(str(body["project"]).strip()))["id"]
        tickets = []
        if uid:
            tickets = [factory_mod.label(ticket_or_404(uid, r))
                       for r in factory_mod.depends_ok(body.get("tickets"))]
        asm = []
        for a in body.get("assumptions") or []:
            got = store.get_assumption(uid, str(a)) if uid else None
            if got is None:
                h.err(KeyError(f"no assumption {a!r} in that project"), 404)
            asm.append(got["label"])
        by, _ = who(request, body)
        d = store.add_desk_item(uid, q, ctx, opts, rec, why,
                                " ".join(str(body.get("blocks") or "").split())[:300], tickets,
                                bool(body.get("blocking")), str(body.get("asked_by") or by)[:120],
                                asm, now_utc())
        return desk_out(d)

    @app.get("/api/desk")
    async def desk_list(state: str = "open", project: str = ""):
        """The desk, blockers first, then oldest."""
        st = state.strip().lower() or None
        if st and st not in factory_mod.DESK_STATES:
            h.err(ValueError("state is one of: " + ", ".join(factory_mod.DESK_STATES)))
        uid = h.project_or_404(h.resolve_project(project.strip()))["id"] if project.strip() else None
        return {"desk": [desk_out(d) for d in store.list_desk(st, uid)]}

    def desk_or_404(ref: str) -> dict:
        d = store.get_desk_item(ref)
        if d is None:
            h.err(KeyError(f"no desk item {ref!r}"), 404)
        return d

    @app.post("/api/desk/{ref}/withdraw")
    async def desk_withdraw(ref: str, request: Request, body: dict = Body(default={})):
        deny(request, "desk")
        d = desk_or_404(ref)
        if d["state"] != "open":
            h.err(ValueError(f"{d['label']} is already {d['state']}"), 409)
        store.close_desk_item(d["id"], "withdrawn",
                              " ".join(str(body.get("reason") or "").split())[:500] or None,
                              now_utc())
        return desk_out(desk_or_404(d["id"]))

    @app.post("/api/desk/{ref}/answer")
    async def desk_answer(ref: str, request: Request, body: dict = Body(...)):
        """{words, choice?, standing?, scope?: project|all, kept?: [A<n>], overturned?: {A<n>:
        words}}: the person's answer, their words as typed. Tares looks for those words in what
        the person typed into the orchestrator's session (or, from the person's own session, in
        any session) and says whether they match. A standing answer that matches becomes a
        grant. An assumption batch keeps or overturns each assumption."""
        deny(request, "desk")
        d = desk_or_404(ref)
        if d["state"] != "open":
            h.err(ValueError(f"{d['label']} is already {d['state']}"), 409)
        words = str(body.get("words") or "").strip()
        if not words:
            h.err(ValueError("words: the person's reply exactly as they typed it"))
        if len(words) > 4000:
            h.err(ValueError("keep the words to 4000 characters"))
        scope = str(body.get("scope") or "project").strip().lower()
        if scope not in ("project", "all"):
            h.err(ValueError("scope is project or all"))
        if scope == "all" and d["project"] and not factory_mod.says_every_project(words):
            h.err(ValueError("a project's question is answered for that project; it holds for "
                             "every project only when the person says so"))
        from datetime import timedelta
        # only what the person typed after the question was asked can answer it
        since = max(d["created_at"], now_utc() - timedelta(hours=factory_mod.MATCH_HOURS))
        labeled = bool(caller(request)["station"])
        sessions = store.role_sessions("orchestrator") if labeled else None
        turns = store.person_turns(h.cc_source(), sessions, since)
        hit = factory_mod.words_match(words, turns)
        # an assumption batch: every listed assumption kept or overturned, with the person's words
        kept = [str(a) for a in body.get("kept") or []]
        over = body.get("overturned") or {}
        if not isinstance(over, dict):
            h.err(ValueError("overturned is {A<n>: the person's words}"))
        plan = []
        for ref_a, st, w in [(a, "kept", None) for a in kept] + [
                (a, "overturned", str(w)) for a, w in over.items()]:
            a = store.get_assumption(d["project"], ref_a) if d["project"] else None
            if a is None or a["label"] not in d["assumptions"]:
                h.err(KeyError(f"{ref_a!r} is not one of {d['label']}'s assumptions: "
                               + (", ".join(d["assumptions"]) or "it has none")), 404)
            if a["state"] != "open":
                h.err(ValueError(f"{a['label']} was already {a['state']}"), 409)
            plan.append((a, st, w))
        missing = set(d["assumptions"]) - {a["label"] for a, _, _ in plan}
        if d["assumptions"] and missing:
            h.err(ValueError("the answer must keep or overturn every assumption of "
                             f"{d['label']}; left out: " + ", ".join(sorted(missing))))
        unheard = [a["label"] for a, st, w in plan if st == "overturned"
                   and not (factory_mod.words_match(w, turns)
                            or factory_mod._plain(w) in factory_mod._plain(words))]
        if hit is None or unheard:
            # nothing is recorded as the person's unless they typed it: the question stays open
            return {"desk_item": desk_out(d), "matched": False, "granted": False,
                    "recorded": False, "assumptions": [], "follow_up": [],
                    "unheard": unheard}
        standing = bool(body.get("standing"))
        granted = False
        if standing:
            try:
                line = factory_mod.grant_line(words, now_utc().date().isoformat())
            except docs_mod.DocError as e:
                h.err(e)
            granted = append_grant(d["project"] if scope == "project" and d["project"] else None,
                                   line, "the person, on the desk")
        changed = []
        for a, st, w in plan:
            store.decide_assumption(a["id"], st, w, d["label"], now_utc())
            changed.append({"label": a["label"], "state": st, "tickets": a["tickets"]})
        dec = store.add_decision(d["project"], d["id"], d["question"], words,
                                 str(body.get("choice") or "")[:300], standing, scope,
                                 hit is not None, hit["session"] if hit else None, granted,
                                 d["tickets"], now_utc())
        store.close_desk_item(d["id"], "answered", None, now_utc())
        by_id = {t["id"]: t for t in store.list_tickets(d["project"])} if d["project"] else {}
        follow = [{"assumption": c["label"],
                   "tickets": [factory_mod.label(by_id[x]) for x in c["tickets"] if x in by_id]}
                  for c in changed if c["state"] == "overturned"]
        return {"desk_item": desk_out(desk_or_404(d["id"])), "decision": dec,
                "matched": True, "recorded": True, "granted": granted, "assumptions": changed,
                "follow_up": follow, "unheard": []}

    @app.get("/api/projects/{uid}/decisions")
    async def project_decisions(uid: str):
        """The person's answers on this project, newest first."""
        h.project_or_404(uid)
        return {"decisions": store.list_decisions(uid)}

    @app.post("/api/projects/{uid}/handover")
    async def hand_over(uid: str, request: Request, body: dict = Body(default={})):
        """{repo?}: the spec session hands the project to the crew. Recorded with the time; the
        session then messages the orchestrator itself."""
        h.project_or_404(uid)
        repo = str(body.get("repo") or "").strip()[:500]
        store.set_handover(uid, repo, h.by(request, body), now_utc())
        include_shared_grants(uid)   # a crew-built project shows the grants it is built under
        return {"handover": store.get_handover(uid)}
