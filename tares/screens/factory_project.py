"""The factory project page (TR-734): GET /api/factory/projects/{uid}.

One answer for the whole page: where the project stands (spec, build, review or done), what
waits for the person on this project, how many tickets are in each state (`counts`), the few
that move now (`moving`: changes asked, in review, in progress, blocked, at most 5, with
`moving_total` for how many there are), the ticket that goes next, the milestones as compact
rows (a number, a bar's worth of counts, the acceptance checks and a link to their tickets, no
ticket lists), the decisions and the open calls the crew made, the project's documents, the
command that opens a builder's session, and where the tickets live. The full ticket list is the
Tickets screen, which `tickets_href` and each milestone's `tickets_href` open. The tickets are
read once.

Every string is worded for people: a builder is "Builder for M1", never the name of its
station; a time is "3 days ago"; a ticket is "in review", never a stage key. The only folder
path that could reach the answer is the crew's hand-over repo path, and it is kept out: free
text that names one shows the repo instead. The one place a station name appears is the
terminal command that opens a builder's session, because `tares factory crew attach` takes it.

`stage_of` and `who_words` are used by the ticket page (TR-760) as well.
"""
from __future__ import annotations

import re

from fastapi import HTTPException, Request

from .. import factory as factory_mod
from .. import factory_api
from .. import factory_home as fh
from ..envelope import now_utc
from ..factory_home import plain_ago

# ── words ─────────────────────────────────────────────────────────────────────

STAGE_LABEL = {"spec": "Spec", "build": "Build", "review": "Review", "done": "Done"}
STATE_LABEL = {**fh.STATE_LABEL, "finished": "Finished"}

# a ticket's state in words, by its stage key
_STAGE_WORD = {"todo": "to do", "doing": "in progress", "review": "in review",
               "changes": "changes asked", "blocked": "blocked", "merged": "merged",
               "shipped": "merged", "canceled": "canceled"}
_VERDICT_WORD = {"pass": "passed", "changes": "changes asked", "block": "blocked"}
_CI_WORD = {"success": "CI green", "failure": "CI failing", "pending": "CI running"}
# the crew's roles as a person says them, as the author of a call: "by the builder"
_ROLE_BY = {"orchestrator": "the lead", "reviewer": "the reviewer", "releaser": "the releaser",
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
    """A ticket's state in words, counted as the project's ring counts it (factory.progress)."""
    stage, status = t.get("stage"), t.get("status")
    if stage in ("merged", "shipped") or status == "done":
        return "merged"
    if status == "canceled":
        return "canceled"
    if stage == "blocked":
        return "blocked"
    if stage in ("doing", "review", "changes"):
        return _STAGE_WORD[stage]
    if status == "in_progress":
        return "in progress"
    return "blocked" if blocked_by else "to do"


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

    @app.get("/api/factory/projects/{uid}")
    def factory_project(uid: str, request: Request):
        """The whole project page in one read (404 for a project that is not a software factory
        project, and for a draft, which opens at its setup on today's page)."""
        p = store.get_project(uid)
        if p is None or p.get("kind") != "software_factory" or p.get("status") == "draft":
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
        hidden = {}
        for r in (handover or {}).get("repos") or []:
            if r.get("path"):
                name = r.get("name") or r["path"].rstrip("/").rsplit("/", 1)[-1] or "the repo"
                base = r["path"].rstrip("/")
                if base:                                  # with or without a trailing slash
                    hidden[base] = name

        def clean(text):
            out = str(text or "")
            for path in sorted(hidden, key=len, reverse=True):
                out = out.replace(path, hidden[path])
            return out

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

        out_ms = []
        for i, m in enumerate(milestones):
            ts = [t for t in tickets if t.get("milestone") == m["id"] and states[t["id"]] != "canceled"]
            merged = sum(1 for t in ts if states[t["id"]] == "merged")
            out_ms.append({"id": m["id"], "number": f"M{i + 1}", "name": m["name"],
                           "goal": clean(m["goal"]),
                           "done": bool(ts) and merged == len(ts),
                           "counts": {"total": len(ts), "merged": merged},
                           "checks": [{"check": clean(c.get("check")), "expect": clean(c.get("expect"))}
                                      for c in m["checks"]],
                           "tickets_href": f"/tickets?project={uid}&milestone={m['id']}"})

        # the ticket that goes next: the one being built, else the first ready, else the first
        # that waits
        nxt = None
        open_ = [t for t in tickets if not factory_mod.finished(t)]
        building = next((t for t in open_ if t.get("stage") != "blocked"
                         and (t.get("status") == "in_progress"
                              or t.get("stage") in ("doing", "review", "changes"))), None)
        ready = next((t for t in open_ if rd[t["id"]]["ready"]), None)
        pick = building or ready or (open_[0] if open_ else None)
        if pick is not None:
            if pick is building:
                why = "being worked on now"
            elif pick is ready:
                why = "ready, nothing it depends on is open"
            elif pick.get("stage") == "blocked":
                why = "blocked"
            else:
                why = "waits for " + ", ".join(rd[pick["id"]]["blocked_by"] or ["an earlier ticket"])
            nxt = {"ticket": refs[pick["id"]], "title": clean(pick["title"]), "why": why}

        waiting = [w for w in fh.waiting_items(store, fh.for_caller(ctx.fx, request), now)
                   if (w.get("project") or {}).get("id") == uid]
        for w in waiting:        # ids, links and what a button does stay as they are
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
        state = "finished" if closed else fh.project_state(p, tickets, handover, waiting)

        decisions = [{"question": clean(d["question"]), "words": clean(d["words"]),
                      "when": plain_ago(d["at"], now), "at": fh._iso(d["at"])}
                     for d in store.list_decisions(uid)]
        calls = []
        for a in store.list_assumptions(uid, state="open"):
            by = _ROLE_BY.get(a.get("made_role") or "", "the crew")
            calls.append({"ref": a["label"], "question": clean(a["question"]),
                          "choice": clean(a["choice"]), "why": clean(a["why"]),
                          "made": f"by {by}, {plain_ago(a['made_at'], now)}".rstrip(", "),
                          "tickets": [refs[x] for x in a["tickets"] if x in refs]})

        order = list(_DOC_LABEL)
        docs = [{"id": d["id"], "title": clean(d["title"]), "kind_label": _DOC_LABEL[d["kind"]],
                 "updated": plain_ago(d["updated_at"], now), "href": f"/docs?doc={d['id']}"}
                for d in sorted((d for d in own if d["kind"] in _DOC_LABEL),
                                key=lambda d: (order.index(d["kind"]), str(d["created_at"])))]

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

        home = store.project_ticket_home(uid)
        link = (store.get_project_linear(uid) if home == "linear"
                else store.get_project_github(uid) if home == "github" else None)
        return {"id": p["id"], "name": p["name"], "goal": clean(p.get("goal")),
                "stage": stage, "stage_label": STAGE_LABEL[stage],
                "state": state, "state_label": STATE_LABEL[state],
                "waiting": waiting, "counts": counts, "moving": moving, "moving_total": len(movers),
                "tickets_href": f"/tickets?project={uid}", "next": nxt, "milestones": out_ms,
                "decisions": decisions, "calls": calls, "docs": docs, "terminal": terminal,
                "linked": {"tickets_in": {"linear": "Linear", "github": "GitHub"}.get(home or ""),
                           "url": (link or {}).get("url")},
                "can_close": factory_mod.ready_to_close(p, tickets),
                "now": fh._iso(now)}
