"""What waits for the person on a factory cell (TR-769): the builders of `waiting_for_you`.

One function per kind of item, each `(store, fx, now) -> [home_item(...)]`. They are listed in
`factory_home.WAITING_BUILDERS` when this module is imported, which factory_home does at its end.
Every builder reads its table once for all projects (never once per project), because they run
inside GET /api/home, which has to answer in well under 300 ms.

Everything is built from what the cell already stores: the review queue, the specs and the
hand-overs, the open assumptions, the desk and the milestones. Nothing is kept for them.
"""
from __future__ import annotations

import re

from . import factory as factory_mod
from . import factory_home as fh
from . import factory_owners as owners_mod

# the role words a person reads ("The reviewer wants to use ..."), by the role the crew keeps
_ROLE_WORD = {"orchestrator": "the orchestrator", "reviewer": "the reviewer",
              "releaser": "the releaser", "builder": "the builder", "helper": "a helper"}
_TRACKER = {"linear": " in Linear", "github": " in GitHub"}
_SHOWN_CHOICES = 3


def _open_projects(store) -> list[dict]:
    """The open software-factory projects; one kept for later waits for nobody until it starts."""
    return [p for p in store.list_projects()
            if p.get("kind") == "software_factory" and p["status"] not in ("done", "later")]


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
    """At most n characters, cut at a word boundary with an ellipsis (TR-1021), never mid-word."""
    t = " ".join(str(text or "").split()).rstrip(".,;: ")
    if len(t) <= n:
        return t
    cut = t[: n - 1]
    cut = cut.rsplit(" ", 1)[0] if " " in cut else cut
    return cut.rstrip(".,;:-( ") + "…"


def ask_question(text: str) -> str:
    """A question's ask is the question itself, on one line (TR-1447)."""
    return _trim(text, 140)


def ask_permission(command: str) -> str:
    """A permission's ask is what the crew wants to run (TR-1447)."""
    return "Allow `" + _trim(command, 120) + "`"


def ask_calls(n: int, project: str) -> str:
    """Keep the crew's call on atlas; Keep 2 calls on atlas (TR-1447). A desk batch whose calls
    were decided one by one has none left open: its ask keeps the batch, without a count."""
    if n == 1:
        return f"Keep the crew's call on {project}"
    return f"Keep {n} calls on {project}" if n > 1 else f"Keep the crew's calls on {project}"


def ask_prs(rows: list[dict], project: str) -> str:
    """Merge PR 336, or Merge 2 PRs on atlas (TR-1447). A pull request Tares has not read the
    number of yet goes by its ticket's title."""
    if len(rows) > 1:
        return f"Merge {len(rows)} PRs on {project}"
    r = rows[0]
    return f"Merge PR {r['number']}" if r.get("number") else "Merge " + _trim(r["title"], 100)


def holds_tickets(refs: list[str]) -> str | None:
    """T4; T4 and T5; T4 and 2 more tickets: what waits, by name (TR-1448)."""
    if not refs:
        return None
    if len(refs) <= 2:
        return _and(refs)
    return f"{refs[0]} and {len(refs) - 1} more tickets"


def holds_desk(d: dict) -> str | None:
    """What a desk item holds up (TR-1448): what the crew said it blocks, else the tickets it
    names, else, for a refused command, the work of the station that ran it."""
    said = _trim(str(d.get("blocks") or ""), 140)
    if said:
        return said
    named = holds_tickets([str(t) for t in d.get("tickets") or [] if str(t).strip()])
    if named:
        return named
    if d.get("command"):
        return f"{_role_of_station(d.get('station'))}'s work"
    return None


_NUMBERED = re.compile(r"^\s*(?:M\s*)?\d+\s*[.:)\-]", re.I)


def milestone_ask(project_name: str, i: int) -> str:
    """The ask of a milestone to check, as Home shows it and as its answer records it: one
    definition, so Answered today repeats the words the person saw (TR-1451)."""
    return f"Check M{i} of {project_name}"


