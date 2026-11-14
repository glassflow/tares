# Tares enterprise licence, see ee/LICENSE
"""What a person's home shows on a cell with more than one person (TR-739).

`team_home(c, request)` answers for the person of the key; factory_api.register builds `c` (its
own readers: who a ticket or desk item belongs to, the grants waiting for
consent, the review queue) and hands the function out as `fx.team_home`. `factory_home.build`
merges what it returns into the body of GET /api/home and replaces `waiting_for_you` with it. A
cell whose only person is `local` gets `{}` here, so its home stays what TR-729 and TR-769 return.

Every item goes through `factory_home.home_item`, so the shapes, kinds and verbs are the ones
the single-person home uses. Nothing here is kept: it is all read from what the cell stores.
"""
from __future__ import annotations

from . import areas as areas_mod
from . import grants as grants_mod
from tares import factory as factory_mod, factory_home as fh, factory_waiting as fw

YOUR_CREW = {"crew": True, "name": "Your crew"}


class HomeRead:
    """One read of the home for one person: who they are, what the caller cannot see, and the crew
    rows, read once and only when something asks for them.

    `viewer` is False when the read is for someone other than the caller (the project page's
    "Waiting for others"): then what the builders ask of "the person" is asked of `me`, never of
    the request's credential. `share` is another read of the same request whose reads (stations,
    names, people, queue, the cell-wide item lists) this one reuses."""

    def __init__(self, c, request, me: dict, now, viewer: bool = True, share: "HomeRead | None" = None):
        self.c, self.request, self.me, self.now = c, request, me, now
        self.store = c.store
        self.viewer = viewer
        self._reads: dict = share._reads if share is not None else {}
        self.hidden = share.hidden if share is not None else c.h.hidden_ids()

    def _read(self, key: str, fn):
        if key not in self._reads:
            self._reads[key] = fn()
        return self._reads[key]

    def stations(self) -> list[dict]:
        return self._read("stations", self.c.stations)

    def queue(self, keep=None) -> list[dict]:
        """The review queue, read once per request."""
        return self.c.queue(keep) if keep is not None else self._read("queue", self.c.queue)

    def names(self) -> dict[str, str]:
        """{project id: name} of every project."""
        return self._read("names", self._projects)[0]

    @property
    def later(self) -> set[str]:
        """The ids of the projects kept for later."""
        return self._read("names", self._projects)[1]

    def _projects(self):
        ps = self.store.list_projects()
        return {p["id"]: p["name"] for p in ps}, {p["id"] for p in ps if p.get("status") == "later"}

    def people(self) -> dict[str, str]:
        """{person id: name} of everyone on the cell."""
        return self._read("people", lambda: {p["id"]: p.get("name") or p["id"]
                                             for p in self.store.list_people()})

    def person(self, pid: str | None) -> dict | None:
        """{id, name} of a person by id, or None."""
        return {"id": pid, "name": self.people().get(pid) or pid} if pid else None

    def leads(self, pid: str | None) -> bool:
        """`me` leads the project (core's rule, c.leads) and the caller can see it."""
        return bool(pid) and pid not in self.hidden and self.c.leads(self.store.get_project(pid), self.me["id"])

    def projects(self) -> set[str] | None:
        """The projects whose pull requests wait for `me`: those they lead or hold a ticket in
        (core's my_projects rule). None narrows nothing: the caller is the system credential."""
        if self.viewer:
            return self.c.my_projects(self.request)
        return self.c.projects_of(self.me["id"])

    def settings(self) -> dict:
        """`me`'s crew settings (their own over the cell's default): what decides whether a passed
        pull request waits for them to merge."""
        return self.c.settings(self.request) if self.viewer else self.c.settings_of(self.me)

    def consents(self, g: dict) -> str | None:
        """How `me` may agree to grant `g`: "person", "admin" or None (grants.waits_for_me's rule)."""
        if self.viewer:
            return grants_mod.waits_for_me(self.c, g, self.request)
        ids = {w["id"] for w in grants_mod.waiting_for(self.c, g)}
        if self.me["id"] in ids:
            return "person"
        if "admin" in ids and (self.store.get_person(self.me["id"]) or {}).get("is_admin"):
            return "admin"
        return None


