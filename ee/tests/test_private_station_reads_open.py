# Tares enterprise licence, see ee/LICENSE
"""With auth off, the person `local` reads their own station after deleting a private project as
they did before it (TR-1704).

A cell with no TARES_AUTH_TOKEN has one person, `local`, and its crews are theirs. Zephyrvault is
private and `local` leads it. A station worked in Atlas and then in Zephyrvault, tied to it by its
station's project, a link or one reply. Once Zephyrvault is deleted its id is hidden from every
caller, and the station's owner still reads every session of the station:

  1. [station]  GET /api/crew/{name}: the recent lines, conversation and /context reading;
  2. [tool]     GET /api/crew/{name}/tool/{id}: the tool call's output;
  3. [sessions] GET /api/factory/crew/{crew}/{station}/sessions: every earlier session, counted;
  4. [list]     GET /api/crew: the current session, `earlier`, `earlier_count` and
                `coordination_calls`.

What a deleted private project drops for everyone stays dropped: its name on the station, and the
cost of a session that counted for it (TR-1696). Every read is checked on a cell whose sessions table
has caught up and on the events scan. Auth on is test_private_station_reads.

Run: .venv/bin/python ee/tests/test_private_station_reads_open.py
Postgres: TARES_TEST_PG=postgresql://... PYTHONPATH=tests/pg_harness .venv/bin/python ee/tests/test_private_station_reads_open.py
"""
import asyncio
import json
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_private_station_reads as reads   # its licensed cell, session lines and tally
from test_private_station_reads import SECRET, ck

del os.environ["TARES_AUTH_TOKEN"]   # auth off: the daemon reads it when it is imported

STATION = "shop-build-m1"
# oldest first: the open session keeps the station listed once Zephyrvault is deleted
SESSIONS = [("own-0000", "open"), ("own-0001", "station"), ("own-0002", "link"),
            ("own-0003", "reply"), ("own-0004", "station")]
CURRENT = SESSIONS[-1][0]
EARLIER = [sid for sid, _ in SESSIONS[:-1]][::-1]
WORLD = {"who": {"local": {}}}   # an open cell: no credential


async def seed(app) -> dict:
    """The two projects and the station's sessions, all as `local`."""
    store = app.state.store
    ids = {}
    async with reads.client(app, {}) as cx:
        for name in ("Atlas", "Zephyrvault"):
            r = await cx.post("/api/projects", json={"template": "custom", "name": name,
                                                     "objects": [], "kind": "software_factory"})
            assert r.status_code in (200, 201), r.text
            ids[name] = r.json()["id"]
        atlas, vault = ids["Atlas"], ids["Zephyrvault"]
        r = await cx.patch(f"/api/projects/{vault}", json={"scope": "private"})
        p = store.get_project(vault)
        ck("same", "with auth off a private project led by `local` is made through the API",
           r.status_code < 300 and p["scope"] == "private" and p["lead_person_id"] == "local",
           (r.status_code, r.text, p))

    for sid, _ in SESSIONS:
        reads.MINUTE[sid] = len(reads.MINUTE)
    lines = [x for sid, tie in SESSIONS
             for x in reads.session_lines(STATION, sid, tie, atlas, vault)]
    async with reads.client(app, {"Content-Type": "application/x-ndjson"}) as cx:
        r = await cx.post("/ingest/claude_code", content=reads.nd(lines))
        assert r.status_code < 300, r.text
    await asyncio.sleep(0.5)
    read_at = datetime.now(timezone.utc) - timedelta(minutes=2)
    for sid, tie in SESSIONS:
        if tie == "link":
            store.link_session(atlas, sid, None)
            store.link_session(vault, sid, None)
        mark = SECRET if tie != "open" else "open-work"
        store.set_session_context(sid, {
            "model": "claude-opus-5-5", "total_tokens": 500,
            "max_tokens": reads.WINDOW[tie != "open"], "percentage": 1,
            "memory_files": [{"path": f"/w/{mark}/{sid}.md", "tokens": 5}]}, read_at)
        store.ask_session_context(sid, read_at + timedelta(minutes=1))
    await asyncio.sleep(0.5)
    return ids


