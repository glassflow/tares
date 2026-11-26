# Tares enterprise licence, see ee/LICENSE
"""Session reads with auth off, before and after a project is deleted (TR-1710).

The sessions are those of test_private_session_reads; the one person, `local`, owns the cell's
crews. Checked on a cell whose sessions table has caught up and on the events scan.

Run: .venv/bin/python ee/tests/test_private_session_reads_open.py
Postgres: TARES_TEST_PG=postgresql://... PYTHONPATH=tests/pg_harness .venv/bin/python ee/tests/test_private_session_reads_open.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_private_session_reads as reads   # its licensed cell, sessions and checks
from test_private_session_reads import READS, STATIONS, TIES, ck

del os.environ["TARES_AUTH_TOKEN"]   # auth off: the daemon reads it when it is imported

WHO = {"local": {}}   # an open cell: no credential


async def main():
    from tares import session_ingest
    from tares.daemon import make_app
    app = make_app()
    store = app.state.store
    async with app.router.lifespan_context(app):
        world = await reads.seed(app, WHO, "local", "local")
        vault = world["vault"]
        p = store.get_project(vault)
        ck("same", "with auth off a private project led by `local` is made through the API",
           p["scope"] == "private" and p["lead_person_id"] == "local", p)
        ck("same", "the sessions table has caught up", session_ingest.caught_up(store))
        real = session_ingest.caught_up
        paths = (("caught up", real), ("events scan", lambda s: False))
        every = {**{f"hid-{c}": f"app-build-{c}" for c in STATIONS},
                 **{f"open-{c}": f"app-open-{c}" for c in (*TIES, "open")}}
        before = {}
        for path_name, caught in paths:
            session_ingest.caught_up = caught
            try:
                label = f"local before the delete, {path_name}"
                for case in TIES:
                    await reads.check_shown(app, world, "local", case, f"hid-{case}", label)
                got = await reads.asked_stations(app, world, "local")
                ck("asks", f"{label}: every ask of a station is listed", got == every,
                   sorted(set(got) ^ set(every)))
                for case in STATIONS:
                    before[path_name, case] = await reads.owner_reads(app, world, "local", case)
                    posted = before[path_name, case]["ask"]
                    ck("ask", f"{label}: asking for hid-{case} answers its session id",
                       posted == [200, f"hid-{case}", True], posted)
            finally:
                session_ingest.caught_up = real

        async with reads.client(app, {}) as cx:
            r = await cx.delete(f"/api/projects/{vault}")
        ck("same", "the private project is deleted through the API",
           r.status_code < 300 and store.get_project(vault) is None, (r.status_code, r.text))

        for path_name, caught in paths:
            session_ingest.caught_up = caught
            try:
                label = f"local after the delete, {path_name}"
                for case in STATIONS:
                    got = await reads.owner_reads(app, world, "local", case)
                    want = before[path_name, case]
                    if case == "station":   # its only project is gone: not found by name
                        ck("same", f"{label}: the station of the deleted project alone is not found",
                           got["ask"] == [404, None, False], got["ask"])
                        want = {**want, "ask": got["ask"]}
                    for read in READS[:-1]:
                        ck(read, f"{label}: the owner reads hid-{case} as before",
                           got[read] == want[read], (want[read], got[read]))
                await reads.check_hidden(app, world, "local", "plain", label)
            finally:
                session_ingest.caught_up = real
    return reads.report()


if __name__ == "__main__":
    sys.exit(1 if asyncio.run(main()) else 0)
