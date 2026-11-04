"""The factory Crew page (TR-736): GET /api/factory/crew.

Who is on the crew, what each is doing, how far the crew may go, what the plan has left, the rules
the reviewer proposes and the crew's standup, in one read. Every string is shown as it is, so the
states, the times and the roles are worded for people. A station's own name (what `tares factory
crew attach` takes) rides along as `station`, only so the page can ask for its detail; the page
never shows it.

What a member did lately, and the command that opens it in a terminal, come from the routes that
already exist (GET /api/crew/{station}, POST /api/crew/{station}/context); this screen adds no write
route. Settings for pace and grants live on the Settings page, not here.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import Request

from .. import claude_usage
from .. import factory as factory_mod
from .. import standup as standup_mod
from ..envelope import now_utc
from ..factory_api import project_progress
from ..factory_home import (_STALE_MINUTES, _count, _dt, _how_far, _minutes, _plan, _resets,
                            crew_member, for_caller, heard_ago, plain_wait)
from .factory_docs import ago

_SHOWN_ROLES = ("orchestrator", "reviewer", "releaser", "builder")
_NO_PROJECT = {"id": None, "name": "Not on a project yet"}
_MAX_BY_PROJECT = 6


def _cost(usd) -> str:
    try:
        c = float(usd)
    except (TypeError, ValueError):
        return ""
    return "under $1 at API prices" if c < 1 else f"${round(c)} at API prices"


def _how(settings: dict) -> dict:
    far = _how_far(settings)
    merge = ("The crew merges after its review and green CI." if far["autonomy"] == "L4-ship"
             else "You approve each merge.")
    n = settings.get("builders_max")
    detail = ["Releases wait for you." if far["releases"] else "No releases."]
    if isinstance(n, int) and not isinstance(n, bool) and n >= 1:
        detail.append(f"Up to {_count(n, 'builder')} at once.")
    return {"text": merge, "detail": " ".join(detail), "settings_href": "/settings#crew"}


def _pacing(pace: dict, now) -> str | None:
    """A sentence while the plan's limits press on the crew; null when pacing is off or the plan
    has room."""
    if not (pace or {}).get("on") or not pace.get("level"):
        return None
    causes = []
    for kind, p in (("session", pace.get("session_pct")), ("weekly_all", pace.get("weekly_pct"))):
        if isinstance(p, (int, float)) and not isinstance(p, bool) and p >= factory_mod.PACE_SLOW[kind]:
            causes.append(("the 5-hour window" if kind == "session" else "this week's limit") + f" is at {p:g}%")
    if not causes:
        return None
    do = ("builders finish their step and wait, and no new builder starts" if pace["level"] == 2
          else "no new builder starts")
    until = _resets(_dt(pace.get("until")), now) if pace.get("until") else None
    return f"Pacing: {' and '.join(causes)}, so {do}" + (f" until {until}." if until else ".")


def register(app, store, h, ctx):
    fx = ctx.fx

    def by_project(names: dict, now) -> list[dict]:
        out = []
        for r in claude_usage.by_project(store, now - timedelta(days=7)):
            if r.get("kind") != "project" or r.get("project") not in names or r.get("share") is None:
                continue
            if round(r["share"] * 100) < 1:
                continue
            out.append({"name": names[r["project"]], "share_text": f"{round(r['share'] * 100)}%",
                        "cost_text": _cost(r.get("cost_usd")), "_share": r["share"]})
        out.sort(key=lambda x: -x["_share"])
        return [{k: v for k, v in x.items() if k != "_share"} for x in out[:_MAX_BY_PROJECT]]

    def standup(now, hidden) -> dict | None:
        """One line from the ledger over the last day; null when nothing moved."""
        try:
            rep = standup_mod.facts(store, now - timedelta(days=1), now,
                                    lambda uid: project_progress(store, uid), hide=hidden)
        except Exception:
            return None
        ps = rep["projects"]
        since, until = rep["since"], rep["until"]

        def asked_in_range(d: dict) -> bool:
            at = d.get("asked_at")
            return isinstance(at, datetime) and since <= at < until

        # only what happened inside the range: what is merely still standing (a ticket that has
        # sat in review for a week, an old question) is not "in the last day"
        counts = (
            ("done", sum(len(p["done"]) for p in ps), "ticket done", "tickets done"),
            ("started", sum(len(p["started"]) for p in ps), "started", "started"),
            ("review", sum(1 for p in ps for x in p["review"] if x.get("new")),
             "sent to review", "sent to review"),
            ("blocked", sum(1 for p in ps for x in p["blocked"] if x.get("new")), "blocked", "blocked"),
            ("needs_you", sum(1 for p in ps for d in p["needs_you"] if asked_in_range(d)),
             "question for you", "questions for you"),
        )
        parts = [f"{n} {one if n == 1 else many}" for _, n, one, many in counts if n]
        if not parts:
            return None
        names = ", ".join(p["name"] for p in ps[:3]) + (f" and {len(ps) - 3} more" if len(ps) > 3 else "")
        return {"text": f"In the last day: {', '.join(parts)}. Projects that moved: {names}.",
                "when": "The last 24 hours"}

    def build(request) -> dict:
        now = now_utc()
        fx_ = for_caller(fx, request)
        stations = fx_.stations()
        settings = fx_.settings()
        hidden = h.hidden_ids()
        projects = {p["id"]: p for p in store.list_projects()
                    if p.get("kind") == "software_factory" and p["id"] not in hidden}
        names = {pid: p["name"] for pid, p in projects.items()}
        open_ids = [pid for pid, p in projects.items() if p["status"] != "done"]
        tickets = store.tickets_by_project(open_ids) if open_ids else {}
        by_holder: dict[str, list[dict]] = {}
        for ts in tickets.values():
            for t in ts:
                if t.get("holder"):
                    by_holder.setdefault(t["holder"], []).append(t)
        by_name = {s["name"]: s for s in stations}
        desk_open = len([d for d in store.list_desk("open", None) if d.get("project") not in hidden])

        def member(s: dict) -> dict:
            m = crew_member(s, by_holder, desk_open, now, by_name)
            doing, ended = m["doing"], s["state"] == "ended"
            if ended:
                doing = "Finished its work."
            elif s["role"] == "builder" and s["state"] == "idle":
                held = next((t for t in by_holder.get(s["name"], [])
                             if t.get("stage") in ("review", "changes")), None)
                if held:
                    doing = (f"Waiting for the review of {held['title']}" if held["stage"] == "review"
                             else f"Changes asked on {held['title']}")
            return {"station": m["station"], "role_label": m["role_label"],
                    "state": "waiting for you" if m["state"] == "waiting" else m["state"],
                    "state_text": m["state_text"], "doing": doing,
                    "heard": m["heard"] or None,
                    "memory_pct": None if ended else m["memory_pct"],
                    "memory_text": None if ended else m["memory_text"], "helpers": m["helpers"]}

        shown = [s for s in stations if s.get("role") in _SHOWN_ROLES]

        def first(role: str):
            s = next((x for x in shown if x["role"] == role), None)
            return member(s) if s else None

        groups: dict = {}
        for s in sorted((x for x in shown if x["role"] == "builder"), key=lambda x: x["name"]):
            pid = s.get("project")
            p = projects.get(pid)
            if s["state"] == "ended" and p is not None and p["status"] == "done":
                continue   # a finished project's builders are done, not on the crew
            key = pid if p is not None else None
            groups.setdefault(key, {"project": {"id": p["id"], "name": p["name"]} if p else _NO_PROJECT,
                                    "members": []})["members"].append(member(s))
        builders = sorted(groups.values(), key=lambda g: (g["project"]["id"] is None,
                                                          str(g["project"]["name"]).lower()))

        orch = next((s for s in shown if s["role"] == "orchestrator"), None)
        if orch is None:
            summary = "Not started"
        elif orch["state"] == "ended":
            summary = "Not running"
        else:
            silent = _minutes(orch.get("last_at"), now)
            heard = heard_ago(orch.get("last_at"), now)
            if orch["state"] == "quiet" or (silent is not None and silent > _STALE_MINUTES):
                summary = "Not heard from in " + plain_wait(silent) if silent else "Not heard from"
            elif orch["state"] == "working":
                summary = f"Working, last heard {heard}" if heard else "Working"
            else:
                summary = f"Running, last heard {heard}" if heard else "Running"

        plan = _plan(store, now, names)
        for lim in plan["limits"]:
            lim["text"] = f"{lim['pct']}% used" + (f", resets {lim['resets']}" if lim["resets"] else "")
        plan["by_project"] = by_project(names, now)
        proposals = [{"id": r["id"], "text": r["text"], "from": "the reviewer",
                      "when": ago(r["created_at"], now)} for r in store.list_rule_proposals("open")
                     if r.get("project") not in hidden]
        releaser = None if settings.get("releaser") is False else first("releaser")
        return {"summary": summary, "talk_to": first("orchestrator"), "reviewer": first("reviewer"),
                "releaser": releaser, "builders": builders, "how_far": _how(settings),
                "pacing": _pacing(fx_.pace(), now), "plan": plan, "rule_proposals": proposals,
                "standup": standup(now, hidden), "now": now.isoformat()}

    @app.get("/api/factory/crew")
    def get_crew_page(request: Request):
        """The Crew page in one read. `talk_to`, `reviewer` and `releaser` are a member or null
        (not started, or no releaser by choice); `builders` are grouped by project, ended builders
        of finished projects left out, helpers counted under the builder that started them.
        A member is {station, role_label, state (working, idle, waiting for you, not heard,
        ended), state_text, doing, heard, memory_pct, memory_text, helpers}; `station` is for the
        detail call and is never shown. `pacing` is a sentence while the plan presses on the crew,
        else null. `rule_proposals` are the open ones, `standup` is null when nothing moved."""
        return build(request)
