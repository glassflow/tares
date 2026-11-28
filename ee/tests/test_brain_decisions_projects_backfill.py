# Tares enterprise licence, see ee/LICENSE
"""Docs of shared factory projects follow the repos: backfill when a repo is connected, and leave when a
project or a repo stops counting (TR-1220). The reconcile loop is TR-1211's; this is the part about
repos coming and going and projects being linked, unlinked and deleted.

In process against the shared fake lore and fake control plane (ee/tests/fake_lore.py, ee/tests/fake_cp.py),
through the real routes (POST and DELETE /api/brain/repos) and the real loop. The loop's interval is 30 s
for the route scenarios, so a doc that is in lore a moment after the request is there because the request
woke the loop, not because the interval ran out.

Covered: projects linked and written before any repo was connected go in on the first connect (shared
ones only: not a private one, not one linked to another repo, not a doc of another kind), a second repo
brings in the projects linked to it and sends nothing again for the others, a project unlinked from every
connected repo or deleted has its docs removed (one that stays linked to another connected repo keeps
them), a repo removed takes the docs of the projects linked only to it and keeps those of a project that
is also linked to another connected repo, the docs stay while the removal is still running (the row is
there) and go once it is done, the last repo removed empties the source, connecting the repo again puts the
docs back, entries of other sources and hand-added decisions are never touched, the loop alone (no
request) catches an unlink and a delete on its next interval, and no key in any request or log line.

In process, no ports. With CI's Postgres run (TARES_TEST_PG and tests/pg_harness on PYTHONPATH) the same
script keeps its state in Postgres.

Run: .venv/bin/python ee/tests/test_brain_decisions_projects_backfill.py
"""
import asyncio, logging, os, sys, tempfile, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import licensed   # scrubs the machine's TARES_* settings, puts the repo, ee/ and tests/ on sys.path
licensed.install_test_root()

TMP = tempfile.mkdtemp(prefix="tares-brain-backfill-")
ROOT = "root-token-" + "brain-backfill"
os.environ["TARES_HOME"] = os.path.join(TMP, "t")
os.environ["TARES_CATALOG"] = os.path.join(TMP, "none.yaml")
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
os.environ["TARES_AUTH_TOKEN"] = ROOT

import httpx

import fake_cp
import fake_lore
from tares import github_credentials
from tares.envelope import now_utc
from tares_ee.brain import decisions as dec_mod
from tares_ee.brain import index as index_mod
from tares_ee.brain import repos as repos_mod
from tares_ee.brain import sync as sync_mod

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def H(tok):
    return {"Authorization": f"Bearer {tok}"}


BRAIN_KEY = "brain-" + "key-test-backfill"   # gitleaks:allow
GH = "plain-" + "b4c5k6" * 5
SOURCE = "factory_projects"
NEAR = 5.0          # seconds a woken loop gets; the interval below is far longer


