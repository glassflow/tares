"""The background pull request sync (TR-1010): factory_api.sync_prs.

A pull request merged or closed on GitHub reaches its ticket without anyone opening the project:
the daemon runs the same refresh a ticket read runs, every TARES_PR_SYNC_SECONDS, for the projects
with an open pull request. Checked here: which projects it reads (an open ticket with a pull request
neither merged nor closed), that a merge seen this way marks the ticket merged and takes it off the
Reviews page, that a closed one moves it under "Nobody is on it", that "off" turns the sync off,
and that one project's failure does not stop the others. GitHub is not called: read_pr is replaced.

Run: .venv/bin/python tests/test_factory_pr_sync.py
"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _env import scrub_os_environ
scrub_os_environ()

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_TMP = tempfile.mkdtemp(prefix="tares-factory-pr-sync-")
os.environ["TARES_DB"] = os.path.join(_TMP, "api.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(_TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-pr-sync"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
os.environ["TARES_EDITION"] = "factory"
os.environ["TARES_PR_SYNC_SECONDS"] = "off"      # the daemon's own loop stays out of the way

import httpx

from tares import factory_api, factory_github
from tares.store import now_utc

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    if cond:
        P += 1
        print(f"  ok   {label}")
    else:
        F += 1
        print(f"  FAIL {label}  {detail}")


REPO = "example-org/service-a"


async def main():
    from tares.daemon import make_app
    app = make_app()
    store = app.state.store

    def project(name, handed=True):
        uid = f"uc_{name.replace('-', '')}"
        store.create_project(uid, "custom", name, {}, goal="A project for the test")
        store.set_project_kind(uid, "software_factory")
        if handed:
            store.set_handover(uid, [{"path": "/w/a", "name": REPO}], None, None, now_utc())
        return uid

    def ticket(uid, title, n, **pr):
        tid = store.create_ticket(uid, "tares", title, repo=REPO)
        store.update_ticket(tid, stage="review", pr={"url": f"https://github.com/{REPO}/pull/{n}", "number": n,
                                                     "repo": REPO, "head": "aaaaaaa", "ci": "success", **pr})
        return tid

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                                     headers={"Authorization": f"Bearer {TOKEN}"}) as cx:
            print("== which projects it reads ==")
            pa, pb, pc, pd = project("project-a"), project("project-b"), project("project-c"), project("project-d")
            a_open = ticket(pa, "Open one", 1)
            b_open = ticket(pb, "Open two", 2)
            ticket(pc, "Merged already", 3, merged=True, state="closed")
            c_done = ticket(pc, "Done in its home", 4)
            store.update_ticket(c_done, status="done")
            ticket(pd, "Closed already", 5, state="closed", merged=False)
            got = factory_api.projects_with_open_prs(store)
            ck("an open ticket with an open pull request is read", pa in got and pb in got, got)
            ck("merged, closed, or a ticket done in its home: not read", pc not in got and pd not in got, got)

            print("== a merge and a close reach the tickets ==")
            seen = []
            real = factory_github.read_pr

            async def fake(store_, repo, number, prev=None):
                seen.append(number)
                if number == 2 and seen.count(2) == 1:
                    raise RuntimeError("GitHub did not answer")
                base = {**(prev or {}), "repo": repo, "number": number, "checked_at": now_utc().isoformat()}
                if number == 1:
                    return {**base, "state": "closed", "merged": True}
                return {**base, "state": "closed", "merged": False}

            factory_github.read_pr = fake
            os.environ["TARES_PR_SYNC_SECONDS"] = "0.05"
            stop = asyncio.Event()
            task = asyncio.create_task(factory_api.sync_prs(store, stop))
            for _ in range(100):
                await asyncio.sleep(0.05)
                if store.get_ticket(pa, a_open)["stage"] == "merged" and \
                        (store.get_ticket(pb, b_open)["pr"] or {}).get("state") == "closed":
                    break
            stop.set()
            await asyncio.wait_for(task, 2)
            factory_github.read_pr = real
            ck("a merge seen in the background marks the ticket merged", store.get_ticket(pa, a_open)["stage"] == "merged",
               store.get_ticket(pa, a_open)["stage"])
            ck("a read that failed once is tried again on a later pass", seen.count(2) >= 2, seen)
            ck("a close seen in the background is stored on the ticket",
               (store.get_ticket(pb, b_open)["pr"] or {}).get("state") == "closed")
            r = (await cx.get("/api/factory/reviews")).json()
            titles = lambda k: [x["ticket"]["title"] for x in r[k]]
            ck("the merged one is on no list", "Open one" not in titles("waiting_for_you") + titles("with_the_crew")
               + titles("nobody_on_it"), r)
            ck("the closed one is nobody's, with the reason",
               {x["ticket"]["title"]: x["why"] for x in r["nobody_on_it"]}.get("Open two")
               == "The pull request was closed without merging", r["nobody_on_it"])

            print("== off ==")
            os.environ["TARES_PR_SYNC_SECONDS"] = "off"
            stop = asyncio.Event()
            ck("off returns at once", await asyncio.wait_for(factory_api.sync_prs(store, stop), 1) is None)


asyncio.run(main())
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
