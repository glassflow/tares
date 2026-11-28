# Tares enterprise licence, see ee/LICENSE
"""The factory pages read session activity and coordination counts from the sessions table (TR-1499).

A licensed factory cell with a crew's worth of sessions: 6 stations (orchestrator, reviewer, releaser, 3
builders) with 2 sessions each, lines of every kind (plain, Bash, coordination calls by mcp, SendMessage,
Skill and Cron), a session that never wrote a line, and sessions linked to projects. Then each page that
shows a session's lines, first and last time, or coordination calls is read as one person: Home, the Projects
list, one project, one ticket, the ticket list, Docs, Guardrails (the rules), the
two Crew pages, the project's crew and sessions, and a ticket's messages.

Two checks:

- No page runs the events scan: while a route runs, a statement that counts a session's lines in the events
  table (`GROUP BY key_value`) raises, and the test fails if one ran. The table is the only place the numbers
  come from.
- Every body equals ee/tests/fixtures/sessions_readers_golden.json, the bodies the pages gave when they
  counted over the events table (captured with `--write-golden` before the readers changed; that mode does
  not set the guard, as the readers then still read events).

The clock is frozen and the ids are numbered in the order they appear, as in test_factory_query_budget.py.

Run: .venv/bin/python ee/tests/test_sessions_readers.py [--write-golden]
Postgres: TARES_TEST_PG=postgresql://... PYTHONPATH=tests/pg_harness .venv/bin/python ee/tests/test_sessions_readers.py
"""
import asyncio
import contextvars
import json
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import licensed   # scrubs the machine's TARES_* settings and puts the repo and tests/ on sys.path
licensed.install_test_root()

ROOT = licensed.REPO
sys.path.insert(0, ROOT)
LICENCE_LINE = "Tares enterprise licence, see ee/LICENSE"   # the golden file is JSON, so the line is a key
GOLDEN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "sessions_readers_golden.json")
WRITE_GOLDEN = "--write-golden" in sys.argv

os.environ["TZ"] = "UTC"      # DuckDB answers times in the machine's zone; the golden file is one zone's
time.tzset()

