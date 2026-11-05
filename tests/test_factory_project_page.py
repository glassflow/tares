"""The factory project page (TR-734): GET /api/factory/projects/{uid}.

One read answers the whole page: the stage, what waits on this project (and only this project),
the counts of tickets by state, the few that move now, the next ticket, the milestones as
compact rows (counts, checks, a link to their tickets), the decisions, the open calls, the
documents, the command that opens a builder's session and where the tickets live. Nothing in
the answer may name a station, a session or a folder of the crew's machine, and every shown word
passes the edition words list. `stage_of` is checked against every branch of phaseOf() in
ui/src/components/project/factory.tsx.

Run: .venv/bin/python tests/test_factory_project_page.py
"""
import asyncio
import json
import os
import re
import sys
import tempfile
import subprocess
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _env import scrub_os_environ
scrub_os_environ()

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_TMP = tempfile.mkdtemp(prefix="tares-factory-project-")
os.environ["TARES_DB"] = os.path.join(_TMP, "api.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(_TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-project"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
os.environ["TARES_EDITION"] = "factory"

import httpx

import tares
from tares import factory as factory_mod
from tares import factory_home as fh
from tares import words
from tares.screens import factory_project as fp
from tares.store import now_utc

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def strings(v, path="$", key=""):
    """Every (path, key, string) in a JSON value, `key` being the name of the field holding it."""
    if isinstance(v, str):
        yield path, key, v
    elif isinstance(v, dict):
        for k, x in v.items():
            yield from strings(x, f"{path}.{k}", k)
    elif isinstance(v, list):
        for i, x in enumerate(v):
            yield from strings(x, f"{path}[{i}]", key)


def stages():
    print("== stage_of: every branch of phaseOf ==")
    t = lambda i, status: {"id": f"t{i}", "status": status}
    cases = [
        ("no starting prompt", False, [t(1, "todo")], "spec"),
        ("a starting prompt and no ticket", True, [], "spec"),
        ("only canceled tickets", True, [t(1, "canceled"), t(2, "canceled")], "spec"),
        ("every ticket done", True, [t(1, "done"), t(2, "done")], "done"),
        ("done and canceled", True, [t(1, "done"), t(2, "canceled")], "done"),
        ("a plan of one ticket, still to do", True, [t(1, "todo")], "build"),
        ("a plan of one ticket, in progress", True, [t(1, "in_progress")], "build"),
        ("only the last of several is left", True, [t(1, "done"), t(2, "done"), t(3, "todo")], "review"),
        ("only the last is left, a canceled one between", True,
         [t(1, "done"), t(2, "canceled"), t(3, "in_progress")], "review"),
        ("the last is canceled, so the one before it counts as last", True,
         [t(1, "done"), t(2, "todo"), t(3, "canceled")], "review"),
        ("a canceled ticket before the last does not count", True,
         [t(1, "canceled"), t(2, "todo"), t(3, "todo")], "build"),
        ("a ticket before the last is left", True, [t(1, "done"), t(2, "todo"), t(3, "done")], "build"),
        ("two tickets are left", True, [t(1, "done"), t(2, "todo"), t(3, "todo")], "build"),
        ("nothing is done yet", True, [t(1, "todo"), t(2, "todo")], "build"),
    ]
    for label, start, tickets, want in cases:
        got = fp.stage_of(start, tickets)
        ck(f"stage_of: {label} is {want}", got == want, got)
    merged_by_stage = {"id": "t9", "status": "in_progress", "stage": "merged"}
    shipped = {"id": "t8", "status": "todo", "stage": "shipped"}
    ck("stage_of: a ticket merged by its stage is done even when its status still says in progress",
       fp.stage_of(True, [merged_by_stage, shipped]) == "done"
       and fp.stage_of(True, [t(1, "done"), merged_by_stage]) == "done",
       fp.stage_of(True, [merged_by_stage, shipped]))
    ck("stage_of: a ticket merged by its stage is not what is left (only the last is left)",
       fp.stage_of(True, [t(1, "done"), merged_by_stage, t(3, "todo")]) == "review")
    ck("a project the crew closed is done whatever its tickets say",
       fp.stage_of(False, [t(1, "todo")], True) == "done" and fp.stage_of(True, [], True) == "done")


def pull_requests():
    print("== a pull request and a review in words ==")
    url = "https://github.com/example-org/docs-site/pull/7"
    out = lambda t: fp._pr_out(t)
    ck("closed without merging", out({"pr": {"number": 7, "url": url, "state": "closed"}})["state_text"]
       == "closed without merging")
    ck("merged by its flag", out({"pr": {"number": 7, "url": url, "state": "closed", "merged": True}})["state_text"]
       == "merged")
    ck("merged by the stage when the pull request has no state",
       out({"stage": "merged", "pr": {"number": 7, "url": url}})["state_text"] == "merged"
       and out({"stage": "shipped", "pr": {"number": 7, "url": url}})["state_text"] == "merged")
    ck("without a state and not merged it is open",
       out({"stage": "review", "pr": {"number": 7, "url": url}})["state_text"] == "open")
    ck("open with CI", out({"pr": {"number": 7, "state": "open", "ci": "pending"}})["state_text"]
       == "open, CI running")
    ck("the number comes from the link", out({"pr": {"url": url, "state": "open"}})["number"] == 7)
    ck("no pull request, no answer", out({}) is None and out({"pr": {}}) is None)
    last = {"verdict": "block", "head": "aaaaaaa1111", "round": 3, "at": None}
    ck("a review that still counts", fp._review_out(last, {"pr": {"head": "aaaaaaa1111"}}) == "round 3: blocked")
    ck("a review for an older commit says the pull request changed",
       fp._review_out(last, {"pr": {"head": "bbbbbbb2222"}}) == "round 3: blocked, then the pull request changed")
    ck("no review, no words", fp._review_out(None, {}) is None)


def holders():
    print("== who_words ==")
    roles = {"project-x-build-m1": "builder", "crew-reviewer": "reviewer", "crew-orchestrator": "orchestrator",
             "crew-releaser": "releaser", "project-x-helper": "helper"}
    cases = [(("project-x-build-m1", "M1"), "Builder for M1"), (("project-x-build-m1", None), "Builder"),
             (("crew-reviewer", "M1"), "Reviewer"), (("crew-orchestrator", None), "Lead"),
             (("crew-releaser", None), "Releaser"), (("project-x-helper", None), "Helper"),
             (("crew-reviewer-lint", None), "Reviewer"), (("project-y-build-m2", "M2"), "Builder for M2"),
             (("some-session", None), "The crew"), (("", None), None), ((None, None), None)]
    for (holder, m), want in cases:
        got = fp.who_words(holder, roles, m)
        ck(f"who_words({holder!r}, {m!r}) is {want!r}", got == want, got)


async def main():
    from tares.daemon import make_app
    app = make_app()
    store = app.state.store
    cx_ = lambda token=TOKEN: httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t",
        headers={"Authorization": f"Bearer {token}"} if token else {})

    def ago(**kw):
        return now_utc() - timedelta(**kw)

    def project(name, goal="A project for the test", factory=True):
        uid = f"uc_{name.replace('-', '')}"
        store.create_project(uid, "custom", name, {}, goal=goal)
        if factory:
            store.set_project_kind(uid, "software_factory")
        return uid

    def ticket(uid, title, **fields):
        owner = fields.pop("owner", "tares")
        ms, repo = fields.pop("milestone", None), fields.pop("repo", None)
        deps = fields.pop("depends_on", None)
        tid = store.create_ticket(uid, owner, title, milestone=ms, repo=repo, depends_on=deps)
        if fields:
            store.update_ticket(tid, **fields)
        return tid

    PATH = "/Users/alex/code/project-x"
    REPO = "example-org/docs-site"
    STATIONS = ["project-x-build-m1", "project-x-build-m2", "crew-orchestrator", "crew-reviewer"]
    SESSIONS = ["sess-b1", "sess-b2", "sess-lead", "sess-rev"]

    async with app.router.lifespan_context(app):
        # ── project-x: tickets in every stage across two milestones ──────────────
        px = project("project-x")
        store.set_handover(px, [{"path": PATH, "name": REPO}], None, None, now_utc())
        for sid, station, role in zip(SESSIONS, STATIONS, ("builder", "builder", "orchestrator", "reviewer")):
            store.set_session_station(sid, station, role, "" if role == "orchestrator" else "crew-orchestrator",
                                      px if role == "builder" else None, now_utc())
        m1 = store.create_milestone(px, "M1: pages", "The pages are live.",
                                    [{"check": "open the home page", "expect": "it loads"}])
        m2 = store.create_milestone(px, "M2: search", "Search works.", [])
        pr = lambda n, **kw: {"url": f"https://github.com/{REPO}/pull/{n}", "repo": REPO, "number": n,
                              "head": "aaaaaaa1111", **kw}
        t1 = ticket(px, "Add the header", milestone=m1, stage="merged", status="done",
                    pr=pr(11, state="closed", merged=True))
        t2 = ticket(px, "Add the footer", milestone=m1, stage="review", status="in_progress",
                    holder=STATIONS[0], pr=pr(12, state="open", ci="success"))
        t3 = ticket(px, "Add the sitemap", milestone=m1, stage="changes", status="in_progress",
                    holder=STATIONS[0], pr=pr(13, state="open", ci="failure"))
        t4 = ticket(px, "Add the search box", milestone=m2, stage="doing", status="in_progress",
                    holder=STATIONS[1])
        t5 = ticket(px, "Add the results page", milestone=m2, depends_on=[t4])
        t6 = ticket(px, "Add the news page", milestone=m2)
        t7 = ticket(px, "Drop the old theme", milestone=m2, status="canceled")
        t8 = ticket(px, "Write the release notes", stage="blocked", status="in_progress",
                    holder=STATIONS[2], stage_reason="waiting for a date")
        t9 = ticket(px, "Tidy the readme")
        for tid, verdict in ((t2, "pass"), (t3, "changes"), (t3, "changes")):
            store.add_review(px, tid, None, "aaaaaaa1111", verdict, "ran the tests", None, STATIONS[3],
                             [], {}, factory_mod.finding_kind, now_utc())
        for kind, title, body in (("start", "Start", "Build the docs site."), ("spec", "Spec", "# Spec\n\nIt is a site."),
                                  ("plan", "Plan", "# Plan\n\n## M1"), ("agents", "Rules", "Use tabs."),
                                  ("memory", "Memory", "- a fact"), ("note", "A note", "text"),
                                  ("working", "Working notes: T2", "steps")):
            store.create_doc(px, kind, title, body, by="Alex")
        a1 = store.add_assumption(px, "Which date format?", "ISO dates", f"it sorts, see {PATH}/dates", "",
                                  [t2], "project-x-build-m1", "builder", ago(days=2))
        a2 = store.add_assumption(px, "Which logger?", "the standard one", "no new package", "", [],
                                  "crew-reviewer", "reviewer", ago(days=1))
        store.decide_assumption(a2["id"], "kept", None, None, now_utc())
        store.add_decision(px, "", "Use tabs or spaces?", f"Tabs, as in {PATH}/style.md", "tabs", False, "project",
                           False, None, False, [], ago(days=3))
        store.add_decision(px, "", "Which licence?", "MIT", "MIT", False, "project", False, None, False, [],
                           ago(hours=2))

        # ── project-y: its own waiting item and tickets; project-z: nothing ready ───
        py = project("project-y")
        ty1 = ticket(py, "Export the posts")
        ticket(py, "Import the posts", depends_on=[ty1])
        store.add_assumption(py, "Which export format?", "JSON", "it is simple", "", [], "project-y-build-m1",
                             "builder", ago(hours=5))
        pz = project("project-z")
        tz1 = ticket(pz, "Old idea", status="canceled")
        ticket(pz, "Replace it", depends_on=[tz1])
        pq = project("project-q")
        ticket(pq, "Wait for a date", stage="blocked", status="in_progress")
        pm = project("project-m")                # fully merged
        store.create_doc(pm, "start", "Start", "Build it.")
        ticket(pm, "One", stage="merged", status="done")
        ticket(pm, "Two", stage="shipped", status="done")
        store.set_handover(pm, [{"path": "/Users/alex/code/project-m", "name": "example-org/project-m"}], None, None,
                           now_utc())
        pc = project("project-c")                # every ticket canceled
        ticket(pc, "Gone", status="canceled")
        pw = project("project-w", factory=False)
        pp = project("project-p")                # a title that names the crew's folder; builders in every state
        PATH_P = "/Users/kim/code/project-p"
        store.set_handover(pp, [{"path": PATH_P, "name": "example-org/project-p"}], None, None, now_utc())
        ticket(pp, f"Fix the build in {PATH_P}/src", status="in_progress")
        store.set_session_station("sess-p-live", "project-p-build-m1", "builder", "crew-orchestrator", pp, now_utc())
        store.set_session_station("sess-p-live2", "project-p-build-m1", "builder", "crew-orchestrator", pp,
                                  now_utc())        # a second session on the same station: one command
        store.set_session_station("sess-p-old", "project-p-build-m0", "builder", "crew-orchestrator", pp, now_utc())
        store.set_session_state("sess-p-old", "ended", None, now_utc())
        # m2: an older session still working, a newer one ended: the station is over
        store.set_session_station("sess-p2-old", "project-p-build-m2", "builder", "crew-orchestrator", pp, ago(hours=3))
        store.set_session_state("sess-p2-old", "working", None, now_utc())
        store.set_session_station("sess-p2-new", "project-p-build-m2", "builder", "crew-orchestrator", pp, ago(minutes=1))
        store.set_session_state("sess-p2-new", "ended", None, now_utc())
        # m3: an older ended session, a newer one working but silent for hours (shown quiet): still offered
        store.set_session_station("sess-p3-old", "project-p-build-m3", "builder", "crew-orchestrator", pp, ago(hours=5))
        store.set_session_state("sess-p3-old", "ended", None, ago(hours=5))
        store.set_session_station("sess-p3-new", "project-p-build-m3", "builder", "crew-orchestrator", pp, ago(hours=3))
        store.set_session_state("sess-p3-new", "working", None, ago(hours=3))
        # a reviewer of project-p and project-y's builder are not project-p's builders
        store.set_session_station("sess-p-rev", "crew-reviewer-p", "reviewer", "crew-orchestrator", pp, now_utc())
        store.set_session_station("sess-y-b", "project-y-build-m1", "builder", "crew-orchestrator", py, now_utc())

        # project-n: seven tickets in motion, so the list is capped; created out of the page's order
        pn = project("project-n")
        ticket(pn, "Blocked one", stage="blocked", status="in_progress")
        tn_doing = ticket(pn, "Doing one", stage="doing", status="in_progress",
                          pr=pr(22, state="closed", merged=False))
        tn_review = ticket(pn, "Review one", stage="review", status="in_progress",
                           pr=pr(21, state="open", head="bbbbbbb2222"))
        ticket(pn, "Changes one", stage="changes", status="in_progress")
        ticket(pn, "Status only", status="in_progress")
        ticket(pn, "Changes two", stage="changes", status="in_progress")
        ticket(pn, "Blocked two", stage="blocked", status="in_progress")
        store.add_review(pn, tn_review, None, "aaaaaaa1111", "pass", "ran the tests", None, STATIONS[3],
                         [], {}, factory_mod.finding_kind, now_utc())

        # project-s: merged by stage while the status says in progress; an empty milestone
        ps = project("project-s")
        store.create_doc(ps, "start", "Start", "Build it.")
        ms_a = store.create_milestone(ps, "M1: base", "The base is in.", [])
        ms_b = store.create_milestone(ps, "M2: extras", "", [])
        ticket(ps, "Merged by stage", milestone=ms_a, stage="merged", status="in_progress", holder=STATIONS[0])
        ticket(ps, "Merged by status", milestone=ms_a, status="done")
        ticket(ps, "Dropped idea", milestone=ms_a, status="canceled")

        # project-t: a hand-over path that ends in a slash, named in a ticket, a goal, a check
        # and a waiting item; documents made in the opposite order of the page's
        pt = project("project-t")
        PATH_T = "/Users/lee/code/docs-site/"
        store.set_handover(pt, [{"path": PATH_T, "name": REPO}], None, None, now_utc())
        ticket(pt, f"Fix links in {PATH_T}src and {PATH_T[:-1]}", status="in_progress")
        store.create_milestone(pt, "M1: links", f"Links work under {PATH_T[:-1]} and {PATH_T}src.",
                               [{"check": f"open {PATH_T}index.html", "expect": f"it is the one in {PATH_T[:-1]}"}])
        for kind, title in (("working", "Working notes: T1"), ("note", "A note"), ("memory", "Memory"),
                            ("agents", "Rules"), ("plan", "Plan"), ("spec", "Spec"), ("start", "Start")):
            store.create_doc(pt, kind, title, "text", by="Lee")
        fh_kind = fh.KINDS[0]
        fh.WAITING_BUILDERS.append(lambda st, fx, now: [fh.home_item(
            fh_kind, label=f"Look at {PATH_T}", title=f"Check {PATH_T}src", detail=f"See {PATH_T[:-1]}/notes",
            meta=f"in {PATH_T}", since=now, now=now, project={"id": pt, "name": "project-t"},
            actions=[{"label": f"Open {PATH_T}", "href": f"/projects/{pt}", "primary": True}])])

        async with cx_() as cx:
            print("== the answer ==")
            r = await cx.get(f"/api/factory/projects/{px}")
            ck("the route answers", r.status_code == 200, r.text[:300])
            a = r.json()
            ck("the top level has exactly the fields of the page",
               set(a) == {"id", "name", "goal", "stage", "stage_label", "state", "state_label", "waiting",
                          "counts", "moving", "moving_total", "tickets_href", "next", "milestones",
                          "decisions", "calls", "docs", "terminal", "linked", "can_close", "now"}, sorted(a))
            ck("no ticket lists under the milestones and no no_milestone", "no_milestone" not in a
               and all("tickets" not in m for m in a["milestones"]), sorted(a))
            ck("identity and stage", a["id"] == px and a["name"] == "project-x"
               and a["goal"] == "A project for the test" and a["stage"] == "build"
               and a["stage_label"] == "Build", [a["id"], a["stage"], a["stage_label"]])
            ck("state in words", a["state"] in ("building", "waiting_on_you")
               and a["state_label"] in ("Building", "Waiting on you"), [a["state"], a["state_label"]])

            ms = {m["name"]: m for m in a["milestones"]}
            ck("two milestones in order with their goals, checks, numbers and done flag",
               [m["name"] for m in a["milestones"]] == ["M1: pages", "M2: search"]
               and [m["number"] for m in a["milestones"]] == ["M1", "M2"]
               and ms["M1: pages"]["goal"] == "The pages are live."
               and ms["M1: pages"]["checks"] == [{"n": 1, "check": "open the home page", "expect": "it loads",
                                                   "for": "crew", "result": None, "note": None, "by": None}]
               and ms["M1: pages"]["done"] is False and ms["M2: search"]["done"] is False
               and all(set(m) == {"id", "number", "name", "goal", "done", "counts", "checks", "accepted",
                                      "needs_person", "can_accept", "tickets_href"}
                       for m in a["milestones"]), ms)
            ck("a milestone counts its tickets and leaves the canceled one out",
               ms["M1: pages"]["counts"] == {"total": 3, "merged": 1}
               and ms["M2: search"]["counts"] == {"total": 3, "merged": 0}, [m["counts"] for m in a["milestones"]])
            ck("a milestone links to its tickets", ms["M1: pages"]["tickets_href"]
               == f"/tickets?project={px}&milestone={m1}" and ms["M2: search"]["tickets_href"]
               == f"/tickets?project={px}&milestone={m2}", [m["tickets_href"] for m in a["milestones"]])
            ck("the project links to its tickets", a["tickets_href"] == f"/tickets?project={px}", a["tickets_href"])
            ck("counts: every ticket by state, canceled in the total", a["counts"] == {
                "total": 9, "merged": 1, "to_do": 2, "in_progress": 1, "in_review": 1, "changes_asked": 1,
                "blocked": 2, "canceled": 1}, a["counts"])
            tk = {t["title"]: t for t in a["moving"]}
            ck("a ticket has the fields of the page", tk and all(
                set(t) == {"ref", "title", "state", "who", "pr", "review", "blocked_by", "href"}
                for t in tk.values()), [sorted(t) for t in tk.values()][:1])
            ck("moving: changes asked, in review, in progress, then blocked, in the project's order",
               [t["title"] for t in a["moving"]] == ["Add the sitemap", "Add the footer", "Add the search box",
                                                     "Add the results page", "Write the release notes"]
               and a["moving_total"] == 5, [t["title"] for t in a["moving"]])
            ck("moving leaves out merged, canceled and to do",
               not {"Add the header", "Drop the old theme", "Add the news page", "Tidy the readme"} & set(tk))
            want = {"Add the footer": "in review", "Add the sitemap": "changes asked",
                    "Add the search box": "in progress", "Add the results page": "blocked",
                    "Write the release notes": "blocked"}
            ck("every state is a plain word", {k: v["state"] for k, v in tk.items()} == want,
               {k: v["state"] for k, v in tk.items()})
            ck("refs and links", tk["Add the footer"]["ref"] == "T2"
               and tk["Add the footer"]["href"] == f"/projects/{px}/tickets/T2", tk["Add the footer"])
            ck("who is a role in words, with the milestone for a builder",
               tk["Add the footer"]["who"] == "Builder for M1" and tk["Add the sitemap"]["who"] == "Builder for M1"
               and tk["Add the search box"]["who"] == "Builder for M2"
               and tk["Write the release notes"]["who"] == "Lead", {k: v["who"] for k, v in tk.items()})
            ck("a pull request: number, link and its state in words",
               tk["Add the footer"]["pr"] == {"number": 12, "url": f"https://github.com/{REPO}/pull/12",
                                              "state_text": "open, CI green"}
               and tk["Add the sitemap"]["pr"]["state_text"] == "open, CI failing"
               and tk["Add the search box"]["pr"] is None, [tk[k]["pr"] for k in tk])
            ck("the last review round, and whether it still counts",
               tk["Add the footer"]["review"] == "round 1: passed"
               and tk["Add the sitemap"]["review"] == "round 2: changes asked"
               and tk["Add the search box"]["review"] is None, [tk[k]["review"] for k in tk])
            ck("a ticket that waits says for what", tk["Add the results page"]["blocked_by"] == ["T4"]
               and tk["Add the footer"]["blocked_by"] == [], tk["Add the results page"])
            ck("next is the ticket being built", a["next"] == {
                "ticket": "T2", "title": "Add the footer", "why": "being worked on now"}, a["next"])

            ck("decisions: newest first, all of them", [d["question"] for d in a["decisions"]]
               == ["Which licence?", "Use tabs or spaces?"] and a["decisions"][0]["when"] == "2 hours ago"
               and a["decisions"][1]["when"] == "3 days ago"
               and all(set(d) == {"question", "answer", "said", "when", "at"} and d["at"] for d in a["decisions"]),
               a["decisions"])
            ck("calls: the open assumptions only, with who made them and when", a["calls"] == [
                {"ref": "A1", "question": "Which date format?", "choice": "ISO dates",
                 "why": f"it sorts, see {REPO}/dates", "made": "by the builder, 2 days ago", "tickets": ["T2"]}],
               a["calls"])
            ck("docs: the project's own, in reading order, with a word for each kind",
               [(d["kind_label"], d["title"]) for d in a["docs"]]
               == [("Spec", "Spec"), ("Plan", "Plan"), ("Rules", "Rules"), ("Memory", "Memory"),
                   ("Notes", "A note"), ("Working notes", "Working notes: T2")]
               and all(d["href"] == f"/docs?doc={d['id']}" and d["updated"] for d in a["docs"]), a["docs"])
            ck("terminal: one command per live builder, no folder in it",
               sorted(t["command"] for t in a["terminal"]) ==
               [f"tares factory crew attach {STATIONS[0]}", f"tares factory crew attach {STATIONS[1]}"]
               and all(t["label"] == "Open the builder's session" for t in a["terminal"]), a["terminal"])
            ck("linked: tickets live in Tares", a["linked"] == {"tickets_in": None, "url": None}, a["linked"])
            ck("can_close is false while work is open", a["can_close"] is False)
            ck("now is a time", bool(re.match(r"^\d{4}-\d\d-\d\dT", a["now"])), a["now"])

            print("== waiting holds this project only ==")
            kinds = {w["kind"] for w in a["waiting"]}
            ck("project-x has its own item (open calls)", "calls_to_confirm" in kinds, kinds)
            ck("every item names project-x", a["waiting"]
               and all((w["project"] or {}).get("id") == px for w in a["waiting"]), a["waiting"])
            ry = (await cx.get(f"/api/factory/projects/{py}")).json()
            ck("project-y shows its own item and none of project-x's",
               ry["waiting"] and all((w["project"] or {}).get("id") == py for w in ry["waiting"])
               and not any(w["id"] == x["id"] for w in ry["waiting"] for x in a["waiting"]), ry["waiting"])
            ck("a waiting item has the shape of the home page's", all(
                set(w) == {"id", "kind", "label", "title", "detail", "meta", "project", "repo", "from", "since",
                           "waited", "actions", "tickets", "blocking", "older", "question"} for w in a["waiting"]), [sorted(w) for w in a["waiting"]][:1])

            print("== nothing of the crew's machine ==")
            text = json.dumps(a)
            ck("no folder of the crew's machine", "/Users/" not in text and PATH not in text, text[:200])
            hand = (await cx.get(f"/api/projects/{px}/crew")).json()
            ck("the hand-over path is on the crew route (so the check above means something)",
               PATH in json.dumps(hand), hand.get("handover"))
            ck("free text that names the folder shows the repo instead",
               a["decisions"][1]["said"] == f"Tabs, as in {REPO}/style.md", a["decisions"][1])
            ck("a decision reads as the answer chosen, the person's words under it when they add something (TR-1018)",
               (a["decisions"][1]["answer"], a["decisions"][0]["answer"], a["decisions"][0]["said"]) == ("tabs", "MIT", None),
               a["decisions"])
            bad = [(p, k, s) for p, k, s in strings(a)
                   if s[:1] in ("/", "~") and k not in ("url", "href", "tickets_href")]
            ck("no value starts with / or ~ except a link", bad == [], bad)
            shown = {k: v for k, v in a.items() if k != "terminal"}
            hits = [n for n in STATIONS + SESSIONS if n in json.dumps(shown)]
            ck("no station name and no session id, outside the command that opens a session", hits == [], hits)
            raw = [(p, s) for p, k, s in strings(a) if k not in ("state", "stage", "id", "kind", "do")
                   and re.search(r"\b(in_progress|ready_to_close|waiting_on_you|not_started|software_factory)\b", s)]
            ck("no raw stage or state key in a shown string", raw == [], raw)
            found = words.offenders_in(a, ("name", "goal", "title", "words", "question", "choice", "why", "command"))
            ck("the words list finds nothing", found == [], found)
            run = subprocess.run([sys.executable, "-m", "tares.words", "--skip",
                                  "name,goal,title,words,question,choice,why,command"],
                                 input=r.text, capture_output=True, text=True,
                                 env={**os.environ, "PYTHONPATH": ROOT})
            ck("python -m tares.words says 0 problems", run.stdout.strip() == "0 problems" and run.returncode == 0,
               run.stdout + run.stderr)
            ck("no em dash", "—" not in text)

            print("== next, in each case ==")
            n = (await cx.get(f"/api/factory/projects/{py}")).json()["next"]
            ck("the first ticket with nothing open before it is ready", n == {
                "ticket": "T1", "title": "Export the posts", "why": "ready, nothing it depends on is open"}, n)
            n = (await cx.get(f"/api/factory/projects/{pz}")).json()["next"]
            ck("when nothing is ready the first that waits says for what", n == {
                "ticket": "T2", "title": "Replace it", "why": "waits for T1 (canceled)"}, n)
            n = (await cx.get(f"/api/factory/projects/{pq}")).json()["next"]
            ck("a blocked ticket says so", n and n["why"] == "blocked", n)
            n = (await cx.get(f"/api/factory/projects/{pm}")).json()["next"]
            ck("a finished plan has no next ticket", n is None, n)
            e = (await cx.get(f"/api/factory/projects/{pw}"))
            ck("a project that is not a factory project is 404", e.status_code == 404, e.status_code)
            store.update_project(pw, status="draft")
            store.set_project_kind(pw, "software_factory")
            e = (await cx.get(f"/api/factory/projects/{pw}"))
            ck("a draft factory project is 404 too, it opens at its setup", e.status_code == 404, e.status_code)
            store.update_project(pw, status="active")
            ck("and the same project is answered once it is past its setup", (await cx.get(f"/api/factory/projects/{pw}")).status_code == 200)
            store.set_project_kind(pw, None)

            print("== can_close ==")
            ck("a fully merged project can close", (await cx.get(f"/api/factory/projects/{pm}")).json()["can_close"] is True)
            ck("a project with work open cannot", a["can_close"] is False and ry["can_close"] is False)
            ck("a project with every ticket canceled cannot",
               (await cx.get(f"/api/factory/projects/{pc}")).json()["can_close"] is False)
            m = (await cx.get(f"/api/factory/projects/{pm}")).json()
            ck("it is done, finished and not yet closed: state ready to close", m["stage"] == "done"
               and m["state"] == "ready_to_close" and m["state_label"] == "Ready to close", [m["stage"], m["state"]])
            r = await cx.post(f"/api/projects/{pm}/finish", json={"words": "done"})
            m = (await cx.get(f"/api/factory/projects/{pm}")).json()
            ck("once closed it is finished and can no longer close", r.status_code == 200 and m["can_close"] is False
               and m["state"] == "finished" and m["state_label"] == "Finished" and m["stage"] == "done",
               [r.status_code, m["state"], m["can_close"]])
            ck("a milestone with an open ticket is not done",
               (await cx.get(f"/api/factory/projects/{px}")).json()["milestones"][0]["done"] is False)

            print("== a title that names a folder, and the builders' sessions ==")
            b = (await cx.get(f"/api/factory/projects/{pp}")).json()
            ck("a ticket title shows the repo, not the folder",
               [t["title"] for t in b["moving"]] == ["Fix the build in example-org/project-p/src"]
               and PATH_P not in json.dumps(b), [t["title"] for t in b["moving"]])
            ck("the newest session of each builder station decides: working and quiet ones are offered, "
               "an ended one is not, an older working session does not bring it back, two sessions are one command",
               sorted(t["command"] for t in b["terminal"]) ==
               ["tares factory crew attach project-p-build-m1", "tares factory crew attach project-p-build-m3"],
               b["terminal"])
            ck("a reviewer is not offered", "crew-reviewer-p" not in json.dumps(b["terminal"]))
            by = (await cx.get(f"/api/factory/projects/{py}")).json()
            ck("another project's builder is that project's only",
               [t["command"] for t in by["terminal"]] == ["tares factory crew attach project-y-build-m1"]
               and "project-y-build-m1" not in json.dumps(b["terminal"]), [by["terminal"], b["terminal"]])

            print("== moving is capped, counts cover every ticket ==")
            n = (await cx.get(f"/api/factory/projects/{pn}")).json()
            ck("the first five in the page's order, and how many there are", n["moving_total"] == 7
               and [t["title"] for t in n["moving"]]
               == ["Changes one", "Changes two", "Review one", "Doing one", "Status only"],
               [n["moving_total"], [t["title"] for t in n["moving"]]])
            ck("counts hold all seven", n["counts"] == {
                "total": 7, "merged": 0, "to_do": 0, "in_progress": 2, "in_review": 1, "changes_asked": 2,
                "blocked": 2, "canceled": 0}, n["counts"])
            mv = {t["title"]: t for t in n["moving"]}
            ck("a status of in progress with no stage shows as in progress", mv["Status only"]["state"] == "in progress"
               and mv["Doing one"]["state"] == "in progress", {k: v["state"] for k, v in mv.items()})
            ck("a closed pull request says it closed without merging",
               mv["Doing one"]["pr"]["state_text"] == "closed without merging", mv["Doing one"]["pr"])
            ck("a review of an older commit says the pull request changed",
               mv["Review one"]["review"] == "round 1: passed, then the pull request changed", mv["Review one"])

            print("== merged by stage, and milestones ==")
            c = (await cx.get(f"/api/factory/projects/{ps}")).json()
            ck("a ticket merged by its stage counts as merged and the project is done", c["stage"] == "done"
               and c["counts"] == {"total": 3, "merged": 2, "to_do": 0, "in_progress": 0, "in_review": 0,
                                   "changes_asked": 0, "blocked": 0, "canceled": 1}
               and c["moving"] == [] and c["moving_total"] == 0, [c["stage"], c["counts"]])
            ck("a milestone is done when every ticket but the canceled ones is merged; an empty one is not",
               [(m["number"], m["done"], m["counts"]) for m in c["milestones"]]
               == [("M1", True, {"total": 2, "merged": 2}), ("M2", False, {"total": 0, "merged": 0})],
               c["milestones"])
            ck("tickets_href of the milestones", [m["tickets_href"] for m in c["milestones"]]
               == [f"/tickets?project={ps}&milestone={ms_a}", f"/tickets?project={ps}&milestone={ms_b}"])

            print("== the hand-over path with a trailing slash, in every text ==")
            t = (await cx.get(f"/api/factory/projects/{pt}")).json()
            ck("a ticket shows the repo whether the text ends the folder with a slash or not",
               [x["title"] for x in t["moving"]] == [f"Fix links in {REPO}/src and {REPO}"], t["moving"])
            ck("a milestone's goal and checks show the repo", t["milestones"][0]["goal"]
               == f"Links work under {REPO} and {REPO}/src." and t["milestones"][0]["checks"]
               == [{"n": 1, "check": f"open {REPO}/index.html", "expect": f"it is the one in {REPO}",
                    "for": "crew", "result": None, "note": None, "by": None}], t["milestones"])
            w = t["waiting"][0] if t["waiting"] else {}
            ck("a waiting item shows the repo in its words", (w.get("label"), w.get("title"), w.get("detail"), w.get("meta"))
               == (f"Look at {REPO}/", f"Check {REPO}/src", f"See {REPO}/notes", f"in {REPO}/"), w)
            ck("and keeps its links and what it does", w.get("actions") == [
                {"label": f"Open {REPO}/", "href": f"/projects/{pt}", "primary": True}] and w.get("project")
               == {"id": pt, "name": "project-t"}, w.get("actions"))
            ck("no folder anywhere in the answer", "/Users/" not in json.dumps(t), json.dumps(t)[:200])
            ck("documents come in reading order whatever order they were made in",
               [d["kind_label"] for d in t["docs"]]
               == ["Spec", "Plan", "Rules", "Memory", "Notes", "Working notes"], t["docs"])

            print("== not found and not allowed ==")
            e = await cx.get("/api/factory/projects/uc_nope")
            ck("an unknown project is 404", e.status_code == 404, e.status_code)
        async with cx_(None) as anon:
            e = await anon.get(f"/api/factory/projects/{px}")
            ck("without a token it is refused", e.status_code in (401, 403), e.status_code)

        print("== a tracker link ==")
        store.set_project_linear(py, {"id": "lp1", "name": "Docs", "url": "https://linear.app/example/project/docs"})
        async with cx_() as cx:
            l = (await cx.get(f"/api/factory/projects/{py}")).json()["linked"]
            ck("tickets in Linear", l == {"tickets_in": "Linear", "url": "https://linear.app/example/project/docs"}, l)
            store.set_project_linear(py, None)
            store.set_project_github(py, {"repo": REPO, "label": "tares", "url": f"https://github.com/{REPO}"})
            l = (await cx.get(f"/api/factory/projects/{py}")).json()["linked"]
            ck("tickets in GitHub", l == {"tickets_in": "GitHub", "url": f"https://github.com/{REPO}"}, l)
            store.set_project_github(py, None)

            print("== a platform cell still serves the page ==")
            r = await cx.put("/api/settings/edition", json={"edition": "platform"})
            ck("the edition switches to platform", r.status_code == 200, r.text)
            r = await cx.get(f"/api/factory/projects/{px}")
            ck("the route is served on a platform cell too", r.status_code == 200 and r.json()["id"] == px,
               r.status_code)
            await cx.put("/api/settings/edition", json={"edition": "factory"})


stages()
pull_requests()
holders()
print("== the code under test ==")
ck("the module comes from this checkout", os.path.abspath(tares.__file__).startswith(ROOT), tares.__file__)
asyncio.run(main())
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
