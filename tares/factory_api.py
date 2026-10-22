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
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from fastapi import Body, Request

from . import areas as areas_mod
from . import claude_usage
from . import docs as docs_mod
from . import factory as factory_mod
from . import factory_github
from .envelope import now_utc

CREW_SETTING = "crew_settings"
GRANTS_SETTING = "global_grants"   # the id of the cell's "Grants (all projects)" doc
GRANTS_MIGRATED_SETTING = "grants_migrated"   # set once the old grants docs became rows (TR-630)


def caller(request: Request) -> dict:
    """{station, role, parent} the request says it comes from ("" each when unlabeled)."""
    h = request.headers
    return {"station": (h.get("x-tares-station") or "").strip()[:factory_mod.MAX_NAME],
            "role": (h.get("x-tares-role") or "").strip().lower(),
            "parent": (h.get("x-tares-parent") or "").strip()[:factory_mod.MAX_NAME]}


def crew_name_of(request: Request, said: str | None = None) -> str:
    """The crew the call means (TR-633): `said` (the `crew` parameter), else the X-Tares-Crew
    header, else `crew`, the name a crew has when nobody named it. Raises ValueError for a name
    that cannot be a crew's."""
    name = str(said or request.headers.get("x-tares-crew") or "").strip() or "crew"
    if not factory_mod.CREW_NAME_RE.match(name):
        raise ValueError(f"{name!r} is not a crew name: lowercase letters, digits and hyphens")
    return name


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


