"""What waits for the person on a factory cell (TR-769): the builders of `waiting_for_you`.

One function per kind of item, each `(store, fx, now) -> [home_item(...)]`. They are listed in
`factory_home.WAITING_BUILDERS` when this module is imported, which factory_home does at its end.
Every builder reads its table once for all projects (never once per project), because they run
inside GET /api/home, which has to answer in well under 300 ms.

Everything is built from what the cell already stores: the review queue, the specs and the
hand-overs, the open assumptions, the desk and the milestones. Nothing is kept for them.
"""
from __future__ import annotations

from . import factory as factory_mod
from . import factory_home as fh

# the role words a person reads ("The reviewer wants to use ..."), by the role the crew keeps
_ROLE_WORD = {"orchestrator": "the orchestrator", "reviewer": "the reviewer",
              "releaser": "the releaser", "builder": "the builder", "helper": "a helper"}
_TRACKER = {"linear": " in Linear", "github": " in GitHub"}
_SHOWN_CHOICES = 3


def _open_projects(store) -> list[dict]:
    return [p for p in store.list_projects()
            if p.get("kind") == "software_factory" and p["status"] != "done"]


def _for(waited: str) -> str:
    """A wait as a phrase that reads after "Waiting": a span, or "less than a minute"."""
    return "less than a minute" if waited == "just now" else waited


def _list(parts: list[str]) -> str:
    """"a", "a, and b", "a, b, and c": the shape the item texts use."""
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + ", and " + parts[-1]


def _and(parts: list[str]) -> str:
    """"a", "a and b", "a, b and c"."""
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]


def _role_of_station(name: str | None) -> str:
    """The role word of a station by its name: crew-reviewer is the reviewer, a project's
    build station the builder; anything else is the crew."""
    n = (name or "").strip().lower()
    if n.startswith("crew-"):
        return _ROLE_WORD.get(n[5:].split("-", 1)[0], "the crew")
    if "-build" in n:
        return _ROLE_WORD["builder"]
    return "the crew"


def _trim(text: str, n: int = 140) -> str:
    t = " ".join(str(text or "").split()).rstrip(".,;: ")
    return t if len(t) <= n else t[: n - 3].rstrip() + "..."


def _ref(project: dict) -> dict:
    return {"id": project["id"], "name": project["name"]}


# ── pull requests the reviewer passed ─────────────────────────────────────────

def prs_to_review(store, fx, now) -> list[dict]:
    """Per project, the pull requests the crew reviewer passed on their current commit: those wait
    for the person. The others wait for the reviewer or the builder."""
    if fx is None:
        return []
    by: dict[str, list[dict]] = {}
    for q in fx.queue():
        v = q.get("verdict") or {}
        if v.get("verdict") == "pass" and v.get("current"):
            by.setdefault(q["project"], []).append(q)
    out = []
    for pid, rows in by.items():
        n = len(rows)
        titles = ", ".join(_trim(r["title"], 80) for r in rows)
        blocking = sum(1 for r in rows if r["blocks_milestone"])
        note = ("" if not blocking
                else (" It holds up a milestone." if n == 1
                      else " Each one holds up a milestone." if blocking == n
                      else " One holds up a milestone." if blocking == 1
                      else f" {blocking} of them hold up a milestone."))
        since = min((r["waiting_since"] for r in rows if r.get("waiting_since")), default=now,
                    key=lambda t: fh._dt(t) or now)
        repos = list(dict.fromkeys(r["repo"] for r in rows if r.get("repo")))
        waited = _for(fh.plain_wait(fh._minutes(since, now)))
        out.append(fh.home_item(
            "prs_to_review", label="1 PR to review" if n == 1 else f"{n} PRs to review",
            title=rows[0]["project_name"], detail=titles + "." + note,
            meta="Waiting " + waited + (" · " + ", ".join(repos) if repos else ""),
            since=since, now=now, project={"id": pid, "name": rows[0]["project_name"]},
            repo=repos[0] if len(repos) == 1 else None, id=f"prs_to_review:{pid}",
            actions=[{"label": "Review", "primary": True, "href": f"/reviews?project={pid}"}]))
    return out


# ── projects ready to hand to the crew ────────────────────────────────────────

def ready_to_start(store, fx, now) -> list[dict]:
    """Open projects with a spec and tickets that the crew has not been handed."""
    projects = _open_projects(store)
    if not projects:
        return []
    tickets = store.tickets_by_project([p["id"] for p in projects])
    handed = set(store.handed_over_projects())
    specs: dict[str, list] = {}
    for d in store.list_cell_docs():
        if d["kind"] == "spec":
            for pid in d["projects"]:
                specs.setdefault(pid, []).append(d["created_at"])
    out = []
    for p in projects:
        ts = tickets.get(p["id"]) or []
        if not ts or p["id"] in handed or not specs.get(p["id"]):
            continue
        since = min(specs[p["id"]], key=lambda t: fh._dt(t) or now)
        where = next((_TRACKER[t["owner"]] for t in ts if t.get("owner") in _TRACKER), "")
        out.append(fh.home_item(
            "ready_to_start", label="Ready to start", title=p["name"],
            detail=f"Spec and plan written, {fh._count(len(ts), 'ticket')}{where}. "
                   "The crew has not picked it up.",
            meta="Spec written " + fh.plain_ago(since, now), since=since, now=now,
            project=_ref(p), id=f"ready_to_start:{p['id']}",
            actions=[{"label": "Read the spec", "primary": False, "href": f"/projects/{p['id']}"},
                     {"label": "Copy the hand-over command", "primary": True, "do": "copy",
                      "text": f"/tares:ship {p['name']}"}]))
    return out