def milestone_name(milestones: list[dict], mid: str | None) -> str | None:
    """A milestone as people read it: its own name when already numbered ("1. Foundations",
    "M2: Search"), else "M3: Name" with its place in the project's list, "M3" without a name."""
    for i, m in enumerate(milestones, 1):
        if m["id"] == mid:
            name = str(m.get("name") or "").strip()
            if not name:
                return f"M{i}"
            return name if _NUMBERED.match(name) else f"M{i}: {name}"
    return None


def _ref(project: dict) -> dict:
    return {"id": project["id"], "name": project["name"]}


def desk_blocks(d: dict) -> bool:
    """A desk item holds crew work up (TR-1021): asked as blocking, naming what it blocks, or a
    station's refused command."""
    return bool(d.get("blocking") or d.get("blocks") or d.get("command"))


def desk_question(d: dict) -> dict | None:
    """A question's own options and the crew's recommendation, for answering on the row
    (TR-1021); None for a question without options."""
    opts = [str(o) for o in (d.get("options") or []) if str(o).strip()]
    if not opts or d.get("command"):
        return None
    return {"desk": d["label"], "options": opts,
            "recommendation": _trim(d.get("recommendation") or "", 400) or None,
            # why the crew suggests it, so the feed can say so beside the answer (P-TR-273)
            "why": _trim(str(d.get("why") or ""), 400) or None,
            "blocks": _trim(str(d.get("blocks") or ""), 140) or None}


# ── pull requests the reviewer passed ─────────────────────────────────────────

DEFAULT_AUTONOMY = "L3-review"


def effective_autonomy(store, fx, project: str, crew: str | None = None) -> str:
    """The autonomy a project's pull requests are merged at: the one its hand-over set, else the
    crew's, else L3-review. `crew` is the crew's setting when the caller has read it already."""
    own = (store.get_handover(project) or {}).get("autonomy")
    if crew is None and fx is not None:
        crew = (fx.settings() or {}).get("autonomy")
    return own or crew or DEFAULT_AUTONOMY


# why a pull request in review is with nobody, in the words the Reviews page shows (TR-1009)
NOBODY = {
    "finished": "The project is finished, so the crew no longer works on it",
    "not_handed": "The project was never handed to the crew",
    "taken": "You took this project over from the crew",
    "closed": "The pull request was closed without merging",
    "blocked": "The reviewer blocked it; it needs your decision",
}


def _crew_has(q: dict, level: str) -> str | None:
    """Who in the crew has a pull request next, in words, or None when nobody in the crew does.
    Checked, never assumed: the reviewer for a commit not reviewed yet, the builder for changes
    asked or failing CI, CI while it runs, the crew's merge at L4-ship. When the project's review
    gate is an outside tool (`outside_gate` on the row, TR-857) the crew reviewer is not waited
    for: the wait is for that tool, by its name."""
    v = q.get("verdict") or {}
    kind, current = v.get("verdict"), bool(v.get("current"))
    outside = q.get("outside_gate")
    if kind == "block" and current:
        return None
    if kind == "changes" and current:
        return (f"The builder works on the changes {outside} asked for" if outside
                else "The builder works on the changes the reviewer asked for")
    if q.get("ci") == "failure":
        return "The builder fixes the failing CI"
    if not (kind == "pass" and current):
        return f"Waiting for {outside}" if outside else "The reviewer reviews it"
    if q.get("ci") == "pending":
        return "CI is still running; the crew merges it when CI passes"
    if level != DEFAULT_AUTONOMY:
        return "The crew merges it"
    return None


