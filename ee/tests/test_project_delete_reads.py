# Tares enterprise licence, see ee/LICENSE
"""Reads before and after a project is deleted (TR-1708).

Zephyrvault is private to Lee; Mo is a member of it. Kim's crew has stations whose sessions worked
in it, some tied to it by a link only. Ann, a key with no person and a key of the team project Atlas
are not on it. Lee then deletes it.

  [stations] GET /api/crew, GET /api/crew/{name}, GET /api/crew/{name}/tool/{id} and
             GET /api/factory/crew/{crew}/{station}/sessions;
  [reuse]    the same once a new project takes the name;
  [waiting]  what waits for Kim;
  [same]     what Kim, who owns the crew, reads of their stations' sessions;
  [cost]     the totals a delete leaves as they were, and where later replies count.

Run: .venv/bin/python ee/tests/test_project_delete_reads.py
Postgres: TARES_TEST_PG=postgresql://... PYTHONPATH=tests/pg_harness .venv/bin/python ee/tests/test_project_delete_reads.py
"""
import asyncio
import hashlib
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

os.environ["TZ"] = "UTC"
time.tzset()
_TMP = tempfile.mkdtemp(prefix="tares-deleted-private-reads-")
os.environ["TARES_HOME"] = os.path.join(_TMP, "api")
os.environ["TARES_LICENSE"] = licensed.test_licence()
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-deleted-private-reads"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
os.environ["TARES_EDITION"] = "factory"

import httpx

logging.getLogger("httpx").setLevel(logging.WARNING)

GROUPS = ("stations", "reuse", "waiting", "same", "cost", "source", "shared")
TALLY = {g: [0, 0] for g in GROUPS}          # group -> [passed, failed]
SECRET = "zz-secret"                         # in every line and event of the private project
NAME = "Zephyrvault"
T0 = datetime.now(timezone.utc) - timedelta(minutes=30)
ASIDE = "uc_000_aside"                       # a team project whose id sorts before every other
LATER = "uc_zzz_later"                       # a team project whose id sorts after the private one
SECOND = "uc_zzz_second"                     # a private project made after the first delete

# station -> role and its sessions, oldest first
STATIONS = {
    "crew-orchestrator": ("orchestrator", ["orc-0000", "orc-0001"]),
    "crew-reviewer": ("reviewer", ["rev-0000", "rev-0001"]),
    "shop-help-m1": ("helper", ["hlp-0000", "hlp-0001"]),
    "crew-releaser": ("releaser", ["rel-0001"]),
    "shop-build-m1": ("builder", ["mix-0000", "mix-0001"]),
    "shop-build-m2": ("builder", ["own-0001"]),
}
LATE = "shop-build-m4"                        # a station first heard of after the delete
HIDDEN = {"orc-0001": "crew-orchestrator", "rev-0001": "crew-reviewer", "hlp-0001": "shop-help-m1",
          "rel-0001": "crew-releaser", "mix-0001": "shop-build-m1", "own-0001": "shop-build-m2"}
OWNERS_READ = ("orc-0001", "rev-0001", "hlp-0001", "mix-0001")   # their stations have another project
PEOPLE = ("per-0001", "per-0002")   # a person's own sessions, no station, that joined the private one
LINKED = {**HIDDEN, **dict.fromkeys(PEOPLE)}   # every session of it, as the event reads see them
STATUS_COSTS = {"orc-0001": 4.5, "mix-0001": 2.5}


def ck(group, label, cond, detail: object = ""):
    TALLY[group][0 if cond else 1] += 1
    print(("  ok   " if cond else "  FAIL ") + f"[{group}] {label}"
          + ("" if cond else f"  {str(detail)[:300]}"))


