"""A project's milestones, their acceptance checks and the summary of an accepted one."""
from __future__ import annotations

import asyncio
from functools import partial

from fastapi import APIRouter, Body, Request

from .. import answers as answers_mod
from .. import docs as docs_mod
from .. import factory as factory_mod
from .. import factory_api, milestone_summary
from .. import github_tickets as _gh_tickets
from .. import linear as _linear
from ..deps import Deps
from ..envelope import now_utc
from ..integrations import registry as integrations_registry
from .tickets import gh_err, home_title, linear_err


def router(deps: Deps) -> APIRouter:
    r = APIRouter()
    store = deps.store
    _err = deps.err
    _current_person = deps.current_person
    _is_system = deps.is_system
    _project_or_404 = deps.project_or_404
    _by = deps.by
    _tickets_out = deps.tickets_out
    _record_model_usage = deps.record_model_usage
    _home_title = home_title
    _linear_err = partial(linear_err, deps)
    _gh_err = partial(gh_err, deps)

    # ── milestones (TR-411): each ships something a person can check, its acceptance checks.
    # A Linear or GitHub project's milestones come from there; their checks stay Tares's ──────
    def _milestones_out(uid: str) -> list[dict]:
        tickets = _tickets_out(uid)
        raw = store.list_tickets(uid)
        ms = store.list_milestones(uid)
        results = store.milestone_check_results([m["id"] for m in ms])
        accepted = store.milestone_acceptances([m["id"] for m in ms])
        from .. import factory_waiting as _fw
        has = _fw.crew_has(store, store.get_project(uid) or {"id": uid})
        out = []
        for m in ms:
            mine = [t for t in tickets if t["milestone"] == m["id"]]
            live = [t for t in mine if t["status"] != "canceled"]
            done = sum(1 for t in live if t["status"] == "done")
            res = results.get(m["id"]) or {}
            st = factory_mod.milestone_state(m, [t for t in raw if t["milestone"] == m["id"]], res,
                                             accepted.get(m["id"]), has)
            results_out = [{"n": i + 1, "for": c.get("for") or "crew",
                            "result": (res.get(i) or {}).get("result"), "note": (res.get(i) or {}).get("note"),
                            "by": (res.get(i) or {}).get("by_name"), "at": (res.get(i) or {}).get("at")}
                           for i, c in enumerate(m["checks"])]
            out.append({**{k: m[k] for k in ("id", "owner", "name", "goal", "checks", "position",
                                              "updated_at")},
                        # each check's number, who it is for and its latest result (TR-1017)
                        "check_results": results_out,
                        "tickets": len(live), "done": done,
                        "ready": [t["label"] for t in mine if t["ready"]],
                        "finished": bool(live) and done == len(live),
                        # TR-1017: accepted (checks passed, or the person accepted it), and the
                        # checks still to run: the crew's are yours to run and record_milestone_check
                        "accepted": st["accepted"], "needs_person": st["needs_person"],
                        "crew_to_run": [i + 1 for i in st["crew_unrun"]],
                        "crew_failed": [i + 1 for i in st["crew_failed"]],
                        "person_wrong": [i + 1 for i in st["person_wrong"]]})
        return out

    def _milestone_or_404(uid: str, ref: str) -> dict:
        m = store.get_milestone(uid, ref)
        if m is None:
            _err(KeyError(f"project has no milestone {ref!r}"), 404)
        return m

    @r.get("/api/projects/{uid}/milestones")
    def list_project_milestones(uid: str):
        """The project's milestones in order, each with its goal, acceptance checks, and how far
        its tickets are (count, done, the ready ones, finished)."""
        _project_or_404(uid)
        return {"milestones": _milestones_out(uid)}

    @r.post("/api/projects/{uid}/milestones/{ref}/checks/{n}")
    def record_milestone_check(uid: str, ref: str, n: str, request: Request, body: dict = Body(...)):
        """{result: pass|fail, note?}: one run of a milestone's acceptance check n (1-based)
        (TR-1017). A station of the crew records its own checks; a person records the checks
        marked for a person. A person's fail is "something's wrong": on a project whose tickets
        are Tares's, a ticket to fix it is opened in the milestone; otherwise the orchestrator
        sees the failed check in list_milestones and opens it where the tickets live."""
        _project_or_404(uid)
        m = _milestone_or_404(uid, ref)
        n = int(n) if str(n).isdigit() else 0
        if not 1 <= n <= len(m["checks"]):
            _err(KeyError(f"{m['name']} has {len(m['checks'])} checks; there is no check {n}"), 404)
        result = str((body or {}).get("result") or "").strip().lower()
        if result not in ("pass", "fail"):
            _err(ValueError("result is pass or fail"))
        note = " ".join(str((body or {}).get("note") or "").split())[:2000] or None
        check = m["checks"][n - 1]
        station = factory_api.caller(request)["station"]
        person = _current_person(request)
        if not station and check.get("for") != "person":
            _err(ValueError(f"check {n} of {m['name']} is the crew's to run and record"), 409)
        if station and check.get("for") == "person":
            _err(ValueError(f"check {n} of {m['name']} is for a person to try; it shows on their "
                            "waiting list"), 409)
        if result == "fail" and not station and not note:
            _err(ValueError("say in one sentence what is wrong"))
        store.record_milestone_check(uid, m["id"], n - 1, result, note,
                                     "crew" if station else "person", station or _by(request, body),
                                     None if station else (person or {}).get("id"), now_utc())
        opened = None
        if result == "fail" and not station and store.project_ticket_home(uid) is None:
            title = f"Fix: {check['check']}"[:docs_mod.MAX_TITLE]
            opened = store.create_ticket(uid, "tares", title, "todo", milestone=m["id"])
            store.add_ticket_history(opened, uid, "stage", "todo",
                                     f"{_by(request, body)} tried check {n} of {m['name']}: {note}",
                                     "", "person", now_utc(), (person or {}).get("id"))
        out = next(x for x in _milestones_out(uid) if x["id"] == m["id"])
        return {**out, "opened": factory_mod.label(store.get_ticket(uid, opened)) if opened else None}

    # ── the summary of an accepted milestone (TR-1645, milestone_summary.py) ─────────────
    summaries = milestone_summary.Summaries(store, _record_model_usage)

    def _summary_of(uid: str, ref: str) -> tuple[dict, bool]:
        """(the milestone, whether it is accepted), for a project the caller may see."""
        _project_or_404(uid)
        m = _milestone_or_404(uid, ref)
        return m, next((x["accepted"] for x in _milestones_out(uid) if x["id"] == m["id"]), False)

    @r.get("/api/projects/{uid}/milestones/{ref}/summary")
    def read_milestone_summary(uid: str, ref: str):
        """{milestone, summary: {text, source, figures, written_at} | null, writing}: a few lines
        on what an accepted milestone delivered, the decisions taken on the way and what is left.
        Written once, when it is accepted or else on the first read, in the background: `writing`
        says to ask again shortly. A milestone not accepted has none."""
        m, accepted = _summary_of(uid, ref)
        if not accepted:
            return summaries.answer(m["id"], None, False)
        return summaries.read(uid, m["id"])

    @r.post("/api/projects/{uid}/milestones/{ref}/summary/rewrite", status_code=202)
    def rewrite_milestone_summary(uid: str, ref: str, request: Request):
        """Write an accepted milestone's summary again, from today's figures. Answers at once with
        the kept text and `writing`."""
        m, accepted = _summary_of(uid, ref)
        if factory_api.caller(request)["station"]:
            _err(ValueError("a person asks for a summary again, not a station"), 403)
        if not accepted:
            _err(ValueError(f"{m['name']} is not accepted yet, so it has no summary"), 409)
        return summaries.rewrite(uid, m["id"])

    @r.post("/api/projects/{uid}/milestones/{ref}/accept")
    def accept_milestone(uid: str, ref: str, request: Request, body: dict = Body(default={})):
        """The person accepts a milestone as it is (TR-1017): every ticket merged, whatever checks
        are still open. Its item leaves the person's waiting list."""
        _project_or_404(uid)
        m = _milestone_or_404(uid, ref)
        if factory_api.caller(request)["station"]:
            _err(ValueError("a person accepts a milestone, not a station"), 403)
        person = _current_person(request)
        store.accept_milestone(uid, m["id"], _by(request, body), (person or {}).get("id"), now_utc())
        summaries.queue(uid, m["id"])   # written in the background (TR-1645); never raises
        # the answer to the milestone's home item (TR-1450), in the words the item asked
        from .. import factory_waiting as _fw
        place = next((i for i, x in enumerate(store.milestones_by_project([uid]).get(uid) or [], 1)
                      if x["id"] == m["id"]), 1)
        answers_mod.record(store, request, item=f"milestone_to_check:{uid}:{m['id']}",
                           kind="milestone_to_check", project=uid,
                           ask=_fw.milestone_ask(store.get_project(uid)["name"], place),
                           choice="yes", said="Accepted",
                           person_id=(person or {}).get("id")
                           or ("local" if _is_system(request) else None))
        return next(x for x in _milestones_out(uid) if x["id"] == m["id"])

    @r.post("/api/projects/{uid}/milestones", status_code=201)
    async def create_project_milestone(uid: str, body: dict = Body(...)):
        """{name, goal?, checks?: [{check, expect}], position?}. A milestone with the same name
        is changed instead of added twice, keeping the goal and checks the body leaves out. When
        the project's tickets live in Linear or GitHub, a new milestone is created there through
        the tickets integration (TR-851), and one that exists only in Tares takes its place; its
        name and order change there afterwards (`position` is ignored)."""
        await asyncio.to_thread(_project_or_404, uid)
        try:
            name, goal, checks = factory_mod.milestone_ok(body.get("name"), body.get("goal"),
                                                          body.get("checks"))
        except docs_mod.DocError as e:
            _err(e)
        home = await asyncio.to_thread(store.project_ticket_home, uid)
        cur = await asyncio.to_thread(store.get_milestone, uid, name)
        if home and (cur is None or cur["owner"] == "tares"):
            cls = integrations_registry.get("tickets", home)
            if cls is None:
                _err(ValueError(f"this project's milestones live in {_home_title(home)}: add it "
                                "there, then write its checks here"), 409)
            try:
                refs = await cls(store).create(uid, [{"name": name, "goal": goal}], [])
            except (_linear.LinearError, _gh_tickets.GitHubTicketsError) as e:
                (_linear_err if home == "linear" else _gh_err)(e)
            except (ValueError, KeyError) as e:
                _err(e)
            if len(refs) != 1:
                _err(ValueError(f"{_home_title(home)} did not answer with the milestone"), 502)
            # a sync may have mirrored it meanwhile: the row is found by its external id, else the
            # Tares milestone of the name takes it over
            mid = await asyncio.to_thread(
                store.adopt_milestone, uid, home, refs[0].id, name,
                goal if "goal" in body else None, checks if "checks" in body else None)
            return await asyncio.to_thread(
                lambda: next(m for m in _milestones_out(uid) if m["id"] == mid))

        def _write() -> dict:
            pos = body.get("position")
            if cur:
                fields = {k: v for k, v in (("goal", goal), ("checks", checks)) if k in body}
                if pos is not None and cur["owner"] == "tares":
                    fields["position"] = float(pos)
                store.update_milestone(cur["id"], **fields)
                mid = cur["id"]
            else:
                mid = store.create_milestone(uid, name, goal, checks,
                                             float(pos) if pos is not None else None)
            return next(m for m in _milestones_out(uid) if m["id"] == mid)
        return await asyncio.to_thread(_write)

    @r.put("/api/projects/{uid}/milestones/{ref}")
    def update_project_milestone(uid: str, ref: str, body: dict = Body(...)):
        """Change a milestone's name, goal, checks or position. A Linear or GitHub milestone only
        takes goal and checks (its name and order change where it lives)."""
        _project_or_404(uid)
        m = _milestone_or_404(uid, ref)
        if m["owner"] != "tares" and ({"name", "position"} & set(body)):
            _err(ValueError(f"{m['name']} lives in {_home_title(m['owner'])}: rename or reorder "
                            "it there"), 409)
        fields: dict = {}
        try:
            if "name" in body:
                fields["name"] = docs_mod.title_ok(body["name"])
                other = store.get_milestone(uid, fields["name"])
                if other and other["id"] != m["id"]:
                    _err(ValueError(f"the project already has a milestone {other['name']!r}; "
                                    "milestone names are unique"), 409)
            if "goal" in body:
                fields["goal"] = factory_mod.goal_ok(body["goal"])
            if "checks" in body:
                fields["checks"] = factory_mod.checks_ok(body["checks"])
        except docs_mod.DocError as e:
            _err(e)
        if body.get("position") is not None:
            fields["position"] = float(body["position"])
        if fields:
            store.update_milestone(m["id"], **fields)
        return next(x for x in _milestones_out(uid) if x["id"] == m["id"])

    @r.delete("/api/projects/{uid}/milestones/{ref}")
    def delete_project_milestone(uid: str, ref: str):
        """Delete a Tares milestone; its tickets stay, without a milestone."""
        _project_or_404(uid)
        m = _milestone_or_404(uid, ref)
        if m["owner"] != "tares":
            _err(ValueError(f"{m['name']} lives in {_home_title(m['owner'])}: remove it there"),
                 409)
        store.delete_milestone(m["id"])
        return {"ok": True, "deleted": m["id"]}

    return r