def review_split(store, fx, rows: list[dict] | None = None
                 ) -> tuple[list[dict], list[dict], list[dict]]:
    """THE rule for who has each pull request in review (assumption A10, TR-1009): the queue rows
    (`fx.queue()`) split into (waits for the person, with the crew, nobody is on it), each row
    with `why` in words. A pull request waits for a person when the merge queue merges its
    project at L3-review (merge_queue.autonomy_of: the hand-over's level, else the crew of whoever
    handed it over, under the cell's ceiling; never the viewer's) and the reviewer passed it on its
    current commit; which person is pr_owners' (the ticket's owner). Unless its project is finished or
    the pull request was closed without merging. It is with the crew only when the crew has the
    project (handed over, not finished, not taken back) and a named role has the next step
    (`_crew_has`). Everything else is nobody's, with the reason (`NOBODY`). The Home page and
    the Reviews page both read this and pr_owners and nowhere else, so their lists are always the
    same. `fx` gives the queue."""
    if fx is None:
        return [], [], []
    from .merge_queue import autonomy_of   # merge_queue imports this module
    rows = fx.queue() if rows is None else rows
    projects = {p["id"]: p for p in store.list_projects()}
    level: dict[str, str] = {}
    held: dict[str, str | None] = {}
    mine, theirs, nobody = [], [], []
    for q in rows:
        pid = q["project"]
        if pid not in level:
            # the level the merge queue merges this project at (its hand-over, else the crew of
            # whoever handed it over): the same for every viewer, so Home and Reviews agree
            level[pid] = autonomy_of(store, pid)
            ho = store.get_handover(pid)
            p = projects.get(pid) or {}
            held[pid] = ("finished" if p.get("status") == "done" or (ho or {}).get("finished")
                         else "not_handed" if not ho
                         else "taken" if (ho.get("takeover") and not ho["takeover"].get("released_at"))
                         else None)
        v = q.get("verdict") or {}
        # closed or finished: nobody's, whatever the review says
        gone = ("closed" if q.get("pr_state") == "closed" and not q.get("pr_merged")
                else "finished" if held[pid] == "finished" else None)
        if gone:
            nobody.append({**q, "why": NOBODY[gone]})
        elif level[pid] == DEFAULT_AUTONOMY and v.get("verdict") == "pass" and v.get("current"):
            mine.append({**q, "why": None})
        elif held[pid]:      # never handed over, or taken back: the crew does not have it
            nobody.append({**q, "why": NOBODY[held[pid]]})
        elif (has := _crew_has(q, level[pid])):
            theirs.append({**q, "why": has})
        else:
            nobody.append({**q, "why": NOBODY["blocked"]})
    return mine, theirs, nobody


def pr_owners(store, rows: list[dict], o=None) -> dict[tuple[str, str], tuple[str, ...]]:
    """{(project, ticket ref): the people} a pull request in review waits for: the owner of its
    ticket (factory_owners, the one rule). The home's "PRs to review" and the Reviews page read
    this and nowhere else, so who merges a pull request is the same on both."""
    o = o or owners_mod.Owners(store)
    pids = sorted({q["project"] for q in rows})
    if not pids:
        return {}
    projects = {p["id"]: p for p in store.list_projects() if p["id"] in pids}
    tickets = {(pid, factory_mod.label(t)): t for pid, ts in store.tickets_by_project(pids).items() for t in ts}
    return {(q["project"], q["ticket"]): o.ticket(tickets.get((q["project"], q["ticket"])) or {},
                                                  projects.get(q["project"]))
            for q in rows}


def waits_for_person(store, fx) -> list[dict]:
    """The queue rows that wait for the person to merge: the first half of review_split."""
    return review_split(store, fx)[0]


def holds_prs(rows: list[dict], tickets: list[dict], milestones: list[dict]) -> str | None:
    """What merging these pull requests unblocks (TR-1448): the open tickets that depend on
    theirs, else the milestone one of them is the last open ticket of, else nothing."""
    by_label = {factory_mod.label(t): t for t in tickets}
    mine = {by_label[r["ticket"]]["id"] for r in rows if r["ticket"] in by_label}
    after = [factory_mod.label(t) for t in tickets
             if t["id"] not in mine and not factory_mod.finished(t)
             and mine.intersection(t.get("depends_on") or ())]
    if after:
        return holds_tickets(after)
    for r in rows:
        named = milestone_name(milestones, r.get("milestone")) if r.get("blocks_milestone") else None
        if named:
            return named
    return None


