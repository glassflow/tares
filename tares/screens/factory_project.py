"""The factory project page (TR-734): GET /api/factory/projects/{uid}.

One answer for the whole page: where the project stands (spec, build, review or done), what
waits for the person on this project, how many tickets are in each state (`counts`), the few
that move now (`moving`: changes asked, in review, in progress, blocked, at most 5, with
`moving_total` for how many there are), the ticket that goes next, the milestones as compact
rows (a number, a bar's worth of counts, the acceptance checks and a link to their tickets, no
ticket lists), the decisions and the open calls the crew made, the project's documents, the
command that opens a builder's session, and where the tickets live. The full ticket list is the
project's Tickets page, which `tickets_href` and each milestone's `tickets_href` open. The tickets are
read once.

Every string is worded for people: a builder is "Builder for M1", never the name of its
station; a time is "3 days ago"; a ticket is "in review", never a stage key. The only folder
path that could reach the answer is the crew's hand-over repo path, and it is kept out: free
text that names one shows the repo instead. The one place a station name appears is the
terminal command that opens a builder's session, because `tares factory crew attach` takes it.

`stage_of` and `who_words` are used by the ticket page (TR-760) as well.

GET /api/factory/projects/{uid}/github is the project's GitHub page: every pull request its
tickets opened, by repo, open, merged or closed without merging, with CI, the review, the size of
the change and who merged it, as GitHub said when Tares last read it.
"""
from __future__ import annotations

import re

from fastapi import HTTPException, Request

from .. import factory as factory_mod
from .. import factory_api
from .. import factory_github
from .. import factory_home as fh
from .. import factory_waiting as fw
from .. import licence
from .. import project_secrets
from .. import team as team_mod
from ..envelope import now_utc
from ..factory_home import plain_ago

# ── words ─────────────────────────────────────────────────────────────────────

STAGE_LABEL = {"spec": "Spec", "build": "Build", "review": "Review", "done": "Done"}
STATE_LABEL = {**fh.STATE_LABEL, "finished": "Finished", "later": "Later"}

# a ticket's state in words, by its stage key
_STAGE_WORD = factory_mod.STAGE_WORD
_VERDICT_WORD = {"pass": "passed", "changes": "changes asked", "block": "blocked"}
_CI_WORD = {"success": "CI green", "failure": "CI failing", "pending": "CI running"}
# the crew's roles as a person says them, as the author of a call: "by the builder"
_ROLE_BY = {"orchestrator": "the orchestrator", "reviewer": "the reviewer", "releaser": "the releaser",
            "builder": "the builder", "helper": "a helper"}
_ROLE_WHO = {"orchestrator": "Lead", "reviewer": "Reviewer", "releaser": "Releaser",
             "builder": "Builder", "helper": "Helper"}
# the documents the page lists, in reading order, and the word for each
_DOC_LABEL = {"spec": "Spec", "plan": "Plan", "agents": "Rules", "memory": "Memory",
              "note": "Notes", "working": "Working notes"}
_PR_NUMBER = re.compile(r"/pull/(\d+)")
# the states that move now, in the order the page lists them, and the key each state counts under
_MOVING = ("changes asked", "in review", "in progress", "blocked")
_MOVING_CAP = 5
_COUNT_KEY = {"to do": "to_do", "in progress": "in_progress", "in review": "in_review",
              "changes asked": "changes_asked", "blocked": "blocked", "merged": "merged",
              "canceled": "canceled"}
# a station whose newest session shows one of these plays no part now
_GONE = ("ended", "stopped", "replaced")


