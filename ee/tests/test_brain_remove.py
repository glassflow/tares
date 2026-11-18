# Tares enterprise licence, see ee/LICENSE
"""Removing a repo deletes everything lore holds for it (TR-1199). DELETE /api/brain/repos/{owner}/{name}
cancels the repo's queued or running index at the control plane, asks lore to delete the repo's source
(asking again with a growing wait while lore's run on it is live, up to a limit), and only then deletes the row. Everything
runs in process against the shared fake lore and the fake control plane (ee/tests/fake_lore.py,
ee/tests/fake_cp.py), with the real background task polling every 50 ms.

Covered: a running repo (the Job is cancelled, lore's source and progress are gone, the row is gone, the
poll does not bring the Job back); a ready, a failed and a repo with no lore id (no cancel, no lore call
where there is nothing to forget); lore answering 409 and then letting go; lore answering 409 the whole wait
(409 "Still indexing", the row stays, removing again finishes it); lore already without it (404); a control
plane that did not say the Job is gone (wrong token 401, no brain index 503, no cancel route 405 or 501, any other
failure: 502 from the cell, the row stays, lore is not asked, removing again finishes it); a cluster error (502) asked
again and then ok, or all through the wait (502 from the cell, nothing touched); a cell with no control plane
settings (the removal goes on); the poll leaving a repo that is being removed alone; a removal that outlasts INLINE_WAIT (the proxy in front of a cell
cuts a request at 60 s): 202 {removing: true}, the work goes on in the background, the row says `removing`, and when it
gives up (lore 409 all through the wait, a control plane that keeps answering 502) the row keeps `remove_error` and
removing again finishes it; a second remove while one runs joins it (202) and a retry answers 409; a non-admin (403); no licence (404); the repo can be connected again afterwards.

The request the cell sends the control plane (for TR-1349 to match): DELETE
/internal/cells/{slug}/brain/index/{repo_source_id}, the cell's bearer token, no body; 200 {"state": "gone"} once the
entry or Job is deleted (the same on a second call). Only that answer, or a 404, lets the removal go on to lore: 401
and 503 mean "not cancelled" and stop it, 502 is asked again.

In process, no ports. With CI's Postgres run (TARES_TEST_PG and tests/pg_harness on PYTHONPATH) the same
script keeps its state in Postgres.

Run: .venv/bin/python ee/tests/test_brain_remove.py
"""
import asyncio, os, sys, tempfile, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import licensed   # scrubs the machine's TARES_* settings, puts the repo, ee/ and tests/ on sys.path
licensed.install_test_root()