def _for_team(c, request) -> bool:
    """True when the team home answers: a cell of more than one person and a caller with a person
    (or the system)."""
    if c.store.only_local_person():
        return False
    return not (c.h.current_person(request) is None and not c.h.is_system(request))


def _ref(person: dict | None) -> dict | None:
    """{id, name, picture} of a person; the id stands in for a name nobody set."""
    if not person or not person.get("id"):
        return None
    out = {"id": person["id"], "name": person.get("name") or person["id"]}
    if person.get("picture"):
        out["picture"] = person["picture"]
    return out


def _project(t: HomeRead, pid: str | None) -> dict | None:
    """{id, name} of a project the person can see and nobody kept for later, else None."""
    name = t.names().get(pid) if pid else None
    return ({"id": pid, "name": name} if name and pid not in t.hidden and pid not in t.later
            else None)


# ── a question a spec asked of the person ────────────────────────────────────

def question_items(t: HomeRead) -> list[dict]:
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


# ── a grant that waits for the person to agree ───────────────────────────────

def grant_items(t: HomeRead) -> list[dict]:
    rows = [g for g in t.store.all_grant_rows()
            if g["state"] == "waiting" and g["kind"] == "project"
            and t.consents(g)]
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
        elif t.consents(g) == "admin":
            detail = "It takes effect when an admin agrees."
        else:
            detail = (f"{who} gave this on {project['name'] if project else 'a project'}, which you "
                      "lead. It takes effect when you agree.")
        since = g["given_at"] or t.now
        # the giver's words may name a private project the person is not on: say what it covers
        title = (f"A grant over {fw._and(owned) if owned else 'a repo you own'}"
                 if g["project"] in t.hidden else fw._trim(g["words"], 80))
        out.append(fh.home_item(
            "grant", label="Grant to agree to", title=title, detail=detail,
            meta="Asked " + fh.plain_ago(since, t.now), since=since, now=t.now, project=project,
            repo=owned[0] if len(owned) == 1 else None, from_=giver, id=f"grant:{g['id']}",
            actions=[{"label": "Agree", "primary": True, "do": "agree", "grant": g["id"]},
                     {"label": "Decline", "primary": False, "do": "decline", "grant": g["id"]}]))
    return out


# ── a desk item addressed to the person ──────────────────────────────────────

def _desk_from(t: HomeRead, d: dict) -> dict | None:
    """The person's own crew when the crew that asked is theirs, else the person whose crew
    asked; an item no crew is known for comes from the person who put it on the desk, else from
    the person's crew."""
    name = d.get("station") or d.get("asked_by") or ""
    rows = [s for s in t.stations() if s["name"] == name]
    if any((s.get("person") or {}).get("id") == t.me["id"] for s in rows):
        return YOUR_CREW
    for s in rows:
        if _ref(s.get("person")):
            return _ref(t.store.get_person(s["person"]["id"]) or s["person"])
    # no station known for it: a crew asked it (asked_by names a station) of this person, so it is
    # their crew's, never "from" themselves (TR-1021); else the person who put it on the desk
    who = _ref(d.get("person"))
    if who is None or who["id"] == t.me["id"]:
        return YOUR_CREW
    return _ref(t.store.get_person(who["id"])) or who


def _open_calls(t: HomeRead, d: dict) -> list[dict]:
    """The assumptions of a desk batch nobody has decided yet."""
    found = (t.store.get_assumption(d["project"], x) for x in d["assumptions"])
    return [a for a in found if a and a["state"] == "open"]