def stage_of(has_start: bool, tickets: list[dict], finished: bool = False) -> str:
    """Where a project stands: "spec", "build", "review" or "done". It is phaseOf() of
    ui/src/components/project/factory.tsx in Python.

    No starting prompt, or no ticket that is not canceled: spec. Every such ticket finished
    (factory.finished: done, or merged or shipped by its stage): done.
    The last ticket of the plan verifies and opens the pull request, so when it is the only one
    left (in a plan of more than one) the build is in review. Otherwise build. A project the
    crew's hand-over closed (`finished`) is done whatever its tickets say."""
    if finished:
        return "done"
    live = [t for t in tickets if t.get("status") != "canceled"]
    if not has_start or not live:
        return "spec"
    left = [t for t in live if not factory_mod.finished(t)]
    if not left:
        return "done"
    if len(left) == 1 and left[0]["id"] == live[-1]["id"] and len(live) > 1:
        return "review"
    return "build"


def station_roles(rows: list[dict]) -> dict[str, str]:
    """{station name: role} from store.station_sessions(...), the newest session's role when a
    station has had several."""
    out: dict[str, str] = {}
    for r in rows:
        if r.get("station") and r.get("role"):
            out.setdefault(r["station"], r["role"])
    return out


def who_words(holder: str | None, roles: dict[str, str], milestone: str | None = None) -> str | None:
    """Who holds a ticket in plain words: "Builder for M1" (`milestone` is the ticket's own, "M1"),
    "Reviewer", "Lead". The role comes from the station's session (`roles`, station_roles()), else
    from its name (crew-reviewer, a project's -build station). None without a holder. Never the
    station's name."""
    name = str(holder or "").strip()
    if not name:
        return None
    role = roles.get(name)
    if not role and name.startswith("crew-"):
        role = name[len("crew-"):].split("-", 1)[0]
    if not role and "-build" in name:
        role = "builder"
    word = _ROLE_WHO.get(role or "")
    if word is None:
        return "The crew"
    return f"{word} for {milestone}" if role == "builder" and milestone else word


def ticket_state(t: dict, blocked_by: list[str]) -> str:
    """A ticket's state in words: factory.state_of, the one rule every screen reads."""
    return factory_mod.state_of(t, blocked_by)


def _stamp(v) -> float:
    return v.timestamp() if hasattr(v, "timestamp") else 0.0


def _pr_out(t: dict) -> dict | None:
    pr = t.get("pr") or {}
    url = pr.get("url")
    number = pr.get("number")
    if number is None and url:
        m = _PR_NUMBER.search(str(url))
        number = int(m.group(1)) if m else None
    if number is None and not url:
        return None
    if pr.get("merged") or (not pr.get("state") and t.get("stage") in ("merged", "shipped")):
        text = "merged"
    elif pr.get("state") == "closed":
        text = "closed without merging"
    else:
        ci = _CI_WORD.get(pr.get("ci") or "")
        text = "open, " + ci if ci else "open"
    return {"number": number, "url": url, "state_text": text}


_PR_STATE_LABEL = {"open": "Open", "draft": "Draft", "merged": "Merged",
                   "closed": "Closed without merging"}
_PR_ORDER = {"open": 0, "draft": 0, "merged": 1, "closed": 2}


def pr_state(t: dict) -> str | None:
    """open, draft, merged or closed (without merging) for a ticket's pull request; None for a
    ticket with none. Merged also when the ticket says so and GitHub was never read (_pr_out)."""
    pr = t.get("pr") or {}
    if not (pr.get("url") or pr.get("number")):
        return None
    if pr.get("merged") or (not pr.get("state") and t.get("stage") in ("merged", "shipped")):
        return "merged"
    if pr.get("state") == "closed":
        return "closed"
    return "draft" if pr.get("draft") else "open"


def hidden_paths(handover: dict | None) -> dict[str, str]:
    """The crew's folders, each to the repo name free text shows instead."""
    hidden = {}
    for r in (handover or {}).get("repos") or []:
        if r.get("path"):
            name = r.get("name") or factory_mod.folder_name(r["path"]) or "the repo"
            base = factory_mod.hidden_key(r["path"])
            if base:                                  # with or without a trailing slash
                hidden[base] = name
    return hidden


