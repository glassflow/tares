# Tares enterprise licence, see ee/LICENSE
"""Reading a station shows a non-member nothing from a private project's sessions (TR-1704).

Kim's crew has stations whose sessions worked in Atlas (a team project) and in Zephyrvault, which
is private to Lee. A session is tied to Zephyrvault in one of three ways: its station's project, a
link, or one reply stamped with it. Kim is the owner of the crew and not on Zephyrvault, Lee leads
it and Mo is a member of it, Ann is neither. A key with no person and a key of Atlas are neither too.

For Ann and the two keys, each of the four reads leaves those sessions out:

  1. [station]  GET /api/crew/{name}: no recent lines, conversation or /context reading when the
                current session is one of them;
  2. [tool]     GET /api/crew/{name}/tool/{id}: the answer for a tool call that is not there;
  3. [sessions] GET /api/factory/crew/{crew}/{station}/sessions: not listed, counted or paged by;
  4. [list]     GET /api/crew (and the Crew page, the project's crew and one station's row): not in
                `earlier`, `earlier_count` or `coordination_calls`; a current session that is one of
                them shows no session id, lines, start, context window or Claude Code version.

Kim, Lee, Mo and the cell's own credential read everything ([same]), `usage` stays what TR-1696 made
it, and the two writes on a station keep working. Every read is checked on a cell whose sessions
table has caught up and on the events scan.

Run: .venv/bin/python ee/tests/test_private_station_reads.py
Postgres: TARES_TEST_PG=postgresql://... PYTHONPATH=tests/pg_harness .venv/bin/python ee/tests/test_private_station_reads.py
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
_TMP = tempfile.mkdtemp(prefix="tares-private-station-reads-")
os.environ["TARES_DB"] = os.path.join(_TMP, "api.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(_TMP, "none.yaml")
os.environ["TARES_LICENSE"] = licensed.test_licence()
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-private-station-reads"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
os.environ["TARES_EDITION"] = "factory"

import httpx

logging.getLogger("httpx").setLevel(logging.WARNING)

READS = ("station", "tool", "sessions", "list", "same")
TALLY = {r: [0, 0] for r in READS}          # read -> [passed, failed]
SECRET = "zz-secret"                        # in every line and reading of a session tied to the vault
VERSION = {False: "2.1.9", True: "9.8.7"}   # Claude Code version, by whether the session is hidden
WINDOW = {False: 200000, True: 731913}      # context window, the same way
T0 = datetime(2026, 11, 1, tzinfo=timezone.utc)

BYSTATION, BYLINK, BYREPLY = "shop-build-m1", "shop-build-m2", "shop-build-m3"
HISTORY, DEEP, CONTROL, VAULTED = "shop-build-m4", "shop-build-m5", "shop-build-m6", "shop-build-m7"
CURRENT_HIDDEN = (BYSTATION, BYLINK, BYREPLY)


def _run(tag, *ties):
    return [(f"{tag}-{i:04d}", tie) for i, tie in enumerate(ties)]


# each station's sessions, oldest first, with what ties each one to the private project
PLAN = {
    BYSTATION: _run("sta", "open", "station"),
    BYLINK: _run("lnk", "open", "link"),
    BYREPLY: _run("rep", "open", "reply"),
    HISTORY: _run("his", "open", "station", "link", "reply", "open"),
    DEEP: _run("dee", "open", "open", "open", "station", "link", "reply", "station", "link",
               "reply", "station", "open"),
    CONTROL: _run("ctl", "open", "open", "open"),
    VAULTED: _run("pri", "station", "station"),
}
HIDDEN = [sid for ss in PLAN.values() for sid, tie in ss if tie != "open"]
MINUTE = {sid: k for k, sid in enumerate(sid for ss in PLAN.values() for sid, _ in ss)}
ROW_KEYS = ("session", "started_at", "lines", "claude_version", "context", "earlier",
            "earlier_count", "coordination_calls")


def ck(read, label, cond, detail=""):
    TALLY[read][0 if cond else 1] += 1
    print(("  ok   " if cond else "  FAIL ") + f"[{read}] {label}"
          + ("" if cond else f"  {str(detail)[:300]}"))


def iso(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def session_lines(station, sid, tie, atlas, vault):
    """A session's lines: its station, a prompt, a reply, a coordination call and its result, and
    the status line. A session tied to the vault says SECRET in all of its text."""
    hidden = tie != "open"
    mark = f"{SECRET if hidden else 'open-work'} {sid}"

    def head(s):
        return {"sessionId": sid, "cwd": "/w/crew",
                "timestamp": iso(T0 + timedelta(minutes=MINUTE[sid], seconds=s))}

    def reply(s, mid, text):
        return {**head(s), "type": "assistant", "message": {
            "id": mid, "role": "assistant", "model": "claude-opus-5-5",
            "content": [{"type": "text", "text": text}],
            "usage": {"input_tokens": 2, "output_tokens": 3, "cache_creation_input_tokens": 10,
                      "cache_read_input_tokens": 100}}}

    out = [{**head(0), "type": "session_station", "station": station, "role": "builder",
            "parent": "crew-orchestrator", "factory_station": station, "factory_crew": "crew",
            "crew": "crew", "factory_machine": "kim-mbp", "machine": "kim-mbp",
            "tares_project": vault if tie == "station" else atlas},
           {**head(1), "type": "user", "message": {"role": "user", "content": f"{mark} asked"}},
           reply(2, f"m-{sid}", f"{mark} said"),
           {**head(3), "type": "assistant", "message": {
               "id": f"c-{sid}", "role": "assistant", "content": [
                   {"type": "tool_use", "id": f"t-{sid}", "name": "SendMessage",
                    "input": {"to": "x", "message": f"{mark} sent"}}]}},
           {**head(4), "type": "user", "message": {"role": "user", "content": [
               {"type": "tool_result", "tool_use_id": f"t-{sid}", "content": f"{mark} output"}]}}]
    if tie == "reply":
        out.append({**reply(5, f"v-{sid}", f"{mark} for the vault"), "tares_project": vault})
    out.append({**head(6), "type": "session_status", "status": {
        "version": VERSION[hidden],
        "context_window": {"context_window_size": WINDOW[hidden], "used_percentage": 10},
        "cost": {"total_cost_usd": 4.5}}})
    return out


def nd(lines) -> str:
    return "\n".join(json.dumps(x) for x in lines) + "\n"


def client(app, headers, **kw):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                             headers=headers, timeout=120, **kw)


async def seed(app) -> dict:
    """The people, the two projects and Kim's crew. Returns what the reads need."""
    store = app.state.store
    root = {"Authorization": f"Bearer {TOKEN}"}
    who, pid = {"root": root}, {}
    async with client(app, root) as cx:
        r = await cx.post("/api/sources", json={"name": "claude_code", "connector": "claude_code",
                                                "poll": "10s", "config": {"push": True}})
        assert r.status_code < 300, r.text
        for name in ("kim", "lee", "mo", "ann", "bot"):
            body = {"name": f"{name}-laptop", "scopes": ["admin", "ingest", "read"]}
            if name != "bot":   # the bot's key has no person
                body["person"] = {"email": f"{name}-psr@example.com", "name": name.capitalize()}
            r = await cx.post("/api/keys", json=body)
            assert r.status_code < 300, r.text
            who[name] = {"Authorization": f"Bearer {r.json()['secret']}"}
            if name != "bot":
                pid[name] = r.json()["person"]["id"]

    async def make_project(owner, name):
        async with client(app, who[owner]) as cx:
            r = await cx.post("/api/projects", json={"template": "custom", "name": name,
                                                     "objects": [], "kind": "software_factory"})
            assert r.status_code in (200, 201), r.text
            return r.json()["id"]

    atlas = await make_project("kim", "Atlas")
    vault = await make_project("lee", "Zephyrvault")
    async with client(app, who["lee"]) as cx:
        r = await cx.patch(f"/api/projects/{vault}", json={"scope": "private"})
        assert r.status_code < 300 and store.get_project(vault)["scope"] == "private", r.text
    store.add_project_member(vault, pid["mo"])
    async with client(app, who["kim"]) as cx:
        r = await cx.post(f"/api/projects/{atlas}/keys", json={"name": "atlas-agent"})
        assert r.status_code < 300, r.text
        who["atlaskey"] = {"Authorization": f"Bearer {r.json()['secret']}"}

    lines = [x for station, ss in PLAN.items() for sid, tie in ss
             for x in session_lines(station, sid, tie, atlas, vault)]
    async with client(app, {**who["kim"], "Content-Type": "application/x-ndjson"}) as cx:
        r = await cx.post("/ingest/claude_code", content=nd(lines))
        assert r.status_code < 300, r.text
    await asyncio.sleep(0.5)
    read_at = datetime.now(timezone.utc) - timedelta(minutes=2)
    for station, ss in PLAN.items():
        for sid, tie in ss:
            if tie == "link":
                store.link_session(atlas, sid, None)
                store.link_session(vault, sid, None)
            mark = SECRET if tie != "open" else "open-work"
            store.set_session_context(sid, {
                "model": "claude-opus-5-5", "total_tokens": 500, "max_tokens": WINDOW[tie != "open"],
                "percentage": 1, "memory_files": [{"path": f"/w/{mark}/{sid}.md", "tokens": 5}]},
                read_at)
            store.ask_session_context(sid, read_at + timedelta(minutes=1))
    await asyncio.sleep(0.5)
    return {"who": who, "atlas": atlas, "vault": vault}


