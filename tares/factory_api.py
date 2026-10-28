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
from datetime import datetime, timedelta
from types import SimpleNamespace

from fastapi import Body, HTTPException, Request

from . import claude_usage
from . import docs as docs_mod
from . import factory as factory_mod
from . import factory_github
from .envelope import now_utc
from .factory_store import MapChanged

CREW_SETTING = "crew_settings"
CLAUDE_RETRY_SETTING = "crew_claude_retry"   # stations the watch is retrying onto a new Claude Code
GRANTS_SETTING = "global_grants"   # the id of the cell's "Grants (all projects)" doc


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
    """What a merge seen on GitHub changes on the ticket (nothing when it already says so). A
    Linear or GitHub ticket keeps its own status; only a Tares one is set done."""
    if not pr.get("merged") or t["stage"] in ("merged", "shipped"):
        return {}
    out = {"stage": "merged"}
    if t["owner"] == "tares":
        out["status"] = "done"
    return out


def _reread_only(cur: dict, fields: dict) -> bool:
    """The PR was read again and nothing but when (or whether the read failed) differs: that is
    not ticket activity, and the Projects list's Updated column must not move for it."""
    if set(fields) != {"pr"} or not cur["pr"]:
        return False
    volatile = {"checked_at": None, "error": None}
    return {**cur["pr"], **volatile} == {**fields["pr"], **volatile}


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
    linked = [t for t in tickets if t["pr"] and t["pr"].get("repo")]
    lasts = store.last_reviews([t["id"] for t in linked])

    async def one(t: dict) -> None:
        pr = t["pr"]
        if factory_github.stale(pr):
            async with sem:
                pr = await factory_github.read_pr(store, pr["repo"], int(pr["number"]), pr)
        elif not _follows(t, pr, lasts.get(t["id"])):
            return               # not read again and nothing to write: skip the re-read below
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
            store.update_ticket(cur["id"], touch=not _reread_only(cur, fields), **fields)
        if fields.get("stage") == "merged":
            store.add_ticket_history(cur["id"], uid, "stage", "merged",
                                     "GitHub shows the pull request merged", "github", "github",
                                     now_utc())

    await asyncio.gather(*(one(t) for t in linked), return_exceptions=True)


def _follows(t: dict, pr: dict, last: dict | None) -> bool:
    """Whether a PR that was not read again still changes the ticket: a merge it does not show
    yet, or new commits after the verdict that asked for changes (the same tests `one` makes)."""
    if merged_fields(t, pr):
        return True
    return bool(t["stage"] == "changes" and last and pr.get("head")
                and not verdict_now({"pr": pr}, last)["current"])


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