CI_WORD = {"success": "passing", "failure": "failing", "pending": "running", "none": "none"}


def pr_checks(q: dict) -> dict:
    """A pull request's CI and drift in a word, a number and a sentence (TR-1449), from what Tares
    read last: {ci, behind_by, line}. None where it has not been read."""
    ci = CI_WORD.get(q.get("ci") or "")
    behind, base = q.get("behind_by"), q.get("base") or "main"
    said = {"passing": "Checks pass.", "failing": "Checks fail.", "running": "Checks are still running.",
            "none": "No checks ran."}.get(ci or "", "Checks not read yet.")
    if isinstance(behind, int) and behind > 0:
        said += f" {base} has {fh._count(behind, 'commit')} this branch lacks."
    elif behind == 0:
        said += f" Up to date with {base}."
    return {"ci": ci, "behind_by": behind if isinstance(behind, int) else None, "line": said}


def _merge_asks(store) -> tuple[dict[tuple[str, int], tuple[dict, dict]], set]:
    """({(repo, PR number): (queue row, its open desk question)} for the merges the merge queue
    asks a person for, {(repo, PR number, head)} approved and on their way) (TR-1450)."""
    out, approved = {}, set()
    for r in store.merge_queue_open():
        if r.get("approved_head"):
            approved.add((r["repo"], int(r["pr"]), r["approved_head"].lower()))
        if r["state"] != "waiting_approval" or not r.get("desk_item"):
            continue
        d = store.get_desk_item(r["desk_item"])
        if d and d["state"] == "open":
            out[(r["repo"], int(r["pr"]))] = (r, d)
    return out, approved


def prs_to_review(store, fx, now) -> list[dict]:
    """Each pull request that waits for the person (waits_for_person), one item each: the
    reviewer passed it on its current commit and the project's autonomy leaves the merge to the
    person. The others wait for the reviewer, the builder or the crew. An item carries its CI
    and drift (`checks`, TR-1449). When the merge queue asks the same people to approve it, the
    item's Merge approves it in one tap and the queue's own desk question shows no more
    (`absorbs`, read by order_waiting, TR-1450); when it asks someone else, both stay. A merge
    approved for the PR's commit no longer waits: the queue has it."""
    if fx is None:
        return []
    o = owners_mod.Owners(store)
    rows_in = waits_for_person(store, fx)
    owners = pr_owners(store, rows_in, o)
    projects = {p["id"]: p for p in store.list_projects()}
    pids = sorted({q["project"] for q in rows_in})
    tickets, milestones = store.tickets_by_project(pids), store.milestones_by_project(pids)
    asks, approved = _merge_asks(store) if rows_in else ({}, set())
    out = []
    for q in rows_in:
        if q.get("number") and (q.get("repo"), int(q["number"]), (q.get("head") or "").lower()) in approved:
            continue     # the person said yes to this commit; the queue merges it
        pid, p = q["project"], projects.get(q["project"])
        ids = owners[(pid, q["ticket"])]
        since = q.get("waiting_since") or now
        detail = f"{q['ticket']}, {_trim(q['title'], 80)}" + (
            f", PR {q['number']}." if q.get("number") else ".") + (
            " It holds up a milestone." if q.get("blocks_milestone") else "")
        acts = [{"label": "Review", "primary": True, "href": f"/reviews?project={pid}"}]
        absorbs = []
        ask = asks.get((q.get("repo"), int(q["number"]))) if q.get("number") else None
        if ask and o.desk(ask[1], p) == ids:
            acts = [{"label": "Merge", "primary": True, "do": "approve_merge", "queue": ask[0]["id"]},
                    {**acts[0], "primary": False}]
            absorbs = [ask[1]["label"]]
        item = owners_mod.tag(fh.home_item(
            "prs_to_review", label="PR to review", title=f"{q['project_name']}, {_trim(q['title'], 80)}",
            detail=detail,
            meta="Waiting " + _for(fh.plain_wait(fh._minutes(since, now)))
                 + (" · " + q["repo"] if q.get("repo") else ""),
            since=since, now=now, project={"id": pid, "name": q["project_name"]},
            repo=q.get("repo"), id=f"prs_to_review:{pid}:{q['ticket']}",
            ask=ask_prs([q], q["project_name"]),
            holds_up=holds_prs([q], tickets.get(pid) or [], milestones.get(pid) or []),
            checks=pr_checks(q), actions=acts, blocking=True,
            # whose merge waits for the person: not in the crew's hands (project_state)
            tickets=[q["ticket"]]), o, ids, p if ids == o.project(p) else None)
        if absorbs:
            item["absorbs"] = absorbs
        out.append(item)
    return out