async def get(app, world, who, path, **params):
    async with client(app, world["who"][who]) as cx:
        return await cx.get(path, params=params or None)


def row(body, name):
    rows = [s for s in body["stations"] if s["name"] == name]
    return rows[0] if rows else {}


def page_members(body) -> dict:
    """The Crew page's builders by station."""
    return {m["station"]: m for c in body["crews"] for g in c["builders"] for m in g["members"]}


def leaked(body) -> list[str]:
    """What of a hidden session shows in a response body."""
    s = json.dumps(body, default=str)
    out = [sid for sid in HIDDEN if sid in s]
    for mark in (SECRET, f'"{VERSION[True]}"', str(WINDOW[True])):
        if mark in s:
            out.append(mark)
    return out


async def pages(app, world, who, path, limit):
    """Every page of the sessions route: (the ids, each page's next_before, the first body)."""
    ids, marks, first, before = [], [], None, None
    for _ in range(50):
        r = await get(app, world, who, path, limit=limit, **({"before": before} if before else {}))
        assert r.status_code == 200, (r.status_code, r.text)
        body = r.json()
        first = first or body
        ids += [s["session"] for s in body["sessions"]]
        before = body["next_before"]
        if not before:
            return ids, marks, first
        marks.append(before)
    raise AssertionError(f"the pages of {path} never end")