def project_progress(store, uid: str) -> dict:
    """Tickets done of all, by stage, and the first milestone with work left: what the crew's
    project list shows (canceled tickets count as done here), and `progress`, the same tickets
    as the console's ring counts them (canceled kept out, tares/factory.py `progress`)."""
    tickets = store.list_tickets(uid)
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
    cc_source(), global_ids(create), current_person(request), is_system(request), is_admin(request),
    area_owner(repo) -> the person who owns that GitHub repo (`owner/name`, any case) or None
    (TR-627); is_admin(request) says whether the credential has the admin scope (the system
    credential does, and so does everyone with auth off)."""

    def deny(request: Request, action: str) -> None:
        why = factory_mod.may(caller(request), action)
        if why:
            h.err(PermissionError(why), 403)

    # ── crew settings (TR-419, TR-624): the cell's default, and each person's own over it ──
    def stored(key: str) -> dict:
        try:
            cur = json.loads(store.get_setting(key) or "{}")
        except ValueError:
            cur = {}
        return {k: cur.get(k) for k in factory_mod.CREW_DEFAULTS}

    def personal_key(person: dict) -> str:
        return f"{CREW_SETTING}:{person['id']}"

    def writes_default(request: Request) -> bool:
        """Does this caller's write change the cell-wide default? The system credential does, and
        the person `local`, and auth off (a single-user cell stays as it was). Anyone else
        changes their own."""
        person = h.current_person(request)
        return h.is_system(request) or (
            (person or {}).get("id") == "local" and h.is_admin(request))

    def settings(request: Request | None = None) -> dict:
        """The cell's default, with the caller's own settings over it where they set one."""
        out = {**factory_mod.CREW_DEFAULTS, **stored(CREW_SETTING)}
        person = h.current_person(request) if request is not None else None
        if person and person.get("id") != "local" and not h.is_system(request):
            mine = stored(personal_key(person))
            out.update({k: v for k, v in mine.items() if v is not None})
            # the team's autonomy and challenger are a ceiling, also when the team tightened
            # them after the person chose theirs
            out.update({k: v for k, v in factory_mod.stricter_than_team(
                stored(CREW_SETTING), out).items() if k in ("autonomy", "challenger")})
        return out

    @app.get("/api/crew/settings")
    async def get_crew_settings(request: Request):
        """The crew's settings for every project: autonomy (L3-review, L4-ship), whether it runs
        a releaser, release profile, prod pattern, challenger on builders, most builders at
        once. Unset ones are null. The caller's own come over the cell's default."""
        return {**settings(request), "autonomy_levels": factory_mod.AUTONOMY}

    @app.put("/api/crew/settings")
    async def put_crew_settings(request: Request, body: dict = Body(...)):
        """Change the fields given; the rest keep their value. The system credential and the
        person `local` change the cell's default; any other person changes their own."""
        deny(request, "grants")
        if writes_default(request):
            key = CREW_SETTING
        else:
            person = h.current_person(request)
            if not person:
                h.err(PermissionError("This key has no person"), 403)
            if person["id"] == "local":
                h.err(PermissionError("The cell's settings need the admin scope"), 403)
            key = personal_key(person)
        try:
            if key != CREW_SETTING:
                factory_mod.personal_ceiling_ok(stored(CREW_SETTING), body)
            out = factory_mod.crew_settings_ok(body, stored(key))
        except docs_mod.DocError as e:
            h.err(e)
        store.set_setting(key, json.dumps(out))
        return {**settings(request), "autonomy_levels": factory_mod.AUTONOMY}

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

    # ── grants by kind (TR-630) ───────────────────────────────────────────────
    # A grant is a row: team (what every project starts from), project (what the work may do) or
    # personal (what may run on the holder's machine). A project grant that names a repo whose
    # area owner is not the giver waits until that owner consents; one from someone who is neither
    # the project's lead nor an admin also waits for the lead (for an admin when the project has
    # no lead). A grant in effect is also written to the grants docs (as before), so what reads
    # those docs keeps working.
    def pending_owners(areas, given_by, consents) -> list[dict]:
        """The owners of `areas` who have still to consent: not the giver, not already consented."""
        done = {c["person_id"] for c in consents}
        out: dict[str, dict] = {}
        for repo in areas:
            o = h.area_owner(repo)
            if o and o["id"] != given_by and o["id"] not in done:
                out[o["id"]] = {"id": o["id"], "name": o["name"]}
        return list(out.values())

    def pending_lead(project, needs_lead, consents) -> list[dict]:
        """The lead who has still to consent, or {id: "admin"} when the project has no lead."""
        if not needs_lead or any(c.get("as") == "admin" for c in consents):
            return []
        lead = (store.get_project(project) or {}).get("lead_person_id") if project else None
        if not lead:
            return [{"id": "admin", "name": "an admin"}]
        if lead in {c["person_id"] for c in consents}:
            return []
        return [{"id": lead, "name": (store._person_refs({lead}).get(lead) or {}).get("name")}]

    def waiting_for(g: dict) -> list[dict]:
        if g["state"] != "waiting":
            return []
        out = pending_owners(g["areas"], g["given_by"], g["consents"])
        have = {w["id"] for w in out}
        return out + [w for w in pending_lead(g["project"], g["needs_lead"], g["consents"])
                      if w["id"] not in have]

    def waits_for_me(g: dict, request: Request) -> str | None:
        """How this caller may consent to g: "person" (they are named), "admin" (the grant waits
        for an admin and the credential has the admin scope), or None."""
        ids = {w["id"] for w in waiting_for(g)}
        person = h.current_person(request)
        if person and person["id"] in ids:
            return "person"
        if "admin" in ids and h.is_admin(request):
            return "admin"
        return None

    def settle(g: dict, by: str) -> dict:
        """A grant still waiting with nobody left to wait for (an owner removed, the repo given
        to the giver) takes effect now."""
        if g["state"] == "waiting" and not waiting_for(g):
            mirror_in(g, by)   # first: a failure leaves it waiting, to settle on the next try
            store.update_grant_row(g["id"], state="active")
            g = store.get_grant_row(g["id"])
        return g

    def settle_on_read(g: dict, request: Request) -> dict:
        """Settle a grant the reader may see, as Tares and not as the reader. One that fails
        stays as it was and does not fail the read."""
        if g["state"] != "waiting" or not visible_grant(g, request):
            return g
        try:
            return settle(g, "tares")
        except Exception:
            return g

    def grant_out(g: dict, refs: dict) -> dict:
        def ref(pid):
            return refs.get(pid) or {"id": pid, "name": None}
        return {"id": g["id"], "kind": g["kind"], "project": g["project"], "words": g["words"],
                "state": g["state"], "given_at": g["given_at"],
                "given_by": ref(g["given_by"]) if g["given_by"] else None,
                "holder": ref(g["holder_person_id"]) if g["holder_person_id"] else None,
                "areas": g["areas"],
                "consents": [{**ref(c["person_id"]), "at": c["at"]} for c in g["consents"]],
                "waiting_for": waiting_for(g), "in_effect": g["state"] == "active"}

    def grants_view(rows: list[dict]) -> list[dict]:
        ids = {g["given_by"] for g in rows} | {g["holder_person_id"] for g in rows}
        ids |= {c["person_id"] for g in rows for c in g["consents"]}
        refs = store._person_refs(ids)
        order = {k: i for i, k in enumerate(factory_mod.GRANT_KINDS)}
        rows = sorted(rows, key=lambda g: (g["state"] != "active", order.get(g["kind"], 9),
                                           int(g["id"][1:])))
        return [grant_out(g, refs) for g in rows]

    _LEGACY_LINE = re.compile(r'^- "(.*)" \((\d{4}-\d{2}-\d{2})\)(?: \(added by .+\))?$')

    def legacy_lines(body: str):
        """(words, day or None) of each line of an old grants doc."""
        for raw in (body or "").splitlines():
            if not raw.startswith("- "):
                continue
            m = _LEGACY_LINE.match(raw.strip())
            if m:
                yield m.group(1), m.group(2)
            elif raw[2:].strip():
                yield raw[2:].strip(), None

    def migrate_grants() -> None:
        """Once: each line of the old grants docs becomes an active team or project grant. The
        docs stay as they are."""
        if store.get_setting(GRANTS_MIGRATED_SETTING):
            return
        have = {(g["kind"], g["project"], g["words"]) for g in store.all_grant_rows()}

        def take(kind: str, uid: str | None, body: str) -> None:
            for words, day in legacy_lines(body):
                if (kind, uid, words) in have:
                    continue
                have.add((kind, uid, words))
                at = (datetime.fromisoformat(day).replace(tzinfo=timezone.utc) if day
                      else now_utc())
                store.add_grant_row(kind, words, project=uid, given_at=at)

        gid = shared_grants_id()
        if gid:
            take("team", None, store.get_doc(None, gid)["body"])
        for p in store.list_projects():
            d = project_grants_doc(p["id"])
            if d:
                take("project", p["id"], store.get_doc(p["id"], d["id"])["body"])
        store.set_setting(GRANTS_MIGRATED_SETTING, "1")

    def mirror_in(g: dict, by: str) -> None:
        """A team or project grant in effect, into the grants doc that readers of the docs use."""
        if g["kind"] == "personal":
            return
        day = g["given_at"].date().isoformat() if g["given_at"] else now_utc().date().isoformat()
        append_grant(g["project"] if g["kind"] == "project" else None,
                     factory_mod.grant_line(g["words"], day), by)

    def mirror_out(g: dict, by: str) -> None:
        """A grant revoked: its lines go from the grants doc (the doc is the old view of it)."""
        if g["kind"] == "personal":
            return
        uid = g["project"] if g["kind"] == "project" else None
        if uid is None:
            doc_id = shared_grants_id()
        else:
            d = project_grants_doc(uid)
            doc_id = d["id"] if d else None
        if not doc_id:
            return
        doc = store.get_doc(None, doc_id) if uid is None else store.get_doc(uid, doc_id)
        keep = [ln for ln in doc["body"].splitlines()
                if not any(w == g["words"] for w, _ in legacy_lines(ln))]
        text = "\n".join(keep) + "\n"
        if text != doc["body"]:
            store.update_doc(doc_id, body=text, by=by)

    def visible_grant(g: dict, request: Request) -> bool:
        """May this caller read this grant? Personal: its holder. Project: whoever can see the
        project. Team: anyone."""
        person = h.current_person(request)
        if g["kind"] == "personal":
            return bool(person and g["holder_person_id"] == person["id"])
        if g["kind"] == "project":
            try:
                h.project_or_404(g["project"])
            except Exception:
                return False
        return True

    def grant_or_404(gid: str, request: Request, consenting: bool = False) -> dict:
        """The grant, or 404. Someone a grant waits for may see it to consent, even on a private
        project they are not in (they get its words and areas, not the project)."""
        g = store.get_grant_row(gid)
        if g is None or not (visible_grant(g, request)
                             or (consenting and g["kind"] == "project"
                                 and waits_for_me(g, request))):
            h.err(KeyError(f"unknown grant {gid!r}"), 404)
        return g

    @app.get("/api/grants")
    async def get_grants(request: Request, project: str = ""):
        """The grants for every project (`all`) and, with `project`, that project's own, as
        markdown (what the docs hold), and `grants`: the team grants, that project's and the
        caller's own personal ones, each {id, kind, words, state, given_by, consents,
        waiting_for, in_effect}, those in effect first. Revoked ones are left out.
        `waiting_for_me` holds the grants of any project that wait for the caller's consent (the
        caller may not see that project), and `known_words` the words of every team and project
        grant in any state, revoked ones too."""
        uid = None
        if project.strip():
            uid = h.project_or_404(h.resolve_project(project.strip()))["id"]
        person = h.current_person(request)
        mine = person["id"] if person else None
        every = [settle_on_read(g, request) for g in store.all_grant_rows()]
        rows = [g for g in every if g["state"] != "revoked" and (
            g["kind"] == "team"
            or (g["kind"] == "project" and uid is not None and g["project"] == uid)
            or (g["kind"] == "personal" and mine is not None and g["holder_person_id"] == mine
                and g["project"] in (None, uid)))]
        mine_to_consent = [g for g in every if g["state"] == "waiting" and g["kind"] == "project"
                           and waits_for_me(g, request)]
        return {**grants_out(uid), "grants": grants_view(rows),
                "waiting_for_me": grants_view(mine_to_consent),
                "known_words": sorted({g["words"] for g in every if g["kind"] == "team" or (
                    g["kind"] == "project" and g["project"] == uid)})}

    def put_grant(kind: str, words: str, uid: str | None, pid: str | None, areas: list[str],
                  needs_lead: bool, by: str) -> tuple[dict, bool]:
        """The grant row for these words, given by `pid`: the one already there when the same
        words are in effect or waiting, else a new one that waits for the owners of `areas` (and
        the lead when `needs_lead`) or takes effect now. Returns (row, added). The caller has
        checked that `pid` may give it."""
        holder = pid if kind == "personal" else None
        twins = [g for g in store.all_grant_rows()
                 if g["state"] != "revoked" and (g["kind"], g["project"], g["words"],
                                                 g["holder_person_id"]) == (kind, uid, words, holder)]
        active = next((g for g in twins if g["state"] == "active"), None)
        waiting = next((g for g in twins if g["state"] == "waiting"), None)
        waits = bool(pending_owners(areas, pid, []) or pending_lead(uid, needs_lead, []))
        if active is not None or (waiting is not None and waits):
            return active or waiting, False
        if waiting is not None:
            # the lead's (or an admin's) own add of words that sit waiting for someone else's
            # consent: it takes effect, with the areas and owner consents of this add
            store.update_grant_row(waiting["id"], state="active", areas=areas, needs_lead=False)
            row = store.get_grant_row(waiting["id"])
            mirror_in(row, by)
            return row, True
        row = store.add_grant_row(kind, words, project=uid, holder=holder, given_by=pid,
                                  areas=areas, state="waiting" if waits else "active",
                                  needs_lead=needs_lead)
        if row["state"] == "active":
            mirror_in(row, by)
        return row, True

    @app.post("/api/grants", status_code=201)
    async def add_grant(request: Request, body: dict = Body(...)):
        """{kind: team|project|personal, words, project?, areas?}: one grant, quoting the person,
        dated today (`scope: all|project` is the older name for kind). Team needs the admin
        scope; project, the project's lead, an area owner for repos they own, or the admin
        scope; personal, a key that belongs to a person, for themselves. A project grant that
        names a repo owned by someone other than the giver waits for that owner, and one from a
        person who is neither the project's lead nor an admin waits for the lead too (for an admin
        when the project has no lead). Only the
        orchestrator among the crew may add one; a person's own session may."""
        deny(request, "grants")
        project_ref = str(body.get("project") or "").strip()
        kind = str(body.get("kind") or body.get("scope") or "").strip().lower()
        kind = "team" if kind == "all" else kind or ("project" if project_ref else "team")
        if kind not in factory_mod.GRANT_KINDS:
            h.err(ValueError("kind is team, project or personal"))
        try:
            words = factory_mod.grant_words(body.get("words"))
        except docs_mod.DocError as e:
            h.err(e)
        raw_areas = body.get("areas") or []
        if not isinstance(raw_areas, list):
            h.err(ValueError("areas is a list of repos, like [\"acme/web\"]"))
        if raw_areas and kind != "project":
            h.err(ValueError("areas belong to a project grant"))
        try:
            areas = list(dict.fromkeys(areas_mod.norm_repo(a) for a in raw_areas))
        except ValueError as e:
            h.err(e)
        person = h.current_person(request)
        pid = person["id"] if person else None
        p = None
        if kind == "project" and not project_ref:
            h.err(ValueError("name the project the grant is for"))
        if kind != "team" and project_ref:
            p = h.project_or_404(h.resolve_project(project_ref))
        needs_lead = False
        if kind == "team":
            if not h.is_admin(request):
                h.err(PermissionError("a team grant needs the admin scope"), 403)
        elif kind == "project":
            owns_all = bool(areas) and pid is not None and all(
                (h.area_owner(a) or {}).get("id") == pid for a in areas)
            leads = bool(pid and p.get("lead_person_id") == pid)
            if not (h.is_admin(request) or leads or owns_all):
                h.err(PermissionError("a project grant comes from the project's lead, or from "
                                      "the owner of the repos it names"), 403)
            needs_lead = not (h.is_admin(request) or leads)
        elif pid is None:
            h.err(PermissionError("a personal grant belongs to a person, and this key has none"),
                  403)
        uid = p["id"] if p else None
        line = factory_mod.grant_line(words, now_utc().date().isoformat())
        row, added = put_grant(kind, words, uid, pid, areas, needs_lead, h.by(request, body))
        return {**grants_out(uid if kind != "personal" else None), "added": added, "line": line,
                "grant": grants_view([row])[0]}

    @app.post("/api/grants/{gid}/consent")
    async def consent_grant(gid: str, request: Request):
        """The owner of a repo the grant names, or the project's lead (an admin when it has none)
        when the giver is neither, says yes. Only someone it is waiting for may, even on a private
        project they are not in; the grant takes effect when the last of them has."""
        deny(request, "grants")
        g = grant_or_404(gid, request, consenting=True)
        person = h.current_person(request)
        if g["state"] != "waiting":
            h.err(ValueError(f"{g['id']} is {g['state']}; there is nothing to consent to"), 409)
        how = waits_for_me(g, request)
        if how is None:
            names = ", ".join(w["name"] or w["id"] for w in waiting_for(g))
            h.err(PermissionError(f"{g['id']} waits for {names or 'no one'}; this key is not "
                                  "one of them"), 403)
        entry = {"person_id": person["id"] if person else "system", "at": now_utc().isoformat()}
        if how == "admin":
            entry["as"] = "admin"
        consents = g["consents"] + [entry]
        store.update_grant_row(g["id"], consents=consents)
        g = settle({**g, "consents": consents}, h.by(request, None))
        if g["state"] == "waiting":
            g = store.get_grant_row(g["id"])
        return {"grant": grants_view([g])[0]}

    @app.post("/api/grants/{gid}/revoke")
    async def revoke_grant(gid: str, request: Request):
        """The giver, the project's lead, the holder of a personal grant, or the admin scope for
        a team or project one, ends it. A revoked grant is never in effect again."""
        deny(request, "grants")
        g = grant_or_404(gid, request)
        person = h.current_person(request)
        pid = person["id"] if person else None
        lead = False
        if g["kind"] == "project":
            lead = bool(pid and (store.get_project(g["project"]) or {}).get("lead_person_id") == pid)
        allowed = bool(pid and pid in (g["given_by"], g["holder_person_id"])) or lead or (
            h.is_admin(request))
        if not allowed:
            h.err(PermissionError("a grant is ended by who gave it, the project's lead, or its "
                                  "holder"), 403)
        if g["state"] == "revoked":
            h.err(ValueError(f"{g['id']} is already revoked"), 409)
        was = g["state"]
        store.update_grant_row(g["id"], state="revoked")
        g = store.get_grant_row(g["id"])
        if was == "active":
            mirror_out(g, h.by(request, None))
        return {"grant": grants_view([g])[0]}

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

    try:
        migrate_grants()
    except Exception as e:   # the grants docs still serve; the next start tries again
        print(f"[tares] moving the grants into the grants table failed: {type(e).__name__}: {e}",
              flush=True)

    # ── stations (TR-420): sessions grouped by the station they play ──────────
    def _ts(v) -> float:
        return v.timestamp() if isinstance(v, datetime) else 0.0

    def stations(project: str | None = None) -> list[dict]:
        """The stations, one row each per crew (TR-628): two crews with the same station names
        never merge. Each row names its crew, machine and person."""
        rows = store.station_sessions(h.cc_source())
        names = {p["id"]: p["name"] for p in store.list_projects()}
        crews = {c["id"]: c for c in store.list_crews()}
        now = now_utc()
        by: dict[tuple, list[dict]] = {}
        for s in rows:
            by.setdefault((s["crew_id"], s["station"]), []).append(s)
        out = []
        for (cid, name), ss in by.items():
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
            crew = crews.get(cid) or {}
            out.append({"name": name, "role": cur["role"], "parent": cur["parent"],
                        "project": cur["project"], "project_name": names.get(cur["project"]),
                        "state": state, "state_reason": cur["state_reason"],
                        "state_at": cur["state_at"], "quiet_minutes": quiet,
                        "session": cur["session"], "started_at": cur["started_at"],
                        "last_at": cur["last_at"], "lines": cur["lines"], "earlier": earlier,
                        "crew_id": cid, "crew": crew.get("name"),
                        "machine": crew.get("machine") or "", "person": crew.get("person"),
                        "projects": sorted({s["project"] for s in ss if s["project"]})})
        hidden = h.hidden_ids()
        if hidden:   # a project the caller cannot see is not named by a station (TR-629)
            kept = []
            for st in out:
                if st["projects"] and set(st["projects"]) <= hidden:
                    continue
                st["projects"] = [u for u in st["projects"] if u not in hidden]
                if st["project"] in hidden:
                    st["project"], st["project_name"] = None, None
                kept.append(st)
            out = kept
        meters = store.session_meters([st["session"] for st in out])
        spent = store.usage_by_session([s["session"] for s in rows])
        for st in out:
            # the stations this one started: in its own crew, whose names may repeat in another
            st["children"] = sorted(x["name"] for x in out
                                    if x["parent"] == st["name"] and x["crew_id"] == st["crew_id"])
            # the context window from the station's status line, and what all its sessions spent
            st["context"] = claude_usage.context_now(meters.get(st["session"]))
            mine = [spent[s] for s in [st["session"], *(e["session"] for e in st["earlier"])]
                    if s in spent]
            st["usage"] = ({k: sum(x[k] for x in mine) for k in mine[0]} if mine else None)
        if project:
            out = [s for s in out if project in s["projects"]]
        order = {r: i for i, r in enumerate(factory_mod.ROLES)}
        out.sort(key=lambda s: (order.get(s["role"] or "", 99), s["name"], s["crew_id"] or ""))
        return out

    def caller_person_id(request: Request) -> str:
        """The person whose crews the caller means by "mine". A recording with no person (the
        system credential) is filed under `local`, so a caller with no person is `local` too."""
        p = h.current_person(request)
        return p["id"] if p else "local"

    NO_PERSON = "This key has no person. An admin binds it in Settings, Keys."

    def work_person(request: Request) -> dict:
        """The person whose work the caller means (TR-633): the key's person; the system
        credential is the person `local`, like its recordings. A key with no person is refused."""
        p = h.current_person(request)
        if p:
            return {"id": p["id"], "name": p.get("name") or p["id"]}
        if not h.is_system(request):
            h.err(PermissionError(NO_PERSON), 403)
        local = store.get_person("local") or {}
        return {"id": "local", "name": local.get("name") or "You"}

    def is_mine(t: dict, me: str) -> bool:
        """A ticket is the person's when it is assigned to them; the person `local` (a cell of
        one) also has the tickets nobody owns."""
        a = t.get("assignee_person_id")
        return a == me or (me == "local" and not a)

    def desk_is_mine(d: dict, me: str) -> bool:
        """A desk item is the person's when it is addressed to them; the person `local` (a cell of
        one) also has the items from before they were addressed."""
        to = d.get("to_person_id")
        return to == me or (me == "local" and not to)

    def leads(p: dict | None, me: str) -> bool:
        lead = (p or {}).get("lead_person_id")
        return lead == me or (me == "local" and not lead)

    def my_projects(request: Request) -> set[str] | None:
        """The projects the caller's person leads or holds a ticket assignment in, or None when
        nothing narrows the list: the system credential, or a key with no person (a project key)."""
        if h.is_system(request) or not h.current_person(request):
            return None
        me = h.current_person(request)["id"]
        return {p["id"] for p in store.list_projects()
                if leads(store.get_project(p["id"]), me)
                or any(is_mine(t, me) for t in store.list_tickets(p["id"]))}

    def crews_of(rows: list[dict]) -> list[dict]:
        """The stations (as `stations()` lists them) grouped by crew, the crew seen last first."""
        info = {c["id"]: c for c in store.list_crews()}
        out = []
        for cid in dict.fromkeys(r["crew_id"] for r in rows):
            c = info.get(cid) or {}
            out.append({"id": cid, "name": c.get("name"), "machine": c.get("machine") or "",
                        "person": c.get("person"), "person_id": c.get("person_id"),
                        "last_seen": c.get("last_seen"),
                        "stations": [r for r in rows if r["crew_id"] == cid]})
        out.sort(key=lambda c: (-_ts(c["last_seen"]), c["id"] or ""))
        return out

    def pick_station(name: str, request: Request, crew: str | None = None) -> dict:
        """The station called `name`. Crews can repeat a name: `crew` (a crew id or name) narrows
        it down, and of several that remain the caller's own crew's wins, then the newest."""
        rows = [s for s in stations() if s["name"] == name]
        if crew:
            rows = [s for s in rows if crew in (s["crew_id"], s["crew"])]
        if not rows:
            h.err(KeyError(f"no station {name!r} has reported to Tares"
                           + (f" in crew {crew!r}" if crew else "")), 404)
        me = caller_person_id(request)
        rows.sort(key=lambda s: (((s["person"] or {}).get("id") != me), -_ts(s["last_at"])))
        return rows[0]

    @app.get("/api/crew")
    async def get_crew(request: Request, mine: bool = False):
        """The crews (TR-628): one per person, crew name and machine, each with its stations (the
        current session and state, the earlier sessions that played it, the stations it
        started), the crews' settings, and the shared grants. `mine=1` keeps the caller's own
        crews. `stations` is the same rows flat, for a client that does not group by crew yet."""
        rows = stations()
        if mine:
            me = caller_person_id(request)
            rows = [s for s in rows if (s["person"] or {}).get("id") == me]
        return {"crews": crews_of(rows), "stations": rows, "settings": settings(request),
                "grants": grants_out(None)["all"],
                "now": now_utc()}

    @app.get("/api/crew/my-work")
    async def my_work(request: Request, crew: str | None = None):
        """What belongs to the caller's person (TR-633), across the projects that have an open
        hand-over and that the caller can see: `ready` (their tickets that can start),
        `in_progress` (their tickets a station holds), and `led_projects` with the milestones
        whose tickets are all delivered. `desk` is the open desk items addressed to them (TR-634);
        `spec_questions` is filled by later work."""
        me = work_person(request)
        try:
            crew_name = crew_name_of(request, crew)
        except ValueError as e:
            h.err(e)
        hidden = h.hidden_ids()
        ready, doing, led = [], [], []
        for uid in store.handed_over_projects(finished=False, taken=False):
            p = store.get_project(uid)
            if p is None or uid in hidden:
                continue
            tickets = store.list_tickets(uid)
            wait = factory_mod.readiness(tickets)
            for t in tickets:
                if not is_mine(t, me["id"]) or factory_mod.finished(t):
                    continue
                if t["holder"]:
                    doing.append({"project": p["name"], "ticket": factory_mod.label(t),
                                  "title": t["title"], "holder": t["holder"],
                                  "stage": t["stage"] or ""})
                elif wait[t["id"]]["ready"]:
                    ready.append({"project": p["name"], "ticket": factory_mod.label(t),
                                  "title": t["title"], "repo": t.get("repo") or ""})
            if leads(p, me["id"]):
                ms = []
                for m in store.list_milestones(uid):
                    mine = [t for t in tickets if t["milestone"] == m["id"]
                            and t["status"] != "canceled"]
                    if mine and all(factory_mod.delivered(t) for t in mine):
                        ms.append(m["name"])
                led.append({"project": p["name"], "milestones_ready_to_check": ms})
        desk = [{k: d[k] for k in ("label", "project_name", "question", "options", "recommendation",
                                   "why", "context", "blocks", "tickets", "blocking", "created_at")}
                for d in map(desk_out, store.list_desk("open"))
                if d["project"] not in hidden and desk_is_mine(d, me["id"])]
        return {"person": me, "crew": crew_name, "ready": ready, "in_progress": doing,
                "led_projects": led, "desk": desk, "spec_questions": []}

    @app.get("/api/crew/projects")
    async def crew_projects(request: Request):
        """The projects handed to the crew, oldest hand-over first: each with its repos (the
        folder on the crew's machine and the GitHub name), the autonomy it was handed over at
        when stricter than the crew's, its tickets by stage, the milestone it is on, and which
        tickets each repo has. The orchestrator works from this list (TR-462). A person's key
        sees the projects the person leads or holds a ticket in; the system credential sees all
        (TR-633)."""
        names = {p["id"]: p["name"] for p in store.list_projects()}
        hidden = h.hidden_ids()
        mine = my_projects(request)
        names = {u: n for u, n in names.items()
                 if u not in hidden and (mine is None or u in mine)}
        out = []
        for uid in store.handed_over_projects(finished=False, taken=False):
            if uid not in names:
                continue
            ho = store.get_handover(uid) or {}
            out.append({"id": uid, "name": names[uid], **project_progress(store, uid),
                        "repos": repo_rows(uid, ho.get("repos") or []),
                        "autonomy": ho.get("autonomy"), "handed_over_at": ho.get("at"),
                        "handed_over_by": ho.get("by_label") or ho.get("by"),
                        "handed_over_person": ho.get("person")})
        out.sort(key=lambda p: str(p["handed_over_at"] or ""))
        done = []
        for uid in store.handed_over_projects(finished=True):
            if uid in names:
                ho = store.get_handover(uid) or {}
                done.append({"id": uid, "name": names[uid], **project_progress(store, uid),
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
    async def get_station(name: str, request: Request, recent: int = 30,
                          crew: str | None = None):
        """One station: its row as in /api/crew, the tickets it holds in every project, its
        current session's last lines (`recent`, at most 200), and the command that opens it.
        Crews can repeat a station name: `crew` (id or name) says which; without it the caller's
        own crew's station comes back."""
        st = pick_station(name, request, crew)
        held = []
        hidden = h.hidden_ids()
        for p in store.list_projects():
            if p["id"] in hidden:
                continue
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
                "attach": factory_mod.attach_command(name, st.get("role") or "")}

    @app.post("/api/crew/{name}/end")
    async def end_station(name: str, request: Request, body: dict | None = Body(None),
                          crew: str | None = None):
        """{reason?}: mark the station's current session ended, for a station that was retired or
        whose session is gone, so it stops showing as working or quiet. Its earlier sessions and
        lines are kept. The orchestrator or the person (unlabeled, such as `factory retire`) may.
        A session that is still alive turns the row back with its next line."""
        deny(request, "end_station")
        st = pick_station(name, request, crew)
        reason = " ".join(str((body or {}).get("reason") or "").split())[:100] or "marked ended"
        if (store.session_state(st["session"]) or {}).get("state") != "ended":
            store.set_session_state(st["session"], "ended", reason, now_utc(), force=True)
        return next(s for s in stations() if s["session"] == st["session"])

    # ── a station's context by category: asked from the console, read on the crew's machine ──
    ASK_TTL = 15   # minutes an unanswered ask stays open; the watch reads on the crew's machine

    @app.post("/api/crew/{name}/context")
    async def ask_station_context(name: str, request: Request, crew: str | None = None):
        """Ask for a new /context reading of the station's session. The factory watch on the
        crew's machine reads it (no model call) within a few seconds, if it runs."""
        st = pick_station(name, request, crew)
        at = now_utc()
        store.ask_session_context(st["session"], at)
        return {"session": st["session"], "asked_at": at}

    @app.get("/api/crew/context/asks")
    async def station_context_asks():
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
    async def get_project_crew(uid: str):
        """The stations that worked on this project (builders started for it among them), and
        when it was handed to the crew."""
        h.project_or_404(uid)
        rows = stations(uid)
        return {"crews": crews_of(rows), "stations": rows, "handover": store.get_handover(uid),
                "now": now_utc()}

    # ── the ledger (TR-427, TR-428, TR-410, TR-429) ───────────────────────────
    def ticket_or_404(uid: str, ref: str) -> dict:
        t = store.get_ticket(uid, ref)
        if t is None:
            h.err(KeyError(f"project has no ticket {ref!r}"), 404)
        return t

    def by_of(request: Request, body: dict | None = None) -> str:
        """The name a record keeps for the call: the station, else what the caller says. A key's
        own name stands in only when no person stands behind the call (TR-619): next to a person
        it is not a station, and the person's name shows instead."""
        c = caller(request)
        said = str((body or {}).get("by") or "").strip()[:100]
        if c["station"] or said:
            return c["station"] or said
        return "" if h.current_person(request) else (h.by(request, body) or "")

    def who(request: Request, body: dict | None = None) -> tuple[str, str]:
        return (by_of(request, body) or "a session"), caller(request)["role"]

    def person_of(request: Request) -> str | None:
        """The person behind the call's credential, for the record of who did it (TR-619). None
        for the system credential."""
        p = h.current_person(request)
        return p["id"] if p else None

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
        store.add_ticket_history(t["id"], uid, "holder", holder, None, by, role, now_utc(),
                                 person_of(request))
        return h.ticket_out(uid, ticket_or_404(uid, t["id"]), with_doc=False)

    @app.post("/api/projects/{uid}/tickets/{ref}/stage")
    async def set_stage(uid: str, ref: str, request: Request, body: dict = Body(...)):
        """{stage, reason?, pr?}: move the ticket. Only the station holding it (or a helper it
        started) moves it; only the releaser marks it shipped; a person's own session may do
        anything. `pr` (a PR URL or owner/repo#12) links the ticket to its pull request, read
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
        store.update_ticket(t["id"], **fields)
        by, role = who(request, body)
        store.add_ticket_history(t["id"], uid, "stage", stage, reason, by, role or None, now_utc(),
                                 person_of(request))
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
                                 by, role, now_utc(), person_of(request))
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
                        "", now_utc(), person_of(request))
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
                             now_utc(), person_of(request))
        repo = (t["pr"] or {}).get("repo")
        if repo:
            offer_rules(repo)
        rv = next(x for x in store.ticket_reviews(t["id"]) if x["id"] == r["id"])
        return {**rv, "open": [f["label"] for f in store.open_findings(t["id"])]}

    def queue() -> list[dict]:
        hidden = h.hidden_ids()
        names = {p["id"]: p["name"] for p in store.list_projects() if p["id"] not in hidden}
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
        # 8. links, not inlined
        L += ["## Read as needed",
              "- The spec, AGENTS.md (the project's and the shared one) and the project memory: "
              "list_docs, then get_doc(id).", "- The crew's messages about it: get_ticket."]
        return "\n".join(L)

    # ── recurring findings and rules offered from them (TR-443) ───────────────
    def recurring(repo: str, hidden: set | frozenset = frozenset()) -> list[dict]:
        from datetime import timedelta
        rows = [f for f in store.repo_findings(repo, now_utc() - timedelta(days=factory_mod.RECUR_DAYS))
                if f["project"] not in hidden]
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
        return {"repo": repo, "recurring": recurring(repo.strip(), h.hidden_ids())}

    @app.get("/api/rule-proposals")
    async def list_rule_proposals(state: str = "open"):
        hidden = h.hidden_ids()
        names = [p["name"] for p in store.list_projects() if p["id"] in hidden]
        out = []
        for rp in store.list_rule_proposals(state.strip() or None):
            if rp.get("project") in hidden:
                continue
            # a rule that came back across projects lists the tickets it came from
            rp = {**rp, "tickets": [t for t in rp.get("tickets") or []
                                    if not any(str(t).startswith(n + " ") for n in names)]}
            out.append(rp)
        return {"proposals": out}

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
        return {**d, "project_name": p["name"] if p else None,
                "project_finished": bool(p and p.get("status") == "done")}

    def desk_target(request: Request, to, uid: str | None, tickets: list[str]) -> str | None:
        """The person a desk item is addressed to (TR-634): `to` (a person id or an email on this
        cell) when given, else the assignee of the first ticket named that has one, else the
        project's lead, else the caller's own person. None only when the caller has no person
        and is not the system credential (a project key): the item then keeps the older rule."""
        said = str(to or "").strip()
        if said:
            person = store.get_person(said) or store.find_person_by_email(said)
            if person is None:
                h.err(KeyError(f"no person {said!r} on this cell: give a person id or an email "
                               "(list_people shows who is here)"), 404)
            return person["id"]
        if uid:
            by_ref = {factory_mod.label(t): t for t in store.list_tickets(uid)}
            for ref in tickets:
                if (by_ref.get(ref) or {}).get("assignee_person_id"):
                    return by_ref[ref]["assignee_person_id"]
            lead = (store.get_project(uid) or {}).get("lead_person_id")
            if lead:
                return lead
        me = person_of(request)
        return me or ("local" if h.is_system(request) else None)

    def close_project_desk(uid: str) -> None:
        """A finished project's open questions have nothing left to decide (TR-559)."""
        for d in store.list_desk("open", uid):
            store.close_desk_item(d["id"], "withdrawn", "the project is finished", now_utc())

    @app.post("/api/desk", status_code=201)
    async def desk_add(request: Request, body: dict = Body(...)):
        """{question, recommendation, why, options?, context?, blocks?, tickets?, blocking?,
        assumptions?, project?, station?, command?, to?}: a question for the person, numbered D1,
        D2... for the whole cell. `to` is the person it is addressed to (an id or an email): it
        shows in their my_work, and only words found in their own recorded sessions answer it.
        Without `to` it goes to the assignee of the first ticket named, else the project's lead,
        else the caller's own person. With `command` (and the `station` that was refused it) it is a
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
        to = desk_target(request, body.get("to"), uid, tickets)
        d = store.add_desk_item(uid, q, ctx, opts, rec, why,
                                " ".join(str(body.get("blocks") or "").split())[:300], tickets,
                                bool(body.get("blocking")), str(body.get("asked_by") or by)[:120],
                                asm, now_utc(), station, command, person_of(request), to)
        return desk_out(d)

    @app.get("/api/desk")
    async def desk_list(request: Request, state: str = "open", project: str = "",
                        mine: bool = False):
        """The desk, blockers first, then oldest. `mine=1` keeps the items addressed to the
        caller's person (TR-634)."""
        st = state.strip().lower() or None
        if st and st not in factory_mod.DESK_STATES:
            h.err(ValueError("state is one of: " + ", ".join(factory_mod.DESK_STATES)))
        uid = h.project_or_404(h.resolve_project(project.strip()))["id"] if project.strip() else None
        hidden = h.hidden_ids()
        me = work_person(request)["id"] if mine else None
        return {"desk": [desk_out(d) for d in store.list_desk(st, uid)
                         if d["project"] not in hidden and (me is None or desk_is_mine(d, me))]}

    def desk_or_404(ref: str) -> dict:
        d = store.get_desk_item(ref)
        # an item of a project the caller cannot see does not exist for them (TR-629)
        if d is None or (d["project"] and d["project"] in h.hidden_ids()):
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

    def standing_grant(d: dict, giver: str | None, scope: str, grant_kind: str,
                       request: Request) -> tuple:
        """What a standing answer becomes (TR-634): (kind, project, areas, needs_lead) for
        put_grant, given by the person who answered (`giver`, the item's person), by the same rules as POST /api/grants:
        a team grant (scope all, or an item with no project) needs an admin; a project grant, the
        project's lead, an admin or the owner of every repo it reaches into; personal, a person,
        for themselves. The repos are those of the item's tickets; one owned by someone else
        makes the grant wait for them, and one from a person who is neither lead nor admin waits
        for the lead too."""
        person = store.get_person(giver) if giver else None
        admin = bool(person["is_admin"]) if person else h.is_admin(request)
        if grant_kind == "personal":
            if giver is None:
                h.err(PermissionError("a personal grant belongs to a person, and this answer has "
                                      "none"), 403)
            uid = d["project"] if scope == "project" else None
            return "personal", uid, [], False
        if scope == "all" or not d["project"]:
            if not admin:
                h.err(PermissionError("a grant for every project needs an admin: this answer "
                                      "can be a project grant, or an admin gives it"), 403)
            return "team", None, [], False
        by_ref = {factory_mod.label(t): t for t in store.list_tickets(d["project"])}
        areas = []
        for ref in d["tickets"]:
            try:
                areas.append(areas_mod.norm_repo((by_ref.get(ref) or {}).get("repo")))
            except ValueError:
                pass
        areas = list(dict.fromkeys(areas))
        p = store.get_project(d["project"]) or {}
        leads_it = bool(giver and p.get("lead_person_id") == giver)
        owns_all = bool(areas) and giver is not None and all(
            (h.area_owner(a) or {}).get("id") == giver for a in areas)
        if not (admin or leads_it or owns_all):
            h.err(PermissionError("a project grant comes from the project's lead, or from the "
                                  "owner of the repos it names"), 403)
        return "project", d["project"], areas, not (admin or leads_it)

    @app.post("/api/desk/{ref}/answer")
    async def desk_answer(ref: str, request: Request, body: dict = Body(...)):
        """{words, choice?, standing?, scope?: project|all, grant_kind?: project|personal, kept?:
        [A<n>], overturned?: {A<n>: words}}: the person's answer, their words as typed. Tares
        looks for those words in what the person the item is addressed to typed in their own
        recorded sessions, whichever station or session that was (TR-634). Words not found yet
        are a retryable error: the recording can take a minute to arrive. An item from before
        items were addressed is matched as before: in the orchestrator's session, or from a
        person's own session in any session. A standing answer that matches becomes a grant
        given by that person: a project grant (the default; `scope` all makes it a team grant,
        which needs an admin) or, with `grant_kind` personal, one for their own machine. A
        project grant that reaches into repos other people own waits for their consent. An
        assumption batch keeps or overturns each assumption."""
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
        grant_kind = str(body.get("grant_kind") or "project").strip().lower()
        if grant_kind not in ("project", "personal"):
            h.err(ValueError("grant_kind is project or personal"))
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
        # an item addressed to a person is answered in that person's own recorded sessions; one
        # from before (no person) keeps the orchestrator's session, or any from an unlabelled caller
        to = d.get("to_person_id")
        labeled = bool(caller(request)["station"])
        sessions = store.role_sessions("orchestrator") if labeled and not to else None
        giver = to or person_of(request)
        standing = bool(body.get("standing"))
        grant = None
        if standing:
            try:
                factory_mod.grant_line(words, now_utc().date().isoformat())
            except docs_mod.DocError as e:
                h.err(e)
            grant = standing_grant(d, giver, scope, grant_kind, request)
        kept = [str(a) for a in body.get("kept") or []]
        over = body.get("overturned") or {}
        if not isinstance(over, dict):
            h.err(ValueError("overturned is {A<n>: the person's words}"))

        def look():
            turns = store.person_turns(h.cc_source(), sessions, since, to)
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
        if (hit is None or unheard) and to:
            name = (store.get_person(to) or {}).get("name") or to
            h.err(ValueError(f"Answer not found yet in {name}'s recorded sessions. Recordings can "
                             "take a minute to arrive; try again after the next sync."), 409)
        if hit is None or unheard:
            # nothing is recorded as the person's unless they typed it: the question stays open
            return {"desk_item": desk_out(d), "matched": False, "granted": False,
                    "recorded": False, "assumptions": [], "follow_up": [],
                    "unheard": unheard}
        granted = False
        if grant:
            kind, grant_project, areas, needs_lead = grant
            row, _ = put_grant(kind, words, grant_project, giver, areas, needs_lead,
                               "the person, on the desk")
            granted = row["state"] == "active"   # one waiting for consent is not in effect yet
        changed = []
        for a, st, w in plan:
            store.decide_assumption(a["id"], st, w, d["label"], now_utc())
            changed.append({"label": a["label"], "state": st, "tickets": a["tickets"]})
        dec = store.add_decision(d["project"], d["id"], d["question"], words,
                                 choice[:300], standing, scope,
                                 hit is not None, hit["session"] if hit else None, granted,
                                 d["tickets"], now_utc(), person_id=to or person_of(request))
        store.close_desk_item(d["id"], "answered", None, now_utc())
        by_id = {t["id"]: t for t in store.list_tickets(d["project"])} if d["project"] else {}
        follow = [{"assumption": c["label"],
                   "tickets": [factory_mod.label(by_id[x]) for x in c["tickets"] if x in by_id]}
                  for c in changed if c["state"] == "overturned"]
        return {"desk_item": desk_out(desk_or_404(d["id"])), "decision": dec,
                "matched": True, "recorded": True, "granted": granted,
                "grant": grants_view([row])[0] if grant else None, "assumptions": changed,
                "follow_up": follow, "unheard": []}

    @app.post("/api/desk/{ref}/allow")
    async def desk_allow(ref: str, request: Request, body: dict = Body(...)):
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
        # A crew station (<crew>-reviewer, <crew>-releaser and their helpers) serves every project
        # and starts with only the grants for every project: one kept for a project never reaches it.
        if scope == "project" and d["station"] and factory_mod.on_crew_side(d["station"], stations()):
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
                                 via="console", person_id=person_of(request))
        store.close_desk_item(d["id"], "answered", None, now_utc())
        return {"desk_item": desk_out(desk_or_404(d["id"])), "decision": dec,
                "station": d["station"], "line": line, "added": added, "scope": scope}

    @app.get("/api/projects/{uid}/decisions")
    async def project_decisions(uid: str):
        """The person's answers on this project, newest first."""
        h.project_or_404(uid)
        return {"decisions": store.list_decisions(uid)}

    @app.post("/api/projects/{uid}/handover")
    async def hand_over(uid: str, request: Request, body: dict = Body(default={})):
        """{repos?: [{path, name}], repo?, autonomy?}: the spec session hands the project to the
        crew. Each repo is the folder it is checked out in on the crew's machine and its GitHub
        name (owner/name). A later hand-over adds repos, never replaces them. `autonomy` only
        when the person wants this project stricter than the crew (L3-review). Recorded with
        the time, the person and the crew (`crew`, else X-Tares-Crew, else `crew`); the
        orchestrator finds it in /api/crew/projects. A key with no person is refused (TR-633)."""
        h.project_or_404(uid)
        work_person(request)
        try:
            crew = crew_name_of(request, body.get("crew"))
            new = factory_mod.handover_repos(body.get("repos"))
            if body.get("repo"):
                new = factory_mod.merge_repos(new, factory_mod.handover_repos(body.get("repo")))
            autonomy = str(body.get("autonomy") or "").strip() or None
            if autonomy and autonomy not in factory_mod.AUTONOMY:
                raise docs_mod.DocError(f"unknown autonomy {autonomy!r}; one of: "
                                        + ", ".join(factory_mod.AUTONOMY))
        except (docs_mod.DocError, ValueError) as e:
            h.err(e)
        old = store.get_handover(uid) or {}
        repos = factory_mod.merge_repos(old.get("repos") or [], new)
        autonomy = autonomy or old.get("autonomy")
        store.set_handover(uid, repos, autonomy, by_of(request, body), now_utc(),
                           person_of(request), crew)
        if old.get("finished"):   # more work on a finished project: it is open again
            store.update_project(uid, status="active")
        include_shared_grants(uid)   # a crew-built project shows the grants it is built under
        return {"handover": store.get_handover(uid)}

    @app.post("/api/projects/{uid}/takeover")
    async def take_over(uid: str, request: Request, body: dict = Body(default={})):
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
            store.take_over(uid, words, by, now_utc(), person_of(request))
        return {"project": uid, "name": p["name"], "takeover": store.get_handover(uid)["takeover"],
                "holders": sorted({t["holder"] for t in store.list_tickets(uid) if t["holder"]})}

    @app.post("/api/projects/{uid}/release")
    async def release_project(uid: str, request: Request):
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
                                             "released to the person's session", by, role, at,
                                             person_of(request))
            store.release_takeover(uid, at)
        return {"project": uid, "name": p["name"],
                "takeover": store.get_handover(uid)["takeover"]}

    def pr_row(t: dict) -> dict:
        """A ticket's pull request as the GitHub section shows it: what GitHub said when Tares last
        read it (title, CI, the factory/review status), and the crew's own last verdict."""
        pr = t["pr"] or {}
        last = store.ticket_reviews(t["id"])[-1:]
        v = verdict_now(t, last[0] if last else None)
        return {"url": pr["url"], "ticket": factory_mod.label(t), "number": pr.get("number"),
                "title": pr.get("title") or t["title"], "branch": pr.get("branch"),
                "head": (pr.get("head") or "")[:7] or None, "ci": pr.get("ci"),
                "github_verdict": pr.get("verdict"), "stage": t["stage"], "holder": t["holder"],
                "review": ({"verdict": v["verdict"], "round": v["round"], "current": v["current"]}
                           if v else None),
                "merged_at": pr.get("merged_at"), "checked_at": pr.get("checked_at"),
                "error": pr.get("error")}

    def repo_rows(uid: str, repos: list[dict]) -> list[dict]:
        """Each hand-over repo with the tickets that change it, their open pull requests (newest
        first) and the ones merged. A ticket with no repo belongs to the first one; a repo only
        tickets name is added. `github_error` says when Tares could not read GitHub for it."""
        tickets = store.list_tickets(uid)
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
                row["merged_prs"].append(pr_row(t))
            elif pr.get("state") != "closed":
                row["open_prs"].append(pr_row(t))
        for row in rows:
            row["open_prs"].sort(key=lambda p: -(p["number"] or 0))
            row["merged_prs"].sort(key=lambda p: -(p["number"] or 0))
        return rows

    @app.post("/api/projects/{uid}/finish")
    async def finish_project(uid: str, request: Request, body: dict = Body(...)):
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
            store.finish_handover(uid, words, None, by, now_utc(), person_of(request))
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
        store.finish_handover(uid, dec["words"], d["label"], by, now_utc(), person_of(request))
        store.update_project(uid, status="done")
        close_project_desk(uid)
        return {"project": uid, "name": p["name"], "finished": store.get_handover(uid)["finished"],
                "open_projects": len(store.handed_over_projects(finished=False, taken=False))}

    @app.get("/api/projects/{uid}/repos")
    async def project_repos(uid: str):
        """The repos this project touches, each with its tickets and open pull requests
        (TR-463). Empty for a project nobody handed over and whose tickets name no repo."""
        h.project_or_404(uid)
        ho = store.get_handover(uid) or {}
        rows = repo_rows(uid, ho.get("repos") or [])
        return {"repos": [r for r in rows if r["path"] or r["name"]]}
