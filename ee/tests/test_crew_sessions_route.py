# Tares enterprise licence, see ee/LICENSE
"""A station carries its current session and a count, and the earlier sessions have their own route (TR-1500).

A crew's builder station has been played by 600 sessions, a second one by 6, and a third by 8 in
two projects, one of them private. Before TR-1500 GET /api/crew and GET /api/factory/crew listed every
earlier session of every station, so the two bodies grew with the history: 760 KB and 285 KB for 600
sessions. Now:

  - each station keeps its fields, adds `earlier_count`, and lists at most 5 earlier sessions;
  - both bodies are the same size (within 1 KB) for the station with 600 sessions and with 6;
  - `usage`, `coordination_calls` and `projects` are the totals over every session, equal to what the
    per-session readers add up;
  - GET /api/factory/crew/{crew}/{station}/sessions pages through all of them, newest first, with no
    session twice or missed (sessions that share a time stay on one page), the same on a cell that has
    not caught up (the events scan);
  - a project the caller is not on never appears, and a station that only worked in it answers 404.

Run: .venv/bin/python ee/tests/test_crew_sessions_route.py
Postgres: TARES_TEST_PG=postgresql://... PYTHONPATH=tests/pg_harness .venv/bin/python ee/tests/test_crew_sessions_route.py
"""
import asyncio
import json
import logging
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import licensed   # scrubs the machine's TARES_* settings and puts the repo and tests/ on sys.path
licensed.install_test_root()

ROOT = licensed.REPO
sys.path.insert(0, ROOT)