def repo_names(handover: dict | None) -> list[str]:
    """The names of the repos handed over, each once, in the hand-over's order."""
    names = []
    for r in (handover or {}).get("repos") or []:
        name = r.get("name") or (factory_mod.folder_name(r["path"]) if r.get("path") else None)
        if name and name not in names:
            names.append(name)
    return names


def cleaner(handover: dict | None):
    hidden = hidden_paths(handover)

    def clean(text):
        return factory_mod.hide_paths(text, hidden)
    return clean


def pr_card(t: dict, ref: str, uid: str, review: str | None, now, clean=str) -> dict:
    """One pull request on the GitHub page, worded for people."""
    pr = t.get("pr") or {}
    state = pr_state(t)
    number = pr.get("number")
    if number is None and pr.get("url"):
        m = _PR_NUMBER.search(str(pr["url"]))
        number = int(m.group(1)) if m else None
    repo = pr.get("repo") or (factory_github.parse_pr(pr.get("url") or "") or (None,))[0]
    if state == "merged":
        when = plain_ago(pr.get("merged_at"), now)
        when = " ".join(x for x in ("merged", when, f"by {pr['merged_by']}" if pr.get("merged_by") else "") if x)
    elif state == "closed":
        when = " ".join(x for x in ("closed", plain_ago(pr.get("closed_at"), now)) if x)
    else:
        when = " ".join(x for x in ("opened", plain_ago(pr.get("created_at"), now),
                                    f"by {pr['author']}" if pr.get("author") else "") if x)
    size = None
    if pr.get("changed_files") is not None:
        size = {"files": pr["changed_files"], "additions": pr.get("additions") or 0,
                "deletions": pr.get("deletions") or 0, "commits": pr.get("commits")}
    return {"number": number, "url": pr.get("url") or (f"https://github.com/{repo}/pull/{number}"
                                                       if repo and number else None),
            "repo": repo, "title": clean(pr.get("title") or t["title"]), "ticket": ref,
            "ticket_title": clean(t["title"]), "ticket_href": f"/projects/{uid}/tickets/{ref}",
            "state": state, "state_label": _PR_STATE_LABEL[state],
            "ci": None if state in ("merged", "closed") else _CI_WORD.get(pr.get("ci") or ""),
            "review": review, "branch": pr.get("branch"), "base": pr.get("base"),
            "head": (pr.get("head") or "")[:7] or None, "when": when, "size": size,
            "error": pr.get("error"), "_at": str(pr.get("merged_at") or pr.get("closed_at")
                                                  or pr.get("created_at") or "")}


def recent_prs(tickets: list[dict], uid: str, reviews: dict, now, clean=str, n: int = 3) -> list[dict]:
    """The `n` pull requests the project's tickets opened last (newest first; a PR GitHub never
    dated goes by its number)."""
    with_pr = [t for t in tickets if pr_state(t)]
    with_pr.sort(key=lambda t: (str((t["pr"] or {}).get("created_at") or ""),
                                (t["pr"] or {}).get("number") or 0), reverse=True)
    out = []
    for t in with_pr[:n]:
        c = pr_card(t, factory_mod.label(t), uid, _review_out(reviews.get(t["id"]), t), now, clean)
        c.pop("_at")
        out.append(c)
    return out


def _review_out(last: dict | None, t: dict) -> str | None:
    v = factory_api.verdict_now(t, last)
    if not v:
        return None
    word = _VERDICT_WORD.get(v["verdict"], v["verdict"])
    if v["current"]:
        return f"round {v['round']}: {word}"
    return f"round {v['round']}: {word}, then the pull request changed"