class Logs(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


async def until(cond, timeout=NEAR):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        await asyncio.sleep(0.02)
    return cond()


async def main():
    from tares.daemon import make_app
    logs = Logs()
    logging.getLogger().addHandler(logs)
    logging.getLogger().setLevel(logging.DEBUG)
    index_mod.POLL_SECONDS = 0.05
    index_mod.IDLE_SECONDS = 0.05
    repos_mod.REMOVE_WAIT = 0.4
    repos_mod.REMOVE_STEP = 0.05
    repos_mod.REMOVE_STEP_MAX = 0.2
    repos_mod.CP_WAIT = 0.4
    repos_mod.INLINE_WAIT = 30.0
    dec_mod.INTERVAL_SECONDS = 30.0
    lore, cp = fake_lore.FakeLore(), fake_cp.FakeCp()
    repos_mod.lore_client = lore.client
    index_mod.cp_client = cp.client
    os.environ["TARES_BRAIN_URL"] = fake_lore.BASE_URL
    os.environ["TARES_BRAIN_KEY"] = BRAIN_KEY
    os.environ["TARES_PUBLIC_URL"] = "https://cell.example.test"

    async def never(self):                # the syncer's own task is not under test here
        await asyncio.Event().wait()
    sync_mod.Syncer.run = never

    readable = [{"full_name": f"acme/{n}"} for n in ("web", "api", "docs")]

    async def readable_repos(cred, query=""):
        return readable
    github_credentials.list_repos_for = readable_repos

    app = make_app()
    async with app.router.lifespan_context(app):
        cx = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t", headers=H(ROOT))
        store = app.state.store
        store.upsert_github_credential("setup", GH)
        dec = app.state.brain_decisions
        r = await cx.put("/api/licence", json={"key": licensed.test_licence()})
        ck("the licence is stored", r.status_code == 200, r.text[:200])
        ck("the real loop is running", dec is not None and dec.task is not None and not dec.task.done())

        def project(pid, repos, scope="team"):
            store.create_project(pid, "Name of " + pid)
            store.set_handover(pid, [{"path": "/work/" + r.split("/")[-1], "name": r} for r in repos], None, "t", now_utc())
            if scope != "team":
                store.set_project_scope(pid, scope)

        def doc(pid, kind="spec", title=None):
            return store.create_doc(pid, kind, title or f"{kind} of {pid}", "body of " + pid, by="Ann Lee")

        def held():
            return {ext for (src, ext) in lore.documents if src == SOURCE}

        def of(pid):
            return {e for e in held() if e.startswith(f"project:{pid}:doc:")}

        def puts():
            return lore.calls("POST", "put_documents")

        def dels():
            return lore.calls("DELETE", "delete_document")

        async def connect(repo):
            cp.start_as = ("started", None)
            r = await cx.post("/api/brain/repos", json={"repos": [repo]})
            ck(f"connect {repo} answers 201", r.status_code == 201, f"{r.status_code} {r.text[:200]}")

        async def remove(repo):
            return await cx.delete(f"/api/brain/repos/{repo}")

        def n_puts():
            return sum(len(c.json["documents"]) for c in puts())

        # -- written before any repo was connected ------------------------------------------

        print("== projects linked and written before any repo is connected ==")
        project("pa", ["acme/web"]); ids_a = {doc("pa", "spec"), doc("pa", "plan"), doc("pa", "note")}
        doc("pa", "memory")                                  # a kind that never goes in
        project("pb", ["acme/web"], scope="private"); doc("pb")
        project("pc", ["acme/other"]); doc("pc")
        project("pd", ["acme/web"])                          # shared and linked, no docs yet
        await asyncio.sleep(0.3)                             # the loop's first pass has run
        ck("no repo connected: nothing in lore, nothing put", not held() and not puts(), str(sorted(held())))

        print("== the first connect puts them in at once, not at the next interval ==")
        t0 = time.monotonic()
        await connect("acme/web")
        ck("the spec, plan and note of the shared linked project are in lore", await until(lambda: held() == {f"project:pa:doc:{d}" for d in ids_a}), str(sorted(held())))
        ck("... well before the interval ran out", time.monotonic() - t0 < dec_mod.INTERVAL_SECONDS / 2, f"{time.monotonic() - t0:.1f}s")
        ck("a private project, one linked to another repo and a memory doc are not there", not of("pb") and not of("pc") and len(held()) == 3, str(sorted(held())))
        ck("one put, three documents, no delete", len(puts()) == 1 and n_puts() == 3 and not dels(), str([(c.method, c.route) for c in lore.requests]))

        print("== a doc written later is caught by the next pass, and nothing already there is sent again ==")
        late = doc("pd", "spec", "Late")
        n = n_puts()
        await connect("acme/api")                            # another repo: its connect wakes the loop too
        ck("the new spec of pd is in", await until(lambda: f"project:pd:doc:{late}" in held()), str(sorted(held())))
        ck("only that one document was sent", n_puts() == n + 1, f"{n_puts() - n} sent")

        print("== a second repo brings in the projects linked to it ==")
        project("pe", ["acme/api"]); e1 = doc("pe", "plan"); e2 = doc("pe", "note")
        project("pm", ["acme/web", "acme/api"]); m1 = doc("pm", "spec")
        dec.wake()                                           # (nothing in core announces a new project: the interval does; this stands in for it)
        ck("pe and pm are in", await until(lambda: {f"project:pe:doc:{e1}", f"project:pe:doc:{e2}", f"project:pm:doc:{m1}"} <= held()), str(sorted(held())))

        # -- projects that stop counting -----------------------------------------------------

        print("== a project unlinked from every connected repo has its docs removed ==")
        store.set_handover("pe", [{"path": "/x", "name": "acme/elsewhere"}], None, "t", now_utc())
        dec.wake()
        ck("pe's docs are gone", await until(lambda: not of("pe")), str(sorted(held())))
        ck("the others are untouched", of("pa") and of("pd") and of("pm"), str(sorted(held())))
        store.set_handover("pm", [{"path": "/x", "name": "acme/api"}], None, "t", now_utc())
        dec.wake()
        await asyncio.sleep(0.3)
        ck("a project that is still linked to a connected repo keeps its docs", len(of("pm")) == 1, str(sorted(held())))

        print("== a deleted project has its docs removed ==")
        store.delete_project("pd")
        dec.wake()
        ck("pd's docs are gone", await until(lambda: not of("pd")), str(sorted(held())))

        # -- repos removed -------------------------------------------------------------------

        print("== entries of other sources and hand-added decisions are never touched ==")
        meta = {"where": "repo_folder", "decided_by": ["A"], "decided_at": "2026-01-01"}
        lore.documents[("decisions", "hand:1")] = {"id": "h1", "kind": "decision", "title": "by hand", "uri": None, "text": "t", "metadata": dict(meta)}
        lore.documents[("repo_docs", "project:pa:doc:x")] = {"id": "h2", "kind": "decision", "title": "other source", "uri": None, "text": "t", "metadata": dict(meta)}

        print("== removing a repo: its docs stay while the removal runs, go when it is done ==")
        for _ in range(40):
            lore.fail("delete_repo", 409, "indexing")        # lore keeps saying its run is live
        r = await remove("acme/web")
        ck("the removal gives up (409) and the row stays", r.status_code == 409 and store.get_brain_repo("acme/web") is not None, f"{r.status_code} {r.text[:200]}")
        dec.wake()
        await asyncio.sleep(0.3)
        ck("the repo is still connected, so pa's docs are still in lore", len(of("pa")) == 3, str(sorted(held())))
        lore._fail.clear()
        r = await remove("acme/web")
        ck("removing again finishes it", r.status_code == 200 and store.get_brain_repo("acme/web") is None, f"{r.status_code} {r.text[:200]}")
        ck("pa, linked only to acme/web, has no docs left, at once", await until(lambda: not of("pa")), str(sorted(held())))
        ck("pm is linked to acme/api, still connected: it keeps its doc", len(of("pm")) == 1, str(sorted(held())))
        ck("a hand-added decision and another source's entry are still there", ("decisions", "hand:1") in lore.documents
           and ("repo_docs", "project:pa:doc:x") in lore.documents, str(sorted(lore.documents)))

        print("== the last repo removed empties the source ==")
        r = await remove("acme/api")
        ck("acme/api is removed", r.status_code == 200 and not store.list_brain_repos(), f"{r.status_code} {r.text[:200]}")
        ck("no project:* entry is left", await until(lambda: not held()), str(sorted(held())))
        ck("the hand-added decision is still there", ("decisions", "hand:1") in lore.documents)

        print("== connecting the repo again puts the docs back ==")
        await connect("acme/web")
        ck("pa's three docs are back", await until(lambda: len(of("pa")) == 3), str(sorted(held())))

        # -- the loop alone ------------------------------------------------------------------

        print("== with nobody asking, the interval does it ==")
        dec_mod.INTERVAL_SECONDS = 0.1
        dec.wake()                                           # cut the long wait; from here the loop ticks every 0.1 s
        project("pf", ["acme/web"]); f1 = doc("pf", "plan")
        ck("a new project's doc goes in on a later pass", await until(lambda: f"project:pf:doc:{f1}" in held()), str(sorted(held())))
        store.set_handover("pf", [{"path": "/x", "name": "acme/else"}], None, "t", now_utc())
        ck("unlinking it takes the doc out on a later pass", await until(lambda: not of("pf")), str(sorted(held())))
        store.delete_project("pa")
        ck("deleting pa takes its docs out on a later pass", await until(lambda: not of("pa")), str(sorted(held())))
        dec_mod.INTERVAL_SECONDS = 30.0

        print("== no secret anywhere ==")
        blob = repr([(c.path, c.query, c.json) for c in lore.requests]) + "\n".join(l for l in logs.lines if "brain" in l)
        ck("no brain key, GitHub token or cell token in any request or log line", not any(t in blob for t in (BRAIN_KEY, GH, ROOT)))

    print(f"\n{P} passed, {F} failed")
    sys.exit(1 if F else 0)


if __name__ == "__main__":
    asyncio.run(main())