# ── projects ready to hand to the crew ────────────────────────────────────────

def ready_to_start(store, fx, now) -> list[dict]:
    """Open projects with a spec and tickets that the crew has not been handed, and no question
    still open: a project waiting on an answer is in "You are waiting on" instead."""
    projects = _open_projects(store)
    if not projects:
        return []
    o = owners_mod.Owners(store)
    tickets = store.tickets_by_project([p["id"] for p in projects])
    handed = set(store.handed_over_projects())
    asking = {q["project"] for q in store.list_spec_questions(state="open")}   # one read, every project
    specs: dict[str, list] = {}
    for d in store.list_cell_docs():
        if d["kind"] == "spec":
            for pid in d["projects"]:
                specs.setdefault(pid, []).append(d["created_at"])
    out = []
    for p in projects:
        ts = tickets.get(p["id"]) or []
        if not ts or p["id"] in handed or not specs.get(p["id"]) or p["id"] in asking:
            continue
        since = min(specs[p["id"]], key=lambda t: fh._dt(t) or now)
        where = next((_TRACKER[t["owner"]] for t in ts if t.get("owner") in _TRACKER), "")
        out.append(owners_mod.tag(fh.home_item(
            "ready_to_start", label="Ready to start", title=p["name"],
            detail=f"Spec and plan written, {fh._count(len(ts), 'ticket')}{where}. "
                   "The crew has not picked it up.",
            meta="Spec written " + fh.plain_ago(since, now), since=since, now=now,
            project=_ref(p), id=f"ready_to_start:{p['id']}", ask=f"Hand {p['name']} to the crew",
            holds_up=fh._count(len(ts), "ticket"),
            actions=[{"label": "Read the spec", "primary": False, "href": f"/projects/{p['id']}"},
                     {"label": "Later", "primary": False, "do": "later", "project": p["id"]},
                     {"label": "Copy the hand-over command", "primary": True, "do": "copy",
                      "text": f"/tares:ship {p['name']}"}]), o, o.project(p), p))
    return out


# ── calls the crew made without the person ────────────────────────────────────

def call_lines(calls: list[dict]) -> list[str]:
    """The choices a row shows of its calls, and how many more there are."""
    n = len(calls)
    shown = [_trim(a["choice"]) for a in calls[:_SHOWN_CHOICES]]
    if n > _SHOWN_CHOICES:
        shown.append(f"{n - _SHOWN_CHOICES} more")
    return shown


