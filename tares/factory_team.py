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
from . import factory as factory_mod
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
        self._people = None

    def stations(self) -> list[dict]:
        if self._stations is None:
            self._stations = self.c.stations()
        return self._stations

    def names(self) -> dict[str, str]:
        """{project id: name} of every project."""
        if self._names is None:
            self._names = {p["id"]: p["name"] for p in self.store.list_projects()}
        return self._names


    def people(self) -> dict[str, str]:
        """{person id: name} of everyone on the cell."""
        if self._people is None:
            self._people = {p["id"]: p.get("name") or p["id"] for p in self.store.list_people()}
        return self._people

    def person(self, pid: str | None) -> dict | None:
        """{id, name} of a person by id, or None."""
        return {"id": pid, "name": self.people().get(pid) or pid} if pid else None


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


# ── what the single-person home asks of whoever leads a project, kept for its lead ──

def lead_items(t: Team) -> list[dict]:
    """A spec ready to hand to the crew, calls the crew made, a project ready to close: the
    single-person home shows them to the one person, a team home to the project's lead."""
    from types import SimpleNamespace
    fx = SimpleNamespace(queue=t.c.queue, settings=lambda: t.c.settings(t.request))
    led: dict[str, bool] = {}

    def mine(pid: str | None) -> bool:
        if pid not in led:
            led[pid] = pid not in t.hidden and t.c.leads(t.store.get_project(pid), t.me["id"])
        return led[pid]

    return [i for build in (fw.ready_to_start, fw.calls_to_confirm, fw.projects_to_close)
            for i in build(t.store, fx, t.now) if mine((i["project"] or {}).get("id"))]


# ── the whole answer ─────────────────────────────────────────────────────────

BUILDERS = [question_items, signoff_items, grant_items, desk_items, milestone_items, pr_items,
            lead_items]


# ═════════════════════════════════════════════════════════════════════════════
# Part two: who the person waits on, the project list, the crews, the repos they own
# ═════════════════════════════════════════════════════════════════════════════

# ── "You are waiting on": the projects the person leads that cannot go to a crew yet ──

def _repo_state(r: dict) -> str:
    if r["owner"] is None:
        return "no_owner"
    if r["is_lead"]:
        return "yours"
    return "signed_off" if r["signed_off"] else "waiting"


def _waiting_detail(rows: list[dict], questions: list[dict]) -> str:
    bits = []
    waiting = [r for r in rows if r["state"] == "waiting"]
    if waiting:
        names = list(dict.fromkeys(r["owner"]["name"] for r in waiting))
        bits.append(f"Waiting for {fw._and(names)} to sign off on "
                    f"{fw._and([r['repo'] for r in waiting])}.")
    unowned = [r["repo"] for r in rows if r["state"] == "no_owner"]
    if unowned:
        bits.append(f"{fw._and(unowned)} {'has' if len(unowned) == 1 else 'have'} no owner.")
    if questions:
        names = list(dict.fromkeys(q["to"]["name"] for q in questions))
        bits.append(f"{fh._count(len(questions), 'question')} open for {fw._and(names)}.")
    return " ".join(bits) or "Everyone has signed off. The project can go to a crew."


def waiting_on(t: Team) -> list[dict]:
    """One entry per project the person leads that is not handed over yet and depends on someone
    else: a repo another person owns (signed off or not), a repo nobody owns, or a question
    still open. A project whose repos are all the person's own, with nothing asked, is left out."""
    handed = set(t.store.handed_over_projects())
    out = []
    for p in t.store.list_projects():
        if (p.get("kind") != "software_factory" or p["status"] == "done" or p["id"] in handed
                or p["id"] in t.hidden or not t.c.leads(p, t.me["id"])):
            continue
        rows = [{"repo": r["repo"], "owner": _ref(r["owner"]), "state": _repo_state(r)}
                for r in t.c.signoff_state(p["id"], p)]
        questions = [{"id": q["id"], "to": _ref(q["to_person"]) or t.person(q["to_person_id"]),
                      "asked_at": fh._iso(fh._dt(q["asked_at"])),
                      "waited": fh.plain_wait(fh._minutes(q["asked_at"], t.now))}
                     for q in t.store.list_spec_questions(p["id"], state="open")]
        if not questions and all(r["state"] in ("yours", "signed_off") for r in rows):
            continue
        actions = []
        if any(r["state"] in ("waiting", "no_owner") for r in rows):
            actions.append({"label": "Hand over anyway", "primary": False,
                            "do": "hand_over_anyway", "project": p["id"]})
        if any(r["state"] == "no_owner" for r in rows):
            actions.append({"label": "Set an owner", "primary": False, "href": "/settings/advanced"})
        out.append({"project": {"id": p["id"], "name": p["name"]}, "title": p["name"],
                    "detail": _waiting_detail(rows, questions), "repos": rows,
                    "open_questions": questions, "actions": actions})
    return out