def minute_of(stamp: str) -> int:
    return int((datetime.fromisoformat(stamp.replace("Z", "+00:00")) - T0).total_seconds() // 60)


async def check_outsider(app, world, who, path_name):
    """Ann, or a key that is nobody's: the four reads show nothing of a hidden session."""
    label = f"{who}, {path_name}"
    crew = (await get(app, world, who, "/api/crew")).json()
    cid = row(crew, CONTROL)["crew_id"]

    for name in CURRENT_HIDDEN:
        sid = PLAN[name][-1][0]
        one = (await get(app, world, who, f"/api/crew/{name}", recent=200, conversation=500)).json()
        older = (await get(app, world, who, f"/api/crew/{name}", conversation=3, before=3)).json()
        since = (await get(app, world, who, f"/api/crew/{name}", conversation=500, since=0)).json()
        ck("station", f"{label}: {name} has no recent lines",
           one["recent"] == [] and older["recent"] == [] and since["recent"] == [],
           (len(one["recent"]), len(older["recent"]), len(since["recent"])))
        convo = [json.dumps(b.get("conversation") or []) for b in (one, older, since)]
        ck("station", f"{label}: {name} has no conversation, also with before and since",
           convo == ["[]"] * 3, [c[:80] for c in convo])
        got = [one.get(k) for k in ("context_detail", "context_at", "context_asked_at")]
        ck("station", f"{label}: {name} has no /context reading or its times",
           got == [None] * 3, got)

        r = await get(app, world, who, f"/api/crew/{name}/tool/t-{sid}")
        none = await get(app, world, who, f"/api/crew/{name}/tool/t-none")
        ck("tool", f"{label}: a tool call of {name}'s hidden session answers as one that is not there",
           r.status_code == none.status_code == 404 and SECRET not in r.text
           and r.text.replace(f"t-{sid}", "t-none") == none.text, (r.status_code, r.text))

        for shape, body in (("the list", row(crew, name)), ("its own read", one)):
            ck("list", f"{label}: {name} in {shape} shows no id, lines, start or version of its session",
               [body.get(k, "missing") for k in ("session", "lines", "started_at", "claude_version")]
               == [None] * 4, [body.get(k) for k in ("session", "lines", "started_at", "claude_version")])
            ctx = body.get("context") or {}
            ck("list", f"{label}: {name} in {shape} shows no context window",
               all(v is None for v in ctx.values()), ctx)
            earlier = [e["session"] for e in body.get("earlier") or []]
            ck("list", f"{label}: {name} in {shape} keeps the open session before it, and its call",
               earlier == [PLAN[name][0][0]] and body["earlier_count"] == 1
               and body["coordination_calls"] == 1,
               (earlier, body["earlier_count"], body["coordination_calls"]))

    for name, shown, calls in ((HISTORY, ["his-0000"], 2),
                               (DEEP, ["dee-0002", "dee-0001", "dee-0000"], 4)):
        body = row(crew, name)
        earlier = [e["session"] for e in body["earlier"]]
        ck("list", f"{label}: {name} lists and counts only the open earlier sessions",
           earlier == shown and body["earlier_count"] == len(shown), (earlier, body["earlier_count"]))
        ck("list", f"{label}: {name} counts the calls of the open sessions only",
           body["coordination_calls"] == calls, body["coordination_calls"])
        ck("list", f"{label}: {name}'s open current session reads as before",
           body["session"] == PLAN[name][-1][0] and body["lines"] > 0
           and body["claude_version"] == VERSION[False]
           and body["context"]["size"] == WINDOW[False],
           {k: body.get(k) for k in ("session", "lines", "claude_version", "context")})

        path = f"/api/factory/crew/{cid}/{name}/sessions"
        ids, marks, first = await pages(app, world, who, path, 200)
        ck("sessions", f"{label}: {name}'s route lists only the open sessions",
           ids == shown and not marks and not leaked(first), (ids, leaked(first)))
        ck("sessions", f"{label}: {name}'s route counts only them",
           first["earlier_count"] == len(shown), first["earlier_count"])
        ids, marks, first = await pages(app, world, who, path, 1)
        want = [MINUTE[s] for s in shown[:-1]]
        ck("sessions", f"{label}: {name}'s pages of 1 never point at a hidden session's time",
           ids == shown and [minute_of(m) for m in marks] == want,
           (ids, [minute_of(m) for m in marks], want))

    control = row(crew, CONTROL)
    ck("same", f"{label}: a station of open sessions only reads as before",
       control["session"] == "ctl-0002" and control["earlier_count"] == 2
       and control["coordination_calls"] == 3, control)
    r = await get(app, world, who, f"/api/crew/{CONTROL}/tool/t-ctl-0002")
    one = (await get(app, world, who, f"/api/crew/{CONTROL}", recent=200, conversation=500)).json()
    ck("same", f"{label}: its tool call, lines, conversation and reading are all there",
       r.status_code == 200 and "open-work ctl-0002 output" in r.text and one["recent"]
       and one["conversation"] and one["context_detail"] and one["context_at"], (r.status_code, one))
    # TR-1696: usage counts the replies that count for a project the caller can see, in any session
    got = {n: row(crew, n)["usage"]["replies"] for n in (BYSTATION, BYLINK, BYREPLY, HISTORY, CONTROL)}
    want = {BYSTATION: 1, BYLINK: 2, BYREPLY: 2, HISTORY: 4, CONTROL: 3}
    ck("same", f"{label}: usage is what TR-1696 made it", got == want, got)
    ck("same", f"{label}: a station that only worked in the vault is not there",
       not row(crew, VAULTED)
       and (await get(app, world, who, f"/api/crew/{VAULTED}")).status_code == 404
       and (await get(app, world, who, f"/api/factory/crew/{cid}/{VAULTED}/sessions")).status_code == 404)

    for path in ("/api/crew", "/api/factory/crew", f"/api/projects/{world['atlas']}/crew"):
        r = await get(app, world, who, path)
        ck("list", f"{label}: nothing of a hidden session is in GET {path.replace(world['atlas'], 'atlas')}",
           r.status_code == 200 and not leaked(r.json()), (r.status_code, leaked(r.json())))
    members = page_members((await get(app, world, who, "/api/factory/crew")).json())
    got = {n: (members[n]["memory_pct"], members[n]["memory_text"]) for n in CURRENT_HIDDEN}
    ck("list", f"{label}: the Crew page says nothing of how full a hidden session's memory is",
       got == {n: (None, None) for n in CURRENT_HIDDEN}, got)
    for name in CURRENT_HIDDEN + (HISTORY, DEEP):
        one = (await get(app, world, who, f"/api/crew/{name}")).json()
        shown = {k: one.get(k) for k in ROW_KEYS}
        ck("list", f"{label}: nothing of a hidden session is in the row GET /api/crew/{name} gives",
           not leaked(shown), leaked(shown))


async def check_insider(app, world, who, path_name):
    """Kim (the owner), Lee (the lead), Mo (a member) and the cell's own credential read
    everything."""
    label = f"{who}, {path_name}"
    crew = (await get(app, world, who, "/api/crew")).json()
    cid = row(crew, CONTROL)["crew_id"]
    members = page_members((await get(app, world, who, "/api/factory/crew")).json())
    got = {n: members[n]["memory_pct"] for n in CURRENT_HIDDEN}
    ck("same", f"{label}: the Crew page says how full each one's memory is",
       got == {n: 10 for n in CURRENT_HIDDEN}, got)
    for name in CURRENT_HIDDEN:
        sid = PLAN[name][-1][0]
        body = row(crew, name)
        ck("same", f"{label}: {name} shows its current session",
           body["session"] == sid and body["lines"] > 0 and body["started_at"]
           and body["claude_version"] == VERSION[True] and body["context"]["size"] == WINDOW[True]
           and body["earlier_count"] == 1 and body["coordination_calls"] == 2,
           {k: body.get(k) for k in ROW_KEYS})
        one = (await get(app, world, who, f"/api/crew/{name}", recent=200, conversation=500)).json()
        ck("same", f"{label}: {name}'s own read has its lines, conversation and reading",
           SECRET in json.dumps(one["recent"]) and SECRET in json.dumps(one["conversation"])
           and SECRET in json.dumps(one["context_detail"]) and one["context_at"]
           and one["context_asked_at"], one)
        r = await get(app, world, who, f"/api/crew/{name}/tool/t-{sid}")
        ck("same", f"{label}: {name}'s tool call reads",
           r.status_code == 200 and f"{SECRET} {sid} output" in r.text, (r.status_code, r.text))
    for name in (HISTORY, DEEP):
        before = [sid for sid, _ in PLAN[name][:-1]][::-1]
        body = row(crew, name)
        ck("same", f"{label}: {name} lists, counts and sums every session",
           [e["session"] for e in body["earlier"]] == before[:5]
           and body["earlier_count"] == len(before)
           and body["coordination_calls"] == len(before) + 1,
           (body["earlier"], body["earlier_count"], body["coordination_calls"]))
        path = f"/api/factory/crew/{cid}/{name}/sessions"
        ids, _, first = await pages(app, world, who, path, 200)
        paged, _, _ = await pages(app, world, who, path, 1)
        ck("same", f"{label}: {name}'s route pages every earlier session",
           ids == paged == before and first["earlier_count"] == len(before), (ids, paged))


async def check_writes(app, world):
    """A key with no person may still ask for a reading and end a station of the person `local`,
    though it reads nothing of its hidden session."""
    store = app.state.store
    # the open session before it keeps the station in the list for someone who is not on the vault
    name, older, sid = "shop-build-m9", "loc-0000", "loc-0001"
    MINUTE[older] = len(MINUTE)
    MINUTE[sid] = len(MINUTE)
    lines = [x for s, tie in ((older, "open"), (sid, "station"))
             for x in session_lines(name, s, tie, world["atlas"], world["vault"])]
    async with client(app, {**world["who"]["root"], "Content-Type": "application/x-ndjson"}) as cx:
        r = await cx.post("/ingest/claude_code", content=nd(lines))
        assert r.status_code < 300, r.text
    await asyncio.sleep(0.5)
    async with client(app, world["who"]["bot"]) as cx:
        asked = await cx.post(f"/api/crew/{name}/context")
        ended = await cx.post(f"/api/crew/{name}/end")
    ck("same", "a key with no person still asks for the reading of a station of `local`, and the "
       "answer names no session", asked.status_code == 200
       and asked.json().get("session", "missing") is None
       and sid in store.context_asks(datetime.now(timezone.utc) - timedelta(minutes=15)),
       (asked.status_code, asked.text))
    ck("same", "and still ends it, on the session the station really runs",
       ended.status_code == 200 and ended.json()["name"] == name
       and (store.session_state(sid) or {}).get("state") == "ended", (ended.status_code, ended.text))
    ck("list", "the answer to ending it shows nothing of the hidden session",
       ended.status_code == 200 and ended.json().get("session", "missing") is None
       and ended.json()["claude_version"] is None, ended.text)


async def main():
    from tares import session_ingest
    from tares.daemon import make_app
    app = make_app()
    store = app.state.store
    async with app.router.lifespan_context(app):
        world = await seed(app)
        ck("same", "the sessions table has caught up", session_ingest.caught_up(store))
        every = [sid for ss in PLAN.values() for sid, _ in ss]
        tied = {s for s, ps in store.projects_by_session(every).items() if world["vault"] in ps}
        tied |= store.sessions_counting_for(every, {world["vault"]})
        ck("same", "the sessions tied to the vault are the ones the plan says", tied == set(HIDDEN),
           (sorted(tied ^ set(HIDDEN))))

        real = session_ingest.caught_up
        for path_name, caught in (("caught up", real), ("events scan", lambda s: False)):
            session_ingest.caught_up = caught
            try:
                for who in ("ann", "bot"):
                    await check_outsider(app, world, who, path_name)
                for who in ("kim", "lee", "mo", "root"):
                    await check_insider(app, world, who, path_name)
                r = await get(app, world, "atlaskey", f"/api/projects/{world['atlas']}/crew")
                ck("list", f"Atlas's key, {path_name}: nothing of a hidden session in the project's crew",
                   r.status_code == 200 and r.json()["stations"] and not leaked(r.json()),
                   (r.status_code, r.text[:200]))
            finally:
                session_ingest.caught_up = real
        await check_writes(app, world)

    for read in READS:
        print(f"[{read}] {TALLY[read][0]} passed, {TALLY[read][1]} failed")
    passed, failed = (sum(t[i] for t in TALLY.values()) for i in (0, 1))
    print(f"{passed} passed, {failed} failed")
    return failed


if __name__ == "__main__":
    sys.exit(1 if asyncio.run(main()) else 0)