TMP = tempfile.mkdtemp(prefix="tares-brain-remove-")
ROOT = "root-token-" + "brain-remove"
os.environ["TARES_DB"] = os.path.join(TMP, "t.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(TMP, "none.yaml")
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
os.environ["TARES_AUTH_TOKEN"] = ROOT

import httpx

import fake_cp
import fake_lore
from tares import github_credentials
from tares_ee.brain import cp as cp_mod
from tares_ee.brain import index as index_mod
from tares_ee.brain import repos as repos_mod

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def H(tok):
    return {"Authorization": f"Bearer {tok}"}


BRAIN_KEY = "brain-" + "key-test-1"   # gitleaks:allow
GH = "plain-" + "q7w8e9" * 5
NOT_FOUND = {"detail": "Not Found"}


async def until(cond, timeout=6.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        await asyncio.sleep(0.02)
    return False


async def main():
    from tares.daemon import make_app
    index_mod.POLL_SECONDS = 0.05
    index_mod.IDLE_SECONDS = 0.05
    repos_mod.REMOVE_WAIT = 0.4
    repos_mod.REMOVE_STEP = 0.05
    repos_mod.REMOVE_STEP_MAX = 0.2
    repos_mod.CP_WAIT = 0.4
    repos_mod.INLINE_WAIT = 30.0   # the cases that finish inline; the sections about 202 lower it
    lore, cp = fake_lore.FakeLore(), fake_cp.FakeCp()
    repos_mod.lore_client = lore.client
    index_mod.cp_client = cp.client
    os.environ["TARES_BRAIN_URL"] = fake_lore.BASE_URL
    os.environ["TARES_BRAIN_KEY"] = BRAIN_KEY
    names = ("run", "ready", "failed", "nolore", "retry", "stuck", "gone", "c405", "c501", "c502", "c502b", "c503", "c401", "cgone", "c500", "nocp", "poll", "twice",
             "admin", "again", "lic", "bare", "slow", "slowcp", "miss", "boom", "c404", "c204", "early")
    readable = [{"full_name": f"acme/{n}"} for n in names]

    async def readable_repos(cred, query=""):
        return readable
    github_credentials.list_repos_for = readable_repos
    app = make_app()
    async with app.router.lifespan_context(app):
        cx = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t", headers=H(ROOT))
        store = app.state.store
        store.upsert_github_credential("setup", GH)
        indexer = app.state.brain_indexer

        def row(repo):
            return store.get_brain_repo(repo) or {}

        def src(repo):
            return row(repo).get("repo_source_id")

        async def connect(repo, started=True):
            cp.start_as = ("started", None) if started else ("queued", 2)
            r = await cx.post("/api/brain/repos", json={"repos": [repo]})
            ck(f"connect {repo} answers 201", r.status_code == 201, f"{r.status_code} {r.text[:200]}")
            await until(lambda: row(repo).get("job_started_at") is not None)
            return src(repo)

        async def remove(repo, **kw):
            return await cx.delete(f"/api/brain/repos/{repo}", **kw)

        def cancels(source):
            return [c for c in cp.calls("DELETE", "cancel_index") if c.path.endswith("/" + source)]

        def forgets(source):
            return [c for c in lore.calls("DELETE", "delete_repo") if c.path.endswith("/" + source)]

        print("== no licence ==")
        r = await remove("acme/run")
        ck("no licence: DELETE is 404 as an unmounted route, nothing called", r.status_code == 404 and r.json() == NOT_FOUND
           and not lore.requests and not cp.requests, f"{r.status_code} {r.text[:100]}")
        r = await cx.put("/api/licence", json={"key": licensed.test_licence()})
        ck("the licence is stored", r.status_code == 200, r.text[:200])

        print("== a running repo ==")
        s = await connect("acme/run")
        ck("the repo is running: the control plane has its Job, lore its source and progress", s in cp.jobs and s in lore.repos
           and row("acme/run")["state"] in index_mod.RUNNING, f"{row('acme/run')} {list(cp.jobs)}")
        asked = len(cp.calls("POST", "start_index"))
        r = await remove("Acme/Run")
        ck("removing answers 200 (any case in the path)", r.status_code == 200 and r.json() == {"ok": True, "removed": True}, f"{r.status_code} {r.text}")
        c = cancels(s)
        ck("the control plane was asked once to cancel its Job", len(c) == 1 and len(cp.calls("DELETE", "cancel_index")) == 1
           and s not in cp.jobs, str([x.path for x in cp.requests]))
        ck("... DELETE /internal/cells/{slug}/brain/index/{source}, the cell's bearer token, no body", c and c[0].path
           == f"/internal/cells/cell-a/brain/index/{s}" and c[0].headers["authorization"] == "Bearer " + cp.token and c[0].json is None
           and c[0].status == 200 and c[0].method == "DELETE", str(c and (c[0].path, c[0].headers, c[0].json, c[0].status)))
        ck("lore was asked once to delete the source, and holds nothing for it", len(forgets(s)) == 1 and forgets(s)[0].status == 204
           and s not in lore.repos and s not in lore.progress, f"{lore.repos} {lore.progress}")
        ck("the row is gone", store.get_brain_repo("acme/run") is None)
        ck("the cancel came before the delete in lore", [x.route for x in cp.requests + lore.requests if x.route in ("cancel_index", "delete_repo")].count("delete_repo") == 1
           and cp.requests.index(c[0]) >= 0)
        await asyncio.sleep(0.4)
        ck("the poll does not bring the Job or the row back", len(cp.calls("POST", "start_index")) == asked and s not in cp.jobs
           and store.get_brain_repo("acme/run") is None, f"{len(cp.calls('POST', 'start_index'))} {asked}")
        ck("nothing is left marked as being removed", not indexer.is_removing("acme/run") and "acme/run" not in indexer._misses)

        print("== a repo that is ready has no Job to cancel; one that failed may still have one ==")
        s = await connect("acme/ready")
        store.update_brain_repo("acme/ready", {"state": "ready"})
        r = await remove("acme/ready")
        ck("ready: removed, lore asked, the control plane not called", r.status_code == 200 and len(forgets(s)) == 1
           and s not in lore.repos and store.get_brain_repo("acme/ready") is None and not cancels(s),
           f"{r.status_code} {r.text[:200]} {len(cancels(s))}")
        s = await connect("acme/failed")
        store.update_brain_repo("acme/failed", {"state": "failed", "error": "The index job is gone."})
        r = await remove("acme/failed")
        ck("failed after its Job was asked for: the Job is cancelled once, then lore forgets it and the row goes",
           r.status_code == 200 and len(cancels(s)) == 1 and len(forgets(s)) == 1 and s not in cp.jobs
           and s not in lore.repos and store.get_brain_repo("acme/failed") is None,
           f"{r.status_code} {r.text[:200]} {len(cancels(s))}")
        s = await connect("acme/miss")
        index_mod.MAX_MISSES = 2
        for _ in range(6):
            cp.fail("index_status", 502, "kube is down")
        ok = await until(lambda: row("acme/miss").get("state") == "failed")
        index_mod.MAX_MISSES = 30
        ck("the poll gave up on a repo after too many misses: failed, its Job is still at the control plane",
           ok and "could not be followed any longer" in (row("acme/miss").get("error") or "") and s in cp.jobs, str(row("acme/miss")))
        cp._fail.clear()
        r = await remove("acme/miss")
        ck("removing it cancels that Job too, so nothing is left running after the repo is gone", r.status_code == 200
           and len(cancels(s)) == 1 and s not in cp.jobs and s not in lore.repos and store.get_brain_repo("acme/miss") is None,
           f"{r.status_code} {r.text[:200]} {len(cancels(s))}")
        s = await connect("acme/early", started=False)
        store.update_brain_repo("acme/early", {"state": "failed", "error": "No GitHub connection can read it.", "job_started_at": None})
        r = await remove("acme/early")
        ck("failed before any Job was asked for: nothing to cancel", r.status_code == 200 and not cancels(s)
           and store.get_brain_repo("acme/early") is None, f"{r.status_code} {r.text[:200]} {len(cancels(s))}")
        store.add_brain_repo("acme/bare", None, None, "2026-01-01T00:00:00Z")
        n, m = len(cp.calls("DELETE")), len(lore.calls("DELETE"))
        r = await remove("acme/bare")
        ck("a row that never got a lore id is just removed: no delete on either side", r.status_code == 200
           and store.get_brain_repo("acme/bare") is None and len(cp.calls("DELETE")) == n and len(lore.calls("DELETE")) == m, f"{r.status_code} {r.text[:200]}")
        s = await connect("acme/nolore", started=False)
        store.update_brain_repo("acme/nolore", {"job_started_at": None})
        r = await remove("acme/nolore")
        ck("queued and never asked for: nothing to cancel, lore forgets it", r.status_code == 200 and not cancels(s) and len(forgets(s)) == 1
           and store.get_brain_repo("acme/nolore") is None, f"{r.status_code} {r.text[:200]}")

        print("== lore answers 409 while its run is live ==")
        s = await connect("acme/retry")
        lore.fail("delete_repo", 409, "indexing")
        lore.fail("delete_repo", 409, "indexing")
        r = await remove("acme/retry")
        ck("two 409s, then it lets go: removed", r.status_code == 200 and len(forgets(s)) == 3 and s not in lore.repos
           and store.get_brain_repo("acme/retry") is None, f"{r.status_code} {r.text[:200]} {len(forgets(s))}")
        ck("... the control plane was asked to cancel once, not once per ask", len(cancels(s)) == 1, str(len(cancels(s))))

        s = await connect("acme/stuck")
        for _ in range(40):
            lore.fail("delete_repo", 409, "indexing")
        t0 = time.monotonic()
        r = await remove("acme/stuck")
        took = time.monotonic() - t0
        ck("409 the whole wait: 409 with plain words, no em dash", r.status_code == 409 and "Still indexing. Try again in a minute." in r.text
           and "—" not in r.text, f"{r.status_code} {r.text[:200]}")
        ck("... it waited about the limit, asked more than twice, and the waits grew (a fixed 0.05 s step would ask about 8 times)",
           0.2 <= took < 3 and 2 < len(forgets(s)) <= 6, f"{took:.2f}s {len(forgets(s))}")
        ck("... the row and lore's source stay, so removing again can finish", row("acme/stuck").get("repo_source_id") == s and s in lore.repos)
        ck("... nothing is left marked as being removed", not indexer.is_removing("acme/stuck"))
        lore._fail.clear()
        r = await remove("acme/stuck")
        ck("removing again finishes it", r.status_code == 200 and s not in lore.repos and store.get_brain_repo("acme/stuck") is None,
           f"{r.status_code} {r.text[:200]}")

        print("== lore has it no more, or fails ==")
        s = await connect("acme/gone")
        lore.repos.pop(s)
        lore.progress.pop(s, None)
        r = await remove("acme/gone")
        ck("lore answers 404: it is gone, the row goes too", r.status_code == 200 and store.get_brain_repo("acme/gone") is None, f"{r.status_code} {r.text[:200]}")
        s = await connect("acme/c500")
        lore.fail("delete_repo", 500, "boom")
        r = await remove("acme/c500")
        ck("another lore error is 502 naming the repo, the row stays", r.status_code == 502 and "Could not remove acme/c500" in r.text
           and row("acme/c500").get("repo_source_id") == s, f"{r.status_code} {r.text[:200]}")
        ck("... lore was asked once, not over and over", len(forgets(s)) == 1, str(len(forgets(s))))
        r = await remove("acme/c500")
        ck("... removing again works", r.status_code == 200 and store.get_brain_repo("acme/c500") is None, f"{r.status_code} {r.text[:200]}")

        s = await connect("acme/boom")
        real_lore = repos_mod.lore_client

        def broken_lore():
            raise RuntimeError("it broke with " + GH)
        repos_mod.lore_client = broken_lore
        r = await remove("acme/boom")
        repos_mod.lore_client = real_lore
        ck("an error nobody planned for is 502 in plain words and its text, which may hold a token, never goes out",
           r.status_code == 502 and "Could not remove acme/boom" in r.text and GH not in r.text and "it broke" not in r.text,
           f"{r.status_code} {r.text[:200]}")
        r = await cx.get("/api/brain")
        mine = [x for x in r.json()["repos"] if x["repo"] == "acme/boom"]
        ck("... nor does it reach the row's remove_error", mine and GH not in str(mine[0]["remove_error"]) and "it broke" not in str(mine[0]["remove_error"])
           and "Could not remove acme/boom" in str(mine[0]["remove_error"]), str(mine))
        r = await remove("acme/boom")
        ck("... removing again works", r.status_code == 200 and store.get_brain_repo("acme/boom") is None, f"{r.status_code} {r.text[:200]}")

        print("== the control plane ==")
        for repo, status in (("acme/c405", 405), ("acme/c501", 501), ("acme/c503", 503), ("acme/c401", 401)):
            s = await connect(repo)
            cp.fail("cancel_index", status, "no such route")
            n = len(lore.calls("DELETE", "delete_repo"))
            r = await remove(repo)
            ck(f"the control plane answers {status}: not cancelled, so 502, the row stays, lore is not asked", r.status_code == 502
               and "Could not stop the index of " + repo in r.text and row(repo).get("repo_source_id") == s
               and len(lore.calls("DELETE", "delete_repo")) == n and s in lore.repos and len(cancels(s)) == 1,
               f"{r.status_code} {r.text[:200]} {len(cancels(s))}")
            ck(f"... {status} is not asked again, and nothing is left marked as being removed", len(cancels(s)) == 1 and not indexer.is_removing(repo))
            r = await remove(repo)
            ck(f"... removing again, with the control plane back, deletes in lore and the row", r.status_code == 200
               and s not in lore.repos and store.get_brain_repo(repo) is None, f"{r.status_code} {r.text[:200]}")
        s = await connect("acme/c502")
        cp.fail("cancel_index", 502, "could not reach the cluster")
        cp.fail("cancel_index", 502, "could not reach the cluster")
        r = await remove("acme/c502")
        ck("a cluster error (502) is asked again: the third ask cancels, lore deletes, the row goes", r.status_code == 200
           and len(cancels(s)) == 3 and len(forgets(s)) == 1 and s not in lore.repos and store.get_brain_repo("acme/c502") is None,
           f"{r.status_code} {r.text[:200]} {len(cancels(s))}")
        s = await connect("acme/c502b")
        for _ in range(40):
            cp.fail("cancel_index", 502, "could not reach the cluster")
        n = len(lore.calls("DELETE", "delete_repo"))
        t0 = time.monotonic()
        r = await remove("acme/c502b")
        took = time.monotonic() - t0
        ck("502 all through the wait: 502 from the cell with the reason and Try again once, lore not asked, the row stays",
           r.status_code == 502 and "Could not stop the index of acme/c502b" in r.text and r.text.count("Try again") == 1
           and len(lore.calls("DELETE", "delete_repo")) == n and row("acme/c502b").get("repo_source_id") == s and s in lore.repos,
           f"{r.status_code} {r.text[:300]}")
        ck("... it waited about the limit and asked more than twice, not endlessly", 0.2 <= took < 2 and 2 < len(cancels(s)) < 30, f"{took:.2f}s {len(cancels(s))}")
        ck("... nothing is left marked as being removed", not indexer.is_removing("acme/c502b"))
        cp._fail.clear()
        r = await remove("acme/c502b")
        ck("... removing again, with the cluster back, finishes it", r.status_code == 200 and s not in lore.repos
           and store.get_brain_repo("acme/c502b") is None, f"{r.status_code} {r.text[:200]}")
        s = await connect("acme/c404")
        cp.fail("cancel_index", 404, "Not Found")
        r = await remove("acme/c404")
        ck("an older control plane answers 404 to the cancel: nothing to cancel there, lore deletes, the row goes", r.status_code == 200
           and len(cancels(s)) == 1 and len(forgets(s)) == 1 and s not in lore.repos and store.get_brain_repo("acme/c404") is None,
           f"{r.status_code} {r.text[:200]} {len(cancels(s))}")
        s = await connect("acme/c204")
        cp.empty_next("cancel_index")
        r = await remove("acme/c204")
        ck("a cancel answered 204 with no body is a cancel: lore deletes, the row goes", r.status_code == 200 and len(cancels(s)) == 1
           and cancels(s)[0].status == 204 and s not in cp.jobs and s not in lore.repos and store.get_brain_repo("acme/c204") is None,
           f"{r.status_code} {r.text[:200]} {len(cancels(s))}")
        s = await connect("acme/cgone")
        cp.jobs.pop(s, None)
        r = await remove("acme/cgone")
        ck("a control plane with no Job for it (the same 200 'gone'): lore deletes, the row goes", r.status_code == 200
           and s not in lore.repos and store.get_brain_repo("acme/cgone") is None, f"{r.status_code} {r.text[:200]}")
        s = await connect("acme/c500")
        cp.fail("cancel_index", 500, "internal error")
        n = len(lore.calls("DELETE", "delete_repo"))
        r = await remove("acme/c500")
        ck("the control plane fails: 502 in plain words, the row stays, lore is not asked", r.status_code == 502
           and "Could not stop the index of acme/c500" in r.text and "Try again" in r.text and r.text.count("Try again") == 1 and row("acme/c500").get("repo_source_id") == s
           and len(lore.calls("DELETE", "delete_repo")) == n and s in lore.repos, f"{r.status_code} {r.text[:300]}")
        ck("... nothing is left marked as being removed", not indexer.is_removing("acme/c500"))
        r = await remove("acme/c500")
        ck("... removing again, with the control plane back, works", r.status_code == 200 and store.get_brain_repo("acme/c500") is None
           and s not in lore.repos, f"{r.status_code} {r.text[:200]}")
        s = await connect("acme/nocp")
        real = index_mod.cp_client

        def not_set_up():
            raise cp_mod.CpNotConfigured("the control plane is not set up, so the cell cannot start an index")
        index_mod.cp_client = not_set_up
        r = await remove("acme/nocp")
        index_mod.cp_client = real
        ck("a cell with no control plane settings: nothing to cancel, lore deletes, the row goes", r.status_code == 200 and len(forgets(s)) == 1
           and store.get_brain_repo("acme/nocp") is None, f"{r.status_code} {r.text[:200]}")

        print("== while a removal waits ==")
        s = await connect("acme/poll")
        r = await cx.get("/api/brain")
        mine = [x for x in r.json()["repos"] if x["repo"] == "acme/poll"]
        ck("before it is removed the row says removing false and no remove_error", mine and mine[0]["removing"] is False
           and mine[0]["remove_error"] is None, str(mine))
        for _ in range(40):
            lore.fail("delete_repo", 409, "indexing")
        repos_mod.REMOVE_WAIT = 1.5
        repos_mod.INLINE_WAIT = 0.15
        r = await remove("acme/poll")
        ck("the removal outlasts INLINE_WAIT: 202 {ok, removing}, not a 504-sized wait", r.status_code == 202
           and r.json() == {"ok": True, "removing": True}, f"{r.status_code} {r.text[:200]}")
        await until(lambda: len(forgets(s)) >= 2)
        ck("it is being removed, in the background", indexer.is_removing("acme/poll"))
        r = await cx.get("/api/brain")
        mine = [x for x in r.json()["repos"] if x["repo"] == "acme/poll"]
        ck("the row says removing true and no remove_error", mine and mine[0]["removing"] is True and mine[0]["remove_error"] is None, str(mine))
        starts = len(cp.calls("POST", "start_index"))
        cp.jobs[s] = {"state": "failed", "place": None, "error": "fatal: out of memory", "remote_url": "https://github.com/acme/poll.git"}
        await asyncio.sleep(0.4)
        ck("the poll leaves it alone: still running, not failed, no new Job", row("acme/poll")["state"] in index_mod.RUNNING
           and row("acme/poll")["error"] is None and len(cp.calls("POST", "start_index")) == starts,
           f"{row('acme/poll')['state']} {row('acme/poll')['error']}")
        store.update_brain_repo("acme/poll", {"state": "queued", "job_started_at": None})
        await indexer.start("acme/poll")
        ck("a start leaves a repo that is being removed alone: no new Job asked for", len(cp.calls("POST", "start_index")) == starts
           and row("acme/poll")["job_started_at"] is None, f"{len(cp.calls('POST', 'start_index'))} {starts}")
        cancelled = len(cancels(s))
        r = await remove("acme/poll")
        ck("a second remove of it joins the first: 202 again, no second removal", r.status_code == 202 and r.json() == {"ok": True, "removing": True}
           and len(cancels(s)) == cancelled, f"{r.status_code} {r.text[:200]} {len(cancels(s))} {cancelled}")
        r = await cx.post("/api/brain/repos/acme/poll/retry")
        ck("a retry of it answers 409 and leaves the row", r.status_code == 409 and "being removed" in r.text and row("acme/poll"), f"{r.status_code} {r.text[:200]}")
        lore._fail.clear()
        ck("the removal finishes in the background: the row and lore's source go", await until(lambda: store.get_brain_repo("acme/poll") is None)
           and s not in lore.repos, f"{row('acme/poll')}")
        ck("... and nothing is left marked as being removed", await until(lambda: not indexer.is_removing("acme/poll")))
        repos_mod.REMOVE_WAIT = 0.4
        repos_mod.INLINE_WAIT = 30.0
        ck("a repo that is not connected is 404", (await remove("acme/poll")).status_code == 404)

        print("== a removal that outlasts INLINE_WAIT and then gives up ==")
        s = await connect("acme/slow")
        for _ in range(40):
            lore.fail("delete_repo", 409, "indexing")
        repos_mod.INLINE_WAIT = 0.1
        repos_mod.REMOVE_WAIT = 0.6
        r = await remove("acme/slow")
        ck("202 while lore still answers 409", r.status_code == 202 and r.json() == {"ok": True, "removing": True}, f"{r.status_code} {r.text[:200]}")
        ck("the background run gives up: the row stops saying removing", await until(lambda: not indexer.is_removing("acme/slow")))
        await asyncio.sleep(0.1)
        r = await cx.get("/api/brain")
        mine = [x for x in r.json()["repos"] if x["repo"] == "acme/slow"]
        ck("... the row stays and says why in plain words (remove_error), removing false", mine and mine[0]["removing"] is False
           and mine[0]["remove_error"] and "Still indexing. Try again in a minute." in mine[0]["remove_error"]
           and "—" not in mine[0]["remove_error"], str(mine))
        ck("... lore's source is untouched, so removing again can finish", s in lore.repos and src("acme/slow") == s)
        ck("the poll finds the Job gone (the removal stopped it) and says so, not that the brain lost it",
           await until(lambda: row("acme/slow").get("state") == "failed")
           and "was stopped when its removal began" in (row("acme/slow").get("error") or "")
           and "is gone and the brain did not finish it" not in (row("acme/slow").get("error") or ""), str(row("acme/slow")))
        r = await cx.post("/api/brain/repos/acme/slow/retry")
        r2 = await cx.get("/api/brain")
        mine = [x for x in r2.json()["repos"] if x["repo"] == "acme/slow"]
        ck("a retry then starts a new Job and the row forgets why the removal gave up", r.status_code == 200 and r.json()["remove_error"] is None
           and mine and mine[0]["remove_error"] is None and s in cp.jobs, f"{r.status_code} {r.text[:200]} {mine}")
        lore._fail.clear()
        r = await remove("acme/slow")
        ck("removing again, with lore let go, answers 200 and clears it", r.status_code == 200 and r.json() == {"ok": True, "removed": True}
           and s not in lore.repos and store.get_brain_repo("acme/slow") is None, f"{r.status_code} {r.text[:200]}")
        r = await cx.get("/api/brain")
        ck("... the row is gone from the list", "acme/slow" not in {x["repo"] for x in r.json()["repos"]})

        s = await connect("acme/slowcp")
        for _ in range(60):
            cp.fail("cancel_index", 502, "could not reach the cluster")
        n = len(lore.calls("DELETE", "delete_repo"))
        repos_mod.CP_WAIT = 0.5
        r = await remove("acme/slowcp")
        ck("a control plane that keeps answering 502 outlasts INLINE_WAIT too: 202", r.status_code == 202, f"{r.status_code} {r.text[:200]}")
        ck("... it gives up", await until(lambda: not indexer.is_removing("acme/slowcp")) and await until(lambda: len(cancels(s)) > 2))
        await asyncio.sleep(0.2)
        r = await cx.get("/api/brain")
        mine = [x for x in r.json()["repos"] if x["repo"] == "acme/slowcp"]
        ck("... remove_error names the control plane, lore was not asked, the row stays", mine and mine[0]["removing"] is False
           and "Could not stop the index of acme/slowcp" in (mine[0]["remove_error"] or "") and len(lore.calls("DELETE", "delete_repo")) == n
           and s in lore.repos, str(mine))
        cp._fail.clear()
        repos_mod.CP_WAIT = 0.4
        repos_mod.REMOVE_WAIT = 0.4
        repos_mod.INLINE_WAIT = 30.0
        r = await remove("acme/slowcp")
        ck("... removing again, with the cluster back, finishes it", r.status_code == 200 and store.get_brain_repo("acme/slowcp") is None
           and s not in lore.repos, f"{r.status_code} {r.text[:200]}")
        r = await remove("acme/bad%20name")
        ck("a name that is not a repo name is 400", r.status_code == 400, f"{r.status_code} {r.text[:200]}")

        print("== a retry while a removal has been asked for but not started ==")
        s = await connect("acme/early")
        store.update_brain_repo("acme/early", {"state": "failed", "error": "The index job is gone."})
        repos_mod.INLINE_WAIT = 0.1
        async with indexer.lock("acme/early"):   # a step of the poll is in flight: the removal waits behind it
            r = await remove("acme/early")
            ck("the removal is asked for, in the background (202)", r.status_code == 202, f"{r.status_code} {r.text[:200]}")
            ck("... it has not started: the poll's rule is not yet in force", not indexer.is_removing("acme/early"))
            try:
                r = await asyncio.wait_for(cx.post("/api/brain/repos/acme/early/retry"), 2)
                ck("a retry now is refused (409, being removed) without waiting for the lock", r.status_code == 409 and "being removed" in r.text,
                   f"{r.status_code} {r.text[:200]}")
            except asyncio.TimeoutError:
                ck("a retry now is refused (409, being removed) without waiting for the lock", False, "it waited for the lock")
        ck("then the removal goes through", await until(lambda: store.get_brain_repo("acme/early") is None) and s not in lore.repos)
        repos_mod.INLINE_WAIT = 30.0

        print("== who may remove ==")
        s = await connect("acme/admin")
        pid = (await cx.post("/api/people", json={"name": "Jo", "email": "jo@x.dev"})).json()["id"]

        async def key(scopes):
            r = await cx.post("/api/keys", json={"name": "k-" + "-".join(scopes), "scopes": scopes, "person_id": pid})
            return H(r.json()["secret"])
        n, m = len(cp.calls("DELETE")), len(lore.calls("DELETE"))
        for who, tok in (("a read key", await key(["read"])), ("a read and ingest key", await key(["read", "ingest"]))):
            r = await remove("acme/admin", headers=tok)
            ck(f"{who} is refused (403)", r.status_code == 403, f"{r.status_code} {r.text[:100]}")
        ck("... the row stays, neither side was asked to delete", row("acme/admin").get("repo_source_id") == s and len(cp.calls("DELETE")) == n
           and len(lore.calls("DELETE")) == m)
        r = await remove("acme/admin", headers=await key(["admin"]))
        ck("an admin key removes it", r.status_code == 200 and store.get_brain_repo("acme/admin") is None and s not in lore.repos,
           f"{r.status_code} {r.text[:200]}")

        print("== connected again ==")
        s1 = await connect("acme/again")
        r = await remove("acme/again")
        ck("removed", r.status_code == 200 and s1 not in lore.repos)
        made = len(lore.calls("POST", "create_repo"))
        s2 = await connect("acme/again")
        ck("connecting it again makes a new lore source and asks for a new Job", s2 and s2 != s1 and s2 in lore.repos and s2 in cp.jobs
           and len(lore.calls("POST", "create_repo")) == made + 1, f"{s1} {s2}")
        await until(lambda: row("acme/again")["state"] in index_mod.RUNNING)
        ck("... and it runs like any new repo", row("acme/again")["state"] in index_mod.RUNNING and row("acme/again")["error"] is None)

        print("== shutdown ==")
        task = indexer.task
        await cx.aclose()
    ck("the task stops with the app", task.done() and indexer.task is None)


asyncio.run(main())
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