def waiting_counts(entries: list[dict]) -> list[tuple[str, int]]:
    """[(person name, how many things the person waits on them for)], most first: a repo
    waiting for their sign-off and a question open for them each count one. People are told
    apart by id, so two people of one name stay two entries."""
    n: dict[str, int] = {}
    names: dict[str, str] = {}
    for e in entries:
        for who in ([r["owner"] for r in e["repos"] if r["state"] == "waiting"]
                    + [q["to"] for q in e["open_questions"]]):
            names[who["id"]] = who["name"]
            n[who["id"]] = n.get(who["id"], 0) + 1
    return sorted(((names[i], k) for i, k in n.items()), key=lambda kv: (-kv[1], kv[0]))


# ── the project list: lead, members, scope, and what is the person's ─────────

PROJECT_VIEWS = ("mine", "team")


def _yours_text(t: Team, tickets: list[dict], p: dict) -> tuple[int, str]:
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
    if not _applies(c, request):
        return {}
    t = Team(c, request, c.work_person(request), now)
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
        if p["id"] in t.hidden and p.get("kind") == "software_factory" and p["status"] != "done":
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


def _crew_label(t: Team, crew: dict) -> str:
    if crew["person_id"] == t.me["id"]:
        return "Your crew"
    who = (_ref(crew["person"]) or {}).get("name")
    return f"{who}'s crew" if who else "A crew"


def _crew_doing(stations: list[dict]) -> str:
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
        return waiting.get("state_reason") or "Waiting for an answer."
    return "Idle." if live else "Not running."


def crews(t: Team) -> tuple[list[dict], list[str]]:
    """(the crews heard from in the last 24 hours, the person's own first, then each other
    person's, newest first; the names of the crews not heard from since)."""
    own, others, quiet = [], [], []
    for cr in t.c.crews_of(t.stations()):
        label = _crew_label(t, cr)
        silent = fh._minutes(cr["last_seen"], t.now)
        if silent is None or silent > QUIET_MINUTES:
            quiet.append((cr["person_id"] != t.me["id"], label))
            continue
        row = {"name": label, "machine": cr["machine"], "person": _ref(cr["person"]) or t.person(cr["person_id"]),
               "last_seen": fh._iso(fh._dt(cr["last_seen"])), "heard": fh.plain_ago(cr["last_seen"], t.now),
               "working": sum(1 for s in cr["stations"] if s["state"] == "working"),
               "doing": _crew_doing(cr["stations"])}
        (own if cr["person_id"] == t.me["id"] else others).append(row)
    return own + others, [label for _, label in sorted(quiet)]


# ── how far a crew may go: the team's limit and the person's own ─────────────

_UP_TO = {"L3-review": "up to merge",
          "L4-ship": "merging on its own after review and green CI"}


def crew_limit(t: Team) -> dict:
    """{mine, team, text}: the autonomy of the person's crew and the team's, in words. The
    person's can be stricter than the team's, never looser (factory.stricter_than_team)."""
    team = t.c.stored("crew_settings").get("autonomy")
    team = team if team in factory_mod.AUTONOMY else "L3-review"
    mine = t.c.settings(t.request).get("autonomy")
    mine = mine if mine in factory_mod.AUTONOMY else team
    return {"mine": factory_mod.AUTONOMY[mine], "team": factory_mod.AUTONOMY[team],
            "text": f"The team allows {_UP_TO[team]}. Yours can be stricter, not looser."}


# ── the repos the person owns, and what waits on each ────────────────────────

def areas(t: Team, waiting: list[dict]) -> list[dict]:
    """[{repo, note}] for the repos the person owns: how many sign-offs and questions wait for
    them there, from the items already built."""
    out = []
    for a in sorted(areas_mod.load(t.store), key=lambda a: a["repo"]):
        if a["person_id"] != t.me["id"]:
            continue
        mine = [i for i in waiting if (i["repo"] or "").lower() == a["repo"]]
        n_sign = sum(1 for i in mine if i["kind"] == "signoff")
        n_ask = sum(1 for i in mine if i["kind"] == "question")
        bits = ([f"{n_sign} sign-off{'' if n_sign == 1 else 's'} asked"] if n_sign else []) + \
               ([fh._count(n_ask, "question") + " open"] if n_ask else [])
        out.append({"repo": a["repo"], "note": ", ".join(bits) or "nothing waiting"})
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


def _applies(c, request) -> bool:
    """Whether the team keys apply: a cell with more than one person, and a caller with a person
    (or the cell's own credential)."""
    if c.store.only_local_person():
        return False
    return not (c.h.current_person(request) is None and not c.h.is_system(request))


def team_home(c, request, now) -> dict:
    """The team keys of the home body for the caller, or {} for a caller the single-person home
    answers: a cell of one, or a key with no person."""
    if not _applies(c, request):
        return {}
    t = Team(c, request, c.work_person(request), now)
    waiting = [i for build in BUILDERS for i in build(t)]
    waiting.sort(key=lambda i: fh._dt(i["since"]) or now)
    on = waiting_on(t)
    crew_rows, quiet = crews(t)
    return {"waiting_for_you": waiting, "waiting_total": len(waiting),
            "summary": summary(waiting, waiting_counts(on)), "you_are_waiting_on": on,
            "crews": crew_rows, "crews_quiet": quiet, "crew_limit": crew_limit(t),
            "areas": areas(t, waiting)}