async def owner_reads(app) -> dict:
    """What the four reads give the station's owner, by read."""
    async def get(path, **params):
        return await reads.get(app, WORLD, "local", path, **params)

    row = reads.row((await get("/api/crew")).json(), STATION)
    one = (await get(f"/api/crew/{STATION}", recent=200, conversation=500)).json()
    tool = await get(f"/api/crew/{STATION}/tool/t-{CURRENT}")
    page = await get(f"/api/factory/crew/{row.get('crew_id')}/{STATION}/sessions", limit=200)
    sessions = page.json()
    return {
        "station": {k: one.get(k) for k in ("session", "recent", "conversation", "context_detail",
                                            "context_at", "context_asked_at")},
        "tool": {"status": tool.status_code, "output": tool.json().get("output")},
        "sessions": {"status": page.status_code, "earlier_count": sessions.get("earlier_count"),
                     "sessions": [{k: s[k] for k in ("session", "started_at", "last_at", "lines")}
                                  for s in sessions.get("sessions") or []]},
        "list": {**{k: row.get(k) for k in ("session", "started_at", "lines", "claude_version",
                                            "context", "earlier_count", "coordination_calls")},
                 "earlier": [e["session"] for e in row.get("earlier") or []]},
        "projects": [row.get("projects"), sessions.get("projects")],
    }


def check_whole(label, got):
    """The owner reads all of it: what the reads after the delete are compared with."""
    ck("station", f"{label}: the station's read has its lines, conversation and reading",
       got["station"]["session"] == CURRENT
       and all(SECRET in json.dumps(got["station"][k])
               for k in ("recent", "conversation", "context_detail"))
       and got["station"]["context_at"] and got["station"]["context_asked_at"], got["station"])
    ck("tool", f"{label}: the tool call reads",
       got["tool"] == {"status": 200, "output": f"{SECRET} {CURRENT} output"}, got["tool"])
    ck("sessions", f"{label}: the route lists and counts every earlier session",
       [s["session"] for s in got["sessions"]["sessions"]] == EARLIER
       and got["sessions"]["earlier_count"] == len(EARLIER), got["sessions"])
    ck("list", f"{label}: the row shows the current session and every earlier one",
       got["list"]["session"] == CURRENT and got["list"]["lines"] > 0
       and got["list"]["claude_version"] == reads.VERSION[True]
       and got["list"]["earlier"] == EARLIER and got["list"]["earlier_count"] == len(EARLIER)
       and got["list"]["coordination_calls"] == len(SESSIONS)
       and got["list"]["context"]["cost_usd"] is not None, got["list"])


async def main():
    from tares import session_ingest
    from tares.daemon import make_app
    app = make_app()
    store = app.state.store
    async with app.router.lifespan_context(app):
        to_duckdb = reads.keep_held(store)
        ids = await seed(app)
        vault = ids["Zephyrvault"]
        ck("same", "the sessions table has caught up", session_ingest.caught_up(store))
        real = session_ingest.caught_up
        paths = (("caught up", real), ("events scan", lambda s: False))
        before = {}
        for path_name, caught in paths:
            if path_name == "events scan":
                to_duckdb()
            session_ingest.caught_up = caught
            try:
                before[path_name] = await owner_reads(app)
            finally:
                session_ingest.caught_up = real
            check_whole(f"before the delete, {path_name}", before[path_name])

        async with reads.client(app, {}) as cx:
            r = await cx.delete(f"/api/projects/{vault}")
        ck("same", "the private project is deleted through the API",
           r.status_code < 300 and store.get_project(vault) is None, (r.status_code, r.text))

        for path_name, caught in paths:
            session_ingest.caught_up = caught
            try:
                got = await owner_reads(app)
            finally:
                session_ingest.caught_up = real
            for read in ("station", "tool", "sessions", "list"):
                want = before[path_name][read]
                if read == "list":   # nobody reads the cost of a hidden project's session (TR-1696)
                    want = {**want, "context": {**want["context"], "cost_usd": None}}
                ck(read, f"after the delete, {path_name}: the owner reads it as before",
                   got[read] == want,
                   {k: (want[k], got[read][k]) for k in want if got[read][k] != want[k]})
            ck("same", f"after the delete, {path_name}: the deleted project is not named",
               got["projects"] == [[ids["Atlas"]]] * 2, got["projects"])

    for read in reads.READS:
        print(f"[{read}] {reads.TALLY[read][0]} passed, {reads.TALLY[read][1]} failed")
    passed, failed = (sum(t[i] for t in reads.TALLY.values()) for i in (0, 1))
    print(f"{passed} passed, {failed} failed")
    return failed


if __name__ == "__main__":
    sys.exit(1 if asyncio.run(main()) else 0)