def iso(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def head(sid, step, second, project=None):
    out = {"sessionId": sid, "cwd": "/w/crew",
           "timestamp": iso(T0 + timedelta(seconds=10 * step + second))}
    if project:
        out["tares_project"] = project
    return out


def turn(sid, step, mark, tool, project=None, usage=True):
    """A prompt, a reply, a coordination call and its result."""
    reply = {"id": f"m-{tool}", "role": "assistant", "model": "claude-opus-5-5",
             "content": [{"type": "text", "text": f"{mark} {sid} said"}]}
    if usage:
        reply["usage"] = {"input_tokens": 2, "output_tokens": 3,
                          "cache_creation_input_tokens": 10, "cache_read_input_tokens": 100}
    return [
        {**head(sid, step, 1, project), "type": "user",
         "message": {"role": "user", "content": f"{mark} {sid} asked"}},
        {**head(sid, step, 2, project), "type": "assistant", "message": reply},
        {**head(sid, step, 3, project), "type": "assistant", "message": {
            "id": f"c-{tool}", "role": "assistant", "content": [
                {"type": "tool_use", "id": f"t-{tool}", "name": "SendMessage",
                 "input": {"to": "x", "message": f"{mark} {sid} sent"}}]}},
        {**head(sid, step, 4, project), "type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": f"t-{tool}",
             "content": f"{mark} {sid} output"}]}}]


def station_line(station, role, sid, step, project=None):
    return {**head(sid, step, 0, project), "type": "session_station", "station": station,
            "role": role, "parent": "crew-orchestrator", "factory_station": station,
            "factory_crew": "crew", "crew": "crew", "factory_machine": "kim-mbp",
            "machine": "kim-mbp"}


def client(app, headers, **kw):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                             headers=headers, timeout=120, **kw)


async def ingest(app, world, lines):
    headers = {**world["who"]["kim"], "Content-Type": "application/x-ndjson"}
    async with client(app, headers) as cx:
        r = await cx.post("/ingest/claude_code",
                          content="\n".join(json.dumps(x) for x in lines) + "\n")
        assert r.status_code < 300, r.text
    await asyncio.sleep(0.5)


async def make_project(app, world, owner, name):
    async with client(app, world["who"][owner]) as cx:
        r = await cx.post("/api/projects", json={"template": "custom", "name": name,
                                                 "objects": [], "kind": "software_factory"})
        assert r.status_code in (200, 201), r.text
        return r.json()["id"]


async def seed(app) -> dict:
    """The people, the projects, Kim's crew and the events."""
    store = app.state.store
    root = {"Authorization": f"Bearer {TOKEN}"}
    world = {"who": {"root": root}, "pid": {}}
    who = world["who"]
    async with client(app, root) as cx:
        for name in ("kim", "lee", "mo", "ann", "bot"):
            body = {"name": f"{name}-laptop", "scopes": ["admin", "ingest", "read"]}
            if name != "bot":   # the bot's key has no person
                body["person"] = {"email": f"{name}-dpr@example.com", "name": name.capitalize()}
            r = await cx.post("/api/keys", json=body)
            assert r.status_code < 300, r.text
            who[name] = {"Authorization": f"Bearer {r.json()['secret']}"}
            if name != "bot":
                world["pid"][name] = r.json()["person"]["id"]

    atlas = world["atlas"] = await make_project(app, world, "kim", "Atlas")
    beacon = world["beacon"] = await make_project(app, world, "kim", "Beacon")
    vault = world["vault"] = await make_project(app, world, "lee", NAME)
    store.create_project(ASIDE, "custom", "Aside", {})
    store.create_project(LATER, "custom", "Later", {})
    async with client(app, who["lee"]) as cx:
        r = await cx.patch(f"/api/projects/{vault}", json={"scope": "private"})
        assert r.status_code < 300 and store.get_project(vault)["scope"] == "private", r.text
    store.add_project_member(vault, world["pid"]["mo"])
    async with client(app, who["kim"]) as cx:
        r = await cx.post(f"/api/projects/{atlas}/keys", json={"name": "atlas-agent"})
        assert r.status_code < 300, r.text
        who["atlaskey"] = {"Authorization": f"Bearer {r.json()['secret']}"}

    lines, minute = [], 0
    for station, (role, sids) in STATIONS.items():
        for sid in sids:
            minute += 1
            mark = SECRET if sid in HIDDEN else "open-work"
            # a builder's station line names its project; every line of own-0001 does
            named = {"mix-0000": atlas, "mix-0001": atlas, "own-0001": vault}.get(sid)
            lines.append(station_line(station, role, sid, minute, named))
            if sid == "own-0001":
                lines += turn(sid, minute, mark, sid, project=vault)
            elif sid == "hlp-0001":      # its only reply names Atlas
                said = turn(sid, minute, mark, sid)
                said[1]["tares_project"] = atlas
                lines += said
            else:                        # rev-0001 and rel-0001 have no counted reply
                lines += turn(sid, minute, mark, sid,
                              usage=sid not in ("rev-0001", "rel-0001"))
    for sid in PEOPLE:
        minute += 1
        lines += turn(sid, minute, SECRET, sid)
    for sid, usd in STATUS_COSTS.items():   # a status line gives one cost for the whole session
        lines.append({**head(sid, minute, 5), "type": "session_status", "status": {
            "context_window": {"context_window_size": 200000, "used_percentage": 10},
            "cost": {"total_cost_usd": usd}}})
    await ingest(app, world, lines)
    for sid in ("orc-0000", "rev-0000", "hlp-0000"):
        store.link_session(atlas, sid, None)
    for sid in ("rev-0001", "hlp-0001", "rel-0001", "mix-0001"):
        store.link_session(vault, sid, None)
    store.link_session(ASIDE, "orc-0001", None)
    store.link_session(vault, "orc-0001", None)
    store.link_session(beacon, "bea-0001", None)
    for sid in PEOPLE:
        store.link_session(vault, sid, None)
    store.link_session(LATER, "per-0002", None)   # the project it joins next
    now = datetime.now(timezone.utc)
    for sid in ("rev-0001", "mix-0001"):
        store.set_session_state(sid, "waiting", f"{SECRET} permission", now)

    await asyncio.sleep(0.5)
    return world


