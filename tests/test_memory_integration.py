"""Tares memory as the memory integration, and the recall tool (TR-865).

One in-process cell with auth on and a fake memory integration installed beside the built-in one.
Covered:
 - the built-in `tares` memory integration is installed, /health lists the `recall` capability;
 - `remember_for_project` and `add_to_global(doc="memory")` keep their names and their answers, and
   write the Tares doc; the MCP tool `recall` finds a line written earlier, prefixed [project] or
   [shared];
 - ranking: more shared words first, ties newest first, the project before the shared memory on a
   tie of both, a plural s and capitals do not matter, empty words (the, how) are left out, the
   limit is clamped to 1..50;
 - empty and missing input: an empty question, an empty line, a project that does not exist, a
   question nothing matches;
 - a line already in the memory is not added twice (and not offered to the integration again);
 - with a non-Tares memory integration: the Tares doc is written first, then `add(scope, line)`
   gets the same line; a failing `add` leaves a pending_memory row (the line is in the Tares doc
   anyway), the loop in sync_prs offers it again, counts the attempt, and drops the row once
   the integration takes it; the cell going back to Tares memory drops the rows; a project's
   delete drops its rows;
 - `recall` asks the integration the cell uses; when it fails, the Tares docs answer and `note`
   says so;
 - `add_to_global` with scope personal stays a Tares doc and never reaches the integration.

Run: python tests/test_memory_integration.py
"""
import asyncio
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
sys.path.insert(0, ROOT)
from _env import scrub_os_environ
scrub_os_environ()