def calls_to_confirm(store, fx, now, skip: set | None = None) -> list[dict]:
    """Per project and owner, its open assumptions: a call a desk batch holds waits for whoever the
    batch is for, the rest for the project's owner (factory_owners). `skip` holds (project, label)
    pairs a team home already shows as a batch on someone's desk."""
    rows = [a for a in store.open_assumptions() if (a["project"], a["label"]) not in (skip or ())]
    if not rows:
        return []
    open_ps = {p["id"]: p for p in _open_projects(store)}
    names = {pid: p["name"] for pid, p in open_ps.items()}
    o = owners_mod.Owners(store)
    # a call in a batch on the desk waits for whoever the batch is for (its addressee, else the
    # project's owner); any other call, for the project's owner. One item per project and owners.
    batched: dict[tuple[str, str], tuple[str, ...]] = {}
    for d in store.list_desk("open", None):
        for x in d.get("assumptions") or ():
            batched[(d["project"], x)] = o.desk(d, open_ps.get(d["project"]))
    by: dict[tuple, list[dict]] = {}
    for a in rows:
        if a["project"] in names:
            ids = batched.get((a["project"], a["label"])) or o.project(open_ps[a["project"]])
            by.setdefault((a["project"], ids), []).append(a)
    groups: dict[str, int] = {}
    for pid, _ in by:
        groups[pid] = groups.get(pid, 0) + 1
    out = []
    for (pid, ids), items in by.items():
        n = len(items)
        shown = call_lines(items)
        keep = "both" if n == 2 else "it" if n == 1 else "them"
        roles = list(dict.fromkeys(_ROLE_WORD.get(a["made_role"] or "", "the crew") for a in items))
        since = min((a["made_at"] for a in items), key=lambda t: fh._dt(t) or now)
        out.append(owners_mod.tag(fh.home_item(
            "calls_to_confirm", label="1 call to confirm" if n == 1 else f"{n} calls to confirm",
            title=names[pid], detail="The crew decided without you:", lines=shown,
            meta="Made by " + _and(roles), since=since, now=now,
            project={"id": pid, "name": names[pid]}, ask=ask_calls(n, names[pid]),
            id=f"calls_to_confirm:{pid}" + (f":{'+'.join(ids)}" if groups[pid] > 1 else ""),
            actions=[{"label": "Read them", "primary": False, "href": f"/projects/{pid}/calls"},
                     {"label": {"both": "Keep both", "it": "Keep it", "them": "Keep all"}[keep],
                      "primary": True, "do": "keep_assumptions", "project": pid,
                      "refs": [a["label"] for a in items]}]), o, ids,
            open_ps[pid] if ids == o.project(open_ps[pid]) else None))
    return out


# ── questions and permission requests on the desk ─────────────────────────────

def desk_items(store, fx, now) -> list[dict]:
    """The open desk: a question for the person, or a permission request (it has a command). A
    batch of assumptions is not here: calls_to_confirm shows those."""
    rows = [d for d in store.list_desk("open", None) if not d.get("assumptions")]
    if not rows:
        return []
    projects = {p["id"]: p for p in store.list_projects()}
    names = {pid: p["name"] for pid, p in projects.items()}
    o = owners_mod.Owners(store)
    out = []
    for d in rows:
        pid = d["project"] if d["project"] in names else None
        ids = o.desk(d, projects.get(pid))
        # addressed to nobody who is here: it fell to the project's owner, who should know why
        fell = projects.get(pid) if pid and o.here(d.get("to_person_id")) is None else None
        project = {"id": pid, "name": names[pid]} if pid else None
        # a question about a project opens on its page; one for every project, on the crew page
        href = f"/projects/{pid}#desk-{d['label']}" if pid else f"/crew#desk-{d['label']}"
        answer = {"label": "Answer", "primary": not d.get("command"), "href": href}
        if d.get("command"):
            who = _role_of_station(d.get("station"))
            # the grant may hold in its project alone unless a crew station asked: the crew serves
            # every project (the Crew page decides the same way, by the station's name)
            may_limit = bool(pid) and not str(d.get("station") or "the station").startswith("crew-")
            # the command is the crew's own text, in backticks so the page can show it in a
            # monospace span
            out.append(owners_mod.tag(fh.home_item(
                "permission", label="Permission", title=names[pid] if pid else "Every project",
                detail=f"{who[0].upper()}{who[1:]} wants to use: `{d['command']}`",
                meta="Asked by the crew, " + fh.plain_ago(d["created_at"], now),
                since=d["created_at"], now=now, project=project, id=f"permission:{d['label']}",
                blocking=True, ask=ask_permission(d["command"]), holds_up=holds_desk(d),
                actions=[{**answer, "primary": False},
                         {"label": "Allow", "primary": True, "do": "allow", "desk": d["label"], "station": d.get("station"),
                          "may_limit": may_limit}]), o, ids, fell))
        else:
            out.append(owners_mod.tag(fh.home_item(
                "question", label="Question for you", title=names[pid] if pid else "Every project",
                detail=d["question"], meta="Asked by the crew, " + fh.plain_ago(d["created_at"], now),
                since=d["created_at"], now=now, project=project, id=f"question:{d['label']}",
                blocking=desk_blocks(d), question=desk_question(d), ask=ask_question(d["question"]),
                holds_up=holds_desk(d),
                actions=[answer]), o, ids, fell))
    return out