def desk_items(t: HomeRead) -> list[dict]:
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
                    title=project["name"] if project else "Every project",
                    blocking=fw.desk_blocks(d))
        if d.get("assumptions"):
            # A batch of calls: a free answer can never name them all, so the row keeps them all
            # in one step or sends the person to the page where one can be overturned.
            calls = _open_calls(t, d)
            n = len(calls)
            actions = ([{"label": "Change some", "primary": False, "href": f"/projects/{d['project']}/calls"}]
                       if project else [])
            actions.append({"label": "Keep it" if n == 1 else "Keep both" if n == 2 else "Keep all",
                            "primary": True, "do": "keep_batch", "desk": d["label"],
                            "kept": [a["label"] for a in calls]})
            out.append(fh.home_item("desk", label="1 call to confirm" if n == 1 else f"{n} calls to confirm",
                                    detail=d["question"], lines=fw.call_lines(calls), **base, actions=actions))
        elif d.get("command"):
            who = fw._role_of_station(d.get("station"))
            out.append(fh.home_item(
                "desk", label="Permission",
                detail=f"{who[0].upper()}{who[1:]} wants to use: `{d['command']}`", **base,
                actions=[{"label": "Answer", "primary": False, "do": "answer", "desk": d["label"],
                          "options": list(d.get("options") or [])},
                         {"label": "Allow", "primary": True, "do": "allow", "desk": d["label"], "station": d.get("station"),
                          "may_limit": may_limit}]))
        else:
            out.append(fh.home_item(
                "desk", label="Question for you", detail=d["question"], question=fw.desk_question(d), **base,
                actions=[{"label": "Answer", "primary": True, "do": "answer",
                          "desk": d["label"], "options": list(d.get("options") or [])}]))
    return out


# ── milestones of the projects the person leads whose tickets all merged ─────

def milestone_items(t: HomeRead) -> list[dict]:
    """The single-person rule (factory_waiting.milestones_to_check, TR-1017 and TR-1021), kept to
    the projects the person leads: only what truly needs them, nothing for a project ready to
    close, and the crew's unrecorded checks stay the crew's on a project it works on."""
    rows = t._read("milestones", lambda: fw.milestones_to_check(t.store, None, t.now))
    return [{**i, "from": YOUR_CREW} for i in rows if t.leads((i.get("project") or {}).get("id"))]


# ── pull requests the reviewer passed, in the projects the person leads or holds tickets in ──

def pr_items(t: HomeRead) -> list[dict]:
    from types import SimpleNamespace
    mine = t.projects()   # None: nothing narrows the list (the cell's own credential)
    settings = t.settings()
    fx = SimpleNamespace(queue=t.queue, settings=lambda: settings)
    # which pull requests wait for a merge depends on the person's how-far: read once per value
    rows = t._read(f"prs:{settings.get('autonomy')}", lambda: fw.prs_to_review(t.store, fx, t.now))
    return [i for i in rows
            if (i["project"] or {}).get("id") not in t.hidden
            and (mine is None or (i["project"] or {}).get("id") in mine)]


# ── what the single-person home asks of whoever leads a project, kept for its lead ──

def lead_items(t: HomeRead) -> list[dict]:
    """A spec ready to hand to the crew, calls the crew made, a project ready to close: the
    single-person home shows them to the one person, a team home to the project's lead."""
    from types import SimpleNamespace
    fx = SimpleNamespace(queue=t.queue, settings=t.settings)
    led: dict[str, bool] = {}

    def mine(pid: str | None) -> bool:
        if pid not in led:
            led[pid] = t.leads(pid)
        return led[pid]

    # calls already on this person's desk as a batch show once, there
    batched = {(d["project"], x) for d in t.store.list_desk("open")
               if d.get("assumptions") and t.c.desk_is_mine(d, t.me["id"]) for x in d["assumptions"]}
    calls = lambda store, fx_, now: fw.calls_to_confirm(store, fx_, now, skip=batched)
    # the calls depend on what is on this person's desk; the other two are read once per request
    ready = t._read("ready_to_start", lambda: fw.ready_to_start(t.store, fx, t.now))
    close = t._read("projects_to_close", lambda: fw.projects_to_close(t.store, fx, t.now))
    return [i for i in [*ready, *calls(t.store, fx, t.now), *close]
            if mine((i["project"] or {}).get("id"))]


# ── the whole answer ─────────────────────────────────────────────────────────

BUILDERS = [question_items, grant_items, desk_items, milestone_items, pr_items,
            lead_items]


# ═════════════════════════════════════════════════════════════════════════════
# Part two: who the person waits on, the project list, the crews, the repos they own
# ═════════════════════════════════════════════════════════════════════════════

# ── "You are waiting on": the projects the person leads with a question open for someone else ──