_TMP = tempfile.mkdtemp(prefix="tares-memory-integration-")
os.environ["TARES_DB"] = os.path.join(_TMP, "mem.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(_TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-memory"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
os.environ["TARES_PR_SYNC_SECONDS"] = "off"      # the cell's own loop stays off; the test runs one

import httpx

import tares.mcp_factory as mf
import tares.mcp_server as m
from tares import factory_api, memory_route
from tares.integrations import registry
from tares.integrations.builtin import memory_tares as mt
from tares.integrations.memory import MemoryStore

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


class FakeMemory(MemoryStore):
    """A memory service of its own: it can be down, and it keeps what it was given."""
    name = "fakemem"
    title = "Fake memory"
    settings_schema: list = []
    down = False
    recall_down = False
    added: list = []

    def __init__(self, store=None):
        self.store = store

    async def check_connection(self, settings):
        return True, "ok"

    async def add(self, scope, line):
        if FakeMemory.down:
            raise ConnectionError("the memory service is down")
        FakeMemory.added.append((scope, line))

    async def recall(self, scope, question, limit=10):
        if FakeMemory.recall_down:
            raise ConnectionError("the memory service is down")
        return [f"[fake] answer to {question}"]


registry.load()
registry._loaded[("memory", "fakemem")] = FakeMemory


def lines_of(out: str):
    return json.loads(out)["lines"]


async def main():
    from tares.daemon import make_app
    app = make_app()
    store = app.state.store

    def mk():
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                                 headers={"Authorization": f"Bearer {TOKEN}"})
    m.TARESD = mf.TARESD = "http://t"
    m._cx = mf._cx = lambda timeout=10: mk()

    async with app.router.lifespan_context(app):
        store.create_project("p1", "custom", "alpha", {})
        store.create_project("p2", "custom", "beta", {})
        async with mk() as root:
            print("== installed, and in /health ==")
            ck("the built-in memory integration is installed",
               registry.get("memory", "tares") is mt.MemoryTares
               and "tares.integrations.builtin.memory_tares:MemoryTares" in registry.BUILTINS)
            ok, msg = await mt.MemoryTares().check_connection({})
            ck("a bare one tests its connection", ok and "Tares" in msg, msg)
            h = (await root.get("/health")).json()
            ck("/health lists the recall capability", "recall" in (h.get("capabilities") or []), h)

            print("== write and recall, on Tares memory ==")
            out = json.loads(await mf.remember_for_project("tests need the dev database running", project="alpha"))
            ck("remember_for_project keeps its name and answer", out["added"] is True and out["id"], out)
            ck("the answer says nothing is waiting", out.get("pending") is False, out)
            docs = json.loads(await m.list_docs(project="alpha"))
            mem = [d for d in docs if d["kind"] == "memory" and not d["global"]]
            body = json.loads(await m.get_doc(mem[0]["id"], project="alpha"))["body"] if mem else ""
            ck("the project's own memory doc holds the dated line",
               len(mem) == 1 and "tests need the dev database running (20" in body, body)
            await mf.remember_for_project("deploys go out on Tuesday", project="alpha")
            await mf.remember_for_project("database migrations run before the tests", project="alpha")
            await mf.remember_for_project("the staging database is reset nightly", project="alpha")
            await mf.remember_for_project("only beta uses the queue", project="beta")

            got = lines_of(await mf.recall("how do the tests use the dev database?", project="alpha"))
            ck("recall finds a line written earlier, marked [project]",
               got and got[0].startswith("[project] tests need the dev database running"), got)
            ck("a line with fewer shared words comes later",
               len(got) == 3 and "migrations run before the tests" in got[1]
               and "staging database" in got[2], got)
            ck("a line with no shared word is not returned", not any("Tuesday" in g for g in got), got)
            got = lines_of(await mf.recall("DEPLOY", project="alpha"))
            ck("capitals and a plural s do not matter", len(got) == 1 and "Tuesday" in got[0], got)
            got = lines_of(await mf.recall("database", project="alpha"))
            ck("a tie goes to the newest line first",
               len(got) == 3 and "staging database" in got[0] and "migrations" in got[1]
               and "dev database" in got[2], got)
            ck("a project does not see another project's memory",
               not any("queue" in g for g in lines_of(await mf.recall("queue", project="alpha"))))
            ck("the other project finds its own",
               len(lines_of(await mf.recall("queue", project="beta"))) == 1)
            raw = json.loads(await mf.recall("database", project="alpha"))
            ck("the answer names the integration, with no note",
               raw["integration"] == "tares" and raw["note"] == "", raw)

            print("== the shared memory ==")
            r = json.loads(await m.add_to_global("memory", "the household uses euros"))
            ck("add_to_global(memory) keeps its name and answer", r["added"] is True and r["doc"], r)
            got = lines_of(await mf.recall("which currency: euros?", project="alpha"))
            ck("a shared line is found, marked [shared]",
               got and got[0].startswith("[shared] the household uses euros"), got)
            got = lines_of(await mf.recall("euros", project="beta"))
            ck("and from every project", got and got[0].startswith("[shared]"), got)
            r = json.loads(await m.add_to_global("memory", "the household uses euros"))
            ck("the same shared line is not added twice", r["added"] is False, r)
            await mf.remember_for_project("euros are the unit of every price here", project="alpha")
            got = lines_of(await mf.recall("euros", project="alpha"))
            ck("on a tie of words the newest line wins, project or shared",
               got[0].startswith("[project] euros are") and got[1].startswith("[shared]"), got)
            r = await m.add_to_global("memory", "dark mode is on", scope="personal")
            ck("a personal line stays a Tares doc and is not recalled for the team",
               "dark mode" not in "".join(lines_of(await mf.recall("dark mode", project="alpha"))), r)
            ck("the old argument check still holds",
               "scope is `team` or `personal`" in await m.add_to_global("memory", "x", scope="mine"))

            print("== empty and missing input ==")
            ck("an empty question is told what to give",
               "say what you want to recall" in await mf.recall("  ", project="alpha"))
            ck("a question nothing matches gives an empty list",
               lines_of(await mf.recall("zebra", project="alpha")) == [])
            ck("only empty words fall back to them, no crash",
               isinstance(lines_of(await mf.recall("the", project="alpha")), list))
            bad = await mf.recall("database", project="nosuchproject")
            ck("a project that does not exist is named", "no project" in bad, bad)
            bad = await mf.remember_for_project("   ", project="alpha")
            ck("an empty line is refused", bad.startswith("error 400"), bad)
            r = await root.post("/api/projects/nosuch/memory/lines", json={"text": "x"})
            ck("a line for a missing project is 404", r.status_code == 404, r.status_code)
            r = await root.get("/api/projects/p1/recall", params={"q": ""})
            ck("the recall route wants a question", r.status_code == 400, r.text)
            r = await root.get("/api/projects/nosuch/recall", params={"q": "x"})
            ck("the recall route wants a project", r.status_code == 404, r.status_code)
            again = json.loads(await mf.remember_for_project("deploys go out on Tuesday", project="alpha"))
            ck("a line already there is not added twice", again["added"] is False, again)

            print("== ranking and the limit ==")
            src = [("project", ["kay ell em", "kay ell", "kay"]), ("shared", ["kay ell", "kay"])]
            ck("most shared words first, then newest, then the earlier source",
               mt.rank(src, "kay ell") == ["[project] kay ell", "[shared] kay ell", "[project] kay ell em",
                                           "[project] kay", "[shared] kay"], mt.rank(src, "kay ell"))
            ck("a word that means nothing alone is left out of a question",
               mt.question_words("how is the database") == {"database"} and mt.question_words("the") == {"the"})
            many = [("project", [f"word{i} common" for i in range(80)])]
            ck("the limit is clamped to 50", len(mt.rank(many, "common", 1000)) == 50)
            ck("and to at least 1", len(mt.rank(many, "common", 0)) == 10 and len(mt.rank(many, "common", -5)) == 1)
            ck("no question words, no lines", mt.rank(many, "") == [] and mt.rank(many, None) == [])
            ck("a doc with no bullets has no lines", mt.doc_lines("# title\n\ntext\n") == [])

            print("== a memory service of its own ==")
            store.set_choice("memory", "fakemem", {}, "test")
            FakeMemory.added.clear()
            out = json.loads(await mf.remember_for_project("the build cache lives in /var/cache", project="alpha"))
            ck("the line goes to the integration too", out["added"] and out["pending"] is False
               and len(FakeMemory.added) == 1 and FakeMemory.added[0][0] == "p1", (out, FakeMemory.added))
            got = lines_of(await mf.recall("build cache", project="alpha"))
            ck("recall now asks the integration",
               got == ["[fake] answer to build cache"], got)
            ck("the Tares doc has the line as well, written first",
               "the build cache lives in /var/cache" in
               store.get_doc("p1", mt.project_memory_doc(store, "p1"))["body"]
               and FakeMemory.added[0][1].startswith("the build cache lives in /var/cache (20"))
            await mf.remember_for_project("the build cache lives in /var/cache", project="alpha")
            ck("a line already in the doc is not offered again", len(FakeMemory.added) == 1, FakeMemory.added)
            r = json.loads(await m.add_to_global("memory", "prices are in euros"))
            ck("a shared line goes to the integration under scope factory",
               r["added"] and FakeMemory.added[-1][0] == "factory"
               and FakeMemory.added[-1][1].startswith("prices are in euros"), FakeMemory.added)
            n = len(FakeMemory.added)
            await m.add_to_global("memory", "I like tabs", scope="personal")
            ck("a personal line never reaches the integration", len(FakeMemory.added) == n)
            r = json.loads(await m.add_to_global("agents", "always branch"))
            ck("agents lines are not memory: no integration call", len(FakeMemory.added) == n and r["added"], r)

            print("== the integration is down ==")
            FakeMemory.down = True
            out = json.loads(await mf.remember_for_project("flaky tests are rerun once", project="alpha"))
            ck("the answer says the line is waiting", out["added"] is True and out["pending"] is True, out)
            ck("the Tares doc has it all the same",
               "flaky tests are rerun once" in store.get_doc("p1", mt.project_memory_doc(store, "p1"))["body"])
            rows = store.list_pending_memory()
            ck("one pending row: scope, line, one attempt, the reason",
               len(rows) == 1 and rows[0]["scope"] == "p1" and rows[0]["attempts"] == 1
               and rows[0]["line"].startswith("flaky tests are rerun once (20")
               and "memory service is down" in rows[0]["last_error"], rows)
            r = json.loads(await m.add_to_global("memory", "shared and flaky"))
            ck("a shared line waits too, under scope factory",
               r["added"] and r.get("pending") is True
               and [x["scope"] for x in store.list_pending_memory()] == ["p1", "factory"], r)

            stop = asyncio.Event()
            os.environ["TARES_PR_SYNC_SECONDS"] = "0.2"
            task = asyncio.create_task(factory_api.sync_prs(store, stop))
            await asyncio.sleep(1.2)
            rows = store.list_pending_memory()
            ck("the loop retries each round and counts it, rows stay while the service is down",
               len(rows) == 2 and all(x["attempts"] >= 2 for x in rows), rows)
            n = len(FakeMemory.added)
            FakeMemory.down = False
            for _ in range(30):
                await asyncio.sleep(0.2)
                if not store.list_pending_memory():
                    break
            stop.set()
            await asyncio.wait_for(task, 5)
            os.environ["TARES_PR_SYNC_SECONDS"] = "off"
            ck("once the service is back the rows are taken and removed",
               store.list_pending_memory() == [], store.list_pending_memory())
            ck("each line reached it once, under its own scope",
               [a for a in FakeMemory.added[n:]] and
               sorted(s for s, _ in FakeMemory.added[n:]) == ["factory", "p1"], FakeMemory.added[n:])

            print("== retry by hand, and the cell going back to Tares ==")
            FakeMemory.down = True
            await mf.remember_for_project("the lint step is slow", project="beta")
            ck("a retry that fails again counts and keeps the row",
               await memory_route.retry_pending(store) == 0
               and store.list_pending_memory()[0]["attempts"] == 2)
            store.set_choice("memory", "tares", {}, "test")
            ck("on Tares memory the rows are dropped, the doc has the lines",
               await memory_route.retry_pending(store) == 0 and store.list_pending_memory() == [])
            ck("nothing to retry is a no-op", await memory_route.retry_pending(store) == 0)
            store.set_choice("memory", "fakemem", {}, "test")
            await mf.remember_for_project("the lint step runs ruff", project="beta")
            ck("a pending row of a deleted project goes with the project",
               len(store.list_pending_memory("p2")) == 1)
            store.delete_project("p2")
            ck("...it is gone", store.list_pending_memory("p2") == [])
            FakeMemory.down = False

            print("== recall when the integration fails ==")
            FakeMemory.recall_down = True
            raw = json.loads(await mf.recall("database", project="alpha"))
            ck("the Tares docs answer, and the note says so",
               raw["integration"] == "tares" and "did not answer" in raw["note"]
               and raw["lines"] and raw["lines"][0].startswith("["), raw)
            FakeMemory.recall_down = False
            store.set_choice("memory", "gone", {}, "test")
            raw = json.loads(await mf.recall("database", project="alpha"))
            ck("a choice that is not installed reads as Tares memory", raw["integration"] == "tares"
               and raw["lines"], raw)
    print(f"\n{P} passed, {F} failed")
    sys.exit(1 if F else 0)


asyncio.run(main())