# ── milestones whose tickets all merged ───────────────────────────────────────

def crew_has(store, project: dict) -> bool:
    """The project is with a crew that will run its checks (TR-1021): handed over, not finished
    and not taken back."""
    if project.get("status") == "done":
        return False
    ho = store.get_handover(project["id"])
    if not ho or ho.get("finished"):
        return False
    return not (ho.get("takeover") and not ho["takeover"].get("released_at"))


def _milestone_states(store, projects: list[dict]) -> dict[str, list[tuple[int, dict, dict, list[dict]]]]:
    """{project id: [(position, milestone, factory.milestone_state, its tickets)]} in three queries."""
    ids = [p["id"] for p in projects]
    tickets, milestones = store.tickets_by_project(ids), store.milestones_by_project(ids)
    mids = [m["id"] for ms in milestones.values() for m in ms]
    results, accepted = store.milestone_check_results(mids), store.milestone_acceptances(mids)
    out: dict[str, list] = {}
    for p in projects:
        has = crew_has(store, p)
        for i, m in enumerate(milestones.get(p["id"]) or [], 1):
            mine = [t for t in tickets.get(p["id"]) or [] if t["milestone"] == m["id"]]
            st = factory_mod.milestone_state(m, mine, results.get(m["id"]), accepted.get(m["id"]), has)
            out.setdefault(p["id"], []).append((i, m, st, mine))
    return out


def milestones_to_check(store, fx, now) -> list[dict]:
    """Per open project, each milestone that needs the person (TR-1017): every ticket merged,
    not accepted, nothing failed, and a check marked for a person still open, or crew checks the
    crew never recorded (a milestone from before results were kept), which the person may accept
    as it is. A milestone whose checks passed is accepted without asking; a failed check is the
    crew's work. A project ready to close shows none: its Ready to close item names them."""
    projects = [p for p in _open_projects(store)]
    if not projects:
        return []
    states = _milestone_states(store, projects)
    tickets = store.tickets_by_project([p["id"] for p in projects])
    o = owners_mod.Owners(store)
    out = []
    for p in projects:
        if factory_mod.ready_to_close(p, tickets.get(p["id"]) or []):
            continue
        rows = states.get(p["id"]) or []
        for i, m, st, mine in rows:
            if not st["needs_person"]:
                continue
            live = [t for t in mine if t["status"] != "canceled"]
            since = max((t["updated_at"] for t in live if t.get("updated_at")),
                        key=lambda t: fh._dt(t) or now, default=m["updated_at"])
            if st["person_open"]:
                n = len(st["person_open"])
                detail = (f"Every ticket of this milestone merged. {fh._count(n, 'check')} "
                          f"{'needs' if n == 1 else 'need'} you to try {'it' if n == 1 else 'them'}.")
            else:
                n = len(st["crew_unrun"])
                detail = (f"Every ticket of this milestone merged, but the crew has not recorded "
                          f"{'its check' if n == 1 else f'its {n} checks'}. Accept it as it is, or ask the crew.")
            out.append(owners_mod.tag(fh.home_item(
                "milestone_to_check", label="Milestone to check", title=f"{p['name']}, {m['name']}",
                detail=detail, meta="Merged " + fh.plain_ago(since, now), since=since, now=now,
                project=_ref(p), id=f"milestone_to_check:{p['id']}:{m['id']}",
                ask=milestone_ask(p["name"], i),
                # the next milestone starts when this one is accepted
                holds_up=milestone_name([r[1] for r in rows], rows[i][1]["id"]) if i < len(rows) else None,
                # accepting is one tap (TR-1450); trying the checks first is the page's
                actions=[{"label": "Accept", "primary": True, "do": "accept_milestone",
                          "project": p["id"], "milestone": m["id"]},
                         {"label": "Check it", "href": f"/projects/{p['id']}#milestone-{m['id']}"}]),
                o, o.project(p), p))
    return out