def waiting_on(t: HomeRead) -> list[dict]:
    """One entry per project the person leads (`local` also gets those with no lead, marked
    `no_lead`) that is not handed over yet and has a question open for someone else. Nothing
    else holds a project back: a project with nothing open is ready to start, and one kept for
    later is left out."""
    handed = set(t.store.handed_over_projects())
    out = []
    for p in t.store.list_projects():
        if (p.get("kind") != "software_factory" or p["status"] in ("done", "later")
                or p["id"] in handed or p["id"] in t.hidden or not t.c.leads(p, t.me["id"])):
            continue
        questions = [{"id": q["id"], "to": _ref(q["to_person"]) or t.person(q["to_person_id"]),
                      "asked_at": fh._iso(fh._dt(q["asked_at"])),
                      "waited": fh.plain_wait(fh._minutes(q["asked_at"], t.now))}
                     for q in t.store.list_spec_questions(p["id"], state="open")
                     if q["to_person_id"] != t.me["id"]]
        if not questions:
            continue
        names = list(dict.fromkeys(q["to"]["name"] for q in questions))
        # a project with no lead reaches `local` only because nobody else leads it: say so
        no_lead = not p.get("lead_person_id")
        actions = ([{"label": "Set a lead", "primary": False, "href": f"/projects/{p['id']}#people"}]
                   if no_lead else [])
        ref = {"id": p["id"], "name": p["name"], **({"icon": p["icon"]} if p.get("icon") else {})}
        out.append({"project": ref, "title": p["name"],
                    "detail": f"{fh._count(len(questions), 'question')} open for {fw._and(names)}.",
                    "open_questions": questions, "actions": actions, "no_lead": no_lead})
    return out


def waiting_counts(entries: list[dict]) -> list[tuple[str, int]]:
    """[(person name, how many questions the person waits on them for)], most first. People are
    told apart by id, so two people of one name stay two entries."""
    n: dict[str, int] = {}
    names: dict[str, str] = {}
    for e in entries:
        for who in (q["to"] for q in e["open_questions"]):
            names[who["id"]] = who["name"]
            n[who["id"]] = n.get(who["id"], 0) + 1
    return sorted(((names[i], k) for i, k in n.items()), key=lambda kv: (-kv[1], kv[0]))


# ── the project list: lead, members, scope, and what is the person's ─────────

PROJECT_VIEWS = ("mine", "team")


def _yours_text(t: HomeRead, tickets: list[dict], p: dict) -> tuple[int, str]:
    """(how many of the project's tickets are the person's, "1 ticket yours, 1 Kim's")."""
    mine, others = 0, {}
    for tk in tickets:
        if tk["status"] == "canceled":
            continue
        if t.c.is_mine(tk, t.me["id"], p):
            mine += 1
            continue
        who = tk.get("assignee_person_id") or p.get("lead_person_id")
        name = t.people().get(who) if who else None
        others[name or "unassigned"] = others.get(name or "unassigned", 0) + 1
    parts = [f"{fh._count(mine, 'ticket')} yours"] if mine else []
    for i, (name, n) in enumerate(sorted(others.items(), key=lambda kv: (-kv[1], kv[0]))):
        who = f"{name}'s" if name != "unassigned" else name
        parts.append(f"{fh._count(n, 'ticket')} {who}" if not parts and i == 0 else f"{n} {who}")
    return mine, ", ".join(parts)


def _hidden_summary(stubs: list[dict]) -> str:
    n = len(stubs)
    leads = list(dict.fromkeys(s["lead"]["name"] for s in stubs if s["lead"]))
    return (f"{fh._count(n, 'private project')} you are not on"
            + (f", led by {fw._and(leads)}" if leads else ""))