def found(text, world, sessions=(), name=True) -> list[str]:
    """What of the private project shows in a response."""
    marks = (SECRET, world["vault"], *((NAME,) if name else ()), *sessions)
    return [m for m in marks if m in text]


def counted(store) -> dict:
    """How many replies count for each project, for "crew" and for "other"."""
    return {r["project"] or r["kind"]: r["replies"] for r in store.usage_by_project()}


def moved(was: dict, now: dict) -> dict:
    return {k: now.get(k, 0) - was.get(k, 0) for k in {*was, *now} if now.get(k, 0) != was.get(k, 0)}


async def check_cost(app, world, before: dict):
    """`before` is `counted` as it was just before the delete."""
    store, vault = app.state.store, world["vault"]
    was = counted(store)
    ck("cost", "the delete moves no reply to another total", was == before and was.get(vault),
       moved(before, was))
    await ingest(app, world, [line for minute, sid in ((90, "per-0001"), (91, "per-0002"))
                              for line in turn(sid, minute, SECRET, f"newer-{sid}")])
    now = counted(store)
    ck("cost", "new replies count for the project joined next, else Other sessions",
       moved(was, now) == {"other": 1, LATER: 1}, (moved(was, now), now))

    # the session with no other project joins a second private one, which is then deleted too
    store.create_project(SECOND, "custom", "Quartzite", {})
    store.set_project_scope(SECOND, "private")
    store.link_session(SECOND, "per-0001", None)
    await ingest(app, world, turn("per-0001", 92, SECRET, "second-per-0001"))
    joined = counted(store)
    ck("cost", "its replies since the delete count for the second private project",
       moved(now, joined) == {"other": -1, SECOND: 2}, (moved(now, joined), joined))
    store.set_session_state("per-0001", "waiting", f"{SECRET} permission", datetime.now(timezone.utc))
    store.delete_project(SECOND)
    ck("cost", "the second delete moves no reply to another total", counted(store) == joined,
       moved(joined, counted(store)))
    ck("cost", "a session left with deleted projects only keeps no stored state",
       store.session_state("per-0001") is None, store.session_state("per-0001"))


def check_records(store):
    """What is remembered of a deleted private project."""
    older, newer = (datetime(2026, 1, day, tzinfo=timezone.utc) for day in (1, 2))
    digest = hashlib.sha256(b"Onyx").hexdigest()

    # a delete that stopped half way had taken the source out of the project already
    store.create_project("uc_opal", "custom", "Opal", {})
    store.set_project_scope("uc_opal", "private")
    store._change_deleted_private(lambda d: d.update(
        {"uc_opal": {"name": digest, "at": older.isoformat(), "sources": ["a"]}}))
    store.delete_project("uc_opal")
    got = store._deleted_records()["uc_opal"]
    ck("source", "the delete done again keeps the sources noted the first time",
       got["sources"] == ["a"] and got["at"] > newer.isoformat(), got)
    store._change_deleted_private(lambda d: d.pop("uc_opal"))


