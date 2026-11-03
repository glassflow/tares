"""What a person's home shows on a cell with more than one person (TR-739).

`team_home(c, request)` answers for the person of the key; factory_api.register builds `c` (its
own readers: who a ticket or desk item belongs to, the sign-off rows, the grants waiting for
consent, the review queue) and hands the function out as `fx.team_home`. `factory_home.build`
merges what it returns into the body of GET /api/home and replaces `waiting_for_you` with it. A
cell whose only person is `local` gets `{}` here, so its home stays what TR-729 and TR-769 return.

Every item goes through `factory_home.home_item`, so the shapes, kinds and verbs are the ones
the single-person home uses. Nothing here is kept: it is all read from what the cell stores.
"""
from __future__ import annotations

from . import areas as areas_mod
from . import factory_home as fh
from . import factory_waiting as fw

YOUR_CREW = {"crew": True, "name": "Your crew"}


class Team:
    """One read of the home for one person: who they are, what they cannot see, and the crew
    rows, read once and only when something asks for them."""

    def __init__(self, c, request, me: dict, now):
        self.c, self.request, self.me, self.now = c, request, me, now
        self.store = c.store
        self.hidden = c.h.hidden_ids()
        self._stations = None
        self._names = None

    def stations(self) -> list[dict]:
        if self._stations is None:
            self._stations = self.c.stations()
        return self._stations

    def names(self) -> dict[str, str]:
        """{project id: name} of every project."""
        if self._names is None:
            self._names = {p["id"]: p["name"] for p in self.store.list_projects()}
        return self._names


def _ref(person: dict | None) -> dict | None:
    """{id, name} of a person; the id stands in for a name nobody set."""
    if not person or not person.get("id"):
        return None
    return {"id": person["id"], "name": person.get("name") or person["id"]}


def _project(t: Team, pid: str | None) -> dict | None:
    """{id, name} of a project the person can see, else None."""
    name = t.names().get(pid) if pid else None
    return {"id": pid, "name": name} if name and pid not in t.hidden else None


# ── a question a spec asked of the person ────────────────────────────────────

def question_items(t: Team) -> list[dict]:
    out = []
    for q in t.store.list_spec_questions(state="open", to_person_id=t.me["id"]):
        project = _project(t, q["project"])
        if project is None:
            continue
        out.append(fh.home_item(
            "question", label="Question for you", title=f"{project['name']}, {q['id']}",
            detail=q["text"], meta="Asked " + fh.plain_ago(q["asked_at"], t.now),
            since=q["asked_at"], now=t.now, project=project, repo=q["repo"],
            from_=_ref(q["asked_by_person"]), id=f"question:{project['id']}:{q['id']}",
            actions=[{"label": "Answer", "primary": True, "do": "answer",
                      "project": project["id"], "question": q["id"]}]))
    return out


# ── a repo the person owns, in a project that cannot go to the crew until they sign off ──

def _tickets_in(tickets: list[dict], repo: str) -> list[dict]:
    out = []
    for tk in tickets:
        if tk["status"] == "canceled":
            continue
        try:
            if areas_mod.norm_repo(tk.get("repo")) == repo:
                out.append(tk)
        except ValueError:
            pass
    return out


def signoff_items(t: Team) -> list[dict]:
    handed = set(t.store.handed_over_projects())
    out = []
    for p in t.store.list_projects():
        if (p.get("kind") != "software_factory" or p["status"] == "done" or p["id"] in handed
                or p["id"] in t.hidden):
            continue
        rows = [r for r in t.c.signoff_state(p["id"], p)
                if r["owner"] and r["owner"]["id"] == t.me["id"] and not r["exempt"]
                and not r["signed_off"]]
        if not rows:
            continue
        tickets = t.store.list_tickets(p["id"])
        lead = _ref(t.store.get_person(p["lead_person_id"])) if p.get("lead_person_id") else None
        for r in rows:
            mine = _tickets_in(tickets, r["repo"])
            n = len(mine)
            since = min((tk["created_at"] for tk in mine if tk.get("created_at")),
                        key=lambda v: fh._dt(v) or t.now, default=p.get("created_at") or t.now)
            who = (lead["name"] + " cannot hand the project") if lead else "The project cannot go"
            out.append(fh.home_item(
                "signoff", label="Sign-off needed", title=p["name"],
                detail=f"{fh._count(n, 'ticket')} {'touches' if n == 1 else 'touch'} {r['repo']}. "
                       f"{who} to a crew until you sign off.",
                meta="Waiting " + fw._for(fh.plain_wait(fh._minutes(since, t.now))),
                since=since, now=t.now, project={"id": p["id"], "name": p["name"]}, repo=r["repo"],
                from_=lead, id=f"signoff:{p['id']}:{r['repo']}",
                actions=[{"label": "Sign off", "primary": True, "do": "sign_off",
                          "project": p["id"], "repo": r["repo"]},
                         {"label": "Read the ticket" if n == 1 else f"Read the {n} tickets",
                          "primary": False, "href": f"/projects/{p['id']}"}]))
    return out


# ── a grant that waits for the person to agree ───────────────────────────────