def team_projects(c, request, open_ps: list[dict], tickets: dict, rows: list[dict], which: str,
                  now, waiting: list[dict] = ()) -> dict:
    """The `projects`, `counts` and `hidden_summary` keys. A project is mine when the person leads
    it, holds a ticket in it, or an item in `waiting` is in it. `rows` are the home's project rows of
    the projects the person can see (`open_ps`, with their `tickets`); a private project they are
    not on is not among them and shows only as {hidden: true, lead: {name}} in the team view."""
    if not _for_team(c, request):
        return {}
    t = HomeRead(c, request, c.work_person(request), now)
    by_id = {p["id"]: p for p in open_ps}
    asked = {(i["project"] or {}).get("id") for i in waiting}   # projects with something for the person
    shown = []
    for r in rows:
        p = by_id[r["id"]]
        yours, text = _yours_text(t, tickets.get(p["id"], []), p)
        lead_id = p.get("lead_person_id")
        shown.append({**r, "lead": t.person(lead_id),
                      "members": [t.person(m["person_id"]) for m in t.store.list_project_members(p["id"])
                                  if m["person_id"] != lead_id],
                      "scope": p.get("scope") or "team", "yours": yours, "yours_text": text,
                      "mine": bool(t.c.leads(p, t.me["id"]) or yours or r["id"] in asked)})
    stubs = []
    for p in t.store.list_projects():
        if p["id"] in t.hidden and p.get("kind") == "software_factory" and p["status"] not in ("done", "later"):
            lead = t.person(p.get("lead_person_id"))
            stubs.append({"hidden": True, "lead": {"name": lead["name"]} if lead else None})
    counts = {"mine": sum(1 for r in shown if r["mine"]), "team": len(shown)}
    view = which if which in PROJECT_VIEWS else "mine"
    out = {"projects": [r for r in shown if r["mine"]] if view == "mine" else shown + stubs,
           "counts": counts}
    if stubs:
        out["hidden_summary"] = _hidden_summary(stubs)
    return out


# ── the crews: the person's own first, then the team's; the silent ones named apart ──

QUIET_MINUTES = 24 * 60


def _crew_label(t: HomeRead, crew: dict) -> str:
    if crew["person_id"] == t.me["id"]:
        return "Your crew"
    who = (_ref(crew["person"]) or {}).get("name")
    return f"{who}'s crew" if who else "A crew"


def _crew_doing(stations: list[dict], own: bool = True) -> str:
    live = [s for s in stations if s["state"] != "ended"]
    building = list(dict.fromkeys(s["project_name"] for s in live
                                  if s["state"] == "working" and s["role"] == "builder"
                                  and s.get("project_name")))
    if building:
        return "Building " + fw._and(building) + "."
    if any(s["state"] == "working" for s in live):
        return "At work."
    waiting = next((s for s in live if s["state"] == "waiting"), None)
    if waiting:
        # a station of a project carries its reason only when the reader sees that project (the
        # stations rows do that). One with no project (an orchestrator) is not tied to one, so
        # another crew's reason is not shown: it may name a project the reader cannot see
        reason = waiting.get("state_reason") if own or waiting.get("project") else None
        return reason or "Waiting for an answer."
    return "Idle." if live else "Not running."


def crews(t: HomeRead) -> tuple[list[dict], list[str]]:
    """(the crews heard from in the last 24 hours, the person's own first, then each other
    person's, newest first; the names of the crews not heard from since)."""
    own, others, quiet = [], [], []
    for cr in t.c.crews_of(t.stations()):
        label = _crew_label(t, cr)
        silent = fh._minutes(cr["last_seen"], t.now)
        if silent is None or silent > QUIET_MINUTES:
            quiet.append((cr["person_id"] != t.me["id"], label))
            continue
        row = {"id": cr["id"], "name": label, "machine": cr["machine"], "person": _ref(cr["person"]) or t.person(cr["person_id"]),
               "last_seen": fh._iso(fh._dt(cr["last_seen"])), "heard": fh.plain_ago(cr["last_seen"], t.now),
               "working": sum(1 for s in cr["stations"] if s["state"] == "working"),
               "doing": _crew_doing(cr["stations"], cr["person_id"] == t.me["id"])}
        (own if cr["person_id"] == t.me["id"] else others).append(row)
    return own + others, [label for _, label in sorted(quiet)]


# ── how far a crew may go: the team's limit and the person's own ─────────────

_UP_TO = {"L3-review": "up to merge",
          "L4-ship": "merging on its own after review and green CI"}


def crew_limit(t: HomeRead) -> dict:
    """{mine, team, text}: the autonomy of the person's crew and the team's, in words. The
    person's can be stricter than the team's, never looser (factory.stricter_than_team)."""
    team = t.c.stored("crew_settings").get("autonomy")
    team = team if team in factory_mod.AUTONOMY else "L3-review"
    mine = t.c.settings(t.request).get("autonomy")
    mine = mine if mine in factory_mod.AUTONOMY else team
    return {"mine": factory_mod.AUTONOMY[mine], "team": factory_mod.AUTONOMY[team],
            "text": f"The team allows {_UP_TO[team]}. Yours can be stricter, not looser."}


