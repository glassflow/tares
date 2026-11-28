"""A ticket the merge queue sent back for failing CI returns to review when the same commit's CI
turns green (TR-1652), as when GitHub's failed job is rerun.

One in-process cell with the real integrations: the default Tares reviewer as the review gate and
the `github` CI integration, over a stateful fake GitHub behind `factory_github._transport`. A
rerun is modelled as GitHub does it: a new check run with the same name, a new id and a later
`started_at`, beside the old one. The guardrail hook is the only stand-in. Each scenario has its
own project and repo. `tick` here is what the daemon's sync loop does: read the pull requests
again, then run one pass of the queue.

Run: .venv/bin/python tests/test_merge_queue_ci_rerun.py
Postgres: TARES_TEST_PG=<dsn> [PYTHONPATH=tests/pg_harness:.] .venv/bin/python tests/test_merge_queue_ci_rerun.py
"""
import asyncio
import json
import os
import sys
import tempfile
import uuid
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _env import scrub_os_environ
scrub_os_environ()

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_TMP = tempfile.mkdtemp(prefix="tares-merge-queue-rerun-")
os.environ["TARES_HOME"] = os.path.join(_TMP, "api")
os.environ["TARES_CATALOG"] = os.path.join(_TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-merge-queue-rerun"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
os.environ["TARES_EDITION"] = "factory"
os.environ["TARES_PR_SYNC_SECONDS"] = "off"      # the daemon's own loop stays out of the way

PG = os.environ.get("TARES_TEST_PG")
if PG:      # without the harness too, the state goes to Postgres, in a schema of this run
    import psycopg
    _schema = "t_" + uuid.uuid4().hex[:16]
    with psycopg.connect(PG, autocommit=True) as _c:
        _c.execute(f"CREATE SCHEMA {_schema}")
    os.environ["TARES_STATE_DSN"] = (f"{PG}{'&' if '?' in PG else '?'}"
                                     f"options=-csearch_path%3D{_schema}")

import httpx

from tares import factory as factory_mod
from tares import factory_api, factory_github, merge_queue, merge_ready
from tares.integrations import hooks, registry, resolve
from tares.store import now_utc

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


E2E = "e2e (2/3)"
PRS = {}          # (repo, number) -> {head, branch, base, state, merged, draft, mergeable, ...}
RUNS = {}         # (repo, head) -> the commit's check runs, every attempt of every job
BEHIND = {}       # (repo, number) -> commits the base has that the head lacks
MERGES = []       # (repo, number, head)
MERGE_CODE = {}   # repo -> the status the merge call answers instead of merging
DOWN = set()      # (repo, number) whose pull request read answers 500
CALLS = []        # (method, path)
RAILS = {}        # repo -> the guardrail decisions before a merge
STATUS = []       # (ticket id, stage) of every status write
_ids = iter(range(1000, 100000))
_clock = iter(range(1, 100000))
_heads = iter(range(1, 100000))


def stamp(i=None):
    i = next(_clock) if i is None else i
    return f"2026-11-25T{i // 3600 % 24:02d}:{i // 60 % 60:02d}:{i % 60:02d}Z"


def new_head():
    return f"{next(_heads):07x}".rjust(7, "a").ljust(40, "0")


def attempt(repo, head, name=E2E, conclusion="success"):
    """One attempt of the job `name` on a commit; conclusion None is still running."""
    r = {"id": next(_ids), "name": name, "status": "completed" if conclusion else "in_progress",
         "conclusion": conclusion, "started_at": stamp(),
         "completed_at": stamp() if conclusion else None,
         "app": {"id": 15368, "slug": "github-actions"}, "html_url": ""}
    RUNS.setdefault((repo, head), []).append(r)
    return r


def finish(run, conclusion):
    run.update(status="completed", conclusion=conclusion, completed_at=stamp())


def handler(req: httpx.Request):
    path, method = req.url.path, req.method
    CALLS.append((method, path))
    p = path.strip("/").split("/")
    hdr = {"x-ratelimit-remaining": "4000"}

    def answer(code, body):
        return httpx.Response(code, json=body, headers=hdr)

    if len(p) < 4 or p[0] != "repos":
        return answer(404, {})
    repo = f"{p[1]}/{p[2]}"
    if p[3] == "pulls":
        n = int(p[4])
        pr = PRS.get((repo, n))
        if pr is None or ((repo, n) in DOWN and len(p) == 5):
            return answer(500 if pr else 404, {})
        if method == "GET" and len(p) == 5:
            return answer(200, {
                "number": n, "title": "PR", "state": pr["state"], "merged": pr["merged"],
                "draft": False, "html_url": f"https://github.com/{repo}/pull/{n}",
                "head": {"sha": pr["head"], "ref": f"factory/T{n}", "repo": {"full_name": repo}},
                "base": {"ref": "main"}, "mergeable": pr["mergeable"],
                "mergeable_state": "clean" if pr["mergeable"] else "dirty"})
        if method == "GET" and p[-1] == "files":
            return answer(200, [])
        if method == "PUT" and p[-1] == "update-branch":
            pr["head"] = new_head()
            BEHIND[(repo, n)] = 0
            return answer(202, {"message": "Updating pull request branch."})
        if method == "PUT" and p[-1] == "merge":
            if repo in MERGE_CODE:
                return answer(MERGE_CODE[repo], {"message": "GitHub says no"})
            MERGES.append((repo, n, json.loads(req.content or b"{}").get("sha")))
            pr.update(merged=True, state="closed")
            return answer(200, {"merged": True, "message": "Pull Request successfully merged"})
    if p[3] == "compare":
        head = p[4].split("...")[-1]
        n = next((k[1] for k, v in PRS.items() if k[0] == repo and v["head"] == head), None)
        return answer(200, {"behind_by": BEHIND.get((repo, n), 0), "ahead_by": 1, "files": [
            {"filename": "a.py", "status": "modified", "patch": "@@ -1 +1 @@\n-old\n+new"}]})
    if p[3] == "commits" and p[-1] == "status":
        return answer(200, {"statuses": [], "total_count": 0})
    if p[3] == "commits" and p[-1] == "check-runs":
        runs = RUNS.get((repo, p[4]), [])
        return answer(200, {"check_runs": runs, "total_count": len(runs)})
    if p[3] == "contents":
        return answer(200, [{"type": "file", "name": "ci.yml"}])
    if p[3] == "git" and method == "DELETE":
        return httpx.Response(204, headers=hdr)
    return answer(404, {})


factory_github._transport = httpx.MockTransport(handler)


async def rails(stage, project, payload, ticket=""):
    return RAILS.get(payload.get("repo"), [])


hooks.run = rails

_write_status = factory_api.write_status


async def write_status(store, uid, t, stage):
    STATUS.append((t["id"], stage))
    await _write_status(store, uid, t, stage)


factory_api.write_status = write_status


async def main():
    from tares.daemon import make_app
    app = make_app()
    store = app.state.store
    registry.load()
    store.upsert_github_credential("main", "tok-main")
    store.set_choice("ci", "github", {"no_ci": False}, "test")     # the factory's CI is GitHub
    person = store.create_person("Priya", email="priya@example.com")
    print(f"state: {store.sessions_db.dialect}")

    class Case:
        """A project with one ticket in review and its pull request, reviewed and with a green
        lint job; the e2e job is each scenario's to run."""

        def __init__(self, name, autonomy="L3-review", uid=None, n=1, e2e="failure"):
            self.repo = f"o/{name}"
            self.n = n
            self.uid = uid or "uc_" + name.replace("-", "")
            if uid is None:
                store.create_project(self.uid, name, goal="A project for the test")
                store.set_project_kind(self.uid, "software_factory")
                store.set_handover(self.uid, [{"path": "/w/a", "name": name}], autonomy, None,
                                   now_utc(), person_id=person["id"])
            self.tid = store.create_ticket(self.uid, "tares", f"Ticket {n}", repo=self.repo)
            self.url = f"https://github.com/{self.repo}/pull/{n}"
            store.update_ticket(self.tid, stage="review",
                                pr={"url": self.url, "number": n, "repo": self.repo})
            self.head = new_head()
            PRS[(self.repo, n)] = {"head": self.head, "state": "open", "merged": False,
                                   "mergeable": True}
            self.review("pass")
            attempt(self.repo, self.head, "lint")
            if e2e != "none":
                attempt(self.repo, self.head, E2E, e2e)

        # ── what happens ──
        def run(self, conclusion="success", head=None, name=E2E):
            return attempt(self.repo, head or self.head, name, conclusion)

        def push(self):
            self.head = new_head()
            PRS[(self.repo, self.n)]["head"] = self.head
            attempt(self.repo, self.head, "lint")
            return self.head

        def review(self, verdict, by="crew-reviewer"):
            finds = [] if verdict == "pass" else [{"severity": "P2", "file": "a.py", "line": 1,
                                                   "blocking": True, "text": "A batch is lost"}]
            store.add_review(self.uid, self.tid, self.url, PRS[(self.repo, self.n)]["head"],
                             verdict, "Read the delta", "", by, finds, {},
                             factory_mod.finding_kind, now_utc())

        def station_sets(self, stage, reason, by=None, role="builder"):
            """What the set_stage route writes."""
            store.update_ticket(self.tid, stage=stage, stage_reason=reason or None)
            store.add_ticket_history(self.tid, self.uid, "stage", stage, reason,
                                     by or "crew-build-m1", role, now_utc())

        async def sync(self):
            t = store.get_ticket(self.uid, self.tid)
            if t["pr"]:
                store.update_ticket(self.tid, pr={**t["pr"], "checked_at": None})
            await factory_api._refresh(store, self.uid)

        async def tick(self, sync=True):
            """The sync loop's pass; returns the GitHub calls the queue made for this repo."""
            if sync:
                await self.sync()
            before = len(CALLS)
            await merge_queue.tick(store)
            return [c for c in CALLS[before:] if c[1].startswith(f"/repos/{self.repo}/")]

        # ── what it looks like ──
        def t(self):
            return store.get_ticket(self.uid, self.tid)

        def stage(self):
            return self.t()["stage"]

        def reason(self):
            return self.t()["stage_reason"]

        def failing(self, head=None):
            return f"CI is failing on {(head or self.head)[:7]}: {E2E}"

        def hist(self, field):
            return [h for h in store.ticket_history(self.tid) if h["field"] == field]

        def returns(self):
            """The stage rows of the queue's own moves back to review, as their reasons."""
            return [h["reason"] for h in self.hist("stage")
                    if h["value"] == "review" and h["by"] == "merge-queue"]

        def back(self):
            return f"CI is green again on {self.head[:7]}"

        def rows(self):
            return [r for r in store.merge_queue_list(self.repo, "main") if r["pr"] == self.n]

        def row_states(self):
            return sorted((r["state"], "head" if r["head"] == self.head else "old")
                          for r in self.rows())

        async def ready(self):
            ans = await merge_ready.merge_ready(store, f"{self.repo}#{self.n}")
            return ans["ready"]

        async def shape(self):
            """(stage, merge_ready's `ready`, the queue's rows): they have to agree."""
            return self.stage(), await self.ready(), self.row_states()

        def seen(self):
            return (self.stage(), self.reason(), self.row_states(),
                    [(h["field"], h["value"], h["by"]) for h in store.ticket_history(self.tid)])

    async def sent(c):
        """One tick that sends the ticket back for the failed e2e job."""
        await c.tick()
        assert c.stage() == "changes" and c.reason() == c.failing(), c.seen()

    async with app.router.lifespan_context(app):
        print("== a. the failed job is rerun and turns green ==")
        a = Case("a")
        ck("a: the gates are the real ones", (resolve.effective("review", a.uid, store=store).integration,
           resolve.effective("ci", a.uid, store=store).integration) == ("tares", "github"))
        await a.tick()
        ck("a: sent back, the reason names the job", a.stage() == "changes"
           and a.reason() == a.failing(), a.seen())
        ck("a: changes, not ready, no row", await a.shape() == ("changes", False, []),
           await a.shape())
        rerun = a.run(None)
        await a.tick()
        ck("a: stays while the rerun is in progress", await a.shape() == ("changes", False, [])
           and a.reason() == a.failing(), a.seen())
        finish(rerun, "success")
        STATUS.clear()
        await a.tick()
        ck("a: back in review once the rerun is green", a.stage() == "review", a.seen())
        ck("a: the reason is cleared", a.reason() is None, a.reason())
        ck("a: the same tick puts it back in the queue", await a.shape()
           == ("review", True, [("waiting_approval", "head")]), await a.shape())
        last = a.hist("stage")[-1]
        ck("a: history says why, by the merge queue", (last["value"], last["by"], last["reason"])
           == ("review", "merge-queue", f"CI is green again on {a.head[:7]}"), last)
        ck("a: the status is written for review", (a.tid, "review") in STATUS, STATUS)
        ck("a: the bounce count still counts the one send-back", merge_queue.bounces(store, a.tid) == 1,
           merge_queue.bounces(store, a.tid))
        n = len(store.ticket_history(a.tid))
        await a.tick()
        ck("a: a further tick changes nothing", len(store.ticket_history(a.tid)) == n
           and a.stage() == "review", a.seen())

        print("== a. it was in the queue when CI failed ==")
        q = Case("a-row", e2e="success")
        await q.tick()
        first = q.rows()[0]
        merge_queue.approve(store, first)
        q.run("failure")
        await q.tick()
        ck("a-row: sent back, the row says sent_back", await q.shape()
           == ("changes", False, [("sent_back", "head")]), await q.shape())
        q.run("success")
        await q.tick()
        ck("a-row: review again, the sent_back row is gone and a new row asks",
           await q.shape() == ("review", True, [("waiting_approval", "head")])
           and q.rows()[0]["id"] != first["id"], (await q.shape(), q.rows()))

        o = Case("a-step", e2e="success")
        await o.tick()
        merge_queue.approve(store, o.rows()[0])
        o.run("failure")
        await o.tick()
        o.run("success")
        await o.sync()
        g = merge_queue._gather(store)
        held = [r for r in g["sent"] if r["ticket"] == o.tid]
        low, before = factory_github.rate_low, len(CALLS)
        factory_github.rate_low = lambda: {"core": 3}
        try:
            await merge_queue._back_to_review(store, g)
        finally:
            factory_github.rate_low = low
        ck("a-step: nothing is read while GitHub's rate limit is low", len(CALLS) == before
           and o.stage() == "changes", (o.seen(), CALLS[before:]))
        await merge_queue._back_to_review(store, g)
        ck("a-step: the pass's own view is updated: the row is gone and the ticket is in review",
           len(held) == 1 and not [r for r in g["sent"] if r["ticket"] == o.tid]
           and (g["by_id"][o.tid]["stage"], g["by_id"][o.tid]["stage_reason"]) == ("review", None),
           (held, g["sent"], g["by_id"][o.tid]))

        print("== a. at L4-ship the returned pull request merges ==")
        m = Case("a-ship", autonomy="L4-ship")
        await sent(m)
        m.run("success")
        await m.tick()
        ck("a-ship: merged", m.stage() == "merged" and (m.repo, 1, m.head) in MERGES
           and m.row_states() == [("merged", "head")], (m.seen(), MERGES))

        print("== b. the older attempt's failure arrives late ==")
        b = Case("b")
        old = RUNS[(b.repo, b.head)][-1]
        await sent(b)
        b.run("success")
        old["completed_at"] = stamp(80000)      # the failed attempt "completes" after the green one
        await b.tick()
        ck("b: the newer attempt decides, back in review", await b.shape()
           == ("review", True, [("waiting_approval", "head")]), await b.shape())
        old["completed_at"] = stamp(80500)
        await b.tick()
        ck("b: a late failed event does not send it back", await b.shape()
           == ("review", True, [("waiting_approval", "head")])
           and len(b.hist("send_back")) == 1, b.seen())

        print("== c. the reviewer asked for changes meanwhile ==")
        for verdict in ("changes", "block"):
            c = Case(f"c-{verdict}")
            await sent(c)
            c.review(verdict)
            c.run("success")
            await c.tick()
            await c.tick()
            ck(f"c: a reviewer's {verdict} keeps it in changes", await c.shape()
               == ("changes", False, []) and not c.returns(), c.seen())
        c.review("pass")
        await c.tick()
        ck("c: a later pass on the same commit lets it return", await c.shape()
           == ("review", True, [("waiting_approval", "head")]), await c.shape())

        print("== d. somebody else set changes ==")
        d = Case("d-station")
        await sent(d)
        d.station_sets("changes", "Rework the retry")
        d.run("success")
        await d.tick()
        ck("d: a station's changes after the send-back stays", d.stage() == "changes"
           and d.reason() == "Rework the retry" and d.row_states() == []
           and not d.returns(), d.seen())
        d = Case("d-same")
        await sent(d)
        d.station_sets("changes", d.failing())
        d.run("success")
        calls = await d.tick()
        ck("d: so does one with the very same reason", d.stage() == "changes"
           and not d.returns(), d.seen())
        ck("d: and GitHub is not asked about it", calls == [], calls)
        d = Case("d-none", e2e="success")
        d.station_sets("changes", "I want another look")
        await d.tick()
        calls = await d.tick()
        ck("d: a station's changes with no send-back at all stays", d.stage() == "changes"
           and d.row_states() == [] and calls == [], (d.seen(), calls))
        d = Case("d-reason")
        await sent(d)
        store.update_ticket(d.tid, stage_reason="Something else holds it now")
        d.run("success")
        await d.tick()
        ck("d: another reason on the ticket keeps it", d.stage() == "changes"
           and d.reason() == "Something else holds it now", d.seen())
        d = Case("d-newer")
        await sent(d)
        store.update_ticket(d.tid, stage_reason="Another cause")
        store.add_ticket_history(d.tid, d.uid, "stage", "changes", "Another cause", "merge-queue",
                                 "merge-queue", now_utc())
        d.run("success")
        await d.tick()
        ck("d: a later changes whose reason is not the send-back's keeps it", d.stage() == "changes",
           d.seen())
        for name, value, why_ in (("d-row-reason", "changes", "Another cause"),
                                  ("d-row-value", "review", None)):
            d = Case(name)
            await sent(d)
            store.add_ticket_history(d.tid, d.uid, "stage", value, why_ or d.failing(),
                                     "merge-queue", "merge-queue", now_utc())
            d.run("success")
            await d.tick()
            ck(f"d: the latest stage row has to be that move to changes ({name})",
               d.stage() == "changes" and d.back() not in d.returns(), d.seen())
        d = Case("d-meanwhile")
        await sent(d)
        d.run("success")
        real = merge_queue.merge_ready

        async def then_a_station(s, ref, *a_, **kw):
            ans = await real(s, ref, *a_, **kw)
            if ref == f"{d.repo}#1":
                d.station_sets("changes", "Set while GitHub was read")
            return ans

        merge_queue.merge_ready = then_a_station
        try:
            await d.tick()
        finally:
            merge_queue.merge_ready = real
        ck("d: a stage set while GitHub was read stays", d.stage() == "changes"
           and d.reason() == "Set while GitHub was read" and not d.returns(),
           d.seen())

        print("== e. a new commit: only its own checks decide ==")
        e = Case("e-reviewed")
        h1 = e.head
        await sent(e)
        h2 = e.push()
        e.review("pass")                  # reviewed before the sync saw it: the ticket stays put
        h2_run = e.run(None)
        e.run("success", head=h1)         # the old commit's rerun turns green
        calls = await e.tick()
        ck("e: H green, H2 pending: stays", await e.shape() == ("changes", False, [])
           and e.reason() == e.failing(h1), e.seen())
        ck("e: and GitHub is not asked about it", calls == [], calls)
        finish(h2_run, "failure")
        await e.tick()
        ck("e: H green, H2 red: stays", e.stage() == "changes" and e.row_states() == [], e.seen())
        e = Case("e-unread")
        h1 = e.head
        await sent(e)
        e.push()
        e.review("pass")
        e.run("success")                  # H2 is green and reviewed, the snapshot still says H
        e.run("success", head=h1)
        await e.tick(sync=False)
        ck("e: H2's result does not move a ticket sent back for H", e.stage() == "changes"
           and not e.returns(), e.seen())
        e = Case("e-sent")
        h1 = e.head
        await sent(e)
        e.push()
        h2_run = e.run(None)
        await e.tick()
        ck("e: new commits put it in review (as before)", e.stage() == "review", e.seen())
        e.review("pass")
        finish(h2_run, "failure")
        await e.tick()
        ck("e: H2 is sent back for itself", e.stage() == "changes" and e.reason() == e.failing(),
           e.seen())
        e.run("success", head=h1)
        await e.tick()
        ck("e: H turning green does not move it", e.stage() == "changes"
           and e.reason() == e.failing(), e.seen())
        again = e.run(None)
        await e.tick()
        ck("e: nor does H2's rerun while it runs", e.stage() == "changes", e.seen())
        finish(again, "success")
        await e.tick()
        ck("e: H2 green: review, the row is for H2", await e.shape()
           == ("review", True, [("waiting_approval", "head")])
           and e.returns() == [e.back()], (await e.shape(), e.returns()))

        print("== f. CI fails again on the same commit ==")
        f = Case("f")
        await sent(f)
        f.run("success")
        await f.tick()
        f.run("failure")
        merge_queue.approve(store, [r for r in f.rows() if r["state"] == "waiting_approval"][0])
        await f.tick()
        ck("f: sent back again", await f.shape() == ("changes", False, [("sent_back", "head")])
           and f.reason() == f.failing() and len(f.hist("send_back")) == 2, f.seen())
        ck("f: both send-backs are bounces", merge_queue.bounces(store, f.tid) == 2,
           merge_queue.bounces(store, f.tid))
        f.run("success")
        await f.tick()
        ck("f: and returns again when green", await f.shape()
           == ("review", True, [("waiting_approval", "head")])
           and f.returns() == [f.back(), f.back()], f.seen())
        s = Case("f-ship", autonomy="L4-ship")
        MERGE_CODE[s.repo] = 422          # the merge does not go through: the row stays queued
        await sent(s)
        s.run("success")
        await s.tick()
        s.run("failure")
        await s.tick()
        ck("f: at L4-ship too", await s.shape() == ("changes", False, [("sent_back", "head")])
           and len(s.hist("send_back")) == 2, s.seen())
        k = Case("f-manual")
        await sent(k)
        k.station_sets("review", "Look again", by="Priya", role="")
        await k.tick()
        await k.tick()
        ck("f: a stage set to review by hand keeps once per commit", k.stage() == "review"
           and len(k.hist("send_back")) == 1, k.seen())

        print("== g. other send-backs are not cleared ==")
        g = Case("g-rail", e2e="success")
        RAILS[g.repo] = [SimpleNamespace(allow=False, reason="touches payments",
                                         integration="policy", warn_only=False)]
        await g.tick()
        said = g.reason()
        RAILS.pop(g.repo)
        calls = await g.tick()
        ck("g: a guardrail send-back stays", g.stage() == "changes" and said.startswith("Blocked by")
           and g.reason() == said and calls == [], (g.seen(), calls))
        g = Case("g-conflict", e2e="success")
        PRS[(g.repo, 1)]["mergeable"] = False
        await g.tick()
        said = g.reason()
        PRS[(g.repo, 1)]["mergeable"] = True
        calls = await g.tick()
        ck("g: a conflict send-back stays", g.stage() == "changes" and said.startswith("Conflicts")
           and g.reason() == said and g.row_states() == [("sent_back", "head")] and calls == [],
           (g.seen(), calls))
        g = Case("g-then")
        await sent(g)
        g.run("success")
        RAILS[g.repo] = [SimpleNamespace(allow=False, reason="touches payments",
                                         integration="policy", warn_only=False)]
        await g.tick()
        ck("g: green CI with a guardrail now blocking: sent back for the guardrail",
           g.stage() == "changes" and g.reason().startswith("Blocked by policy")
           and g.row_states() == [], g.seen())
        RAILS.pop(g.repo)
        await g.tick()
        ck("g: which then stays", g.stage() == "changes"
           and g.reason().startswith("Blocked by policy"), g.seen())

        print("== h. the commit Tares made by updating the branch ==")
        h = Case("h", e2e="success")
        first_head = h.head
        BEHIND[(h.repo, 1)] = 2
        await h.tick()
        h.head = PRS[(h.repo, 1)]["head"]
        ck("h: Tares updates the branch and waits", h.head != first_head
           and [r["state"] for r in h.rows()] == ["waiting_ci"], h.rows())
        attempt(h.repo, h.head, "lint")
        h.run("failure")
        await h.tick()
        await h.tick()
        ck("h: the new commit fails and is sent back", await h.shape()
           == ("changes", False, [("sent_back", "head")]) and h.reason() == h.failing(), h.seen())
        h.run("success")
        await h.tick()
        ck("h: rerun green: review, queued for the new commit", await h.shape()
           == ("review", True, [("waiting_approval", "head")]), await h.shape())

        print("== j. nothing to go on ==")
        why = "CI is failing on abc1234: " + E2E

        def by_hand(c, head, pr="keep"):
            """The rows of a send-back, written onto a ticket as it is."""
            store.update_ticket(c.tid, stage="changes", stage_reason=why,
                                **({} if pr == "keep" else {"pr": pr}))
            store.add_ticket_history(c.tid, c.uid, "stage", "changes", why, "merge-queue",
                                     "merge-queue", now_utc())
            store.add_ticket_history(c.tid, c.uid, "send_back", f"ci_failure:{head}", why,
                                     "merge-queue", "merge-queue", now_utc())

        j = Case("j-nopr", e2e="success")
        by_hand(j, j.head, pr=None)
        calls = await j.tick()
        ck("j: a ticket with no pull request stays", j.stage() == "changes" and calls == [],
           (j.seen(), calls))
        j = Case("j-nohead", e2e="success")
        by_hand(j, j.head)                # the snapshot was never read: no head
        calls = await j.tick(sync=False)
        ck("j: a pull request with no head stays", j.stage() == "changes" and calls == [],
           (j.seen(), calls))
        j = Case("j-emptyhead", e2e="success")
        await j.sync()
        by_hand(j, "")
        calls = await j.tick(sync=False)
        ck("j: a send-back naming no commit stays", j.stage() == "changes" and calls == [],
           (j.seen(), calls))
        j = Case("j-nohistory", e2e="success")
        store.update_ticket(j.tid, stage="changes")
        await j.sync()
        calls = await j.tick(sync=False)
        ck("j: no history and no reason stays", j.stage() == "changes" and calls == [],
           (j.seen(), calls))

        async def green_but(name, spoil):
            """Sent back, the rerun green, then `spoil` before the queue looks (no new sync)."""
            c = Case(name)
            await sent(c)
            c.run("success")
            spoil(c)
            await c.tick(sync=False)
            return c

        j = await green_but("j-down", lambda c: DOWN.add((c.repo, 1)))
        ck("j: GitHub unreadable: unchanged", j.stage() == "changes" and j.reason() == j.failing()
           and not j.returns(), j.seen())
        DOWN.discard((j.repo, 1))
        await j.tick()
        ck("j: and it returns once GitHub answers", j.stage() == "review", j.seen())
        j = await green_but("j-closed", lambda c: PRS[(c.repo, 1)].update(state="closed"))
        ck("j: a closed pull request stays", j.stage() == "changes", j.seen())
        await j.sync()
        calls = await j.tick(sync=False)
        ck("j: known closed: GitHub is not asked again", j.stage() == "changes" and calls == [],
           (j.seen(), calls))
        j = await green_but("j-merged",
                            lambda c: PRS[(c.repo, 1)].update(state="closed", merged=True))
        ck("j: a merged one is not put in review", j.stage() == "changes"
           and not j.returns(), j.seen())

        one = Case("j-two")
        two = Case("j-two", uid=one.uid, n=2)
        await one.tick()
        one.run("success")
        two.run("success")

        async def breaks_on_one(s, ref, *a_, **kw):
            if ref == f"{one.repo}#1":
                raise RuntimeError("broken for this one")
            return await real(s, ref, *a_, **kw)

        merge_queue.merge_ready = breaks_on_one
        try:
            await one.tick()
        finally:
            merge_queue.merge_ready = real
        ck("j: one ticket's error leaves it and the others go on", one.stage() == "changes"
           and two.stage() == "review", (one.seen(), two.seen()))



asyncio.run(main())
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