_TMP = tempfile.mkdtemp(prefix="tares-sessions-readers-")
os.environ["TARES_HOME"] = os.path.join(_TMP, "api")
os.environ["TARES_CATALOG"] = os.path.join(_TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-sessions-readers"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
os.environ["TARES_EDITION"] = "factory"


def _licence() -> str:
    kit = licensed.kit
    root_priv, _ = kit.keypair(kit.ROOT_PHRASE)
    iss_priv, iss_pub = kit.keypair(kit.ISSUER_PHRASE)
    cert = kit.issuer_cert(root_priv, iss_pub, "2099-01-01")
    return kit.sign(iss_priv, cert, "Test Org", 5, ("enterprise",), "2020-01-01T00:00:00Z",
                    "2099-01-01", "lic_ee_readers")


os.environ["TARES_LICENSE"] = _licence()

import httpx

from tares.envelope import Envelope

NOW = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def ago(**kw):
    return NOW - timedelta(**kw)


def freeze_clock() -> None:
    for name, mod in list(sys.modules.items()):
        if name.split(".")[0] in ("tares", "tares_ee") and getattr(mod, "now_utc", None) is not None:
            mod.now_utc = lambda: NOW


_ID = re.compile(r"\b([a-z]{1,6})_([0-9a-f]{8,32})\b")


def steady(body):
    """The body with each generated id replaced by its prefix and the order it first appears in."""
    seen: dict[str, str] = {}
    n: dict[str, int] = {}

    def sub(m):
        if m.group(0) not in seen:
            n[m.group(1)] = n.get(m.group(1), 0) + 1
            seen[m.group(0)] = f"{m.group(1)}_{n[m.group(1)]}"
        return seen[m.group(0)]

    def walk(v):
        if isinstance(v, str):
            return _ID.sub(sub, v)
        if isinstance(v, list):
            return [walk(x) for x in v]
        if isinstance(v, dict):
            return {walk(k): walk(x) for k, x in v.items()}
        return v
    return walk(body)


# ── the guard: no session count over the events table while a route runs ─────────────────────────
_ROUTE: contextvars.ContextVar = contextvars.ContextVar("route", default=None)
EVENT_SCANS: list[str] = []


class GuardedCon:
    """Stands in for the DuckDB connection. While a route runs (a context variable, so the daemon's
    background threads are not caught), a statement that counts a session's lines in events raises."""

    def __init__(self, real):
        self._real = real

    def execute(self, sql, *a, **kw):
        route = _ROUTE.get()
        if route is not None and "GROUP BY key_value" in str(sql) and "FROM events" in str(sql):
            EVENT_SCANS.append(f"{route}: {str(sql)[:90]}")
            raise AssertionError(f"{route} counted a session's lines over the events table")
        return self._real.execute(sql, *a, **kw)

    def __getattr__(self, name):
        return getattr(self._real, name)


def call_line(sid, text, tool, at):
    """A tool_use line as the connector writes it: the text names the tool, the payload carries it."""
    payload = {"session_id": sid, "message": {"content": [{"type": "tool_use", "name": tool}]}}
    return Envelope("claude_code", "claude_code", sid, "tool_use", text, at, payload, {"session_id": sid}, at)


def plain_line(sid, kind, text, at):
    return Envelope("claude_code", "claude_code", sid, kind, text, at, {"session_id": sid},
                    {"session_id": sid}, at)


def seed(store):
    """3 software factory projects of 4 tickets, 6 stations of 2 sessions each, lines of every kind, one
    session without lines, sessions linked to projects, states for some."""
    uids = []
    tickets = {}
    for i in range(3):
        uid = f"uc_readers{i:02d}"
        uids.append(uid)
        store.create_project(uid, "custom", f"Readers project {i:02d}", {}, goal=f"Goal of project {i:02d}")
        store.set_project_kind(uid, "software_factory")
        repo = f"example-org/readers-{i:02d}"
        m1 = store.create_milestone(uid, "M1: first pages", "The pages are live.",
                                    [{"check": "open the home page", "expect": "it loads"}])
        ids = []
        for j, stage in enumerate(("doing", "review", "changes", None)):
            tid = store.create_ticket(uid, "tares", f"Ticket {j} of project {i:02d}", milestone=m1, repo=repo)
            holder = f"readers{i}-build-m1" if stage in ("doing", "changes") else None
            store.update_ticket(tid, stage=stage, status="in_progress" if stage else "todo", holder=holder)
            ids.append(tid)
        tickets[uid] = ids
        store.set_handover(uid, [{"path": f"/Users/sam/code/readers-{i:02d}", "name": repo}],
                           None, None, ago(days=2))
    stations = [("crew-orchestrator", "orchestrator", "", None), ("crew-reviewer", "reviewer", "crew-orchestrator", None),
                ("crew-releaser", "releaser", "crew-orchestrator", None)] + [
        (f"readers{k}-build-m1", "builder", "crew-orchestrator", uids[k]) for k in range(3)]
    envs = []
    n = 0
    for k, (station, role, parent, project) in enumerate(stations):
        for gen in range(2):               # an older session of the station, then the one playing it now
            sid = f"sess-{k}-{gen}"
            store.set_session_station(sid, station, role, parent, project, ago(hours=10 - gen * 4 - k))
            if project and gen == 1:
                store.link_session(project, sid, f"example-org/readers-{k - 3:02d}")
            if k == 5 and gen == 0:
                continue                   # a session that never wrote a line
            for e in range(3 + k + gen):   # a different number of lines each
                t = ago(hours=9 - gen * 4 - k, minutes=e * 5)
                envs.append(plain_line(sid, "assistant", f"line {e} of {sid}", t))
            t = ago(hours=8 - gen * 4 - k, minutes=1)
            envs.append(call_line(sid, "→ Bash(command=ls)", "Bash", t))
            envs.append(call_line(sid, "→ SendMessage(to=crew-reviewer)", "SendMessage", t + timedelta(minutes=1)))
            if role == "builder":
                envs.append(call_line(sid, "→ mcp__plugin_tares_tares__get_ticket(ticket=T1)",
                                      "mcp__plugin_tares_tares__get_ticket", t + timedelta(minutes=2)))
                envs.append(call_line(sid, "→ mcp__other__thing(x=1)", "mcp__other__thing", t + timedelta(minutes=3)))
            if gen == 1:
                envs.append(call_line(sid, "→ Skill(skill=tares:station-builder)", "Skill", t + timedelta(minutes=4)))
                envs.append(call_line(sid, "→ CronList()", "CronList", t + timedelta(minutes=5)))
            n += 1
    # a station session of a project linked by name only: no lines, linked to a project
    store.set_session_station("sess-spec", "spec", "builder", "crew-orchestrator", None, ago(hours=30))
    store.link_session(uids[0], "sess-spec", "example-org/readers-00")
    # lines of a session that no station played, and of another source
    envs.append(plain_line("sess-loose", "assistant", "a line", ago(hours=20)))
    envs.append(Envelope("deploys", "event_stream", "sess-0-1", "deploy", "a deploy", ago(hours=1)))
    store.append(envs)
    for k in range(6):
        store.set_session_state(f"sess-{k}-1", ("working", "idle", "waiting")[k % 3], None, ago(minutes=k))
        store.set_session_state(f"sess-{k}-0", "ended", "done", ago(hours=3 + k))
    return uids, tickets


def routes(uid, ref):
    return [
        ("home", "/api/home"),
        ("projects", "/api/factory/projects"),
        ("project", f"/api/factory/projects/{uid}"),
        ("ticket", f"/api/factory/projects/{uid}/tickets/{ref}"),
        ("tickets", "/api/factory/tickets"),
        ("docs", "/api/factory/docs"),
        ("project_docs", f"/api/factory/projects/{uid}/docs"),
        ("guardrail_rules", "/api/factory/guardrails/rules"),
        ("crew", "/api/crew"),
        ("factory_crew", "/api/factory/crew"),
        ("project_crew", f"/api/projects/{uid}/crew"),
        ("project_sessions", f"/api/projects/{uid}/sessions"),
        ("messages", f"/api/projects/{uid}/tickets/{ref}/messages"),
    ]


async def main():
    from tares.daemon import make_app
    freeze_clock()
    app = make_app()
    store = app.state.store
    root = {"Authorization": f"Bearer {TOKEN}"}
    bodies = {}

    async def call(hdr, path):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                                     headers=hdr, timeout=60) as cx:
            return await cx.get(path)

    async with app.router.lifespan_context(app):
        r = await httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                                    headers=root).post("/api/keys", json={
                                        "name": "sam-laptop", "scopes": ["admin"],
                                        "person": {"email": "sam-readers@example.com", "name": "Sam"}})
        assert r.status_code in (200, 201), r.text
        sam = {"Authorization": f"Bearer {r.json()['secret']}"}
        uids, tickets = seed(store)
        sessions_rows = store.sessions_db
        ck("the sessions table holds the lines (a row per session with lines)",
           sessions_rows is not None and len(store.station_sessions("claude_code")) >= 12)
        if not WRITE_GOLDEN:
            store.con = GuardedCon(store.con)
        uid = uids[0]
        ref = store.get_ticket(uid, tickets[uid][0])["id"]
        for name, path in routes(uid, ref):
            token = _ROUTE.set(name)
            try:
                resp = await call(sam, path)
            except AssertionError as e:     # the guard: the route reached the events table
                ck(f"GET {path} ran without the events table", False, str(e))
                continue
            finally:
                _ROUTE.reset(token)
            ck(f"GET {path.split('?')[0]} answers 200", resp.status_code == 200, f"{resp.status_code} {resp.text[:200]}")
            try:
                bodies[name] = steady(resp.json())
            except ValueError:
                bodies[name] = {"status": resp.status_code, "text": resp.text[:200]}
        ck("the readers answer with the numbers the pages show (a builder has lines and coordination calls)",
           any(s["coordination_calls"] > 0 for s in bodies.get("crew", {}).get("stations", [])),
           str(bodies.get("crew", {}).get("stations", [])[:1]))

    if not WRITE_GOLDEN:
        ck("no page counted a session's lines over the events table", not EVENT_SCANS, str(EVENT_SCANS[:3]))
    print("== the bodies ==")
    if WRITE_GOLDEN:
        os.makedirs(os.path.dirname(GOLDEN), exist_ok=True)
        with open(GOLDEN, "w") as f:
            json.dump({"_licence": LICENCE_LINE, **bodies}, f, indent=1, sort_keys=True)
            f.write("\n")
        print(f"  wrote {GOLDEN}")
    else:
        with open(GOLDEN) as f:
            want = json.load(f)
        ck("the golden file keeps its licence line (tests/test_ee_headers.py reads it)",
           want.get("_licence") == LICENCE_LINE, want.get("_licence"))
        for name in bodies:
            same = json.loads(json.dumps(bodies[name], sort_keys=True)) == want.get(name)
            ck(f"{name} answers what the golden file has", same, "differs: the pages must answer what they did "
               "when they counted over the events table")
    print(f"\n{P} passed, {F} failed")
    sys.exit(1 if F else 0)


asyncio.run(main())