# ── the repos the person owns, and what waits on each ────────────────────────

def areas(t: HomeRead, waiting: list[dict]) -> list[dict]:
    """[{repo, note}] for the repos the person owns: how many questions wait for them there,
    from the items already built."""
    out = []
    for a in sorted(areas_mod.load(t.store), key=lambda a: a["repo"]):
        if a["person_id"] != t.me["id"]:
            continue
        mine = [i for i in waiting if (i["repo"] or "").lower() == a["repo"]]
        n_ask = sum(1 for i in mine if i["kind"] == "question")
        out.append({"repo": a["repo"], "note": fh._count(n_ask, "question") + " open" if n_ask
                    else "nothing waiting"})
    return out


# ── the whole answer ─────────────────────────────────────────────────────────

def summary(waiting: list[dict], counts: list[tuple[str, int]] | None = None) -> str:
    """"Nothing waits for you.", "1 thing waits for you.", "4 things wait for you.", then
    "You are waiting on Kim for 2." when the person waits on anyone."""
    n = len(waiting)
    first = ("Nothing waits for you." if not n else "1 thing waits for you." if n == 1
             else f"{n} things wait for you.")
    if not counts:
        return first
    return first + " You are waiting on " + fw._and([f"{name} for {k}" for name, k in counts]) + "."


def team_home(c, request, now) -> dict:
    """The team keys of the home body for the caller, or {} for a caller the single-person home
    answers: a cell of one, or a key with no person."""
    if not _for_team(c, request):
        return {}
    t = HomeRead(c, request, c.work_person(request), now)
    waiting = fh.order_waiting([i for build in BUILDERS for i in build(t)], now)
    on = waiting_on(t)
    crew_rows, quiet = crews(t)
    return {"waiting_for_you": waiting, "waiting_total": len(waiting),
            "summary": summary(waiting, waiting_counts(on)), "you_are_waiting_on": on,
            "crews": crew_rows, "crews_quiet": quiet, "crew_limit": crew_limit(t),
            "areas": areas(t, waiting)}


def _for_whom(label: str, names: list[str]) -> str:
    """An item's label said for someone else: "Question for you" becomes "Question for Kiran",
    "Milestone to check" becomes "Milestone to check for Kiran and Adam"."""
    who = fw._and(names)
    base = label[: -len(" for you")] if label.endswith(" for you") else label
    return f"{base} for {who}"


def project_waiting(c, request, now, project: str) -> dict | None:
    """What waits in one project on a team cell, by the one rule of the team home (the BUILDERS):
    `mine` is the caller's items of the project, the ones their home lists, so the project page
    and the home never disagree on what is "waiting on you". `others` is what the same builders
    give every other person on the cell in this project: named for them ("Milestone to check for
    Kiran"), with nothing to press, since only they may act on it. An item waiting for the caller
    and someone else (a pull request in a project both work in) is the caller's. None on a cell
    the single-person home answers."""
    if not _for_team(c, request):
        return None
    t = HomeRead(c, request, c.work_person(request), now)

    def of(r: HomeRead) -> list[dict]:
        return [i for build in BUILDERS for i in build(r)
                if (i.get("project") or {}).get("id") == project]

    mine = fh.order_waiting(of(t), now)
    seen = {i["id"] for i in mine}
    others: dict[str, dict] = {}
    for p in t.store.list_people():
        # `local` nobody claimed is no one a person would know by name ("You"): left out
        if p.get("removed_at") or p["id"] == t.me["id"] or (p["id"] == "local" and not p.get("email")):
            continue
        who = {"id": p["id"], "name": p.get("name") or p["id"]}
        for i in of(HomeRead(c, request, who, now, viewer=False, share=t)):
            if i["id"] in seen:
                continue
            o = others.setdefault(i["id"], {**i, "for_people": []})
            o["for_people"].append(who)
    out = []
    for o in others.values():
        names = [w["name"] for w in o["for_people"]]
        out.append({**o, "label": _for_whom(o["label"], names), "actions": [], "question": None,
                    "for_person": o["for_people"][0]})
    return {"mine": mine, "others": fh.order_waiting(out, now)}