# ── calls the crew made without the person ────────────────────────────────────

def calls_to_confirm(store, fx, now) -> list[dict]:
    """Per project, its open assumptions. A desk item that batches the same assumptions stays on
    the desk and makes no second item here (desk_items leaves it out)."""
    rows = store.open_assumptions()
    if not rows:
        return []
    names = {p["id"]: p["name"] for p in _open_projects(store)}
    by: dict[str, list[dict]] = {}
    for a in rows:
        if a["project"] in names:
            by.setdefault(a["project"], []).append(a)
    out = []
    for pid, items in by.items():
        n = len(items)
        shown = [_trim(a["choice"]) for a in items[:_SHOWN_CHOICES]]
        if n > _SHOWN_CHOICES:
            shown.append(f"{n - _SHOWN_CHOICES} more")
        keep = "both" if n == 2 else "it" if n == 1 else "them"
        roles = list(dict.fromkeys(_ROLE_WORD.get(a["made_role"] or "", "the crew") for a in items))
        since = min((a["made_at"] for a in items), key=lambda t: fh._dt(t) or now)
        out.append(fh.home_item(
            "calls_to_confirm", label="1 call to confirm" if n == 1 else f"{n} calls to confirm",
            title=names[pid], detail="The crew decided without you: " + _list(shown)
            + f". Keep {keep}?", meta="Made by " + _and(roles), since=since, now=now,
            project={"id": pid, "name": names[pid]}, id=f"calls_to_confirm:{pid}",
            actions=[{"label": "Read them", "primary": False, "href": f"/projects/{pid}#calls"},
                     {"label": {"both": "Keep both", "it": "Keep it", "them": "Keep all"}[keep],
                      "primary": True, "do": "keep_assumptions", "project": pid,
                      "refs": [a["label"] for a in items]}]))
    return out


# ── questions and permission requests on the desk ─────────────────────────────

def desk_items(store, fx, now) -> list[dict]:
    """The open desk: a question for the person, or a permission request (it has a command). A
    batch of assumptions is not here: calls_to_confirm shows those."""
    rows = [d for d in store.list_desk("open", None) if not d.get("assumptions")]
    if not rows:
        return []
    names = {p["id"]: p["name"] for p in store.list_projects()}
    out = []
    for d in rows:
        pid = d["project"] if d["project"] in names else None
        project = {"id": pid, "name": names[pid]} if pid else None
        # a question about a project opens on its page; one for every project, on the crew page
        href = f"/projects/{pid}#desk-{d['label']}" if pid else f"/crew#desk-{d['label']}"
        answer = {"label": "Answer", "primary": not d.get("command"), "href": href}
        if d.get("command"):
            who = _role_of_station(d.get("station"))
            # the command is the crew's own text, in backticks so the page can show it in a
            # monospace span
            out.append(fh.home_item(
                "permission", label="Permission", title=names[pid] if pid else "Every project",
                detail=f"{who[0].upper()}{who[1:]} wants to use: `{d['command']}`",
                meta="Asked by the crew, " + fh.plain_ago(d["created_at"], now),
                since=d["created_at"], now=now, project=project, id=f"permission:{d['label']}",
                actions=[{**answer, "primary": False},
                         {"label": "Allow", "primary": True, "do": "allow", "desk": d["label"]}]))
        else:
            out.append(fh.home_item(
                "question", label="Question for you", title=names[pid] if pid else "Every project",
                detail=d["question"], meta="Asked by the crew, " + fh.plain_ago(d["created_at"], now),
                since=d["created_at"], now=now, project=project, id=f"question:{d['label']}",
                actions=[answer]))
    return out


# ── milestones whose tickets all merged ───────────────────────────────────────

def milestones_to_check(store, fx, now) -> list[dict]:
    """Per open project, each milestone whose tickets are all merged (a canceled one does not
    count, at least one must be delivered). There is no "checked" mark: the item shows until the
    project is closed."""
    projects = _open_projects(store)
    if not projects:
        return []
    ids = [p["id"] for p in projects]
    tickets, milestones = store.tickets_by_project(ids), store.milestones_by_project(ids)
    out = []
    for p in projects:
        for m in milestones.get(p["id"]) or []:
            mine = [t for t in tickets.get(p["id"]) or []
                    if t["milestone"] == m["id"] and t["status"] != "canceled"]
            if not mine or not all(factory_mod.delivered(t) for t in mine):
                continue
            n = len(m["checks"])
            since = max((t["updated_at"] for t in mine if t.get("updated_at")),
                        key=lambda t: fh._dt(t) or now, default=m["updated_at"])
            out.append(fh.home_item(
                "milestone_to_check", label="Milestone to check", title=f"{p['name']}, {m['name']}",
                detail="Every ticket of this milestone merged."
                + (f" Try its {fh._count(n, 'check')}." if n else ""),
                meta="Merged " + fh.plain_ago(since, now), since=since, now=now, project=_ref(p),
                id=f"milestone_to_check:{p['id']}:{m['id']}",
                actions=[{"label": "Check it", "primary": True,
                          "href": f"/projects/{p['id']}#milestone-{m['id']}"}]))
    return out


fh.WAITING_BUILDERS.extend([prs_to_review, ready_to_start, calls_to_confirm, desk_items,
                            milestones_to_check])
