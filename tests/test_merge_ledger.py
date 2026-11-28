"""The merge ledger (TR-1672): what each merge through the merge queue went through, and the
Report's Merges section that lists it.

One in-process cell. GitHub is a stateful fake behind `factory_github._transport`; the review gate
and CI are fakes the projects choose. Covered:
 - a record on every path to merged: the queue's own merge (L3-review answered on the desk and in
   the console, L4-ship), a merge made outside Tares, a merge call that got no answer, one a stopped
   tick left behind, a ticket whose stage already says merged;
 - what a record says when something is not known, and that a record that cannot be written never
   holds a merge up or takes one back;
 - the report section: its words, the CSV and its formula guard, the weeks, the newest 100, who
   sees a private project's merges, and a deleted project.

Run: .venv/bin/python tests/test_merge_ledger.py
"""
import asyncio
import contextlib
import csv
import hashlib
import io
import json
import os
import sys
import tempfile
from datetime import datetime, time, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _env import scrub_os_environ
scrub_os_environ()

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_TMP = tempfile.mkdtemp(prefix="tares-merge-ledger-")
os.environ["TARES_HOME"] = os.path.join(_TMP, "api")
os.environ["TARES_CATALOG"] = os.path.join(_TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-merge-ledger"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
os.environ["TARES_EDITION"] = "factory"
os.environ["TARES_PR_SYNC_SECONDS"] = "off"      # the daemon's own loop stays out of the way

import httpx

from tares import factory as factory_mod
from tares import factory_github, merge_ledger, merge_queue, report
from tares.integrations import hooks, registry
from tares.integrations.ci import CISource
from tares.integrations.review import ReviewGate
from tares.integrations.types import CIState, Verdict
from tares.store import now_utc

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


# ── the fake GitHub ──────────────────────────────────────────────────────────────────────────────
PRS = {}                 # (repo, number) -> {head, branch, state, merged, merge_commit, merged_at}
MODE = {}                # repo -> how the merge call answers (default "ok")
UNREADABLE = set()       # repos whose merged pull requests can no longer be read
CI_NONE = set()          # repos whose CI source says "none"


def sha(n, v=0):
    return ("abc1234" + f"{n:03d}{v:03d}").ljust(40, "0")


def made(n):
    """The commit a merge of pull request `n` makes."""
    return ("fee0000" + f"{n:03d}").ljust(40, "0")


def gh_merge(repo, n):
    """The pull request is merged on GitHub."""
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    PRS[(repo, n)].update(merged=True, state="closed", merge_commit=made(n), merged_at=stamp)


def handler(req: httpx.Request):
    p, method = req.url.path.strip("/").split("/"), req.method
    hdr = {"x-ratelimit-remaining": "4000"}
    if len(p) < 4 or p[0] != "repos":
        return httpx.Response(404, json={}, headers=hdr)
    repo = f"{p[1]}/{p[2]}"
    if p[3] == "pulls" and method == "GET" and len(p) == 5:
        pr = PRS.get((repo, int(p[4])))
        if pr is None:
            return httpx.Response(404, json={}, headers=hdr)
        if repo in UNREADABLE and pr["merged"]:
            return httpx.Response(500, json={}, headers=hdr)
        return httpx.Response(200, headers=hdr, json={
            "number": int(p[4]), "title": "PR", "state": pr["state"], "merged": pr["merged"],
            "draft": False, "html_url": f"https://github.com/{repo}/pull/{p[4]}",
            "head": {"sha": pr["head"], "ref": pr["branch"], "repo": {"full_name": repo}},
            "base": {"ref": "main"}, "mergeable": True, "mergeable_state": "clean",
            "merge_commit_sha": pr.get("merge_commit"), "merged_at": pr.get("merged_at")})
    if p[3] == "pulls" and method == "PUT" and p[-1] == "merge":
        n = int(p[4])
        mode = MODE.get(repo, "ok")
        if mode == "no_answer":          # it merged and the answer never arrived
            gh_merge(repo, n)
            raise httpx.ConnectError("the connection dropped", request=req)
        if mode.isdigit():
            return httpx.Response(int(mode), json={"message": f"GitHub says {mode}"}, headers=hdr)
        if json.loads(req.content or b"{}").get("sha") != PRS[(repo, n)]["head"]:
            return httpx.Response(409, json={"message": "Head branch was modified"}, headers=hdr)
        gh_merge(repo, n)
        body = {"merged": True, "message": "Pull Request successfully merged"}
        if mode != "unnamed":            # "unnamed": the answer does not name the commit
            body["sha"] = made(n)
        return httpx.Response(200, json=body, headers=hdr)
    if p[3] == "compare":
        return httpx.Response(200, json={"behind_by": 0, "ahead_by": 1, "files": []}, headers=hdr)
    if p[3] == "commits":
        return httpx.Response(200, headers=hdr,
                              json={"statuses" if p[-1] == "status" else "check_runs": []})
    if p[3] == "git" and method == "DELETE":
        return httpx.Response(204, headers=hdr)
    return httpx.Response(404, json={}, headers=hdr)


factory_github._transport = httpx.MockTransport(handler)


class FakeReview(ReviewGate):
    name = "fakerev"
    title = "Fake review"

    def __init__(self, store=None):
        self.store = store

    async def verdict(self, pr):
        return Verdict(state="approved", head=pr.head, by="Fake", summary="Looks good")


class FakeCI(CISource):
    name = "fakeci"
    title = "Fake CI"

    def __init__(self, store=None):
        self.store = store

    async def state(self, pr, required):
        return CIState(state="none" if pr.repo in CI_NONE else "success", head=pr.head)


async def rails(stage, project, payload, ticket=""):
    return []


hooks.run = rails


@contextlib.contextmanager
def broken(obj, name, times=None):
    """`obj.name` raises, for its first `times` calls (every call when None)."""
    real = getattr(obj, name)
    calls = {"n": 0}

    def fail(*a, **k):
        calls["n"] += 1
        if times is None or calls["n"] <= times:
            raise RuntimeError("the store is away")
        return real(*a, **k)

    setattr(obj, name, fail)
    try:
        yield calls
    finally:
        setattr(obj, name, real)


@contextlib.contextmanager
def printed():
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        yield out


def utc(at):
    return at.astimezone(timezone.utc)


# ── the words of a row, and the CSV guard ────────────────────────────────────────────────────────
def words():
    print("== the words of a row, and the CSV ==")
    for given, want in (
            (("L4-ship", False, "none", None),
             ("L4: merges alone", "Reviewer's pass, No CI", "None needed")),
            (("L3-review", False, "success", {"name": "Priya"}),
             ("L3: a person approves", "Reviewer's pass, CI passed", "Priya")),
            (("L3-review", True, "failure", None),
             ("L3: a person approves", "Merged outside Tares, CI failing", "Approver not recorded")),
            (("L3-review", True, "pending", {"name": ""}),
             ("L3: a person approves", "Merged outside Tares, CI not finished",
              "Approver not recorded")),
            (("L4-ship", True, None, {"name": "Priya"}),
             ("L4: merges alone", "Merged outside Tares, CI not recorded", "None needed")),
            ((None, None, None, None),
             ("Level not recorded", "not recorded", "Approver not recorded")),
            ((None, None, "success", None),
             ("Level not recorded", "CI passed", "Approver not recorded"))):
        got = report._merge_words(*given)
        ck(f"words of level {given[0]}, outside {given[1]}, CI {given[2]}",
           (got["level_words"], got["gate_words"], got["approver_words"]) == want, got)
    for start in ("=", "+", "-", "@", "\t", "\r"):
        ck(f"a CSV text that starts with {start!r} gets an apostrophe",
           report._csv_cell(start + "1+1") == "'" + start + "1+1", report._csv_cell(start + "1+1"))
    kept = ["a=b", "Fix - it", "", 5, None]
    ck("any other cell is left as it is", [report._csv_cell(v) for v in kept] == kept,
       [report._csv_cell(v) for v in kept])


class Said:
    """The three reads `_approval` makes, answering what a case sets."""

    def __init__(self, desk=None, decisions=(), answer=None):
        self.desk, self.decisions, self.answer, self.asked = desk, list(decisions), answer, None

    def get_desk_item(self, item):
        return self.desk

    def list_decisions(self, uid, all_=False):
        return self.decisions

    def last_answer(self, items):
        self.asked = items
        return self.answer


def approvals():
    print("== which yes a merge went on ==")
    t0 = datetime(2026, 5, 4, 10, tzinfo=timezone.utc)
    row = {"project_uid": "uc_x", "ticket": "tid", "desk_item": "d1", "entered_at": t0}
    desk = {"id": "d1", "label": "Q-7"}

    def yes(minutes, who, choice="Yes, merge", item="d1"):
        return {"desk_item": item, "choice": choice, "words": choice, "person": {"id": who},
                "at": t0 + timedelta(minutes=minutes)}

    def answer(minutes, who, choice="Yes, merge"):
        return {"choice": choice, "said": choice, "at": t0 + timedelta(minutes=minutes),
                "person": {"id": who}}

    def of(s, r=row, kept=None):
        return merge_queue._approval(s, r, None, kept or {})

    got = of(Said(desk, [yes(9, "p1")]), kept={"approver_id": "p0", "approved_at": t0})
    ck("the yes noted on the record comes first", got == ("p0", t0), got)
    s = Said(desk, [yes(1, "p1")], answer(2, "p2"))
    got = of(s)
    ck("else the newest yes of the desk and the answers",
       got == ("p2", t0 + timedelta(minutes=2)), got)
    ck("asked under the names an answer can have",
       s.asked == ["prs_to_review:uc_x:tid", "question:Q-7", "permission:Q-7"], s.asked)
    got = of(Said(desk, [yes(3, "p1")], answer(2, "p2")))
    ck("the desk's when it is the newer", got == ("p1", t0 + timedelta(minutes=3)), got)
    got = of(Said(desk, [yes(1, "p1", item="other"), yes(2, "p3", "No, hold")]))
    ck("a no, or a yes to another question, is not one", got == (None, None), got)
    got = of(Said(desk, [], answer(2, "p2", "No, hold")))
    ck("an answer of no is not one", got == (None, None), got)
    got = of(Said(desk, [], answer(-1, "p2")))
    ck("an answer from before the row entered is not one", got == (None, None), got)
    got = of(Said(None, [], None), {**row, "desk_item": None})
    ck("nobody asked, nobody named", got == (None, None), got)


def heads():
    print("== the head the gate passed ==")
    old, new = sha(1, 1), sha(1, 2)
    plain = {"head": new, "before_files": None}
    updated = {"head": new, "before_files": json.dumps({"head": old, "files": []})}

    def ans(head=new, review="approved", ci="success"):
        return {"head": head, "review": {"state": review}, "ci": {"state": ci}}

    ck("the head the row entered with", merge_queue._passed_head(plain, {}, None) == new)
    ck("not a head the queue's update made, before the gate passed it",
       merge_queue._passed_head(updated, {}, None) is None)
    ck("that head once a record says the gate passed it",
       merge_queue._passed_head(updated, {"reviewed_sha": new}, None) == new)
    ck("or the gate's answer does", merge_queue._passed_head(updated, {}, ans()) == new)
    ck("with no CI as with green CI",
       merge_queue._passed_head(updated, {}, ans(ci="none")) == new)
    for what, a in (("another head", ans(head=old)), ("no review", ans(review="none")),
                    ("failing CI", ans(ci="failure")), ("running CI", ans(ci="pending"))):
        ck(f"not on an answer with {what}", merge_queue._passed_head(updated, {}, a) is None)


# ── the queue and the report, one cell ───────────────────────────────────────────────────────────
async def cell():
    from tares.daemon import make_app
    app = make_app()
    store = app.state.store
    registry.load()
    registry._loaded[("review", "fakerev")] = FakeReview
    registry._loaded[("ci", "fakeci")] = FakeCI
    store.upsert_github_credential("main", "tok-main")
    priya = store.create_person("Priya", email="priya@example.com")["id"]

    def hand(uid, autonomy, repos=None):
        store.set_handover(uid, repos or [{"path": "/w/a", "name": uid}], autonomy, None, now_utc(),
                           person_id=priya)

    def project(name, autonomy="L3-review", title=None, handed=True):
        uid = "uc_" + name
        store.create_project(uid, title or name, goal="A project for the test")
        store.set_project_kind(uid, "software_factory")
        if handed:
            hand(uid, autonomy)
        store.set_project_choice(uid, "review", "fakerev", {}, by="test")
        store.set_project_choice(uid, "ci", "fakeci", {}, by="test")
        return uid

    def ticket(uid, repo, n, title="Add the thing", stage="review"):
        tid = store.create_ticket(uid, "tares", title, repo=repo)
        store.update_ticket(tid, stage=stage, pr={"url": f"https://github.com/{repo}/pull/{n}",
                                                  "number": n, "repo": repo})
        PRS[(repo, n)] = {"head": sha(n), "branch": f"factory/T{n}", "state": "open",
                          "merged": False}
        return tid

    def reviewed(uid, tid, repo, n, head, verdict):
        store.add_review(uid, tid, f"https://github.com/{repo}/pull/{n}", head, verdict,
                         "Ran the tests", "", "crew-reviewer", [], {}, factory_mod.finding_kind,
                         now_utc())

    def row(repo, n):
        got = [r for r in store.merge_queue_list(repo, "main") if r["pr"] == n]
        return got[-1] if got else None

    def record(repo, n):
        r = row(repo, n)
        return merge_ledger.get(store, r["id"]) if r else None

    def stage(uid, tid):
        return store.get_ticket(uid, tid)["stage"]

    def question(uid):
        return [d for d in store.list_desk("open", uid) if d["asked_by"] == "merge-queue"][-1]

    def say(d, choice):
        store.add_decision(d["project"], d["id"], d["question"], choice, choice, False, "project",
                           True, None, False, d["tickets"], now_utc(), person_id=priya)
        store.close_desk_item(d["id"], "answered", None, now_utc())

    async def tick():
        await merge_queue.tick(store)

    def claimed(uid, repo, n, tid):
        """A row a tick has claimed."""
        r = store.merge_queue_add(repo, "main", n, sha(n), tid, uid)
        store.merge_queue_claim(r["id"], False)
        return row(repo, n)

    def drop(uid, repo, n, tid):
        """Take a scenario's open row and ticket out of later ticks."""
        store.update_ticket(tid, stage="doing")
        r = row(repo, n)
        if r and r["state"] != "merged":
            store.merge_queue_delete(r["id"])

    def seed(uid, repo, n, at=None, recorded=True, title="Seeded", **values):
        """A merged queue row, with its record unless `recorded` is False."""
        tid = ticket(uid, repo, n, title, stage="merged")
        store.update_ticket(tid, pr={"url": f"https://github.com/{repo}/pull/{n}", "number": n,
                                     "repo": repo, "merged": True})
        r = store.merge_queue_add(repo, "main", n, sha(n), tid, uid)
        store.merge_queue_update(r["id"], state="merged")
        if recorded:
            merge_ledger.save(store, r, **{
                "level": "L3-review", "reviewed_sha": sha(n), "merged_sha": made(n),
                "ci_state": "success", "rounds": 1, "outside": False, "approver_id": priya,
                "approved_at": now_utc(), "merged_at": at or now_utc(), **values})
        return tid, r

    async with app.router.lifespan_context(app):
        http = httpx.ASGITransport(app=app)

        async def report_as(token=TOKEN, scope="factory"):
            async with httpx.AsyncClient(transport=http, base_url="http://t") as cx:
                r = await cx.get("/api/factory/report", params={"scope": scope},
                                 headers={"Authorization": f"Bearer {token}"})
            assert r.status_code == 200, r.text
            return r.json()["sections"]["merges"]

        async def listed(repo, n):
            return [x for x in (await report_as())["rows"] if (x["repo"], x["pr"]) == (repo, n)]

        print("== nothing merged yet ==")
        empty = await report_as()
        ck("no merge, no row, and a CSV of its header alone",
           empty["total"] == 0 and empty["rows"] == [] and empty["levels"] == []
           and len(list(csv.reader(io.StringIO(empty["csv"])))) == 1, empty)
        ck("the ledger answers nothing for no project and no row",
           merge_ledger.merged_between(store, [], now_utc(), now_utc()) == []
           and merge_ledger.recorded(store, []) == set()
           and merge_ledger.get(store, "no-such-row") is None)
        ck("and the queue reads do too",
           store.merge_queue_merged([]) == [] and not store.merge_approvers([]))

        print("== L3-review, answered on the desk ==")
        desk = project("desk")
        t1 = ticket(desk, "o/desk", 1)
        reviewed(desk, t1, "o/desk", 1, sha(1, 1), "changes")
        reviewed(desk, t1, "o/desk", 1, sha(1), "pass")
        await tick()
        ck("while it waits for the yes the report lists nothing",
           row("o/desk", 1)["state"] == "waiting_approval" and await listed("o/desk", 1) == [])
        say(question(desk), "Yes, merge")
        await tick()
        rec = record("o/desk", 1)
        ck("it merged", row("o/desk", 1)["state"] == "merged" and stage(desk, t1) == "merged", rec)
        ck("the record has the level, the reviewed and merged commits, CI and the rounds",
           rec and (rec["level"], rec["reviewed_sha"], rec["merged_sha"], rec["ci_state"],
                    rec["rounds"], rec["outside"])
           == ("L3-review", sha(1), made(1), "success", 2, False), rec)
        ck("and who said yes, and when",
           rec and rec["approver_id"] == priya and rec["approved_at"] is not None
           and rec["approved_at"] <= rec["merged_at"] + timedelta(seconds=2), rec)
        ck("merged at the time GitHub gives",
           rec and utc(rec["merged_at"]).strftime("%Y-%m-%dT%H:%M:%SZ")
           == PRS[("o/desk", 1)]["merged_at"], rec)
        got = await listed("o/desk", 1)
        x = got[0] if got else {}
        ck("the report lists it once, with the approver and the commits",
           len(got) == 1 and (x["approver"] or {}).get("name") == "Priya"
           and x["approver_words"] == "Priya"
           and x["reviewed_sha"] == sha(1) and x["merged_sha"] == made(1)
           and x["level_words"] == "L3: a person approves"
           and x["gate_words"] == "Reviewer's pass, CI passed" and x["rounds"] == 2
           and x["approved_at"].endswith("+00:00"), got)
        ck("and the change it was",
           x.get("ticket") == factory_mod.label(store.get_ticket(desk, t1))
           and x.get("title") == "Add the thing" and x.get("name") == "desk"
           and x.get("url") == "https://github.com/o/desk/pull/1", x)
        before = record("o/desk", 1)
        merge_queue._record_merge(store, row("o/desk", 1), None,
                                  {"head": sha(1, 7), "merge_commit": made(99)})
        ck("a complete record stays as it is", record("o/desk", 1) == before,
           record("o/desk", 1))

        print("== L3-review, approved in the console ==")
        console = project("console")
        t2 = ticket(console, "o/console", 2)
        await tick()
        merge_queue.approve(store, row("o/console", 2), priya)
        rec = record("o/console", 2)
        ck("the yes is noted before the merge",
           rec and rec["approver_id"] == priya and rec["approved_at"] and rec["merged_at"] is None,
           rec)
        ck("and a record with no merge is not listed", await listed("o/console", 2) == [])
        await tick()
        rec = record("o/console", 2)
        ck("the merge keeps the approver",
           rec["merged_at"] is not None and rec["approver_id"] == priya
           and rec["level"] == "L3-review" and rec["rounds"] is None, rec)

        print("== the desk's yes, noted as it is read ==")
        settled = project("settled")
        ticket(settled, "o/settled", 45)
        await tick()
        say(question(settled), "Yes, merge")
        merge_queue._settle_question(store, {}, row("o/settled", 45))
        rec = record("o/settled", 45)
        ck("the desk's yes is noted before the merge",
           rec and rec["approver_id"] == priya and rec["approved_at"] and rec["merged_at"] is None,
           rec)
        store.merge_queue_update(row("o/settled", 45)["id"], desk_item=None)
        await tick()
        rec = record("o/settled", 45)
        ck("and stays though the row no longer holds the question at the merge",
           rec and rec["merged_at"] and rec["approver_id"] == priya, rec)

        print("== L4-ship ==")
        ship = project("ship", "L4-ship")
        CI_NONE.add("o/ship")
        t3 = ticket(ship, "o/ship", 3)
        await tick()
        rec = record("o/ship", 3)
        ck("L4-ship merges with no approver",
           rec and (rec["level"], rec["approver_id"], rec["approved_at"], rec["ci_state"],
                    rec["outside"], rec["rounds"])
           == ("L4-ship", None, None, "none", False, None) and stage(ship, t3) == "merged", rec)
        got = await listed("o/ship", 3)
        ck("the report says reviewer's pass and none needed",
           got and got[0]["approver"] is None and got[0]["approver_words"] == "None needed"
           and got[0]["gate_words"] == "Reviewer's pass, No CI"
           and got[0]["level_words"] == "L4: merges alone", got)
        line = [r for r in csv.reader(io.StringIO((await report_as())["csv"])) if "o/ship" in r]
        ck("the CSV has the whole commits and says which count is not recorded",
           line and line[0][8:] == [sha(3), made(3), "not recorded", "None needed"], line)

        hand(ship, "L3-review")
        sec = await report_as()
        got = [r for r in sec["rows"] if r["repo"] == "o/ship"]
        ck("a later change of level leaves the past row",
           got[0]["level"] == "L4-ship" and got[0]["level_words"] == "L4: merges alone", got)
        now_level = [v for v in sec["levels"] if v["project"] == ship]
        ck("and the project's level now says the new one",
           now_level == [{"project": ship, "name": "ship", "level": "L3-review",
                          "level_words": "L3: a person approves"}], sec["levels"])

        print("== approved at L3-review, merged at L4-ship ==")
        raised = project("raised")
        ticket(raised, "o/raised", 4)
        await tick()
        merge_queue.approve(store, row("o/raised", 4), priya)
        hand(raised, "L4-ship")
        await tick()
        rec = record("o/raised", 4)
        ck("it merged at L4-ship, which needs nobody",
           rec["merged_at"] and (rec["level"], rec["approver_id"], rec["approved_at"])
           == ("L4-ship", None, None), rec)

        print("== merged outside Tares ==")
        outside = project("outside")
        ticket(outside, "o/outside", 5)
        await tick()
        gh_merge("o/outside", 5)
        await tick()
        rec = record("o/outside", 5)
        ck("a merge made while the row waited is recorded as made outside",
           rec and (rec["level"], rec["outside"], rec["reviewed_sha"], rec["merged_sha"],
                    rec["approver_id"]) == ("L3-review", True, sha(5), made(5), None), rec)
        got = await listed("o/outside", 5)
        ck("the report says so",
           got and got[0]["gate_words"].startswith("Merged outside Tares, ")
           and got[0]["approver_words"] == "Approver not recorded", got)

        ticket(outside, "o/outside", 6)
        await tick()
        merge_queue.approve(store, row("o/outside", 6), priya)
        gh_merge("o/outside", 6)
        await tick()
        rec = record("o/outside", 6)
        ck("a yes to the commit that merged outside is kept",
           rec["merged_at"] and (rec["outside"], rec["approver_id"]) == (True, priya), rec)

        ticket(outside, "o/outside", 7)
        await tick()
        merge_queue.approve(store, row("o/outside", 7), priya)
        PRS[("o/outside", 7)]["head"] = sha(7, 2)
        gh_merge("o/outside", 7)
        await tick()
        rec = record("o/outside", 7)
        ck("a yes to an earlier commit does not count for the one that merged",
           rec["merged_at"] and (rec["approver_id"], rec["approved_at"]) == (None, None), rec)
        ck("and the reviewed commit is the one the gate passed, with no CI state of another",
           (rec["reviewed_sha"], rec["ci_state"], rec["outside"]) == (sha(7), None, True), rec)

        t8 = ticket(outside, "o/outside", 8)
        direct = claimed(outside, "o/outside", 8, t8)
        gh_merge("o/outside", 8)
        await merge_queue._merge(store, direct, store.get_ticket(outside, t8))
        rec = record("o/outside", 8)
        ck("a claimed row that finds its pull request merged is recorded",
           rec and rec["merged_at"] and rec["outside"] is True and rec["merged_sha"] == made(8), rec)

        print("== the merge call ==")
        calls = project("calls", "L4-ship")
        MODE["o/lost"] = "no_answer"
        ticket(calls, "o/lost", 9)
        await tick()
        rec = record("o/lost", 9) or {}
        ck("a call with no answer leaves the record waiting",
           row("o/lost", 9)["state"] == "queued" and rec.get("level") == "L4-ship"
           and rec.get("merged_at") is None and await listed("o/lost", 9) == [], rec)
        await tick()
        rec = record("o/lost", 9) or {}
        ck("and once the merge is seen it is the queue's own",
           rec.get("merged_at") and tuple(rec.get(k) for k in (
               "outside", "reviewed_sha", "merged_sha", "ci_state"))
           == (False, sha(9), made(9), "success"), rec)

        MODE["o/unnamed"] = "unnamed"
        ticket(calls, "o/unnamed", 10)
        answer = await factory_github.merge_pr(store, "o/unnamed", 10, "not-the-head")
        ck("a refused merge call names no commit", answer["ok"] is False and answer["sha"] == "",
           answer)
        await tick()
        ck("an answer that names no commit: the record takes it from the pull request",
           record("o/unnamed", 10)["merged_sha"] == made(10), record("o/unnamed", 10))

        UNREADABLE.add("o/dark")
        ticket(calls, "o/dark", 11)
        await tick()
        rec = record("o/dark", 11)
        ck("a pull request that cannot be read after the merge: the commit from the answer",
           rec and rec["merged_at"] and rec["merged_sha"] == made(11) and rec["outside"] is False,
           rec)

        for i, (mode, kept) in enumerate((("401", None), ("403", None), ("404", None),
                                          ("405", None), ("409", None), ("422", None),
                                          ("429", None), ("502", "L4-ship"))):
            repo, n = f"o/answer{mode}", 20 + i
            MODE[repo] = mode
            tid = ticket(calls, repo, n)
            r = claimed(calls, repo, n, tid)
            await merge_queue._merge(store, r, store.get_ticket(calls, tid))
            got = merge_ledger.get(store, r["id"])
            ck(f"after a {mode} the record "
               + ("no longer says a merge was asked for" if kept is None else "keeps what was asked"),
               got is not None and got["level"] == kept and got["merged_at"] is None, got)
            if mode == "405":
                gh_merge(repo, n)
                await tick()
                got = merge_ledger.get(store, r["id"])
                ck("a refused merge that someone then makes is made outside",
                   got["merged_at"] and got["outside"] is True, got)
            drop(calls, repo, n, tid)

        tid = ticket(calls, "o/claimlevel", 30)
        r = claimed(calls, "o/claimlevel", 30, tid)
        await merge_queue._merge(store, r, store.get_ticket(calls, tid), "L3-review")
        ck("the record has the level the row was claimed under",
           merge_ledger.get(store, r["id"])["level"] == "L3-review", merge_ledger.get(store, r["id"]))

        MODE["o/kept"] = "405"
        tid = ticket(calls, "o/kept", 36)
        r = claimed(calls, "o/kept", 36, tid)
        await merge_queue._merge(store, r, store.get_ticket(calls, tid))
        gh_merge("o/kept", 36)
        store.update_ticket(tid, stage="merged", pr={
            "url": "https://github.com/o/kept/pull/36", "number": 36, "repo": "o/kept",
            "merged": True, "head": sha(36), "merge_commit": made(36)})
        await tick()
        got = merge_ledger.get(store, r["id"])
        ck("a merge seen on the ticket alone keeps the CI state its record had",
           got["merged_at"] and (got["ci_state"], got["outside"], got["reviewed_sha"])
           == ("success", True, sha(36)), got)

        print("== a tick that stopped mid-merge ==")
        stopped = project("stopped")
        tid = ticket(stopped, "o/stopped", 31)
        r = claimed(stopped, "o/stopped", 31, tid)
        merge_queue._before_merge(store, r, "L4-ship", {"ci": {"state": "success"}})
        gh_merge("o/stopped", 31)
        await merge_queue._recover(store)
        rec = record("o/stopped", 31)
        ck("with the record of what it asked for, the merge is the queue's",
           rec["merged_at"] and (rec["level"], rec["outside"], rec["ci_state"], rec["merged_sha"])
           == ("L4-ship", False, "success", made(31)) and stage(stopped, tid) == "merged", rec)
        tid = ticket(stopped, "o/stopped", 32)
        claimed(stopped, "o/stopped", 32, tid)
        gh_merge("o/stopped", 32)
        await merge_queue._recover(store)
        rec = record("o/stopped", 32)
        ck("without one, who merged it is not known",
           rec and rec["merged_at"] and rec["outside"] is None and rec["approver_id"] is None, rec)
        got = await listed("o/stopped", 32)
        ck("and the report does not guess",
           got and got[0]["gate_words"] == "not recorded"
           and got[0]["approver_words"] == "Approver not recorded", got)

        print("== a ticket whose stage already says merged ==")
        staged = project("staged")
        tid = ticket(staged, "o/staged", 33)
        await tick()
        store.update_ticket(tid, stage="merged", pr={
            "url": "https://github.com/o/staged/pull/33", "number": 33, "repo": "o/staged",
            "merged": True, "head": sha(33), "merge_commit": made(33),
            "merged_at": "2026-01-02T03:04:05Z"})
        await tick()
        rec = record("o/staged", 33)
        ck("is recorded from what the ticket has",
           rec and (rec["level"], rec["reviewed_sha"], rec["merged_sha"], rec["outside"])
           == ("L3-review", sha(33), made(33), True)
           and utc(rec["merged_at"]) == datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc), rec)
        tid = ticket(staged, "o/staged", 34)
        await tick()
        store.update_ticket(tid, stage="merged")
        await tick()
        ck("a stage alone, with the pull request not merged, is no merge",
           row("o/staged", 34)["state"] == "merged" and record("o/staged", 34) is None
           and await listed("o/staged", 34) == [], record("o/staged", 34))

        print("== a failed read keeps nothing from before the merge ==")
        tid = ticket(staged, "o/stale", 35, stage="doing")
        r = store.merge_queue_add("o/stale", "main", 35, sha(35), tid, staged)
        merge_queue.approve(store, r, priya)
        merge_queue._record_merge(store, r, None, {"error": "could not read it", "head": sha(35)},
                                  {"head": sha(35, 5)})
        rec = merge_ledger.get(store, r["id"])
        ck("the head comes from this tick's answer, so the earlier yes is not counted",
           rec["merged_at"] and (rec["approver_id"], rec["merged_sha"]) == (None, None), rec)
        ck("with no time from GitHub the record takes now",
           abs((now_utc() - rec["merged_at"]).total_seconds()) < 60, rec)
        store.merge_queue_delete(r["id"])

        print("== a record that cannot be written ==")
        failing = project("failing", "L4-ship")
        tid = ticket(failing, "o/failing", 40)
        with printed() as out, broken(merge_ledger, "save") as tried:
            await tick()
        r = row("o/failing", 40)
        ck("the merge goes through all the same",
           PRS[("o/failing", 40)]["merged"] and r["state"] == "merged"
           and stage(failing, tid) == "merged" and tried["n"] >= 2, (r, out.getvalue()))
        ck("and the failure is printed",
           f"merge ledger of {r['id']} failed: RuntimeError: the store is away" in out.getvalue(),
           out.getvalue())
        sec = await report_as()
        got = [x for x in sec["rows"] if x["repo"] == "o/failing"]
        x = got[0] if got else {}
        ck("the report lists the merge from its queue row",
           len(got) == 1 and x["merged_at"] == utc(r["updated_at"]).isoformat()
           and all(x[k] is None for k in ("level", "reviewed_sha", "merged_sha", "rounds",
                                          "ci_state", "outside", "approver", "approved_at")), got)
        ck("saying what is not recorded",
           (x.get("level_words"), x.get("gate_words"), x.get("approver_words"))
           == ("Level not recorded", "not recorded", "Approver not recorded"), x)
        line = [c for c in csv.reader(io.StringIO(sec["csv"])) if "o/failing" in c]
        ck("in the CSV too",
           line and line[0][6:] == ["Level not recorded", "not recorded", "not recorded",
                                    "not recorded", "not recorded", "Approver not recorded"], line)
        ck("and counts it among the unrecorded of its week",
           sum(w["unrecorded"] for w in sec["weeks"]) >= 1, sec["weeks"])

        tid = ticket(failing, "o/unread", 41)
        with printed() as out, broken(merge_ledger, "get"):
            await tick()
        ck("a record that cannot be read holds nothing up either",
           row("o/unread", 41)["state"] == "merged" and stage(failing, tid) == "merged"
           and "merge ledger of" in out.getvalue(), out.getvalue())

        ticket(failing, "o/once", 42)
        with printed(), broken(merge_queue, "_facts", times=1):
            await tick()
        rec = record("o/once", 42)
        ck("when only the write ahead of the merge fails, the record is still complete",
           rec and rec["merged_at"] and (rec["level"], rec["reviewed_sha"], rec["merged_sha"],
                                         rec["outside"]) == ("L4-ship", sha(42), made(42), False),
           rec)

        MODE["o/norecord"] = "405"
        tid = ticket(failing, "o/norecord", 43)
        r = claimed(failing, "o/norecord", 43, tid)
        with printed() as out, broken(merge_ledger, "save"):
            try:
                await merge_queue._merge(store, r, store.get_ticket(failing, tid))
            except RuntimeError as e:       # the case below says so
                print(f"raised: {e}")
        ck("a refused merge is handled though the record cannot be written",
           row("o/norecord", 43)["state"] == "blocked"
           and out.getvalue().count("merge ledger of") == 2, out.getvalue())
        drop(failing, "o/norecord", 43, tid)

        noted = project("noted")
        ticket(noted, "o/noted", 44)
        await tick()
        with printed() as out, broken(merge_ledger, "save"):
            try:
                answered = merge_queue.approve(store, row("o/noted", 44), priya)
            except RuntimeError as e:
                answered = {"raised": str(e)}
        ck("an approval stands though it cannot be noted",
           answered.get("state") == "queued" and answered.get("approved_head") == sha(44)
           and "merge ledger of" in out.getvalue() and record("o/noted", 44) is None,
           (answered, out.getvalue()))
        await tick()
        ck("and the merge finds who gave it",
           record("o/noted", 44)["approver_id"] == priya, record("o/noted", 44))

        print("== the report's rows ==")
        quiet = project("Quiet", title="Zeta notes", handed=False)
        store.create_project("uc_plain", "Plain", goal="g")
        hand("uc_plain", "L3-review")      # a hand-over alone does not make it a factory project
        plus = project("plus", title="+Plus")
        hand(plus, "L3-review", [{"path": "/home/dev/work/shop", "name": "shop"}])
        sam = store.create_person("@Sam", email="sam@example.com")["id"]
        seed(plus, "o/plus", 50, title="=1+1", approver_id=sam)
        moved, _ = seed(plus, "o/plus", 51, title="Read /home/dev/work/shop/config.yml")
        store.update_ticket(moved, pr={"url": "https://github.com/o/plus/pull/151", "number": 151,
                                       "repo": "o/plus", "merged": True})
        gone = store.merge_queue_add("o/plus", "main", 52, sha(52), "no-such-ticket", plus)
        store.merge_queue_update(gone["id"], state="merged")
        merge_ledger.save(store, gone, level="L3-review", merged_at=now_utc())
        sec = await report_as()
        by_pr = {x["pr"]: x for x in sec["rows"] if x["repo"] == "o/plus"}
        line = {c[5]: c for c in csv.reader(io.StringIO(sec["csv"])) if c[4] == "o/plus"}
        ck("the CSV guards the project, the title and the approver against being read as formulas",
           line["50"][1] == "'+Plus" and line["50"][3] == "'=1+1" and line["50"][11] == "'@Sam",
           line.get("50"))
        ck("the page's rows keep the words as they are",
           by_pr[50]["title"] == "=1+1" and by_pr[50]["name"] == "+Plus", by_pr[50])
        ck("a folder path of the hand-over in a title is shortened, as on the project page",
           "/home/dev/work" not in by_pr[51]["title"] and "config.yml" in by_pr[51]["title"],
           by_pr[51])
        ck("no link when the ticket has moved to another pull request",
           by_pr[51]["url"] is None and by_pr[50]["url"] == "https://github.com/o/plus/pull/50",
           by_pr)
        ck("a merge whose ticket is not there has no ticket or title",
           (by_pr[52]["ticket"], by_pr[52]["title"], by_pr[52]["url"]) == (None, None, None)
           and line["52"][2:4] == ["not recorded", "not recorded"], (by_pr[52], line.get("52")))
        ck("newest first",
           [x["merged_at"] for x in sec["rows"]]
           == sorted((x["merged_at"] for x in sec["rows"]), reverse=True))
        names = [v["name"] for v in sec["levels"]]
        ck("the levels are of the handed-over factory projects, by name",
           names == sorted(names, key=str.lower) and "ship" in names and "Zeta notes" not in names
           and "Plain" not in names and quiet not in [v["project"] for v in sec["levels"]], names)

        def section(uid, weeks):
            return report.merges(store, {uid: store.get_project(uid)}, [uid], weeks)

        today = datetime.now(timezone.utc).date()
        monday = today - timedelta(days=today.weekday())

        def start(back):
            return datetime.combine(monday - timedelta(weeks=back), time(), timezone.utc)

        edges = project("edges")
        second = timedelta(seconds=1)
        for n, at in ((60, start(3) - second), (61, start(3)), (62, start(1) - second),
                      (63, start(1))):
            seed(edges, "o/edges", n, at=at)
        _, unseen = seed(edges, "o/edges", 64, recorded=False)
        merge_ledger.save(store, unseen, level="L3-review", approver_id=priya,
                          approved_at=now_utc())
        _, left = seed(edges, "o/edges", 65, recorded=False)
        store.merge_queue_update(left["id"], state="sent_back")
        got = {r["project_uid"] for r in store.merge_queue_merged([edges])}
        ck("the queue read gives the rows of the projects asked for, and no other",
           got == {edges}, got)
        sec = section(edges, [monday])
        ck("a queue row that did not end in a merge is not listed",
           65 not in [x["pr"] for x in sec["rows"]], sec["rows"])
        ck("a queue merge whose record never saw it is still listed, its level not recorded",
           [(x["pr"], x["level"]) for x in sec["rows"]] == [(64, None)], sec["rows"])
        sec = section(edges, [monday - timedelta(weeks=3), monday - timedelta(weeks=2)])
        ck("a range holds its first Monday to the end of its last week",
           [x["pr"] for x in sec["rows"]] == [62, 61] and sec["total"] == 2, sec["rows"])
        ck("each merge in the week it merged",
           [w["L3-review"] for w in sec["weeks"]] == [1, 1]
           and [w["unrecorded"] for w in sec["weeks"]] == [0, 0], sec["weeks"])

        many = project("many")
        for i in range(103):
            seed(many, "o/many", 100 + i, at=start(1) + timedelta(minutes=i),
                 level="L4-ship" if i < 3 else "L3-review")
        for i in range(2):
            seed(many, "o/many", 300 + i, recorded=False)
        sec = section(many, [monday - timedelta(weeks=1), monday])
        ck("the page lists the newest 100 and says how many there are",
           sec["total"] == 105 and len(sec["rows"]) == 100
           and [x["pr"] for x in sec["rows"][:3]] in ([301, 300, 202], [300, 301, 202])
           and sec["rows"][-1]["pr"] == 105, (sec["total"], [x["pr"] for x in sec["rows"]]))
        ck("the CSV holds every one", len(list(csv.reader(io.StringIO(sec["csv"])))) == 106)
        ck("and the weeks count every one, by level",
           [(w["L3-review"], w["L4-ship"], w["unrecorded"]) for w in sec["weeks"]]
           == [(100, 3, 0), (0, 0, 2)], sec["weeks"])

        print("== who sees a private project's merges ==")
        member = store.create_person("Mina", email="mina@example.com")["id"]
        other = store.create_person("Otto", email="otto@example.com")["id"]

        def key(kid, person):
            token = f"nvf_{kid}_{kid}-secret-for-the-test"
            store.insert_api_key(kid, kid, f"nvf_{kid}", hashlib.sha256(token.encode()).hexdigest(),
                                 ["read"], project=None, person_id=person)
            return token

        mina, otto = key("kmina", member), key("kotto", other)
        before = await report_as(otto)
        secret = project("closed", title="Closed plans")
        store.set_project_scope(secret, "private")
        store.add_project_member(secret, member)
        seed(secret, "o/closed", 70, title="Quarter plan")
        seed(secret, "o/closed", 71, recorded=False)
        theirs, mine = await report_as(otto), await report_as(mina)
        ck("a member sees them",
           sorted(x["pr"] for x in mine["rows"] if x["repo"] == "o/closed") == [70, 71]
           and mine["total"] == before["total"] + 2 and "Closed plans" in mine["csv"]
           and secret in [v["project"] for v in mine["levels"]], mine["total"])
        ck("someone else's totals, weeks, rows and levels are as if there were none",
           {k: theirs[k] for k in ("total", "weeks", "rows", "levels", "csv")}
           == {k: before[k] for k in ("total", "weeks", "rows", "levels", "csv")},
           (before["total"], theirs["total"]))
        ck("and nothing of the project is named",
           not any(w in json.dumps(theirs) for w in ("Closed plans", "o/closed", secret,
                                                      "Quarter plan")), "named")
        one = await report_as(mina, f"project:{secret}")
        ck("one project's scope holds its merges alone",
           one["total"] == 2 and [v["project"] for v in one["levels"]] == [secret], one["total"])

        print("== a deleted project ==")
        going, staying = row("o/closed", 70), row("o/desk", 1)
        store.delete_project(secret)
        ck("takes its records with it, and no other",
           merge_ledger.get(store, going["id"]) is None
           and merge_ledger.get(store, staying["id"]) is not None)


def main():
    words()
    approvals()
    heads()
    asyncio.run(cell())
    print(f"\n{P} passed, {F} failed")
    sys.exit(1 if F else 0)


if __name__ == "__main__":
    main()
