"""The factory Reviews page (TR-735): GET /api/factory/reviews.

Every pull request that is in review, across projects, in four lists: the ones that wait for the
caller's merge, the ones that wait for someone else's (named for them), the ones a named role of
the crew has next, and the ones nobody is on (TR-1009). Whose merge it is comes from
`factory_waiting.pr_owners` (the owner of the ticket, factory_owners). Which list is decided in one place,
`factory_waiting.review_split` (assumption A10: a pull request waits for the person when its
project's autonomy is L3-review and the reviewer passed it on its current commit), the same
function the home page's "PRs to review" reads, so the two always agree.

The rows come from the review queue (`ctx.fx.queue()`), read once. The strings are the cell's own
words around the person's own text (project, ticket and pull request titles); the page escapes
every one of them, the API sends them raw.
"""
from __future__ import annotations

import re
from urllib.parse import quote

from fastapi import Query, Request

from .. import factory_home as fh
from .. import factory_waiting as fw
from ..envelope import now_utc

AUTONOMY_TEXT = {
    "L3-review": "You merge every pull request after the crew's review.",
    "L4-ship": "The crew merges after its review and green CI.",
}
# the dot of a CI sentence: ok (green), bad (red), go (amber, still going), none (grey)
CI = {"success": ("CI passed", "ok"), "failure": ("CI failing", "bad"),
      "pending": ("CI in progress", "go")}
NO_CI = ("No CI", "none")
_NUMBERED = re.compile(r"^\s*(?:M\s*)?\d+\s*[.:)\-]", re.I)
_PULL = re.compile(r"/pull/(\d+)")


def review_words(verdict: dict | None) -> str:
    """What the reviewer said, in a sentence."""
    v = verdict or {}
    kind, rnd = v.get("verdict"), v.get("round")
    suffix = f", round {rnd}" if rnd else ""
    if kind == "pass":
        return f"Passed{suffix}" if v.get("current") else "Passed on an older commit"
    if kind == "changes":
        return f"Changes asked{suffix}"
    if kind == "block":
        return f"Blocked by the reviewer{suffix}"
    return "Not reviewed yet"


def milestone_words(milestones: list[dict], mid: str | None) -> str | None:
    """"Holds up 1. Foundations" for a milestone already named with its number (1. / M2: / 3)),
    else "Holds up M3: Name" with the number of its place in the project's list."""
    for i, m in enumerate(milestones, 1):
        if m["id"] == mid:
            name = str(m["name"] or "").strip()
            if not name:
                return f"Holds up M{i}"
            return f"Holds up {name}" if _NUMBERED.match(name) else f"Holds up M{i}: {name}"
    return None


def _number(q: dict):
    if q.get("number"):
        return q["number"]
    m = _PULL.search(q.get("pr") or "")
    return int(m.group(1)) if m else None


def _row(q: dict, now, milestones: dict[str, list[dict]]) -> dict:
    ci, ci_state = CI.get(q.get("ci"), NO_CI)
    since = fh._dt(q.get("waiting_since"))
    pid = q["project"]
    return {
        "project": {"id": pid, "name": q["project_name"]},
        "ticket": {"ref": q["ticket"], "title": q["title"],
                   "href": f"/projects/{quote(pid, safe='')}/tickets/{quote(q['ticket'], safe='')}"},
        "pr": {"number": _number(q), "url": q.get("pr"), "title": q.get("pr_title"),
               "repo": q.get("repo")},
        "waited": fh.plain_wait(fh._minutes(since, now)),
        "since": since.isoformat() if since else None,
        "review": review_words(q.get("verdict")),
        "ci": ci, "ci_state": ci_state,
        "holds_up": (milestone_words(milestones.get(pid) or [], q.get("milestone"))
                     if q.get("blocks_milestone") else None),
        # the challenger found something blocking and nobody has answered it
        "codex_open": not q.get("layer1_clean", True),
        # who has it next ("The reviewer reviews it"), or why nobody does; null when it waits
        # for the person (TR-1009)
        "why": q.get("why"),
    }


def register(app, store, h, ctx):
    @app.get("/api/factory/reviews")
    def factory_reviews(request: Request, project: str = Query("")):
        """{waiting_for_you: [row], waiting_for_others: [row], with_the_crew: [row], nobody_on_it:
        [row], autonomy_text, project, now}. A pull request that waits for a person's merge is the
        caller's when its ticket is theirs (factory_owners), else under waiting_for_others with
        `for_people` [{id, name}]. Every list has the longest waiting first; a crew row's `why` names who has
        it next, a nobody row's says why nobody does (factory_waiting.review_split).
        `?project=<id>` keeps one project's pull requests; an id
        the cell does not know gives three empty lists. `project` is {id, name} of the filter, or
        null."""
        now = now_utc()
        fx = fh.for_caller(ctx.fx, request)
        shown = None
        if project:
            hidden = h.hidden_ids()
            shown = next(({"id": p["id"], "name": p["name"]} for p in store.list_projects()
                          if p["id"] == project and p["id"] not in hidden), None)
        rows = fx.queue() if fx else []
        if project:
            rows = [q for q in rows if q["project"] == project]
        waits, theirs, nobody = fw.review_split(store, fx, rows)
        # of those that wait for a person's merge, the caller's, and the others' named for them
        # (factory_owners: the owner of each ticket, the rule the home reads too)
        o = fh.owners_mod.Owners(store)
        owners = fw.pr_owners(store, waits, o)
        for q in waits:
            q["for_people"] = list(owners[(q["project"], q["ticket"])])
        caller = fh._caller(store, fx, request)
        mine = fh.owners_mod.mine(waits, **caller)
        ids = {(q["project"], q["ticket"]) for q in mine}
        others = [q for q in waits if (q["project"], q["ticket"]) not in ids]
        key = lambda q: (fh._dt(q.get("waiting_since")) or now, q["project_name"], q["ticket"])
        milestones = {pid: store.list_milestones(pid)
                      for pid in sorted({q["project"] for q in rows if q.get("blocks_milestone")})}
        crew = ((fx.settings() or {}).get("autonomy") if fx else None)
        level = (fw.effective_autonomy(store, fx, shown["id"], crew) if shown
                 else crew or fw.DEFAULT_AUTONOMY)
        return {"waiting_for_you": [_row(q, now, milestones) for q in sorted(mine, key=key)],
                "waiting_for_others": [{**_row(q, now, milestones), "for_people": o.who(q["for_people"])}
                                       for q in sorted(others, key=key)],
                "with_the_crew": [_row(q, now, milestones) for q in sorted(theirs, key=key)],
                "nobody_on_it": [_row(q, now, milestones) for q in sorted(nobody, key=key)],
                "autonomy_text": AUTONOMY_TEXT.get(level, AUTONOMY_TEXT[fw.DEFAULT_AUTONOMY]),
                "project": shown, "now": now.isoformat()}