async def check_shared(app, world, who, when):
    """A person's session that worked in a public project and in the private one is not listed
    in the public one (TR-1710), after the delete too."""
    pid, sid = LATER, "per-0002"   # a crew station's session is never listed in a project
    body = (await get(app, world, who, f"/api/projects/{pid}/sessions")).json()
    listed = [s["session"] for s in body["sessions"]]
    one = await get(app, world, who, f"/api/projects/{pid}/sessions/{sid}")
    ck("shared", f"{who}, {when}: {sid} is not listed in {pid} and its lines are not given",
       any(s["session"] == sid for s in app.state.store.project_sessions(pid))
       and sid not in listed and one.status_code == 404 and SECRET not in one.text,
       (listed, one.status_code))


async def get(app, world, who, path, **params):
    async with client(app, world["who"][who]) as cx:
        return await cx.get(path, params=params or None)


def rows_of(body) -> dict:
    return {s["name"]: s for s in body.get("stations") or []}


async def check_stations(app, world, who, when, hidden, group="stations", name=True):
    """The four station reads show nothing of the hidden sessions."""
    stations = sorted({*STATIONS, *hidden.values()})
    label = f"{when}, {who}"
    for path in ("/api/crew", "/api/factory/crew", f"/api/projects/{world['atlas']}/crew"):
        r = await get(app, world, who, path)
        got = found(r.text, world, hidden, name)
        ck(group, f"{label}: GET {path.replace(world['atlas'], 'atlas')} shows nothing of it",
           r.status_code == 200 and not got, (r.status_code, got))
    for station in stations:
        r = await get(app, world, who, f"/api/crew/{station}", recent=200, conversation=500)
        got = found(r.text, world, hidden, name)
        ck(group, f"{label}: GET /api/crew/{station} shows nothing of it",
           r.status_code in (200, 404) and not got, (r.status_code, got))
        r = await get(app, world, who, f"/api/factory/crew/{world['crew']}/{station}/sessions",
                      limit=200)
        got = found(r.text, world, hidden, name)
        ck(group, f"{label}: the sessions of {station} show nothing of it",
           r.status_code in (200, 404) and not got, (r.status_code, got))
    for sid, station in hidden.items():
        for tool in [f"t-{sid}", *world["late_tools"].get(sid, ())]:
            r = await get(app, world, who, f"/api/crew/{station}/tool/{tool}")
            ck(group, f"{label}: GET /api/crew/{station}/tool/{tool} is not there",
               r.status_code == 404 and not found(r.text, world, (), name),
               (r.status_code, r.text))


async def check_open(app, world, who, when):
    """The earlier open session of a station that is still listed reads as before."""
    for station, sid in (("crew-reviewer", "rev-0000"), ("shop-help-m1", "hlp-0000"),
                         ("shop-build-m1", "mix-0000")):
        r = await get(app, world, who, f"/api/factory/crew/{world['crew']}/{station}/sessions",
                      limit=200)
        got = [s["session"] for s in r.json().get("sessions", [])] if r.status_code == 200 else []
        ck("same", f"{when}, {who}: the open session of {station} is listed", got == [sid],
           (r.status_code, got))


async def check_owner(app, world, when, joined=None):
    for sid in OWNERS_READ:
        station = HIDDEN[sid]
        r = await get(app, world, "kim", f"/api/crew/{station}/tool/t-{sid}")
        ck("same", f"{when}, kim owns the crew and reads {station}'s tool call",
           r.status_code == 200 and f"{SECRET} {sid} output" in r.text, (r.status_code, r.text))
        r = await get(app, world, "kim", f"/api/crew/{station}", recent=200, conversation=500)
        ck("same", f"{when}, kim reads {station}'s current session",
           r.status_code == 200 and r.json()["session"] == sid
           and f"{SECRET} {sid} said" in r.text, (r.status_code, r.text))
    for station in ("crew-releaser", "shop-build-m2"):    # no project but the private one
        r = await get(app, world, "kim", f"/api/crew/{station}")
        ck("same", f"{when}, {station} is not listed for kim either", r.status_code == 404,
           (r.status_code, r.text))
        r = await get(app, world, "kim", f"/api/factory/crew/{world['crew']}/{station}/sessions")
        ck("same", f"{when}, {station} has no page of sessions for kim", r.status_code == 404,
           (r.status_code, r.text))
    crew = rows_of((await get(app, world, "kim", "/api/crew")).json())
    # orc-0001 joined the private project last, so its cost is not for kim; mix-0001 works in Atlas
    for station, sid, want in (("crew-orchestrator", "orc-0001", None),
                               ("shop-build-m1", "mix-0001", STATUS_COSTS["mix-0001"])):
        row = crew.get(station) or {}
        context = row.get("context")
        ck("cost", f"{when}, kim and the status line cost of {station}",
           row.get("session") == sid and context is not None and context["cost_usd"] == want,
           (row.get("session"), context))
    # the project a row names: none while the one joined last is private, then the one before it
    row = crew.get("crew-orchestrator") or {}
    ck("same", f"{when}, the project of crew-orchestrator's row",
       row.get("project") == joined, (row.get("project"), joined))