def grant_items(t: Team) -> list[dict]:
    rows = [g for g in t.store.all_grant_rows()
            if g["state"] == "waiting" and g["kind"] == "project"
            and t.c.waits_for_me(g, t.request)]
    if not rows:
        return []
    refs = t.store._person_refs({g["given_by"] for g in rows})
    out = []
    for g in rows:
        # a grant on a private project the person is not on shows its words and repos, not the project
        project = _project(t, g["project"])
        giver = _ref(refs.get(g["given_by"]))
        who = giver["name"] if giver else "Someone"
        owned = [r for r in g["areas"] if (t.c.h.area_owner(r) or {}).get("id") == t.me["id"]]
        if owned:
            detail = (f"{who} gave this for {fw._and(owned)}, which you "
                      f"own. It takes effect when you agree.")
        elif t.c.waits_for_me(g, t.request) == "admin":
            detail = "It takes effect when an admin agrees."
        else:
            detail = (f"{who} gave this on {project['name'] if project else 'a project'}, which you "
                      "lead. It takes effect when you agree.")
        since = g["given_at"] or t.now
        out.append(fh.home_item(
            "grant", label="Grant to agree to", title=fw._trim(g["words"], 80), detail=detail,
            meta="Asked " + fh.plain_ago(since, t.now), since=since, now=t.now, project=project,
            repo=owned[0] if len(owned) == 1 else None, from_=giver, id=f"grant:{g['id']}",
            actions=[{"label": "Agree", "primary": True, "do": "agree", "grant": g["id"]},
                     {"label": "Decline", "primary": False, "do": "decline", "grant": g["id"]}]))
    return out


# ── a desk item addressed to the person ──────────────────────────────────────

def _desk_from(t: Team, d: dict) -> dict | None:
    """The person's own crew when the crew that asked is theirs, else the person whose crew
    asked; an item no crew is known for comes from the person who put it on the desk, else from
    the person's crew."""
    rows = [s for s in t.stations() if s["name"] == (d.get("station") or "")]
    if any((s.get("person") or {}).get("id") == t.me["id"] for s in rows):
        return YOUR_CREW
    for s in rows:
        if _ref(s.get("person")):
            return _ref(s["person"])
    return _ref(d.get("person")) or YOUR_CREW


def desk_items(t: Team) -> list[dict]:
    rows = [d for d in t.store.list_desk("open")
            if t.c.desk_is_mine(d, t.me["id"]) and d["project"] not in t.hidden]
    out = []
    for d in rows:
        project = _project(t, d["project"])
        since = d["created_at"]
        # the grant may hold in its project alone unless a crew station asked (as the single-person
        # home decides it)
        may_limit = bool(project) and not str(d.get("station") or "the station").startswith("crew-")
        base = dict(since=since, now=t.now, project=project, from_=_desk_from(t, d),
                    id=f"desk:{d['label']}", meta="Asked " + fh.plain_ago(since, t.now),
                    title=project["name"] if project else "Every project")
        if d.get("command"):
            who = fw._role_of_station(d.get("station"))
            out.append(fh.home_item(
                "desk", label="Permission",
                detail=f"{who[0].upper()}{who[1:]} wants to use: `{d['command']}`", **base,
                actions=[{"label": "Answer", "primary": False, "do": "answer", "desk": d["label"]},
                         {"label": "Allow", "primary": True, "do": "allow", "desk": d["label"],
                          "may_limit": may_limit}]))
        else:
            out.append(fh.home_item(
                "desk", label="Question for you", detail=d["question"], **base,
                actions=[{"label": "Answer", "primary": True, "do": "answer",
                          "desk": d["label"]}]))
    return out


# ── milestones of the projects the person leads whose tickets all merged ─────

def milestone_items(t: Team) -> list[dict]:
    out = []
    for led in t.c.work_of(t.me, t.hidden, detail=True)["led_projects"]:
        for m in led["milestones"]:
            pid = led["project_id"]
            out.append(fh.home_item(
                "milestone_to_check", label="Milestone to check",
                title=f"{led['project']}, {m['name']}",
                detail="Every ticket of this milestone merged."
                + (f" Try its {fh._count(m['checks'], 'check')}." if m["checks"] else ""),
                meta="Merged " + fh.plain_ago(m["since"], t.now), since=m["since"], now=t.now,
                project={"id": pid, "name": led["project"]}, from_=YOUR_CREW,
                id=f"milestone_to_check:{pid}:{m['id']}",
                actions=[{"label": "Check it", "primary": True,
                          "href": f"/projects/{pid}#milestone-{m['id']}"}]))
    return out


# ── pull requests the reviewer passed, in the projects the person leads or holds tickets in ──

def pr_items(t: Team) -> list[dict]:
    from types import SimpleNamespace
    mine = t.c.my_projects(t.request)   # None: nothing narrows the list (the cell's own credential)
    fx = SimpleNamespace(queue=t.c.queue, settings=lambda: t.c.settings(t.request))
    return [i for i in fw.prs_to_review(t.store, fx, t.now)
            if mine is None or (i["project"] or {}).get("id") in mine]


# ── the whole answer ─────────────────────────────────────────────────────────

BUILDERS = [question_items, signoff_items, grant_items, desk_items, milestone_items, pr_items]


def summary(waiting: list[dict]) -> str:
    """"Nothing waits for you.", "1 thing waits for you.", "4 things wait for you." """
    n = len(waiting)
    if not n:
        return "Nothing waits for you."
    return "1 thing waits for you." if n == 1 else f"{n} things wait for you."


def team_home(c, request, now) -> dict:
    """The team keys of the home body for the caller, or {} for a caller the single-person home
    answers: a cell of one, or a key with no person."""
    if c.store.only_local_person():
        return {}
    if c.h.current_person(request) is None and not c.h.is_system(request):
        return {}
    t = Team(c, request, c.work_person(request), now)
    waiting = [i for build in BUILDERS for i in build(t)]
    waiting.sort(key=lambda i: fh._dt(i["since"]) or now)
    return {"waiting_for_you": waiting, "waiting_total": len(waiting), "summary": summary(waiting)}