# ── projects whose work is all merged ─────────────────────────────────────────

def _ready_for(since, now) -> str:
    """How long the project has been ready: Ready for 3 days, or Ready just now."""
    w = fh.plain_wait(fh._minutes(since, now))
    return "Ready just now" if w == "just now" else f"Ready for {w}"


def projects_to_close(store, fx, now) -> list[dict]:
    """Open projects whose every ticket is finished and at least one merged: the console offers
    Close. Closing runs the project's own finish route, so the item ends with the project."""
    projects = _open_projects(store)
    if not projects:
        return []
    tickets = store.tickets_by_project([p["id"] for p in projects])
    ready = [p for p in projects if factory_mod.ready_to_close(p, tickets.get(p["id"]) or [])]
    states = _milestone_states(store, ready) if ready else {}
    o = owners_mod.Owners(store)
    out = []
    for p in ready:
        ts = tickets.get(p["id"]) or []
        n = sum(1 for t in ts if factory_mod.delivered(t))
        since = max((t["updated_at"] for t in ts if t.get("updated_at")),
                    key=lambda t: fh._dt(t) or now, default=p.get("updated_at") or now)
        # the milestone items fold in here (TR-1017): say which are not accepted yet
        open_ms = [f"M{i}" for i, _, st, _ in states.get(p["id"]) or [] if st["delivered"] and not st["accepted"]]
        note = ("" if not open_ms else
                f" {_and(open_ms)} {'is' if len(open_ms) == 1 else 'are'} not accepted yet.")
        out.append(owners_mod.tag(fh.home_item(
            "signoff", label="Ready to close", title=p["name"],
            detail=(f"All {n} tickets are merged" if n > 1 else "Its ticket is merged")
                   + "." + note + " Close the project when you have checked the result.",
            meta=_ready_for(since, now), since=since, now=now, project=_ref(p),
            id=f"signoff:{p['id']}", ask=f"Close {p['name']}",
            actions=[{"label": "Open project", "primary": False, "href": f"/projects/{p['id']}"},
                     {"label": "Close project", "primary": True, "do": "close"}]), o, o.project(p), p))
    return out


# ── rules the crew proposes ───────────────────────────────────────────────────

def rules_to_decide(store, fx, now) -> list[dict]:
    """Each open rule proposal (TR-443): a finding that kept coming back in review, offered as a
    rule. Accept and Reject live on the Crew page; Home says one waits (TR-1275). A proposal about
    one project carries it, so a private project's proposal stays with that project's people. The
    tickets it came from are not named here: the Crew page leaves out the ones a caller may not see."""
    names = {p["id"]: p["name"] for p in store.list_projects()}
    out = []
    for rp in store.list_rule_proposals("open"):
        pid = rp.get("project") if rp.get("project") in names else None
        out.append(fh.home_item(
            "rule", label="Rule proposed", title=names[pid] if pid else "Every project",
            detail=rp["text"], meta="Proposed by the crew, " + fh.plain_ago(rp["created_at"], now),
            since=rp["created_at"], now=now, project={"id": pid, "name": names[pid]} if pid else None,
            repo=rp.get("repo"), id=f"rule:{rp['id']}",
            ask="Decide on a rule: " + _trim(rp["text"], 100),
            actions=[{"label": "Accept or reject", "primary": True, "href": "/crew#rules"}]))
    return out


fh.WAITING_BUILDERS.extend([prs_to_review, ready_to_start, calls_to_confirm, desk_items,
                            milestones_to_check, projects_to_close, rules_to_decide])