def check_waiting(world, fx, when, owner_reads=False):
    """What waits for a person. `owner_reads`: the private project is still there and the crew is
    kim's. Kim reads why the station of their own project waits; the reviewer, whose only project
    is the private one, gives no reason."""
    items = fx.waiting_for_person({"id": world["pid"]["kim"], "is_admin": False})
    terminal = [i for i in items if i.get("kind") == "terminal"]
    s = json.dumps(items, default=str)
    ck("waiting", f"{when}: what waits for kim lists the stations waiting in a terminal",
       len(terminal) == 2, items)
    if owner_reads:
        shown = {i["holds_up"]: i["detail"] for i in terminal}
        ck("waiting", f"{when}: and kim, whose crew it is, reads what they read before",
           shown == {"shop-build-m1": f"{SECRET} permission.",
                     "crew-reviewer": "It waits for you in its terminal."}, shown)
    else:
        got = found(s, world)
        ck("waiting", f"{when}: and says nothing of the private project", not got, (got, s))
    theirs = fx.waiting_for_person({"id": world["pid"]["ann"], "is_admin": False})
    s = json.dumps(theirs, default=str)
    got = found(s, world)
    ck("waiting", f"{when}: ann, neither the crew's owner nor a member, is told of no station "
       "waiting in a terminal", not [i for i in theirs if i.get("kind") == "terminal"], s)
    ck("waiting", f"{when}: and nothing of the private project reaches ann", not got, (got, s))