def project_progress(store, uid: str, tickets: list[dict] | None = None) -> dict:
    """Tickets done of all, by stage, and the first milestone with work left: what the crew's
    project list shows (canceled tickets count as done here), and `progress`, the same tickets
    as the console's ring counts them (canceled kept out, tares/factory.py `progress`). `tickets`
    is the project's tickets when the caller already read them."""
    tickets = store.list_tickets(uid) if tickets is None else tickets
    done = [t for t in tickets if (t["stage"] in ("merged", "shipped")
                                   or t["status"] in factory_mod.DONE_STATUSES)]
    stages: dict[str, int] = {}
    for t in tickets:
        k = t["stage"] or ("done" if t in done else "todo")
        stages[k] = stages.get(k, 0) + 1
    current = None
    for m in store.list_milestones(uid):
        mine = [t for t in tickets if t["milestone"] == m["id"]]
        if any(t not in done for t in mine):
            current = {"name": m["name"], "done": sum(t in done for t in mine),
                       "total": len(mine)}
            break
    return {"tickets_done": len(done), "tickets_total": len(tickets), "stages": stages,
            "milestone": current, "progress": factory_mod.progress(tickets)}


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
        return {k: cur.get(k, d) for k, d in factory_mod.CREW_DEFAULTS.items()}

    @app.get("/api/crew/settings")
    def get_crew_settings():
        """The crew's settings for every project: autonomy (L3-review, L4-ship), whether it runs
        a releaser, release profile, prod pattern, challenger on builders, most builders at
        once, whether the watch moves stations onto a new Claude Code (claude_update, on unless
        false). Unset ones are null. `kit.frozen` says when `factory crew freeze` froze the
        crew's plugin kit, and by whom, or is null while the crew follows the checkout. `idle_wake`
        (true or false; null counts as false) makes the watch wake an idle station with a message
        when it has work, and hold the station's liveness tick, instead of each station ticking
        on its own. `pace` (true or false; null counts as false) makes the crew slow down before
        the plan's limits: see GET /api/crew/pace."""
        return {**settings(), "autonomy_levels": factory_mod.AUTONOMY}

    @app.put("/api/crew/settings")
    def put_crew_settings(request: Request, body: dict = Body(...)):
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
    def get_grants(project: str = ""):
        """The grants for every project (`all`) and, with `project`, that project's own, as
        markdown."""
        uid = None
        if project.strip():
            uid = h.project_or_404(h.resolve_project(project.strip()))["id"]
        return grants_out(uid)

    @app.post("/api/grants", status_code=201)
    def add_grant(request: Request, body: dict = Body(...)):
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

    def claude_retry() -> dict:
        try:
            cur = json.loads(store.get_setting(CLAUDE_RETRY_SETTING) or "{}")
        except ValueError:
            cur = {}
        return cur if isinstance(cur, dict) else {}

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
            state = factory_mod.shown_state(cur["state"] or "working", cur["last_at"], now)
            if state == "replaced":
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
        meters = store.session_meters([st["session"] for st in out])
        spent = store.usage_by_session([s["session"] for s in rows])
        retry = claude_retry()
        coord = store.coordination_calls(h.cc_source(), [s["session"] for s in rows])
        for st in out:
            st["children"] = sorted(x["name"] for x in out if x["parent"] == st["name"])
            # the context window from the station's status line, and what all its sessions spent
            st["context"] = claude_usage.context_now(meters.get(st["session"]))
            st["claude_version"] = claude_usage.code_version(meters.get(st["session"]))
            st["claude_retry"] = retry.get(st["name"])   # set while the watch retries its move, else null
            mine = [spent[s] for s in [st["session"], *(e["session"] for e in st["earlier"])]
                    if s in spent]
            st["usage"] = ({k: sum(x[k] for x in mine) for k in mine[0]} if mine else None)
            st["coordination_calls"] = sum(coord.get(s, 0) for s in
                                           [st["session"], *(e["session"] for e in st["earlier"])])
        if project:
            out = [s for s in out if project in s["projects"]]
        order = {r: i for i, r in enumerate(factory_mod.ROLES)}
        out.sort(key=lambda s: (order.get(s["role"] or "", 99), s["name"]))
        return out

    def pace() -> dict:
        return factory_mod.pace_now(settings(), claude_usage.plan(store), now_utc())

    @app.get("/api/crew")
    def get_crew():
        """The crew: one row per station (its current session and state, the earlier sessions
        that played it, the stations it started), the crew's settings, the shared grants, and the
        crew's `pace` (as GET /api/crew/pace gives it). `claude_retry` maps each station the
        watch is retrying onto a newer Claude Code to {target, fails, next_at}; the row of each
        shows its own as `claude_retry`."""
        return {"stations": stations(), "settings": settings(), "grants": grants_out(None)["all"],
                "pace": pace(), "claude_retry": claude_retry(), "now": now_utc()}

    @app.put("/api/crew/claude-retry")
    def put_claude_retry(body: dict = Body(...)):
        """{stations: {name: {target, fails, next_at}}}: the stations the factory watch is retrying
        onto a new Claude Code, after a restart kept landing on an older pre-warmed spare. It
        replaces the whole list; a station left out is not retrying. `target` is the version it
        is moving to, `fails` the failed moves so far and `next_at` the unix time of the next try.
        Each row of /api/crew shows its entry as `claude_retry`, or null. The factory CLI sends
        this; it is only information for the Crew page."""
        rows = body.get("stations")
        if not isinstance(rows, dict):
            h.err(ValueError("stations must be an object of station name to {target, fails, next_at}"))
        if len(rows) > 200:
            h.err(ValueError("at most 200 stations"))
        def num(v, lo: int, hi: int, default: int) -> int:
            return v if isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi else default
        out = {}
        for name, r in rows.items():
            if not isinstance(name, str) or not 0 < len(name) <= 128:
                h.err(ValueError("a station name is 1 to 128 characters"))
            if not isinstance(r, dict) or not isinstance(r.get("target"), str) or not r["target"]:
                h.err(ValueError(f"{name}: target must be the Claude Code version it is moving to"))
            out[name] = {"target": r["target"][:40],
                         "fails": num(r.get("fails"), 0, 1_000_000, 1),
                         "next_at": num(r.get("next_at"), 0, 4_102_444_800, 0)}
        store.set_setting(CLAUDE_RETRY_SETTING, json.dumps(out))
        return {"stations": out}

    @app.get("/api/crew/pace")
    def get_crew_pace():
        """Whether the crew is pacing itself before the plan's limits (the crew setting `pace`),
        worked out from the plan usage Tares keeps. `level` is 0 (ok), 1 (slow: the 5-hour window
        at 85% or the weekly limit at 90%) or 2 (hold: the window at 95%); `at_limit` is true while
        a limit is full and has not reset; `state` is off, ok, slow, hold or limit; `until` is when
        the limits behind it reset; `known` is false when no plan reading has arrived. The factory
        watch reads this (TR-674)."""
        return pace()

    @app.get("/api/crew/projects")
    def crew_projects():
        """The projects handed to the crew, oldest hand-over first: each with its repos (the
        folder on the crew's machine and the GitHub name), the autonomy it was handed over at
        when stricter than the crew's, its tickets by stage, the milestone it is on, and which
        tickets each repo has. The orchestrator works from this list (TR-462)."""
        names = {p["id"]: p["name"] for p in store.list_projects()}
        active = store.handed_over_projects(finished=False, taken=False)
        finished = store.handed_over_projects(finished=True)
        # every listed project's tickets in one query, shared by progress and repos
        tk = store.tickets_by_project(sorted({u for u in [*active, *finished] if u in names}))
        out = []
        for uid in active:
            if uid not in names:
                continue
            ho = store.get_handover(uid) or {}
            out.append({"id": uid, "name": names[uid], **project_progress(store, uid, tk[uid]),
                        "repos": repo_rows(uid, ho.get("repos") or [], tk[uid]),
                        "autonomy": ho.get("autonomy"), "handed_over_at": ho.get("at"),
                        "handed_over_by": ho.get("by")})
        out.sort(key=lambda p: str(p["handed_over_at"] or ""))
        done = []
        for uid in finished:
            if uid in names:
                ho = store.get_handover(uid) or {}
                done.append({"id": uid, "name": names[uid], **project_progress(store, uid, tk[uid]),
                             "finished": ho.get("finished")})
        done.sort(key=lambda p: str((p["finished"] or {}).get("at") or ""), reverse=True)
        releasing = []
        for uid in store.handed_over_projects(finished=False, taken=True):
            ho = store.get_handover(uid) or {}
            if uid in names and not (ho.get("takeover") or {}).get("released_at"):
                releasing.append({"id": uid, "name": names[uid], "takeover": ho["takeover"],
                                  "repos": ho.get("repos") or [],
                                  "holders": sorted({t["holder"] for t in store.list_tickets(uid)
                                                     if t["holder"]})})
        return {"projects": out, "finished": done, "releasing": releasing, "now": now_utc()}

    @app.get("/api/crew/{name}")
    def get_station(name: str, recent: int = 30):
        """One station: its row as in /api/crew, the tickets it holds in every project, its
        current session's last lines (`recent`, at most 200), and the command that opens it."""
        st = next((s for s in stations() if s["name"] == name), None)
        if st is None:
            h.err(KeyError(f"no station {name!r} has reported to Tares"), 404)
        held = []
        for p in store.list_projects():
            for t in store.list_tickets(p["id"]):
                if t["holder"] == name:
                    held.append({"project": p["id"], "project_name": p["name"],
                                 "ticket": factory_mod.label(t), "title": t["title"],
                                 "stage": t["stage"], "repo": t.get("repo"),
                                 "pr": (t["pr"] or {}).get("url")})
        n = min(max(recent, 0), 200)
        lines = store.key_events(h.cc_source(), st["session"], limit=n,
                                 offset=max((st["lines"] or 0) - n, 0)) if n else []
        m = store.session_meters([st["session"]]).get(st["session"]) or {}
        return {**st, "tickets": held,
                "context_detail": m.get("context"), "context_at": m.get("context_at"),
                "context_asked_at": m.get("context_asked_at"),
                "recent": [{"at": x.get("event_time"), "type": x.get("event_type"),
                            "text": str(x.get("text") or "")[:600]} for x in lines],
                "attach": factory_mod.attach_command(name)}

    @app.post("/api/crew/{name}/end")
    def end_station(name: str, request: Request, body: dict | None = Body(None)):
        """{reason?}: mark the station's current session ended, for a station that was retired or
        whose session is gone, so it stops showing as working or quiet. Its earlier sessions and
        lines are kept. The orchestrator or the person (unlabeled, such as `factory retire`) may.
        A session that is still alive turns the row back with its next line."""
        deny(request, "end_station")
        st = next((s for s in stations() if s["name"] == name), None)
        if st is None:
            h.err(KeyError(f"no station {name!r} has reported to Tares"), 404)
        reason = " ".join(str((body or {}).get("reason") or "").split())[:100] or "marked ended"
        if (store.session_state(st["session"]) or {}).get("state") != "ended":
            store.set_session_state(st["session"], "ended", reason, now_utc(), force=True)
        return next(s for s in stations() if s["name"] == name)

    # ── a station's context by category: asked from the console, read on the crew's machine ──
    ASK_TTL = 15   # minutes an unanswered ask stays open; the watch reads on the crew's machine

    @app.post("/api/crew/{name}/context")
    def ask_station_context(name: str):
        """Ask for a new /context reading of the station's session. The factory watch on the
        crew's machine reads it (no model call) within a few seconds, if it runs."""
        st = next((s for s in stations() if s["name"] == name), None)
        if st is None:
            h.err(KeyError(f"no station {name!r} has reported to Tares"), 404)
        at = now_utc()
        store.ask_session_context(st["session"], at)
        return {"session": st["session"], "asked_at": at}

    @app.get("/api/crew/context/asks")
    def station_context_asks():
        """The stations someone asked a /context reading of that has not come in yet, for
        `factory crew watch`."""
        out = []
        for x in store.context_asks(now_utc() - timedelta(minutes=ASK_TTL)):
            st = store.session_station(x)
            if st:
                out.append({"station": st["station"], "session": x})
        return {"asks": out}

    # ── the crew on a project, and the hand-over (TR-422, TR-425) ─────────────
    @app.get("/api/projects/{uid}/crew")
    def get_project_crew(uid: str):
        """The stations that worked on this project (builders started for it among them), and
        when it was handed to the crew."""
        h.project_or_404(uid)
        ss = stations(uid)
        return {"stations": ss, "handover": store.get_handover(uid),
                "coordination": coordination_summary(uid, ss), "now": now_utc()}

    def coordination_summary(uid: str, ss: list[dict]) -> dict:
        """Coordination calls of the project's builders and helpers (the always-on stations
        serve every project, so their calls are not the project's) against its merged pull
        requests (TR-683)."""
        mine = {s["name"]: s["coordination_calls"] for s in ss
                if s["role"] in ("builder", "helper") and s["project"] == uid}
        merged = sum(1 for t in store.list_tickets(uid)
                     if (t["pr"] or {}).get("merged") or (t["stage"] in ("merged", "shipped") and t["pr"]))
        calls = sum(mine.values())
        return {"calls": calls, "merged_prs": merged,
                "per_merged_pr": round(calls / merged, 1) if merged else None, "stations": mine}

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
    def assign_ticket(uid: str, ref: str, request: Request, body: dict = Body(...)):
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
        """{stage, reason?, pr?, checks?}: move the ticket. Only the station holding it (or a helper
        it started) moves it; only the releaser marks it shipped; a person's own session may do
        anything. `checks` ({command, result, commit?, broke_test?} each) are recorded in the same
        call, so a builder asking for review need not record them one by one. `pr` (a PR URL or owner/repo#12) links the ticket to its pull request, read
        from GitHub from then on. A Tares ticket's status follows its stage; a Linear or GitHub
        ticket's status stays its home's."""
        h.project_or_404(uid)
        t = ticket_or_404(uid, ref)
        try:
            stage, reason = factory_mod.stage_ok(body.get("stage"), body.get("reason"))
        except docs_mod.DocError as e:
            h.err(e)
        why = factory_mod.may_move(caller(request), t, stage)
        if why:
            h.err(PermissionError(why), 403)
        checks = parse_checks(body) if body.get("checks") else []
        fields: dict = {"stage": stage, "stage_reason": reason or None}
        if t["owner"] == "tares":
            fields["status"] = factory_mod.STATUS_OF_STAGE[stage]
        pr_ref = str(body.get("pr") or "").strip()
        if pr_ref:
            parsed = factory_github.parse_pr(pr_ref)
            if parsed is None:
                h.err(ValueError("pr is the pull request's URL (https://github.com/o/r/pull/12) "
                                 "or o/r#12"))
            prev = t["pr"] if t["pr"] and (t["pr"].get("repo"), t["pr"].get("number")) == parsed else None
            fields["pr"] = await factory_github.read_pr(store, parsed[0], parsed[1], prev)
        if checks:
            record_checks(uid, t, request, body, checks)
            t = ticket_or_404(uid, t["id"])
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
    def ticket_messages(uid: str, ref: str):
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

    def assumption_out(uid: str, a: dict, by_id: dict | None = None) -> dict:
        if by_id is None:
            by_id = {t["id"]: t for t in store.list_tickets(uid)}
        return {**a, "tickets": [factory_mod.label(by_id[t]) for t in a["tickets"] if t in by_id]}

    @app.post("/api/projects/{uid}/assumptions", status_code=201)
    def add_assumption(uid: str, request: Request, body: dict = Body(...)):
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
    def list_assumptions(uid: str, state: str = ""):
        h.project_or_404(uid)
        st = state.strip().lower() or None
        if st and st not in factory_mod.ASSUMPTION_STATES:
            h.err(ValueError("state is one of: " + ", ".join(factory_mod.ASSUMPTION_STATES)))
        by_id = {t["id"]: t for t in store.list_tickets(uid)}
        return {"assumptions": [assumption_out(uid, a, by_id)
                                for a in store.list_assumptions(uid, st)]}

    @app.post("/api/projects/{uid}/assumptions/{ref}/state")
    def decide_assumption(uid: str, ref: str, request: Request, body: dict = Body(...)):
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

    def parse_checks(body: dict) -> list[tuple[str, str, str, str]]:
        """The checks a body carries: {command, result, commit?, broke_test?} itself, and `checks`,
        a list of the same, for the ones recorded together (TR-683)."""
        items = ([body] if body.get("command") is not None or body.get("result") is not None else [])
        more = body.get("checks")
        if more is not None and not isinstance(more, list):
            h.err(ValueError("checks is a list of {command, result, commit?, broke_test?}"))
        items += [x if isinstance(x, dict) else {} for x in more or []]
        if not items:
            h.err(ValueError("say the command you ran"))
        try:
            return [factory_mod.check_ok(x.get("command"), x.get("result"), x.get("commit"),
                                         x.get("broke_test")) for x in items]
        except docs_mod.DocError as e:
            h.err(e)

    def record_checks(uid: str, t: dict, request: Request, body: dict,
                      checks: list[tuple[str, str, str, str]]) -> tuple[list[str], str]:
        by, _ = who(request, body)
        lines = []
        for cmd, result, commit, broke in checks:
            store.add_check(uid, t["id"], cmd, result, commit, broke, caller(request)["station"],
                            "", now_utc())
            lines.append(factory_mod.check_line(cmd, result, commit, broke))
        doc_id = t["working_doc"]
        if doc_id and store.get_doc(uid, doc_id):
            body_now = store.get_doc(uid, doc_id)["body"]
            for line in lines:
                body_now = factory_mod.add_check_to_doc(body_now, line)
            store.update_doc(doc_id, body=body_now, by=by)
        else:
            body_now = f"# {t['title']}\n"
            for line in lines:
                body_now = factory_mod.add_check_to_doc(body_now, line)
            doc_id = store.create_doc(uid, "working", f"Working: {t['title']}", body_now, by=by)
            store.update_ticket(t["id"], working_doc=doc_id)
        return lines, doc_id

    @app.post("/api/projects/{uid}/tickets/{ref}/checks", status_code=201)
    def add_check(uid: str, ref: str, request: Request, body: dict = Body(...)):
        """{command, result, commit?, broke_test?}: a check run on the ticket, and `checks`, more
        of the same recorded in this call. Kept, and added as one line each to the Checks part of
        the ticket's working doc (made when there is none)."""
        p = h.project_or_404(uid)
        t = ticket_or_404(uid, ref)
        lines, doc_id = record_checks(uid, t, request, body, parse_checks(body))
        return {"line": lines[0], "lines": lines, "working_doc": doc_id,
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
    def remember_for_project(uid: str, request: Request, body: dict = Body(...)):
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
    def record_review(uid: str, ref: str, request: Request, body: dict = Body(...)):
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
        # the tickets in review, their milestones' other tickets, when each went to review and
        # its last review: one query each, however many are waiting (TR-648)
        waiting = store.review_tickets()
        ids = [t["id"] for _, t in waiting]
        siblings = store.milestone_statuses(sorted({t["milestone"] for _, t in waiting
                                                    if t["milestone"]}))
        entered = store.entered_stage(ids, "review")
        lasts = store.last_reviews(ids)
        out = []
        for pname, t in waiting:
            uid = t["project"]
            # it blocks its milestone when it is the last of the milestone's tickets open
            others = [x for x in siblings if x["project"] == uid
                      and x["milestone"] == t["milestone"] and x["id"] != t["id"]]
            blocks = bool(t["milestone"]) and all(
                x["status"] in factory_mod.DONE_STATUSES for x in others)
            since = entered.get(t["id"], t["updated_at"])
            layer = factory_mod.challenger_layer(
                store.challenge_events(h.cc_source(), uid, factory_mod.label(t)))
            v = verdict_now(t, lasts.get(t["id"]))
            out.append({"project": uid, "project_name": pname, "ticket": factory_mod.label(t),
                        "title": t["title"], "holder": t["holder"],
                        "repo": t.get("repo") or (t["pr"] or {}).get("repo"),
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
    def review_queue():
        """Tickets in review across every project: those blocking their milestone first, then
        the longest waiting (TR-444)."""
        return {"queue": queue(), "now": now_utc()}

    # ── the repo map (TR-681): one per GitHub repo, shared by every project that names it ──
    def pr_repo(t: dict) -> str | None:
        """The repo of a ticket's PR, once GitHub has answered for it: a repo typed into a PR link is
        not yet a repo anyone has seen."""
        pr = t.get("pr") or {}
        return pr.get("repo") if pr.get("checked_at") and not pr.get("error") else None

    def repo_of(uid: str, t: dict) -> str | None:
        """The repo a ticket changes: its own, its PR's, else the project's only repo (when it has a
        GitHub name). With several repos, or one without a name, a ticket that names none has none."""
        repo = t.get("repo") or pr_repo(t)
        if repo:
            return repo
        repos = (store.get_handover(uid) or {}).get("repos") or []
        return repos[0].get("name") or None if len(repos) == 1 else None

    def repo_map_md(repo: str | None, builder: bool) -> list[str]:
        if not repo:
            return []
        m = store.get_repo_map(repo)
        if not m:
            if not builder:
                return []
            return [f"## Repo map: {repo}",
                    "There is none yet, and you are the first builder here. Once you know the layout, "
                    "write it with write_repo_map (layout, entry points, how to install, build, test "
                    f"and lint, conventions, the files tickets touch most, known traps; at most "
                    f"{factory_mod.MAP_MAX_CHARS} characters). Every project on this repo then starts "
                    "from it.", ""]
        version = factory_mod.map_version(m["body"])
        L = [f"## Repo map: {repo} (shared by every project on it; version {version}, changed "
             f"{str(m['updated_at'])[:10]} by {m['updated_by']})", m["body"].strip()]
        recent = store.repo_map_history(repo, 3)
        if recent:
            L += ["Recent changes to the map:"] + [f"- {str(x['at'])[:10]} {x['by']}: {x['note']}"
                                                   for x in recent]
        if builder:
            L += ["If your pull request changes the layout or the build, test or lint commands, "
                  f"update the map with write_repo_map(base=\"{version}\") after it merges. If someone "
                  "changed the map meanwhile, the answer is the map now: fold your change into it and "
                  "write again."]
        return L + [""]

    @app.get("/api/repo-map")
    def get_repo_map(repo: str):
        try:
            name = factory_mod.repo_ok(repo)
        except docs_mod.DocError as e:
            h.err(e)
        m = store.get_repo_map(name) if name else None
        return {"repo": name, "map": m, "history": store.repo_map_history(name, 10) if m else [],
                "max_chars": factory_mod.MAP_MAX_CHARS}

    def named_repos() -> set[str]:
        """Every GitHub repo some project names: on its hand-over, or on a ticket (its repo, else its PR's)."""
        out: set[str] = set()
        for p in store.list_projects():
            for r in (store.get_handover(p["id"]) or {}).get("repos") or []:
                if r.get("name"):
                    out.add(r["name"].lower())
            for t in store.list_tickets(p["id"]):
                repo = t.get("repo") or pr_repo(t)
                if repo:
                    out.add(repo.lower())
        return out

    @app.put("/api/repo-map")
    def put_repo_map(request: Request, body: dict = Body(...)):
        """{repo, body, base, note, project?}: replace the repo's map. Builders (and a person's own
        session) write it; `base` is the version of the map they read (none when there was none),
        `note` becomes a dated line in the map's history. The repo must be one a project names."""
        deny(request, "repo_map")
        try:
            name = factory_mod.repo_ok(body.get("repo"))
            if not name:
                raise docs_mod.DocError("say which repo the map is for (owner/name)")
            text, note = factory_mod.map_ok(body.get("body"), body.get("note"))
        except docs_mod.DocError as e:
            h.err(e)
        if name.lower() not in named_repos():
            raise HTTPException(404, f"no project names {name}: hand a project over with that repo, "
                                     "or give one of its tickets that repo, before writing its map")
        by, _ = who(request, body)
        try:
            changed = store.set_repo_map(name, text, note, by, str(body.get("project") or "") or None,
                                         now_utc(), str(body.get("base") or "").strip())
        except MapChanged as e:
            cur = e.current
            if not cur:
                raise HTTPException(409, "there is no map for this repo yet: write it with an empty base")
            raise HTTPException(409, (
                f"the map changed since you read it (now version {factory_mod.map_version(cur['body'])}, "
                f"changed {str(cur['updated_at'])[:10]} by {cur['updated_by']}), or you gave no base. "
                "Nothing was written. Fold your change into this map and write again with that "
                f"version as base:\n\n{cur['body']}"))
        return {"repo": name, "changed": changed, "chars": len(text), "version": factory_mod.map_version(
                    store.get_repo_map(name)["body"]),
                "max_chars": factory_mod.MAP_MAX_CHARS, "history": store.repo_map_history(name, 5)}

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
        repo = t.get("repo") or pr.get("repo")
        L += ["## The ticket and its working doc",
              f"Milestone: {full.get('milestone_name') or 'none'}. Held by {t['holder'] or 'nobody'}."
              + (f" Repo: {repo}." if repo else ""),
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
        # 8. the repo map: read it instead of exploring the repo
        L += repo_map_md(repo_of(p["id"], t), builder=False)
        # 8. links, not inlined
        L += ["## Read as needed",
              "- The spec, AGENTS.md (the project's and the shared one) and the project memory: "
              "list_docs, then get_doc(id).", "- The crew's messages about it: get_ticket."]
        return "\n".join(L)

    # ── the builder brief: one read at the start of a ticket and after a revive (TR-683) ──
    @app.get("/api/projects/{uid}/tickets/{ref}/builder-brief")
    async def builder_brief(uid: str, ref: str, request: Request, start: bool = False):
        """Everything a builder needs to start or resume one ticket, as markdown. `start` also
        moves a ticket that has not been started (no stage, or todo, and not done or canceled) to doing; a ticket already
        past that (review, changes, blocked, merged) keeps its stage."""
        p = h.project_or_404(uid)
        await refresh_prs(store, uid)
        t = ticket_or_404(uid, ref)
        note = ""
        if start and (t["stage"] or "todo") == "todo" and not factory_mod.finished(t):
            try:
                await set_stage(uid, ref, request, {"stage": "doing"})
            except HTTPException as e:
                note = f"Could not move the ticket to doing: {e.detail}\n\n"
            t = ticket_or_404(uid, ref)
        return {"brief": note + builder_brief_md(p, t)}

    def builder_brief_md(p: dict, t: dict) -> str:
        uid, lab = p["id"], factory_mod.label(t)
        full = h.ticket_out(uid, t, with_doc=True)
        pr = t["pr"] or {}
        repo = repo_of(uid, t)
        L = [f"# Builder brief: {lab} {t['title']} ({p['name']})", ""]
        # 1. where it stands
        L += ["## Where it stands",
              f"Stage {t['stage'] or 'none'}" + (f" ({t['stage_reason']})" if t.get("stage_reason") else "")
              + f". Held by {t['holder'] or 'nobody'}." + (f" Repo: {repo}." if repo else "")
              + (f" Link: {t['url']}." if t.get("url") else "")]
        if pr.get("url"):
            L += [f"- Pull request {pr['url']}, head {str(pr.get('head') or '?')[:12]}, "
                  f"CI {pr.get('ci') or 'unknown'}" + (", merged" if pr.get("merged") else "")]
        if full.get("pr_note"):
            L += [f"- {full['pr_note']}"]
        v = full.get("verdict") or {}
        if v.get("verdict"):
            L += [f"- Reviewer's verdict: {v['verdict']}" + (f". {v['note']}" if v.get("note") else "")]
        L.append("")
        # 2. what was asked: the ticket's working doc
        L += ["## The ticket and its working doc",
              (full.get("working_doc_body") or "(no working doc)").strip(), ""]
        # 3. the milestone it belongs to and the checks that close it
        ms = store.get_milestone(uid, t["milestone"]) if t.get("milestone") else None
        L += [f"## Milestone: {ms['name']}" if ms else "## Milestone", ]
        if ms:
            L += ([ms["goal"]] if ms["goal"] else [])
            L += [f"- Acceptance check: `{c.get('check')}` should show: {c.get('expect')}"
                  for c in ms["checks"]] or ["No acceptance checks written."]
        else:
            L += ["The ticket is in no milestone."]
        L.append("")
        # 4. what it depends on
        by_id = {x["id"]: x for x in store.list_tickets(uid)}
        deps = [by_id[d] for d in t["depends_on"] if d in by_id]
        L += ["## Dependencies"]
        if not deps:
            L += ["None."]
        for d in deps:
            dpr = d["pr"] or {}
            L += [f"- {factory_mod.label(d)} {d['title']}: {d['stage'] or d['status']}"
                  + (f", {dpr['url']}" if dpr.get("url") else "")
                  + ("" if factory_mod.delivered(d) else
                     " (CANCELED: it delivered nothing and still blocks this ticket)"
                     if d["status"] == "canceled" else " (NOT DONE: it blocks this ticket)")]
        L.append("")
        # 5. the rules: the project's AGENTS.md wins over the generic flow, then the shared one
        gids = h.global_ids(False) or {}
        agents = store.list_docs(uid, "agents")
        mine = [d for d in agents if d["id"] not in gids.values()]
        shared = [d for d in agents if d["id"] == gids.get("agents")]
        for title, docs in (("AGENTS.md of this project (wins over the generic flow)", mine),
                            ("AGENTS.md shared by every project", shared)):
            for d in docs:
                L += [f"## {title}", (store.get_doc(uid, d["id"])["body"] or "").strip(), ""]
        if not (mine or shared):
            L += ["## AGENTS.md", "This project has none.", ""]
        L += repo_map_md(repo, builder=True)
        # 6. what is open on it: assumptions to honour, questions waiting for the person
        asm = [a for a in full.get("assumptions") or [] if a["state"] != "kept"]
        if asm:
            L += ["## Assumptions on this ticket (open, or overturned: follow the person's words)"]
            L += [f"- {a['label']} ({a['state']}): {a['question']} -> {a['choice']}"
                  + (f". The person: \"{a['words']}\"" if a.get("words") else "") for a in asm] + [""]
        desk = [d for d in store.list_desk("open", uid) if lab in (d.get("tickets") or [])]
        if desk:
            L += ["## Questions on the desk about this ticket"]
            L += [f"- {d['label']}{' (blocking)' if d['blocking'] else ''}: {d['question']}. "
                  f"Recommended: {d['recommendation']}" for d in desk] + [""]
        # 7. where you left off: for a revive, and a ticket sent back
        checks = full.get("checks") or []
        layer = full.get("challenger") or {}
        reviews = full.get("reviews") or []
        if checks or layer.get("rounds") or reviews:
            L += ["## Where you left off"]
            L += [f"- Check recorded: `{c['command']}` -> {c['result']}"
                  + (f" (broke {c['broke_test']})" if c.get("broke_test") else "") for c in checks[-8:]]
            if layer.get("rounds"):
                L += [f"- Codex: {layer['rounds']} rounds, last {layer.get('verdict')}, "
                      f"{len(layer.get('open') or [])} open, {len(layer.get('waived') or [])} waived"]
            for r in reviews[-2:]:
                L += [f"- Review round {r['round']} on {r['head'][:7]}: {r['verdict']}"]
                L += [f"  - {f['label']} [{f['severity']}] {f['state']}: {f['text']}"
                      for f in r["findings"] if f["state"] == "open"]
            L.append("")
        msgs = messages_for(p, t)[-5:]
        if msgs:
            L += ["## The crew's latest messages about it"]
            L += [f"- {m['from'] or '?'} -> {m['to']}: [TF:{m['type']}] {m['first_line']}" for m in msgs]
            L.append("")
        # 8. links, not inlined
        L += ["## Read as needed",
              "- The spec, plan and the project memory: list_docs, then get_doc(id).",
              "- The ticket's full history: get_ticket."]
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
    def recurring_findings(repo: str):
        return {"repo": repo, "recurring": recurring(repo.strip())}

    @app.get("/api/rule-proposals")
    def list_rule_proposals(state: str = "open"):
        return {"proposals": store.list_rule_proposals(state.strip() or None)}

    @app.post("/api/rule-proposals/{pid}/{decision}")
    def decide_rule(pid: str, decision: str, request: Request,
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
        return {**d, "project_name": p["name"] if p else None,
                "project_finished": bool(p and p.get("status") == "done")}

    def close_project_desk(uid: str) -> None:
        """A finished project's open questions have nothing left to decide (TR-559)."""
        for d in store.list_desk("open", uid):
            store.close_desk_item(d["id"], "withdrawn", "the project is finished", now_utc())

    @app.post("/api/desk", status_code=201)
    def desk_add(request: Request, body: dict = Body(...)):
        """{question, recommendation, why, options?, context?, blocks?, tickets?, blocking?,
        assumptions?, project?, station?, command?}: a question for the person, numbered D1,
        D2... for the whole cell. With `command` (and the `station` that was refused it) it is a
        permission request, which the person can allow from the console. Only the orchestrator
        (or a person's own session) writes the desk."""
        deny(request, "desk")
        try:
            q, rec, why, opts, ctx, station, command = factory_mod.desk_ok(
                body.get("question"), body.get("recommendation"), body.get("why"),
                body.get("options"), body.get("context"), body.get("station"),
                body.get("command"))
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
                                asm, now_utc(), station, command)
        return desk_out(d)

    @app.get("/api/desk")
    def desk_list(state: str = "open", project: str = ""):
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
    def desk_withdraw(ref: str, request: Request, body: dict = Body(default={})):
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
        choice = str(body.get("choice") or "").strip()
        # a reply of one letter picks an option by position; Tares maps it, never the model
        letter = factory_mod.option_letter(words)
        if letter is not None:
            opts = d["options"] or []
            if letter >= len(opts):
                h.err(ValueError(
                    f"{words} is not one of {d['label']}'s options"
                    + (": " + ", ".join(f"{factory_mod.OPTION_LETTERS[i]}) {o}"
                                        for i, o in enumerate(opts)) if opts else "; it has none")))
            picked = opts[letter]
            if choice and factory_mod._plain(choice) not in (
                    factory_mod._plain(picked), factory_mod._plain(words)):
                h.err(ValueError(f"{words} is {factory_mod.OPTION_LETTERS[letter]}) {picked}, "
                                 f"not {choice!r}"))
            choice = picked
        from datetime import timedelta
        import asyncio
        # only what the person typed after the question was asked can answer it
        since = max(d["created_at"], now_utc() - timedelta(hours=factory_mod.MATCH_HOURS))
        labeled = bool(caller(request)["station"])
        sessions = store.role_sessions("orchestrator") if labeled else None
        kept = [str(a) for a in body.get("kept") or []]
        over = body.get("overturned") or {}
        if not isinstance(over, dict):
            h.err(ValueError("overturned is {A<n>: the person's words}"))

        def look():
            turns = store.person_turns(h.cc_source(), sessions, since)
            hit = (factory_mod.letter_match(letter, turns) if letter is not None
                   else factory_mod.words_match(words, turns))
            heard = all(factory_mod.words_match(str(w), turns)
                        or factory_mod._plain(str(w)) in factory_mod._plain(words)
                        for w in over.values())
            return turns, hit, heard

        turns, hit, heard = look()
        # the session is read a few seconds behind the person: look again before refusing
        waited = 0.0
        while (hit is None or not heard) and waited < factory_mod.MATCH_WAIT_SECONDS:
            await asyncio.sleep(1.0)
            waited += 1.0
            turns, hit, heard = look()
        # an assumption batch: every listed assumption kept or overturned, with the person's words
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
                                 choice[:300], standing, scope,
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

    @app.post("/api/desk/{ref}/allow")
    def desk_allow(ref: str, request: Request, body: dict = Body(...)):
        """{words, scope: project|all}: the person allows a permission request (a desk item with
        a command) from the console. `words` is the grant as they let it stand (the console
        offers a sentence naming the station and the command; they may edit it). It is kept as a
        grant, for the item's project or for every project, and recorded as their decision made
        in the console; the item is answered. The crew's watch restarts the station with it. A
        person decides: refused from any crew station."""
        if caller(request)["station"]:
            h.err(PermissionError("a permission is the person's to allow: they allow it in the "
                                  "console"), 403)
        d = desk_or_404(ref)
        if d["state"] != "open":
            h.err(ValueError(f"{d['label']} is already {d['state']}"), 409)
        if not d.get("command"):
            h.err(ValueError(f"{d['label']} is a question, not a permission request: answer it "
                             "in the orchestrator's session"), 409)
        scope = str(body.get("scope") or "").strip().lower()
        if scope not in ("project", "all"):
            h.err(ValueError("scope is project or all"))
        if scope == "project" and not d["project"]:
            h.err(ValueError(f"{d['label']} has no project: allow it for every project"))
        # A crew station (crew-reviewer, crew-releaser and their helpers) serves every project and
        # starts with only the grants for every project: one kept for a project never reaches it.
        if scope == "project" and str(d["station"] or "").startswith("crew-"):
            h.err(ValueError(f"{d['station']} works for every project, so a grant for one project "
                             "never reaches it: allow it for every project"))
        if scope == "project" and store.get_project(d["project"]) is None:
            h.err(ValueError("the project this was asked for is gone; allow it for every "
                             "project"), 409)
        words = " ".join(str(body.get("words") or "").split()).strip().strip('"')
        try:
            line = factory_mod.grant_line(words, now_utc().date().isoformat())
        except docs_mod.DocError as e:
            h.err(e)
        added = append_grant(d["project"] if scope == "project" else None, line,
                             "the person, in the console")
        dec = store.add_decision(d["project"], d["id"], d["question"], words, "allowed", True,
                                 scope, False, None, True, d["tickets"], now_utc(),
                                 via="console")
        store.close_desk_item(d["id"], "answered", None, now_utc())
        return {"desk_item": desk_out(desk_or_404(d["id"])), "decision": dec,
                "station": d["station"], "line": line, "added": added, "scope": scope}

    @app.get("/api/projects/{uid}/decisions")
    def project_decisions(uid: str):
        """The person's answers on this project, newest first."""
        h.project_or_404(uid)
        return {"decisions": store.list_decisions(uid)}

    @app.post("/api/projects/{uid}/handover")
    def hand_over(uid: str, request: Request, body: dict = Body(default={})):
        """{repos?: [{path, name}], repo?, autonomy?}: the spec session hands the project to the
        crew. Each repo is the folder it is checked out in on the crew's machine and its GitHub
        name (owner/name). A later hand-over adds repos, never replaces them. `autonomy` only
        when the person wants this project stricter than the crew (L3-review). Recorded with
        the time; the orchestrator finds it in /api/crew/projects."""
        h.project_or_404(uid)
        try:
            new = factory_mod.handover_repos(body.get("repos"))
            if body.get("repo"):
                new = factory_mod.merge_repos(new, factory_mod.handover_repos(body.get("repo")))
            autonomy = str(body.get("autonomy") or "").strip() or None
            if autonomy and autonomy not in factory_mod.AUTONOMY:
                raise docs_mod.DocError(f"unknown autonomy {autonomy!r}; one of: "
                                        + ", ".join(factory_mod.AUTONOMY))
        except docs_mod.DocError as e:
            h.err(e)
        old = store.get_handover(uid) or {}
        repos = factory_mod.merge_repos(old.get("repos") or [], new)
        autonomy = autonomy or old.get("autonomy")
        store.set_handover(uid, repos, autonomy, h.by(request, body), now_utc())
        if old.get("finished"):   # more work on a finished project: it is open again
            store.update_project(uid, status="active")
        include_shared_grants(uid)   # a crew-built project shows the grants it is built under
        return {"handover": store.get_handover(uid)}

    @app.post("/api/projects/{uid}/takeover")
    def take_over(uid: str, request: Request, body: dict = Body(default={})):
        """{words}: the person takes the project back from the crew to build it in their own
        session (`tares factory session <project>`, TR-520). Only the person, never a station.
        The project leaves the crew's work list at once and is listed under `releasing` in
        /api/crew/projects until the orchestrator has stopped its builders (they push what they
        have and write their progress first) and calls release. Asking again while it is being
        released changes nothing."""
        deny(request, "takeover")
        p = h.project_or_404(uid)
        ho = store.get_handover(uid)
        if not ho:
            h.err(ValueError(f"{p['name']} is not with the crew; there is nothing to take over"),
                  409)
        if ho.get("finished"):
            h.err(ValueError(f"{p['name']} is finished; hand it to the crew again or work on it "
                             "in a session without taking it over"), 409)
        if not ho.get("takeover"):
            words = " ".join(str(body.get("words") or "").split())[:500] or \
                "Taken over by the person with tares factory session"
            by, _ = who(request, body)
            store.take_over(uid, words, by, now_utc())
        return {"project": uid, "name": p["name"], "takeover": store.get_handover(uid)["takeover"],
                "holders": sorted({t["holder"] for t in store.list_tickets(uid) if t["holder"]})}

    @app.post("/api/projects/{uid}/release")
    def release_project(uid: str, request: Request):
        """The orchestrator has stopped the builders of a project the person took over: the
        person's session owns it now. Clears every ticket's holder (the stages, pull requests and
        working docs stay as the builders left them). The person's own session may release it too,
        when no orchestrator runs."""
        deny(request, "release")
        p = h.project_or_404(uid)
        ho = store.get_handover(uid) or {}
        tk = ho.get("takeover")
        if not tk:
            h.err(ValueError(f"{p['name']} was not taken over; there is nothing to release"), 409)
        if not tk.get("released_at"):
            by, role = who(request)
            at = now_utc()
            for t in store.list_tickets(uid):
                if t["holder"]:
                    store.update_ticket(t["id"], holder=None)
                    store.add_ticket_history(t["id"], uid, "holder", None,
                                             "released to the person's session", by, role, at)
            store.release_takeover(uid, at)
        return {"project": uid, "name": p["name"],
                "takeover": store.get_handover(uid)["takeover"]}

    def pr_row(t: dict, last: dict | None) -> dict:
        """A ticket's pull request as the GitHub section shows it: what GitHub said when Tares last
        read it (title, CI, the factory/review status), and the crew's own last verdict (`last`,
        its latest review round)."""
        pr = t["pr"] or {}
        v = verdict_now(t, last)
        return {"url": pr["url"], "ticket": factory_mod.label(t), "number": pr.get("number"),
                "title": pr.get("title") or t["title"], "branch": pr.get("branch"),
                "head": (pr.get("head") or "")[:7] or None, "ci": pr.get("ci"),
                "github_verdict": pr.get("verdict"), "stage": t["stage"], "holder": t["holder"],
                "review": ({"verdict": v["verdict"], "round": v["round"], "current": v["current"]}
                           if v else None),
                "merged_at": pr.get("merged_at"), "checked_at": pr.get("checked_at"),
                "error": pr.get("error")}

    def repo_rows(uid: str, repos: list[dict], tickets: list[dict] | None = None) -> list[dict]:
        """Each hand-over repo with the tickets that change it, their open pull requests (newest
        first) and the ones merged. A ticket with no repo belongs to the first one; a repo only
        tickets name is added. `github_error` says when Tares could not read GitHub for it.
        `tickets` is the project's tickets when the caller already read them."""
        tickets = store.list_tickets(uid) if tickets is None else tickets
        lasts = store.last_reviews([t["id"] for t in tickets if (t["pr"] or {}).get("url")])
        rows = [{**r, "tickets": [], "open_prs": [], "merged_prs": [], "github_error": None}
                for r in repos]
        for t in tickets:
            name = t.get("repo") or (t["pr"] or {}).get("repo")
            row = next((r for r in rows if name and (r["name"] or "").lower() == name.lower()),
                       None)
            if row is None and name:
                row = {"path": None, "name": name, "tickets": [], "open_prs": [], "merged_prs": [],
                       "github_error": None}
                rows.append(row)
            if row is None and rows:
                row = rows[0]
            if row is None:
                continue
            row["tickets"].append(factory_mod.label(t))
            pr = t["pr"] or {}
            # merged as far as the ticket knows (a builder sets it when Tares can't read GitHub)
            done = (t["stage"] in ("merged", "shipped")
                    or t["status"] in factory_mod.DONE_STATUSES)
            if not pr.get("url"):
                continue
            if pr.get("error"):
                row["github_error"] = pr["error"]
            if pr.get("merged") or (done and pr.get("state") != "closed"):
                row["merged_prs"].append(pr_row(t, lasts.get(t["id"])))
            elif pr.get("state") != "closed":
                row["open_prs"].append(pr_row(t, lasts.get(t["id"])))
        for row in rows:
            row["open_prs"].sort(key=lambda p: -(p["number"] or 0))
            row["merged_prs"].sort(key=lambda p: -(p["number"] or 0))
        return rows

    @app.post("/api/projects/{uid}/finish")
    def finish_project(uid: str, request: Request, body: dict = Body(...)):
        """{decision} or {words}: the person said the project is done. A station names the desk
        item (`decision`, D<n>) that asked them, answered in the orchestrator's session: its words,
        matched against what they typed, are what is kept. The person's own session (no station:
        `tares factory finish` in their terminal) may give `words` instead. Refused while a ticket
        is still open. The project's status becomes done and it leaves the crew's work list; a
        later hand-over opens it again."""
        deny(request, "finish")
        p = h.project_or_404(uid)
        ho = store.get_handover(uid)
        if not ho and caller(request)["station"]:
            h.err(ValueError(f"{p['name']} was never handed to the crew; there is nothing to finish"))
        if ho and ho.get("finished"):
            h.err(ValueError(f"{p['name']} was already finished "
                             f"({ho['finished']['decision'] or 'by hand'})"), 409)
        open_ = [factory_mod.label(t) for t in store.list_tickets(uid)
                 if not (t["stage"] in ("merged", "shipped")
                         or t["status"] in factory_mod.DONE_STATUSES)]
        if open_:
            h.err(ValueError(f"{p['name']} still has open tickets: {', '.join(open_)}. Finish or "
                             "cancel them first"), 409)
        if not ho:
            # built in the person's own session, never by the crew (tares factory session, TR-520):
            # their words are what the session typed for them; the project is done
            if p.get("status") == "done":
                h.err(ValueError(f"{p['name']} was already finished"), 409)
            words = " ".join(str(body.get("words") or "").split())[:500] or \
                "Marked done by the person"
            by, _ = who(request, body)
            store.update_project(uid, status="done")
            close_project_desk(uid)
            return {"project": uid, "name": p["name"],
                    "finished": {"at": now_utc(), "words": words, "decision": None, "by": by},
                    "open_projects": len(store.handed_over_projects(finished=False, taken=False))}
        ref = str(body.get("decision") or "").strip()
        by, _ = who(request, body)
        if not ref and not caller(request)["station"]:
            # the person, in their own terminal: their words, or that they marked it themselves
            words = " ".join(str(body.get("words") or "").split())[:500] or \
                "Marked done by the person with tares factory finish"
            store.finish_handover(uid, words, None, by, now_utc())
            store.update_project(uid, status="done")
            close_project_desk(uid)
            return {"project": uid, "name": p["name"], "finished": store.get_handover(uid)["finished"],
                    "open_projects": len(store.handed_over_projects(finished=False, taken=False))}
        if not ref:
            h.err(ValueError("name the desk item where the person said the project is done "
                             "(decision=D<n>): ask them on the desk first, then record their answer"))
        d = store.get_desk_item(ref)
        if d is None:
            h.err(KeyError(f"no desk item {ref!r}"), 404)
        if d["project"] not in (uid, None):
            h.err(ValueError(f"{d['label']} was asked about another project"), 409)
        dec = next((x for x in store.list_decisions(None) if x["desk_item"] == d["id"]), None)
        if d["state"] != "answered" or dec is None or not dec["matched"]:
            h.err(ValueError(f"{d['label']} has no answer from the person yet: record their words "
                             "with desk_answer first"), 409)
        store.finish_handover(uid, dec["words"], d["label"], by, now_utc())
        store.update_project(uid, status="done")
        close_project_desk(uid)
        return {"project": uid, "name": p["name"], "finished": store.get_handover(uid)["finished"],
                "open_projects": len(store.handed_over_projects(finished=False, taken=False))}

    @app.get("/api/projects/{uid}/repos")
    def project_repos(uid: str):
        """The repos this project touches, each with its tickets and open pull requests
        (TR-463). Empty for a project nobody handed over and whose tickets name no repo."""
        h.project_or_404(uid)
        ho = store.get_handover(uid) or {}
        rows = repo_rows(uid, ho.get("repos") or [])
        return {"repos": [r for r in rows if r["path"] or r["name"]]}