def register(app, store, h, ctx):
    def read_roles() -> tuple[dict[str, str], list[dict]]:
        try:
            rows = store.station_sessions(h.cc_source())
        except Exception:
            rows = []
        return station_roles(rows), rows

    def project_people(p: dict, tickets: list[dict], request: Request) -> dict:
        """Who is on the project (top right of the page): its lead and its members, each {id,
        name, picture, tickets}. A member is anyone added to the project or holding an open ticket
        in it. Also whether the caller may set the lead (an admin, or the lead) and add members
        (the lead), and the people who can be picked. Both are false on a cell without an
        enterprise licence: the routes behind them answer 402 there, so the page shows no control."""
        def ref(pid):
            x = store.get_person(pid) if pid else None
            return ({"id": x["id"], "name": x["name"] or x.get("email") or x["id"],
                     "picture": x.get("picture")} if x else None)
        counts: dict[str, int] = {}
        for t in tickets:
            who = t.get("assignee_person_id")
            if who and t.get("status") != "canceled":
                counts[who] = counts.get(who, 0) + 1
        lead = ref(p.get("lead_person_id"))
        if lead:
            lead["tickets"] = counts.get(lead["id"], 0)
        ids = [m["person_id"] for m in store.list_project_members(p["id"])]
        ids += [i for i in counts if i not in ids]
        members = [{**r, "tickets": counts.get(r["id"], 0)} for r in (ref(i) for i in ids)
                   if r and (not lead or r["id"] != lead["id"])]
        me = h.current_person(request) or {}
        scopes = getattr(request.state, "scopes", None)
        admin = scopes is None or "admin" in scopes
        is_lead = bool(lead and me.get("id") == lead["id"])
        # the cell's first person before anyone signed in (no email, named "You") is nobody to pick
        everyone = [r for r in (ref(x["id"]) for x in store.list_people()
                                if not (x["id"] == "local" and not x.get("email"))) if r]
        team = team_mod.enabled()   # the ee team loaded and the licence allows it; a key alone is not enough
        return {"lead": lead, "members": members,
                "can_set_lead": team and (admin or is_lead),
                "can_add": team and (is_lead or h.is_system(request)),
                "you": me.get("id"), "people": everyone if (team and (admin or is_lead)) else []}

    @app.get("/api/factory/projects/{uid}")
    def factory_project(uid: str, request: Request):
        """The whole project page in one read (404 for a project that is not a software factory
        project, and for a draft, which opens at its setup on today's page)."""
        p = store.get_project(uid)
        if (p is None or p.get("kind") != "software_factory" or p.get("status") == "draft"
                or not h.can_see(p)):   # a private project the caller is not on is not there
            raise HTTPException(404, f"no factory project {uid!r}")
        now = now_utc()
        tickets = store.list_tickets(uid)
        milestones = store.list_milestones(uid)
        handover = store.get_handover(uid)
        roles, station_rows = read_roles()
        rd = factory_mod.readiness(tickets)
        reviews = store.last_reviews([t["id"] for t in tickets])
        number = {m["id"]: f"M{i + 1}" for i, m in enumerate(milestones)}
        refs = {t["id"]: factory_mod.label(t) for t in tickets}

        # the crew's folders stay on the crew's machine: free text that names one shows the repo
        clean = cleaner(handover)

        def ticket_out(t: dict) -> dict:
            done = factory_mod.finished(t)
            blocked_by = [] if done else rd[t["id"]]["blocked_by"]
            ref = refs[t["id"]]
            return {"ref": ref, "title": clean(t["title"]), "state": ticket_state(t, blocked_by),
                    "who": None if done else who_words(t.get("holder"), roles,
                                                       number.get(t.get("milestone"))),
                    "pr": _pr_out(t), "review": _review_out(reviews.get(t["id"]), t),
                    "blocked_by": blocked_by, "href": f"/projects/{uid}/tickets/{ref}"}

        states = {t["id"]: ticket_state(t, rd[t["id"]]["blocked_by"]) for t in tickets}
        counts = {"total": len(tickets), **{k: 0 for k in _COUNT_KEY.values()}}
        for t in tickets:
            counts[_COUNT_KEY[states[t["id"]]]] += 1
        # the tickets that move now: changes asked, then in review, in progress, blocked; each
        # state in the project's own order
        movers = sorted((t for t in tickets if states[t["id"]] in _MOVING),
                        key=lambda t: _MOVING.index(states[t["id"]]))
        moving = [ticket_out(t) for t in movers[:_MOVING_CAP]]

        # whom the project's checks wait for, by the one rule (factory_owners): its lead, else the
        # admins; the caller answers them only when they are among them
        owners = fh.owners_mod.Owners(store)
        check_for = owners.who(owners.project(p))
        yours = fh.caller_owns(store, fh.for_caller(ctx.fx, request), request, p, owners)
        out_ms = []
        results = store.milestone_check_results([m["id"] for m in milestones])
        crew_with_it = fw.crew_has(store, p)
        accepted = store.milestone_acceptances([m["id"] for m in milestones])
        for i, m in enumerate(milestones):
            ts = [t for t in tickets if t.get("milestone") == m["id"] and states[t["id"]] != "canceled"]
            merged = sum(1 for t in ts if states[t["id"]] == "merged")
            res = results.get(m["id"]) or {}
            st = factory_mod.milestone_state(m, [t for t in tickets if t.get("milestone") == m["id"]],
                                             res, accepted.get(m["id"]), crew_with_it)
            out_ms.append({"id": m["id"], "number": f"M{i + 1}", "name": m["name"],
                           "goal": clean(m["goal"]),
                           "done": bool(ts) and merged == len(ts),
                           "counts": {"total": len(ts), "merged": merged},
                           # each check with who it is for and its latest result (TR-1017)
                           "checks": [{"n": j + 1, "check": clean(c.get("check")),
                                       "expect": clean(c.get("expect")),
                                       "for": c.get("for") or "crew",
                                       "result": (res.get(j) or {}).get("result"),
                                       "note": clean((res.get(j) or {}).get("note")) or None,
                                       "by": clean((res.get(j) or {}).get("by_name")) or None}
                                      for j, c in enumerate(m["checks"])],
                           "accepted": st["accepted"], "needs_person": st["needs_person"],
                           # accept as it is: the crew never recorded its checks and none is for you
                           "can_accept": st["needs_person"] and not st["person_open"] and yours,
                           # the person checks wait for these people; `yours` when the caller is one
                           "checks_for": check_for, "yours": yours,
                           "tickets_href": f"/projects/{uid}/tickets?milestone={m['id']}"})

        # the ticket that goes next: the first ready one that has not started. Moving now lists
        # the tickets being worked on and the ones that wait, so next is never one of those.
        moving_ids = {t["id"] for t in movers}
        pick = next((t for t in tickets if not factory_mod.finished(t) and t["id"] not in moving_ids
                     and rd[t["id"]]["ready"]), None)
        nxt = ({"ticket": refs[pick["id"]], "title": clean(pick["title"]),
                "why": "ready, nothing it depends on is open"} if pick is not None else None)

        # what waits for the caller here is what their home lists for this project; on a team
        # cell what waits for someone else is under "Waiting for others", named for them, with
        # nothing to press, and does not make the project "Waiting on you" (fh.project_waiting)
        waiting, others = fh.project_waiting(store, fh.for_caller(ctx.fx, request), now, request, uid)
        for w in [*waiting, *others]:        # ids, links and what a button does stay as they are
            for k in ("label", "title", "detail", "meta"):
                if isinstance(w.get(k), str):
                    w[k] = clean(w[k])
            w["actions"] = [{**a, "label": clean(a["label"])} if isinstance(a.get("label"), str) else a
                            for a in w.get("actions") or []]
        gids = set(h.global_ids().values())
        own = [d for d in store.list_docs(uid) if d["id"] not in gids]
        has_start = any(d["kind"] == "start" for d in own)
        closed = bool((handover or {}).get("finished")) or p.get("status") == "done"
        stage = stage_of(has_start, tickets, closed)
        later = p.get("status") == "later"
        state = ("finished" if closed else "later" if later
                 else fh.project_state(p, tickets, handover, waiting))
        # Later is for a project nobody has handed to the crew yet (or the crew finished)
        with_crew = bool(handover) and not (handover or {}).get("finished")

        # the answer chosen, and the person's words only when they add something (TR-1018)
        decisions = [{"question": clean(d["question"]),
                      **{k: clean(v) if v else None for k, v in factory_mod.decision_words(d).items()},
                      "when": plain_ago(d["at"], now), "at": fh._iso(d["at"])}
                     for d in store.list_decisions(uid)]
        calls, calls_past = [], []
        for a in store.list_assumptions(uid):
            by = _ROLE_BY.get(a.get("made_role") or "", "the crew")
            c = {"ref": a["label"], "question": clean(a["question"]),
                 "choice": clean(a["choice"]), "why": clean(a["why"]),
                 "made": f"by {by}, {plain_ago(a['made_at'], now)}".rstrip(", "),
                 "tickets": [refs[x] for x in a["tickets"] if x in refs]}
            if a["state"] == "open":
                calls.append(c)
            elif a["state"] in ("kept", "overturned"):
                # the record on the calls page: what the person did with it, newest first
                calls_past.append({**c, "state": a["state"], "words": clean(a["words"]) if a.get("words") else None,
                                   "decided": plain_ago(a["decided_at"], now) if a.get("decided_at") else "",
                                   "at": fh._iso(a.get("decided_at"))})
        calls_past.sort(key=lambda c: c["at"] or "", reverse=True)

        # the card shows the four the project is about, one of each (the first written); every
        # document, working notes and notes too, is on the project's docs page
        first: dict[str, dict] = {}
        for d in sorted((d for d in own if d["kind"] in _DOC_LABEL), key=lambda d: str(d["created_at"])):
            first.setdefault(d["kind"], d)
        docs = [{"id": first[k]["id"], "title": clean(first[k]["title"]), "kind_label": _DOC_LABEL[k],
                 "updated": plain_ago(first[k]["updated_at"], now),
                 "href": f"/projects/{uid}/docs?doc={first[k]['id']}"}
                for k in ("spec", "plan", "agents", "memory") if k in first]
        docs_total = sum(1 for d in own if d["kind"] in _DOC_LABEL)

        # a builder's station plays by its newest session, shown as the crew page shows it
        # (factory_api.stations): an older session that is still working does not bring it back
        newest: dict[str, dict] = {}
        for r in station_rows:
            name, cur = r.get("station"), newest.get(r.get("station"))
            if name and (cur is None or _stamp(r.get("last_at")) > _stamp(cur.get("last_at"))):
                newest[name] = r
        terminal = [{"label": "Open the builder's session",
                     "command": factory_mod.attach_command(name)}
                    for name, s in newest.items()
                    if s.get("role") == "builder" and s.get("project") == uid
                    and factory_mod.shown_state(s.get("state") or "working", s.get("last_at"), now)
                    not in _GONE]
        terminal = list({t["command"]: t for t in terminal}.values())

        people = project_people(p, tickets, request)
        home = store.project_ticket_home(uid)
        link = (store.get_project_linear(uid) if home == "linear"
                else store.get_project_github(uid) if home == "github" else None)
        return {"id": p["id"], "name": p["name"], "icon": p.get("icon"),
                # changing the icon is an edit of the project (PUT /api/projects/{id}): admin
                "can_edit_icon": h.is_admin(request),
                "goal": clean(p.get("goal")),
                "stage": stage, "stage_label": STAGE_LABEL[stage],
                "state": state, "state_label": STATE_LABEL[state],
                "waiting": waiting, "waiting_others": others, "counts": counts, "moving": moving, "moving_total": len(movers),
                "tickets_href": f"/projects/{uid}/tickets", "next": nxt,
                # how many pull requests the GitHub page lists, and how many in each state
                "prs_total": sum(1 for t in tickets if pr_state(t)),
                "prs_counts": {"open": sum(pr_state(t) in ("open", "draft") for t in tickets),
                               "merged": sum(pr_state(t) == "merged" for t in tickets),
                               "closed": sum(pr_state(t) == "closed" for t in tickets)},
                # the three newest pull requests, as the GitHub page words them
                "prs_recent": recent_prs(tickets, uid, reviews, now, clean), "milestones": out_ms,
                "decisions": decisions, "calls": calls, "calls_past": calls_past, "docs": docs, "docs_total": docs_total, "terminal": terminal,
                "linked": {"tickets_in": {"linear": "Linear", "github": "GitHub"}.get(home or ""),
                           "url": (link or {}).get("url")},
                # Close is for whoever owns the project's work (factory_owners)
                "can_close": factory_mod.ready_to_close(p, tickets) and yours,
                "later": later, "can_later": not later and not closed and not with_crew,
                "people": people,
                # the repos handed over to the crew, by name only: their folders stay on the crew's machine
                "repos": repo_names(handover),
                "secrets": project_secrets.page_listing(store, h, p, request),   # TR-775: None when not a member
                "now": fh._iso(now)}

    @app.get("/api/factory/projects/{uid}/github")
    async def factory_project_github(uid: str, request: Request):
        """The project's GitHub page: its pull requests by repo (the open ones first, then merged,
        then closed without merging, newest first in each), read again from GitHub when stale,
        waiting at most a moment for it (factory_api.refresh_prs)."""
        p = store.get_project(uid)
        if (p is None or p.get("kind") != "software_factory" or p.get("status") == "draft"
                or not h.can_see(p)):
            raise HTTPException(404, f"no factory project {uid!r}")
        await factory_api.refresh_prs(store, uid)
        now = now_utc()
        tickets = store.list_tickets(uid)
        reviews = store.last_reviews([t["id"] for t in tickets if pr_state(t)])
        handover = store.get_handover(uid) or {}
        clean = cleaner(handover)
        cards = [pr_card(t, factory_mod.label(t), uid, _review_out(reviews.get(t["id"]), t), now, clean)
                 for t in tickets if pr_state(t)]
        order = [r.get("name") for r in handover.get("repos") or [] if r.get("name")]
        by_repo: dict[str, list[dict]] = {}
        for c in cards:
            by_repo.setdefault(c["repo"] or "", []).append(c)
        repos = []
        for name in sorted(by_repo, key=lambda n: (not n, order.index(n.rsplit("/", 1)[-1])
                                                   if n.rsplit("/", 1)[-1] in order else len(order), n)):
            prs = sorted(by_repo[name], key=lambda c: c["_at"], reverse=True)
            prs.sort(key=lambda c: _PR_ORDER[c["state"]])
            for c in prs:
                c.pop("_at")
            merged = [c for c in prs if c["state"] == "merged"]
            repos.append({"name": name or None,
                          "url": f"https://github.com/{name}" if name else None,
                          "counts": {"open": sum(c["state"] in ("open", "draft") for c in prs),
                                     "merged": len(merged),
                                     "closed": sum(c["state"] == "closed" for c in prs)},
                          "added": sum((c["size"] or {}).get("additions", 0) for c in merged),
                          "removed": sum((c["size"] or {}).get("deletions", 0) for c in merged),
                          "prs": prs})
        # the last read that worked; a failed read says why instead
        checked = max((str((t.get("pr") or {}).get("checked_at") or "") for t in tickets
                       if pr_state(t) and not (t.get("pr") or {}).get("error")), default="")
        errors = [c["error"] for c in cards if c["error"]]
        shared = errors[0] if errors and len(set(errors)) == 1 else None
        for c in cards:          # one reason for all of them is said once, at the top
            if shared and c["error"] == shared:
                c["error"] = None
        return {"id": p["id"], "name": p["name"], "icon": p.get("icon"), "repos": repos,
                "counts": {k: sum(r["counts"][k] for r in repos) for k in ("open", "merged", "closed")},
                "checked": plain_ago(checked, now) if checked else None,
                "github_error": (errors[0][:1].upper() + errors[0][1:] + ".") if shared
                                else (f"{len(errors)} could not be read, each says why." if errors else None),
                "now": fh._iso(now)}
