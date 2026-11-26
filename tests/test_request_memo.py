"""Small repeated reads are kept for the length of one page read, and only then (TR-1631).

On the store: inside `request_memo.scope()` a repeated read runs once and each caller gets its own
copy; outside it, and after a write (through a Store method or not), the read runs again; two page
reads share nothing. On the daemon: the page routes have a memo and no other route does, and a
second request reads again. The integration reads Setup repeats are kept too, and a write to their
tables is seen.

Run: .venv/bin/python tests/test_request_memo.py
Postgres: TARES_TEST_PG=postgresql://... PYTHONPATH=tests/pg_harness .venv/bin/python tests/test_request_memo.py
"""
import asyncio
import collections
import contextvars
import os
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _env import scrub_os_environ
scrub_os_environ()

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_TMP = tempfile.mkdtemp(prefix="tares-request-memo-")
os.environ["TARES_DB"] = os.path.join(_TMP, "api.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(_TMP, "none.yaml")
os.environ["TARES_EDITION"] = "factory"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"

import httpx
from sqlalchemy import event, update

from tares import request_memo
from tares.session_store.tables import integration_connections as connections_table
from tares.session_store.tables import settings as settings_table
from tares.store import Store

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    if cond:
        P += 1
    else:
        F += 1
        print(f"FAIL {label} {detail}")


def watch(store) -> list:
    """Every statement the session store runs from now on, as (text, parameters)."""
    seen: list = []
    event.listen(store.sessions_db.engine, "before_cursor_execute",
                 lambda conn, cur, sql, params, *a: seen.append((sql, repr(params))))
    return seen


def selects(seen: list, fn):
    """(what `fn` returned, how many SELECTs it ran)."""
    start = len(seen)
    value = fn()
    return value, sum(1 for sql, _ in seen[start:] if sql.lstrip().upper().startswith("SELECT"))


def elsewhere(fn):
    """Run `fn` on another thread, which has no memo: another request of the same daemon."""
    out = []
    t = threading.Thread(target=lambda: out.append(fn()))
    t.start()
    t.join()
    return out[0]


def on_the_store():
    store = Store(os.path.join(_TMP, "store.duckdb"))
    seen = watch(store)
    store.create_project("uc_a", "custom", "First", {"repos": ["acme/shop"]}, goal="one")
    store.create_ticket("uc_a", "tares", "A ticket")
    store.set_setting("k", "v1")
    read = lambda: store.get_setting("k")

    _, n = selects(seen, lambda: [read(), read(), read()])
    ck("outside a page read nothing is kept", n == 3, n)

    with request_memo.scope():
        got, n = selects(seen, lambda: [read(), read(), read()])
        ck("inside one a repeated read runs once", n == 1 and got == ["v1"] * 3, (n, got))
        got, n = selects(seen, lambda: [store.get_setting("other"), store.get_setting("other")])
        ck("another argument runs again, and a missing value is kept too",
           n == 1 and got == [None, None], (n, got))
        _, n = selects(seen, lambda: [store.list_projects(), store.list_projects(),
                                      store.list_projects(kind="software_factory"),
                                      store.list_projects(kind="software_factory")])
        ck("keyword arguments are part of the key", n == 2, n)
        got, n = selects(seen, lambda: [store.tickets_by_project(["uc_a"]),
                                        store.tickets_by_project(["uc_a"])])
        ck("a list argument is part of the key", n == 1 and got[0] == got[1], n)
        keys = {"uc_a": None}.keys()
        got, n = selects(seen, lambda: [store.tickets_by_project(keys),
                                        store.tickets_by_project(keys)])
        ck("an argument that cannot be a key is passed on and nothing is kept",
           n == 2 and len(got[1]["uc_a"]) == 1, (n, got))
        with request_memo.scope():
            _, n = selects(seen, read)
            ck("a scope inside a scope shares its memo", n == 0, n)
        _, n = selects(seen, read)
        ck("and leaving the inner one ends nothing", n == 0, n)

    # every caller gets its own copy
    with request_memo.scope():
        first = store.list_projects()
        first[0]["name"] = "changed"
        first[0]["params"]["repos"].append("x/y")
        first.append({"id": "extra"})
        second = store.list_projects()
        ck("changing the first answer changes no later one",
           [p.get("name") for p in second] == ["First"]
           and second[0]["params"]["repos"] == ["acme/shop"],
           second)
        second[0]["params"]["repos"].clear()
        third, n = selects(seen, store.list_projects)
        ck("nor does changing a kept answer",
           n == 0 and third[0]["params"]["repos"] == ["acme/shop"], (n, third))
        tickets = store.tickets_by_project(["uc_a"])
        tickets["uc_a"] = []                         # what the Projects route does to its answer
        ck("a replaced list stays the caller's own", len(store.tickets_by_project(["uc_a"])["uc_a"]) == 1)

    # a write ends the memo, whoever writes
    with request_memo.scope():
        read()
        store.set_setting("k", "v2")
        got, n = selects(seen, read)
        ck("after a write the read runs again", got == "v2" and n == 1, (got, n))
        _, n = selects(seen, lambda: [read(), read()])
        ck("and nothing is kept for the rest of that request", n == 2, n)
    with request_memo.scope():
        read()
        with store.sessions_db.begin() as conn:      # a write no Store method names
            conn.execute(update(settings_table).where(settings_table.c.key == "k").values(value="v3"))
        ck("a write made on the session store directly ends it too", read() == "v3", read())
    with request_memo.scope():
        store.list_projects()
        store.create_project("uc_b", "custom", "Second", {}, goal="two")
        ck("a new row is read after it is written", len(store.list_projects()) == 2)

    # two page reads share nothing
    with request_memo.scope():
        ck("the first request reads v3", read() == "v3")
        elsewhere(lambda: store.set_setting("k", "v4"))

        def other_request():
            with request_memo.scope():
                return read()
        ck("another request running now reads what is stored now", elsewhere(other_request) == "v4")
        ck("while the first keeps what it read", read() == "v3", read())
    with request_memo.scope():
        ck("the next request reads again", read() == "v4", read())

    # the memo ends with the request, also for a context copied from it
    with request_memo.scope():
        read()
        copied = contextvars.copy_context()
    _, n = selects(seen, lambda: copied.run(lambda: [read(), read()]))
    ck("a context copied from a request keeps nothing once it ended", n == 2, n)
    try:
        with request_memo.scope():
            read()
            raise KeyError("route failed")
    except KeyError:
        pass
    _, n = selects(seen, lambda: [read(), read()])
    ck("a request that raised leaves no memo behind", n == 2, n)

    # what counts as a write
    for sql, keeps in (("SELECT updated_at FROM t", True), ("  select 1 FOR UPDATE", True),
                       ("BEGIN IMMEDIATE", True), ("WITH a AS (SELECT 1) SELECT * FROM a", True),
                       ("WITH a AS (SELECT 1) DELETE FROM t", False), ("INSERT INTO t VALUES (1)", False),
                       ("UPDATE t SET a = 1", False), ("DELETE FROM t", False),
                       ("ALTER TABLE t ADD c TEXT", False), ("TRUNCATE t", False)):
        with request_memo.scope():
            read()
            request_memo.statement(sql)
            _, n = selects(seen, read)
        ck(f"{sql!r} {'keeps' if keeps else 'ends'} the memo", n == (0 if keeps else 1), n)

    # the integration reads (TR-1617)
    store.set_choice("memory", "tares", {}, "root")
    store.create_project("uc_c", "custom", "Third", {}, goal="three")
    store.set_project_choice("uc_c", "ci", "local", {}, "root")
    with request_memo.scope():
        for label, fn in (("get_choice", lambda: store.get_choice("memory")),
                          ("list_connections", lambda: store.list_connections("memory")),
                          ("get_project_choice", lambda: store.get_project_choice("uc_c", "ci"))):
            before = fn()
            again, n = selects(seen, fn)
            ck(f"a repeated {label} runs once", before and n == 0 and again == before, (n, before, again))
        with store.sessions_db.begin() as conn:
            conn.execute(update(connections_table).where(connections_table.c.slot == "memory")
                         .values(updated_by="someone"))
        seen_by = store.get_choice("memory")["updated_by"]
        ck("a write to the connections is seen by the next read", seen_by == "someone", seen_by)
    store.close()


PAGES = ["/api/home", "/api/factory/crew", "/api/factory/setup", "/api/factory/setup/progress",
         "/api/factory/projects"]


async def on_the_daemon():
    from tares.daemon import make_app
    from tares import factory_home
    app = make_app()
    store = app.state.store
    async with app.router.lifespan_context(app):
        had: set = set()
        for name in ("private_projects", "list_projects", "get_setting"):
            real = getattr(store, name)

            def noted(*a, _real=real, **kw):
                had.add(request_memo._MEMO.get() is not None)
                return _real(*a, **kw)
            setattr(store, name, noted)
        # only what runs under a memo: a background task of the daemon (the licence check) reads the
        # same tables on its own and is no part of the page read
        seen: list = []
        event.listen(store.sessions_db.engine, "before_cursor_execute",
                     lambda conn, cur, sql, params, *a:
                     seen.append((sql, repr(params))) if request_memo._MEMO.get() is not None else None)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                                     timeout=60) as cx:
            async def get(path):
                factory_home.clear_cache()
                had.clear()
                start = len(seen)
                r = await cx.get(path)
                return r, seen[start:]

            store.create_project("uc_one", "custom", "One", {}, goal="one")
            store.set_project_kind("uc_one", "software_factory")
            for path in PAGES:
                r, ran = await get(path)
                ck(f"{path} answers 200", r.status_code == 200, r.text[:200])
                ck(f"{path} reads inside a memo", True in had, had)
                again = collections.Counter((sql, params) for sql, params in ran
                                            if "FROM usecases" in sql or "FROM people" in sql)
                ck(f"{path} asks nothing about projects or people twice",
                   again and max(again.values()) == 1, [k for k, v in again.items() if v > 1][:3])
            r, ran = await get("/api/projects")
            ck("a route that is not a page read has no memo",
               r.status_code == 200 and had == {False} and not ran, (r.status_code, had, len(ran)))
            r, _ = await get("/api/factory/setup/progress")
            first = r.json()["projects"]
            store.create_project("uc_two", "custom", "Two", {}, goal="two")
            store.set_project_kind("uc_two", "software_factory")
            r, _ = await get("/api/factory/setup/progress")
            ck("the next request reads the projects again",
               first == 1 and r.json()["projects"] == 2, (first, r.json()["projects"]))


on_the_store()
asyncio.run(on_the_daemon())
print(f"{P} passed, {F} failed")
sys.exit(1 if F else 0)