async def main():
    from tares import factory_api
    fx = {}
    register = factory_api.register

    def keeping_fx(app, h):
        fx["fx"] = register(app, h)
        return fx["fx"]

    factory_api.register = keeping_fx
    from tares.daemon import make_app
    app = make_app()
    store = app.state.store
    paths = (("the sessions table", None),)

    async def on_both_paths(check):
        for path_name, caught in paths:
            pass
            try:
                await check(path_name)
            finally:
                pass

    async with app.router.lifespan_context(app):
        world = await seed(app)
        world["late_tools"] = {}
        crew = rows_of((await get(app, world, "root", "/api/crew")).json())
        world["crew"] = crew["shop-build-m1"]["crew_id"]
        vault = world["vault"]

        for who in ("ann", "bot"):
            await check_shared(app, world, who, "before the delete")

        async def before(path_name):
            for who in ("ann", "bot"):
                await check_stations(app, world, who, f"before, {path_name}", HIDDEN)
                await check_open(app, world, who, f"before, {path_name}")
            await check_owner(app, world, f"before, {path_name}")
        await on_both_paths(before)
        check_waiting(world, fx["fx"], "before the delete", owner_reads=True)

        counted_before = counted(store)
        async with client(app, world["who"]["lee"]) as cx:
            r = await cx.delete(f"/api/projects/{vault}")
            assert r.status_code < 300 and store.get_project(vault) is None, r.text

        async def after(path_name):
            for who in ("ann", "bot", "mo", "lee"):
                await check_stations(app, world, who, f"after, {path_name}", HIDDEN)
                await check_open(app, world, who, f"after, {path_name}")
            await check_owner(app, world, f"after, {path_name}", ASIDE)
        await on_both_paths(after)
        for who in ("ann", "bot"):
            await check_shared(app, world, who, "after the delete")
        check_waiting(world, fx["fx"], "after the delete")
        check_records(store)
        await check_cost(app, world, counted_before)

        # later lines that name the project by its name only, and a session first heard of now
        late = []
        for minute, sid in ((100, "rev-0001"), (101, "rel-0001")):
            late += turn(sid, minute, SECRET, f"late-{sid}", project=NAME, usage=False)
            world["late_tools"][sid] = [f"t-late-{sid}"]
        late.append(station_line(LATE, "builder", "late-0001", 102, NAME))
        late += turn("late-0001", 102, SECRET, "late-0001", project=NAME)
        # a newer open session makes rev-0001 an earlier one, which the sessions page lists
        late.append(station_line("crew-reviewer", "reviewer", "rev-0002", 105, None))
        late += turn("rev-0002", 105, "open-work", "rev-0002", usage=False)
        await ingest(app, world, late)
        hidden = {**HIDDEN, "late-0001": LATE}

        async def after_late(path_name):
            for who in ("ann", "bot"):
                await check_stations(app, world, who, f"later lines, {path_name}", hidden)
        await on_both_paths(after_late)
        r = await get(app, world, "kim",
                      f"/api/factory/crew/{world['crew']}/crew-reviewer/sessions", limit=200)
        listed = [s["session"] for s in r.json().get("sessions", [])] if r.status_code == 200 else []
        ck("same", "later lines, kim owns the crew and lists the reviewer's earlier session",
           "rev-0001" in listed, (r.status_code, listed))

        # Ann makes a new project of the same name
        new = await make_project(app, world, "ann", NAME)
        again = []
        for minute, sid in ((110, "rev-0001"), (111, "rel-0001")):
            again.append({**head(sid, minute, 0, NAME), "type": "session_project"})
            again += turn(sid, minute, SECRET, f"again-{sid}", project=NAME)
            world["late_tools"][sid].append(f"t-again-{sid}")
        again.append(station_line("shop-build-m3", "builder", "new-0001", 112, NAME))
        again += turn("new-0001", 112, "open-work", "new-0001", project=NAME)
        await ingest(app, world, again)
        await asyncio.sleep(0.5)

        async def reused(path_name):
            for who in ("ann", "bot"):
                await check_stations(app, world, who, f"name taken again, {path_name}", hidden,
                                     "reuse", name=False)
            r = await get(app, world, "ann", f"/api/projects/{new}/crew")
            ck("reuse", f"{path_name}: the new project's crew has its own station only",
               r.status_code == 200 and "shop-build-m3" in rows_of(r.json())
               and not found(r.text, world, hidden, False), (r.status_code, r.text))
            r = await get(app, world, "ann", "/api/crew/shop-build-m3/tool/t-new-0001")
            ck("reuse", f"{path_name}: ann reads a session of the new project",
               r.status_code == 200 and "open-work new-0001 output" in r.text,
               (r.status_code, r.text))
        await on_both_paths(reused)

        # replies sent as lines of their own, in one push under the one name: each counts for its
        # session's project
        await ingest(app, world, [
            {**head(sid, 120, 5, NAME), "type": "session_usage", "message_id": f"own-{sid}",
             "model": "claude-opus-5-5",
             "usage": {"input_tokens": 2, "output_tokens": 3,
                       "cache_creation_input_tokens": 10, "cache_read_input_tokens": 100}}
            for sid in ("rev-0001", "new-0001")])
        from sqlalchemy import select
        from tares.claude_usage_store import session_usage
        with store.sessions_db.begin() as conn:
            own = {r.session: r.project for r in conn.execute(
                select(session_usage.c.session, session_usage.c.project)
                .where(session_usage.c.message.like("own-%")))}
        ck("reuse", "a reply line of the new project's session counts for the new project",
           own.get("new-0001") == new, (own, new))
        ck("reuse", "a reply line of the deleted project's session still counts for it",
           own.get("rev-0001") == vault, (own, vault))

    for group in GROUPS:
        print(f"[{group}] {TALLY[group][0]} passed, {TALLY[group][1]} failed")
    passed, failed = (sum(t[i] for t in TALLY.values()) for i in (0, 1))
    print(f"{passed} passed, {failed} failed")
    return failed


if __name__ == "__main__":
    sys.exit(1 if asyncio.run(main()) else 0)