os.environ["TZ"] = "UTC"      # the events scan answers times in the machine's zone, the table in UTC
time.tzset()
_TMP = tempfile.mkdtemp(prefix="tares-crew-sessions-route-")
os.environ["TARES_DB"] = os.path.join(_TMP, "api.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(_TMP, "none.yaml")
os.environ["TARES_LICENSE"] = licensed.test_licence()
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-crew-sessions-route"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
os.environ["TARES_EDITION"] = "factory"

import httpx

logging.getLogger("httpx").setLevel(logging.WARNING)

P = F = 0
BIG, SMALL, MIXED, VAULTED = "shop-build-m1", "shop-build-m2", "shop-build-m3", "vault-build-m1"
MANY = 600
T0 = datetime(2026, 11, 1, tzinfo=timezone.utc)
TIED = range(545, 555)     # ten sessions that were last active at the same moment, across a page boundary
TIE_AT = 550


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def iso(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def at(k: int) -> datetime:
    """When session k was active: a minute after the one before, except for the tied ones."""
    return T0 + timedelta(minutes=TIE_AT if k in TIED else k)


def session_lines(station, role, parent, k, tag, project=""):
    """A session's lines: its station, a reply that used k+1 output tokens, one coordination call."""
    sid, t = f"{tag}-{k:04d}", iso(at(k))
    head = {"sessionId": sid, "cwd": "/w/crew", "timestamp": t}
    st = {**head, "type": "session_station", "station": station, "role": role, "parent": parent,
          "factory_station": station, "factory_crew": "crew", "crew": "crew",
          "factory_machine": "kim-mbp", "machine": "kim-mbp"}
    if project:
        st["tares_project"] = project
    reply = {**head, "type": "assistant", "message": {
        "id": f"m-{sid}", "role": "assistant", "model": "claude-opus-5-5", "content": "ok",
        "usage": {"input_tokens": 2, "output_tokens": k + 1, "cache_creation_input_tokens": 10,
                  "cache_read_input_tokens": 100}}}
    call = {**head, "type": "assistant", "message": {
        "id": f"c-{sid}", "role": "assistant", "content": [
            {"type": "tool_use", "id": f"t-{sid}", "name": "SendMessage", "input": {"to": "x"}}]}}
    return [st, reply, call]


def nd(lines) -> str:
    return "\n".join(json.dumps(x) for x in lines) + "\n"


def same_usage(a, b) -> bool:
    """Equal, the cost to a millionth of a cent: a sum of floats differs in its last digit by order."""
    return {k: v for k, v in a.items() if k != "cost_usd"} == {k: v for k, v in b.items() if k != "cost_usd"} \
        and abs(a["cost_usd"] - b["cost_usd"]) < 1e-9


def sizes(crew_body, factory_body):
    return len(json.dumps(crew_body, default=str)), len(json.dumps(factory_body, default=str))


async def main():
    from tares import session_ingest
    from tares.daemon import make_app
    app = make_app()
    store = app.state.store
    root = {"Authorization": f"Bearer {TOKEN}"}
    mk = lambda h=None, **kw: httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                                                headers=h or root, timeout=120, **kw)
    async with app.router.lifespan_context(app):
        async with mk() as cx:
            r = await cx.post("/api/sources", json={"name": "claude_code", "connector": "claude_code",
                                                    "poll": "10s", "config": {"push": True}})
            assert r.status_code < 300, r.text
            keys, pid = {}, {}
            for who in ("kim", "lee"):
                r = await cx.post("/api/keys", json={"name": f"{who}-laptop", "scopes": ["admin", "ingest", "read"],
                                                     "person": {"email": f"{who}-sr@example.com", "name": who.capitalize()}})
                keys[who] = {"Authorization": f"Bearer {r.json()['secret']}"}
                pid[who] = r.json()["person"]["id"]

        async def make_project(who, name):
            async with mk(keys[who]) as cx:
                r = await cx.post("/api/projects", json={"template": "custom", "name": name, "objects": [],
                                                         "kind": "software_factory"})
                assert r.status_code in (200, 201), r.text
                return r.json()["id"]

        atlas = await make_project("kim", "Atlas")
        vault = await make_project("lee", "Zephyrvault")
        async with mk(keys["lee"]) as cx:
            r = await cx.patch(f"/api/projects/{vault}", json={"scope": "private"})
            assert r.status_code < 300 and store.get_project(vault)["scope"] == "private", r.text

        async def push(lines, who="kim"):
            async with mk({**keys[who], "Content-Type": "application/x-ndjson"}) as cx:
                for i in range(0, len(lines), 1500):
                    r = await cx.post("/ingest/claude_code", content=nd(lines[i:i + 1500]))
                    assert r.status_code < 300, r.text

        async def read(who, path, **params):
            async with mk(keys[who]) as cx:
                r = await cx.get(path, params=params or None)
                return r

        def member(body, name):
            rows = [s for s in body["stations"] if s["name"] == name]
            return rows[0] if rows else None

        # ── one station with 6 sessions, another with 6, one in two projects ─────────────────
        lines = []
        for k in range(6):
            lines += session_lines(BIG, "builder", "crew-orchestrator", k, "big", atlas)
            lines += session_lines(SMALL, "builder", "crew-orchestrator", k, "small", atlas)
        for k in range(8):
            lines += session_lines(MIXED, "builder", "crew-orchestrator", k, "mix", atlas)
        await push(lines)
        for k in range(8):   # three of the older ones worked in Vault, which Kim is not on
            if k in (1, 2, 3):
                store.link_session(vault, f"mix-{k:04d}", None)
        await push([x for k in range(3) for x in session_lines(VAULTED, "builder", "crew-orchestrator", k, "vlt")],
                   "lee")
        for k in range(3):
            store.set_session_station(f"vlt-{k:04d}", VAULTED, "builder", "crew-orchestrator", vault, at(k))
        await asyncio.sleep(0.5)
        ck("the sessions table has caught up", session_ingest.caught_up(store))

        before_crew = (await read("kim", "/api/crew")).json()
        before_page = (await read("kim", "/api/factory/crew")).json()
        small_crew, small_page = sizes(before_crew, before_page)
        ck("a station of 6 sessions lists its 5 earlier ones and counts them",
           len(member(before_crew, BIG)["earlier"]) == 5 and member(before_crew, BIG)["earlier_count"] == 5,
           (len(member(before_crew, BIG)["earlier"]), member(before_crew, BIG).get("earlier_count")))

        # ── the same station with 600 ─────────────────────────────────────────────────────────
        await push([x for k in range(6, MANY) for x in session_lines(BIG, "builder", "crew-orchestrator", k, "big", atlas)])
        await asyncio.sleep(0.5)
        crew = (await read("kim", "/api/crew")).json()
        page = (await read("kim", "/api/factory/crew")).json()
        big_crew, big_page = sizes(crew, page)
        print(f"  sizes: /api/crew {small_crew} B with 6 sessions, {big_crew} B with 600; "
              f"/api/factory/crew {small_page} B, {big_page} B")
        ck("/api/crew is the same size for 600 sessions as for 6 (within 1 KB)",
           abs(big_crew - small_crew) <= 1024, (small_crew, big_crew))
        ck("/api/factory/crew is the same size for 600 sessions as for 6 (within 1 KB)",
           abs(big_page - small_page) <= 1024, (small_page, big_page))
        row = member(crew, BIG)
        ck("the station lists 5 earlier sessions, the newest first, and counts all 599",
           row["earlier_count"] == MANY - 1 and len(row["earlier"]) == 5
           and [e["session"] for e in row["earlier"]] == [f"big-{k:04d}" for k in range(598, 593, -1)],
           (row["earlier_count"], [e["session"] for e in row["earlier"]]))
        ck("its current session is the newest", row["session"] == f"big-{MANY - 1:04d}", row["session"])
        ck("an earlier row keeps the fields the console reads",
           set(row["earlier"][0]) == {"session", "started_at", "last_at", "lines", "state"}
           and row["earlier"][0]["state"] == "replaced" and row["earlier"][0]["lines"] >= 3, row["earlier"][0])

        # ── the totals are over every session, not the five ────────────────────────────────────
        ids = [f"big-{k:04d}" for k in range(MANY)]
        spent = store.usage_by_session(ids)
        want_out = sum(k + 1 for k in range(MANY))
        ck("usage is the sum over all 600 sessions",
           row["usage"]["replies"] == MANY and row["usage"]["output_tokens"] == want_out
           and row["usage"]["output_tokens"] == sum(s["output_tokens"] for s in spent.values())
           and row["usage"]["cache_read_tokens"] == 100 * MANY, row["usage"])
        coord = store.coordination_calls("claude_code", ids)
        ck("coordination_calls is the sum over all 600 sessions",
           row["coordination_calls"] == sum(coord.values()) == MANY, (row["coordination_calls"], sum(coord.values())))
        ck("projects is every project of the station", row["projects"] == [atlas], row["projects"])
        small = member(crew, SMALL)
        ck("a station of 6 has its own totals",
           small["usage"]["replies"] == 6 and small["coordination_calls"] == 6 and small["earlier_count"] == 5,
           (small["usage"], small["coordination_calls"]))
        flat = [m for g in page["builders"] for m in g["members"]] if page.get("builders") else []
        pb = [m for m in flat if m["station"] == BIG]
        ck("the Crew page lists the station", len(pb) == 1, [m["station"] for m in flat])

        # ── the route pages through all of them ──────────────────────────────────────────────
        crew_id = row["crew_id"]
        path = f"/api/factory/crew/{crew_id}/{BIG}/sessions"
        seen, pages, before = [], 0, None
        while True:
            params = {"limit": 50, **({"before": before} if before else {})}
            r = await read("kim", path, **params)
            assert r.status_code == 200, r.text
            body = r.json()
            pages += 1
            seen += [s["session"] for s in body["sessions"]]
            if pages == 1:
                first = body
            before = body["next_before"]
            if not before:
                break
        ck("the route answers with the shape the console reads",
           set(first) == {"station", "crew_id", "earlier_count", "projects", "sessions", "next_before"}
           and set(first["sessions"][0]) == {"session", "started_at", "last_at", "lines", "state", "projects"}
           and first["earlier_count"] == MANY - 1 and first["projects"] == [atlas], sorted(first))
        ck("a page of 50 holds at least 50, and the tied sessions stay on one page",
           len(first["sessions"]) == 50 + (len(TIED) - 6) and pages == 12, (len(first["sessions"]), pages))
        want = [f"big-{k:04d}" for k in range(MANY - 2, -1, -1)]
        ck("the pages hold all 599 earlier sessions, each once", len(seen) == len(set(seen)) == MANY - 1
           and set(seen) == set(want) and f"big-{MANY - 1:04d}" not in seen, (len(seen), len(set(seen))))
        times = [at(int(s.split("-")[1])) for s in seen]
        ck("newest first", all(a >= b for a, b in zip(times, times[1:])))
        ck("the last page has no next page", before is None)
        r = await read("kim", path, limit=500)
        ck("a limit over 200 is refused", r.status_code in (400, 422), r.status_code)
        r = await read("kim", path, before="not a time")
        ck("a bad before is refused", r.status_code in (400, 422), r.status_code)
        r = await read("kim", f"/api/factory/crew/{crew_id}/no-such-station/sessions")
        ck("an unknown station answers 404", r.status_code == 404, r.status_code)
        r = await read("kim", f"/api/factory/crew/{crew_id}/{SMALL}/sessions", limit=2)
        b = r.json()
        ck("a station of 6 has 5 earlier sessions; a page of 2 has a next", len(b["sessions"]) == 2
           and b["earlier_count"] == 5 and b["next_before"], b)

        # ── the same on a cell that has not caught up (the events scan) ───────────────────────
        real = session_ingest.caught_up
        session_ingest.caught_up = lambda s: False
        try:
            slow_crew = (await read("kim", "/api/crew")).json()
            slow_seen, before = [], None
            while True:
                r = await read("kim", path, limit=50, **({"before": before} if before else {}))
                body = r.json()
                slow_seen += [s["session"] for s in body["sessions"]]
                before = body["next_before"]
                if not before:
                    break
        finally:
            session_ingest.caught_up = real
        srow = member(slow_crew, BIG)
        ck("before the first catch-up the row is the same: count, five, totals",
           srow["earlier_count"] == row["earlier_count"] and srow["earlier"] == row["earlier"]
           and same_usage(srow["usage"], row["usage"]) and srow["coordination_calls"] == row["coordination_calls"]
           and srow["projects"] == row["projects"], (srow["earlier_count"], srow["usage"]))
        ck("and the route pages through the same sessions", slow_seen == seen, (len(slow_seen), len(seen)))

        # ── privacy: Vault is private and Kim is not on it ─────────────────────────────────────
        mixed = member(crew, MIXED)
        ck("a station that worked in Vault names only Atlas to Kim", mixed["projects"] == [atlas], mixed["projects"])
        r = await read("kim", f"/api/factory/crew/{crew_id}/{MIXED}/sessions")
        body = r.json()
        ck("its earlier sessions name only Atlas to Kim", r.status_code == 200
           and body["projects"] == [atlas] and all(s["projects"] == [atlas] for s in body["sessions"])
           and body["earlier_count"] == 7, body)
        ck("Vault's id never appears in anything Kim reads",
           vault not in json.dumps([crew, page, body], default=str) and "Zephyrvault" not in json.dumps(
               [crew, page, body], default=str))
        lee_row = member((await read("lee", "/api/crew")).json(), VAULTED)
        ck("Lee, who leads Vault, sees the station and its project", lee_row is not None
           and lee_row["projects"] == [vault], lee_row)
        r = await read("lee", f"/api/factory/crew/{lee_row['crew_id']}/{VAULTED}/sessions")
        ck("Lee reads its earlier sessions", r.status_code == 200 and r.json()["projects"] == [vault]
           and r.json()["earlier_count"] == 2, r.text[:200])
        r = await read("kim", f"/api/factory/crew/{lee_row['crew_id']}/{VAULTED}/sessions")
        ck("a station that only worked in Vault answers 404 to Kim, and Kim does not see the station",
           r.status_code == 404 and member((await read("kim", "/api/crew")).json(), VAULTED) is None
           and vault not in r.text, r.status_code)

    print(f"{P} passed, {F} failed")
    return F


if __name__ == "__main__":
    sys.exit(1 if asyncio.run(main()) else 0)
