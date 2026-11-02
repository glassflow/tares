"""The factory Reviews page (TR-735): GET /api/factory/reviews.

Every pull request that is in review, across projects, in two lists: the ones that wait for the
person and the ones that are with the crew. Which is which is decided in one place,
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

from fastapi import Query

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
    }


def register(app, store, h, ctx):
    @app.get("/api/factory/reviews")
    def factory_reviews(project: str = Query("")):
        """{waiting_for_you: [row], with_the_crew: [row], autonomy_text, project, now}. Both lists
        have the longest waiting first. `?project=<id>` keeps one project's pull requests; an id
        the cell does not know gives two empty lists. `project` is {id, name} of the filter, or
        null."""
        now = now_utc()
        fx = ctx.fx
        shown = None
        if project:
            shown = next(({"id": p["id"], "name": p["name"]} for p in store.list_projects()
                          if p["id"] == project), None)
        rows = fx.queue() if fx else []
        if project:
            rows = [q for q in rows if q["project"] == project]
        mine, theirs = fw.review_split(store, fx, rows)
        key = lambda q: (fh._dt(q.get("waiting_since")) or now, q["project_name"], q["ticket"])
        milestones = {pid: store.list_milestones(pid)
                      for pid in sorted({q["project"] for q in rows if q.get("blocks_milestone")})}
        crew = ((fx.settings() or {}).get("autonomy") if fx else None)
        level = (fw.effective_autonomy(store, fx, shown["id"], crew) if shown
                 else crew or fw.DEFAULT_AUTONOMY)
        return {"waiting_for_you": [_row(q, now, milestones) for q in sorted(mine, key=key)],
                "with_the_crew": [_row(q, now, milestones) for q in sorted(theirs, key=key)],
                "autonomy_text": AUTONOMY_TEXT.get(level, AUTONOMY_TEXT[fw.DEFAULT_AUTONOMY]),
                "project": shown, "now": now.isoformat()}
