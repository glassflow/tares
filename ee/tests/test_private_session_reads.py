# Tares enterprise licence, see ee/LICENSE
"""Session reads follow project visibility (TR-1710): the session routes of a project, the build
hand-over and the crew's context requests.

Each caller reads a session as their place on its projects allows, on a cell whose sessions table
has caught up and on the events scan. Auth off is test_private_session_reads_open.

Run: .venv/bin/python ee/tests/test_private_session_reads.py
Postgres: TARES_TEST_PG=postgresql://... PYTHONPATH=tests/pg_harness .venv/bin/python ee/tests/test_private_session_reads.py
"""
import asyncio
import json
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_private_station_reads as base   # its licensed cell and HTTP helpers
from test_private_station_reads import client, get, nd

import tares.mcp_server as mcp

READS = ("session", "said", "pickup", "asks", "ask", "same")
TALLY = {r: [0, 0] for r in READS}          # read -> [passed, failed]
SECRET = "zz-secret"                        # in the text and files of a session tied to the vault
TIES = ("station", "link", "reply", "plain")   # what ties a case's hidden session to the vault
STATIONS = ("station", "link", "reply")        # the cases whose hidden session is a station's
HIDDEN = [f"hid-{c}" for c in TIES] + ["hid-local"]
OUTSIDERS = ("ann", "bot", "casekey", "public")


def ck(read, label, cond, detail=""):
    TALLY[read][0 if cond else 1] += 1
    print(("  ok   " if cond else "  FAIL ") + f"[{read}] {label}"
          + ("" if cond else f"  {str(detail)[:300]}"))


def report() -> int:
    for read in READS:
        print(f"[{read}] {TALLY[read][0]} passed, {TALLY[read][1]} failed")
    passed, failed = (sum(t[i] for t in TALLY.values()) for i in (0, 1))
    print(f"{passed} passed, {failed} failed")
    return failed


def ts(minutes: float) -> str:
    """An ISO timestamp `minutes` from now (negative: ago), as Claude Code writes them."""
    return base.iso(datetime.now(timezone.utc) + timedelta(minutes=minutes))


def station_line(sid, name, project, at, machine):
    return {"type": "session_station", "sessionId": sid, "cwd": "/w/atlas", "timestamp": ts(at),
            "station": name, "role": "builder", "parent": "crew-orchestrator",
            "factory_station": name, "factory_crew": "crew", "crew": "crew",
            "factory_machine": machine, "machine": machine,
            **({"tares_project": project} if project else {})}


def work(sid, at, project=None):
    """A reply the session wrote and a file it edited. With `project`, the reply counts for it."""
    mark = f"{SECRET if sid.startswith('hid-') else 'open-work'}-{sid}"
    head = {"sessionId": sid, "cwd": "/w/atlas", "type": "assistant"}
    said = {**head, "timestamp": ts(at), "message": {
        "id": f"m-{sid}-{at}", "role": "assistant", "model": "claude-opus-5-5",
        "content": [{"type": "text", "text": f"{mark} said"}],
        "usage": {"input_tokens": 2, "output_tokens": 3}}}
    if project:
        said["tares_project"] = project
    edit = {**head, "timestamp": ts(at + 0.01), "message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": f"t-{sid}-{at}", "name": "Edit",
         "input": {"file_path": f"/w/atlas/{mark}.py", "old_string": "a", "new_string": "b"}}]}}
    return [said, edit]


def link_line(sid, at, project):
    return {"type": "session_project", "sessionId": sid, "cwd": "/w/atlas", "timestamp": ts(at),
            "tares_project": project}


def waiting(sid, at):
    return {"type": "session_state", "sessionId": sid, "state": "waiting",
            "reason": "the turn ended", "timestamp": ts(at)}


async def ingest(app, headers, lines):
    async with client(app, {**headers, "Content-Type": "application/x-ndjson"}) as cx:
        r = await cx.post("/ingest/claude_code", content=nd(lines))
        assert r.status_code < 300, r.text


async def seed(app, who, lead, owner) -> dict:
    """The projects, the owner's sessions in them and the asks for their readings. `lead` makes the
    private project, `owner` records the sessions."""
    store = app.state.store
    ids, tickets = {}, {}
    async with client(app, who[owner]) as cx:
        r = await cx.post("/api/sources", json={"name": "claude_code", "connector": "claude_code",
                                                "poll": "10s", "config": {"push": True}})
        assert r.status_code < 300, r.text

    async def make_project(person, name):
        async with client(app, who[person]) as cx:
            r = await cx.post("/api/projects", json={"template": "custom", "name": name,
                                                     "objects": [], "kind": "software_factory"})
            assert r.status_code in (200, 201), r.text
            return r.json()["id"]

    ids["vault"] = vault = await make_project(lead, "Zephyrvault")
    async with client(app, who[lead]) as cx:
        r = await cx.patch(f"/api/projects/{vault}", json={"scope": "private"})
        assert r.status_code < 300 and store.get_project(vault)["scope"] == "private", r.text
    for case in (*TIES, "open"):
        ids[case] = await make_project(owner, f"Atlas {case}")
        async with client(app, who[owner]) as cx:
            r = await cx.post(f"/api/projects/{ids[case]}/tickets", json={"title": f"Build {case}"})
            assert r.status_code < 300, r.text
            tickets[case] = r.json()["id"]

    # before the ticket goes in progress: an older open session, and the one that will be on it
    lines = []
    for case in (*TIES, "open"):
        sid = f"open-{case}"
        lines += [station_line(sid, f"app-open-{case}", ids[case], -50, "kim-mbp"),
                  link_line(sid, -50, ids[case]), *work(sid, -49.5), waiting(sid, -49)]
    for case in TIES:
        sid = f"hid-{case}"
        if case != "plain":
            lines.append(station_line(sid, f"app-build-{case}",
                                      vault if case == "station" else ids[case], -40, "kim-mbp"))
        lines += [link_line(sid, -40, ids[case]), *work(sid, -39)]
    await ingest(app, who[owner], lines)
    async with client(app, who[owner]) as cx:
        for case in TIES:
            r = await cx.put(f"/api/projects/{ids[case]}/tickets/{tickets[case]}",
                             json={"status": "in_progress"})
            assert r.status_code < 300, r.text
        r = await cx.put(f"/api/projects/{ids['open']}/tickets/{tickets['open']}",
                         json={"status": "in_progress"})
        assert r.status_code < 300, r.text
    lines = []
    for case in TIES:
        sid = f"hid-{case}"
        lines += [*work(sid, 0.2, vault if case in ("reply", "plain") else None), waiting(sid, 0.3)]
    await ingest(app, who[owner], lines)
    await asyncio.sleep(0.5)
    store.link_session(vault, "hid-link", None)
    asked = datetime.now(timezone.utc) - timedelta(minutes=1)
    for sid in [f"hid-{c}" for c in STATIONS] + [f"open-{c}" for c in (*TIES, "open")]:
        store.ask_session_context(sid, asked)
    return {"who": who, **ids}


async def seed_local(app, world):
    """Two stations of the person `local`, recorded with the cell's own credential: one session
    with a reply that counts for the vault and one open, each with an ask waiting."""
    lines = []
    for sid, name, project in (("hid-local", "app-local-hidden", world["vault"]),
                               ("open-local", "app-local-open", None)):
        lines += [station_line(sid, name, None, -5, "local-mbp"), *work(sid, -4, project),
                  waiting(sid, -3)]
    await ingest(app, world["who"]["root"], lines)
    await asyncio.sleep(0.5)
    for sid in ("hid-local", "open-local"):
        app.state.store.ask_session_context(sid, datetime.now(timezone.utc) - timedelta(minutes=1))


def as_caller(who, case):
    """`casekey` is the key of the case's own team project."""
    return f"key-{case}" if who == "casekey" else who


def leaked(body) -> list[str]:
    """What of a hidden session shows in a response body."""
    s = json.dumps(body, default=str)
    return [x for x in (*HIDDEN, SECRET) if x in s]


async def project_reads(app, world, who, case, sid):
    """The first three reads of `sid` in the case's project: (the session, the list, the
    hand-over)."""
    base_path = f"/api/projects/{world[case]}"
    caller = as_caller(who, case)
    return (await get(app, world, caller, f"{base_path}/sessions/{sid}"),
            await get(app, world, caller, f"{base_path}/sessions"),
            await get(app, world, caller, f"{base_path}/pickup"))


async def check_hidden(app, world, who, case, label):
    """The first three reads show `who` nothing of the case's hidden session."""
    sid = f"hid-{case}"
    one, listed, pick = await project_reads(app, world, who, case, sid)
    none = await get(app, world, as_caller(who, case),
                     f"/api/projects/{world[case]}/sessions/none-{case}")
    ck("session", f"{label}: {case}'s hidden session answers as one that is not there",
       one.status_code == none.status_code == 404 and SECRET not in one.text
       and one.text.replace(sid, f"none-{case}") == none.text, (one.status_code, one.text))
    ids = [s["session"] for s in listed.json().get("sessions", [])]
    ck("said", f"{label}: {case}'s list has no id and nothing said of the hidden session",
       listed.status_code == 200 and ids == [f"open-{case}"] and not leaked(listed.json()),
       (listed.status_code, ids, leaked(listed.json())))
    body = pick.json()
    ck("pickup", f"{label}: {case}'s hand-over has no previous session and none in its place",
       pick.status_code == 200 and body["previous_session"] is None and not leaked(body)
       and f"open-{case}" not in pick.text, (pick.status_code, body.get("previous_session")))
    ck("same", f"{label}: {case}'s hand-over still says a session may be open on the ticket",
       body.get("may_be_open") is True and body.get("in_progress") is True and body.get("ticket"),
       body)


async def check_shown(app, world, who, case, sid, label, read=None):
    """The first three reads give `who` all of the session; `read` files the checks under one
    tally instead of each read's own."""
    mark = f"{SECRET if sid.startswith('hid-') else 'open-work'}-{sid}"
    one, listed, pick = await project_reads(app, world, who, case, sid)
    ck(read or "session", f"{label}: {sid} reads with its lines",
       one.status_code == 200 and one.json()["session"] == sid and mark in one.text,
       (one.status_code, one.text))
    rows = {s["session"]: s for s in listed.json().get("sessions", [])}
    ck(read or "said", f"{label}: {sid} is listed with what it said last",
       sid in rows and rows[sid].get("state") == "waiting"
       and mark in (rows[sid].get("last_said") or ""), (listed.status_code, rows.get(sid)))
    p = pick.json().get("previous_session") or {}
    ck(read or "pickup", f"{label}: {sid} is the session the hand-over names, with words and files",
       p.get("session") == sid and any(mark in m["text"] for m in p.get("said_lately", []))
       and f"/w/atlas/{mark}.py" in p.get("files_touched", [])
       and pick.json()["may_be_open"] is True, (pick.status_code, p))


async def asked_stations(app, world, who) -> dict:
    r = await get(app, world, who, "/api/crew/context/asks")
    assert r.status_code == 200, (who, r.status_code, r.text)
    return {a["session"]: a["station"] for a in r.json()["asks"]}


async def check_asks(app, world, path_name):
    kims = {f"hid-{c}": f"app-build-{c}" for c in STATIONS}
    local = {"hid-local": "app-local-hidden"}
    opened = {**{f"open-{c}": f"app-open-{c}" for c in (*TIES, "open")},
              "open-local": "app-local-open"}
    for who in ("ann", "bot", "public"):
        got = await asked_stations(app, world, who)
        for sid in (*kims, *local):
            ck("asks", f"{who}, {path_name}: the ask of {sid} is not listed", sid not in got, got)
        ck("same", f"{who}, {path_name}: every ask of an open session is listed",
           opened.items() <= got.items(), got)
        ck("asks", f"{who}, {path_name}: and nothing else is", set(got) <= set(opened),
           sorted(set(got) - set(opened)))
    for who, want in (("kim", {**kims, **opened}), ("lee", {**kims, **local, **opened}),
                      ("mo", {**kims, **local, **opened}),
                      ("root", {**kims, **local, **opened})):
        got = await asked_stations(app, world, who)
        ck("asks", f"{who}, {path_name}: lists what is theirs to read", got == want,
           sorted(set(got) ^ set(want)))


async def ask(app, world, who, station, sid):
    """POST the ask. Returns (the response, whether the ask was recorded on `sid`)."""
    store = app.state.store

    def stamp():
        return store.session_meters([sid]).get(sid, {}).get("context_asked_at")

    before = stamp()
    await asyncio.sleep(0.01)
    async with client(app, world["who"][who]) as cx:
        r = await cx.post(f"/api/crew/{station}/context")
    return r, stamp() != before


async def check_ask(app, world, path_name):
    tied = [(f"app-build-{c}", f"hid-{c}") for c in ("link", "reply")]
    own = ("app-build-station", "hid-station")      # its only project is the vault
    local = ("app-local-hidden", "hid-local")
    for who, rows in (("public", [*tied, local]), ("bot", [local])):
        for station, sid in rows:
            r, recorded = await ask(app, world, who, station, sid)
            ck("ask", f"{who}, {path_name}: asking {station} answers no session id",
               r.status_code == 200 and r.json().get("session", "missing") is None
               and r.json().get("asked_at") and not leaked(r.json()), (r.status_code, r.text))
            ck("same", f"{who}, {path_name}: and the ask of {station} is recorded", recorded)
    for who, rows in (("public", [("app-open-open", "open-open")]),
                      ("bot", [("app-local-open", "open-local")]),
                      ("kim", tied), ("root", [own, *tied, local])):
        for station, sid in rows:
            r, recorded = await ask(app, world, who, station, sid)
            ck("ask", f"{who}, {path_name}: asking {station} answers its session id",
               r.status_code == 200 and r.json()["session"] == sid and recorded,
               (r.status_code, r.text))
    for who in ("ann", "bot", "public", "kim"):
        r, recorded = await ask(app, world, who, *own)
        ck("same", f"{who}, {path_name}: the station of the vault alone is still not found",
           r.status_code == 404 and not recorded and not leaked(r.json()),
           (r.status_code, r.text))
    for who in ("ann", "lee", "mo", "bot"):
        r, recorded = await ask(app, world, who, *tied[0])
        ck("same", f"{who}, {path_name}: still may not ask another person's station",
           r.status_code == 403 and not recorded and not leaked(r.json()),
           (r.status_code, r.text))


async def check_take_over(app, world):
    """The take-over marks the session replaced for every caller allowed to make it."""
    store = app.state.store

    def reset(sid):
        store.set_session_state(sid, "waiting", "the turn ended",
                                datetime.now(timezone.utc), force=True)

    for who in ("ann", "bot", "public", "kim", "lee", "mo", "root"):
        for case in TIES:
            # kim owns the stations and is not on the vault: the plain session is no station's
            outsider = who in OUTSIDERS or (who == "kim" and case == "plain")
            sid, path = f"hid-{case}", f"/api/projects/{world[case]}/pickup"
            async with client(app, world["who"][who]) as cx:
                refused = await cx.post(path, json={})
                state_kept = store.session_state(sid)["state"]
                taken = await cx.post(path, json={"take_over": True})
            detail = refused.json().get("detail", "")
            ck("same", f"{who}: {case}'s take-over still needs take_over while it may be open",
               refused.status_code == 409 and "may still be open" in detail
               and "take_over" in detail and state_kept == "waiting",
               (refused.status_code, detail))
            body = taken.json()
            replaced = taken.status_code == 200 and store.session_state(sid)["state"] == "replaced"
            ck("same", f"{who}: {case}'s take-over marks the session replaced", replaced,
               (taken.status_code, taken.text))
            if outsider:
                ck("pickup", f"{who}: {case}'s refusal says nothing of the session's state",
                   "waiting" not in detail and not leaked(refused.json()), detail)
                ck("pickup", f"{who}: {case}'s take-over answers nothing of the session",
                   body.get("previous_session", "missing") is None
                   and body.get("taken_over", "missing") is None and not leaked(body),
                   {k: body.get(k) for k in ("previous_session", "taken_over")})
            else:
                ck("pickup", f"{who}: {case}'s refusal names the state", "(waiting)" in detail,
                   detail)
                ck("pickup", f"{who}: {case}'s take-over names the session it took over",
                   body.get("taken_over") == sid
                   and (body.get("previous_session") or {}).get("session") == sid,
                   {k: body.get(k) for k in ("previous_session", "taken_over")})
            reset(sid)

    real = mcp.TARESD, mcp._cx
    mcp.TARESD = "http://t"
    try:
        for who in ("ann", "mo"):
            mcp._cx = lambda timeout=10, who=who: client(app, world["who"][who])
            out = await mcp.pick_up(project=world["station"])
            if who == "ann":
                ck("pickup", "ann: the pick_up tool stops and says nothing of the session",
                   out.startswith("STOP: the session that was building may still be open.")
                   and "take_over=true" in out and "None" not in out
                   and "## The session that was on it" not in out and not leaked(out), out[:400])
            else:
                ck("pickup", "mo: the pick_up tool stops with the session's state and words",
                   out.startswith("STOP") and "(state waiting, last active " in out
                   and f"{SECRET}-hid-station said" in out, out[:400])
    finally:
        mcp.TARESD, mcp._cx = real


async def owner_reads(app, world, who, case) -> dict:
    """What the five reads give the station's owner of the case's hidden session, by read."""
    sid, station = f"hid-{case}", f"app-build-{case}"
    one, listed, pick = await project_reads(app, world, who, case, sid)
    row = next((s for s in listed.json().get("sessions", []) if s["session"] == sid), {})
    prev = pick.json().get("previous_session") or {}
    asks = await get(app, world, who, "/api/crew/context/asks")
    posted, recorded = await ask(app, world, who, station, sid)
    return {
        "session": [one.status_code, one.json().get("lines")],
        "said": {k: row.get(k) for k in ("session", "state", "last_said", "lines")},
        "pickup": {**{k: v for k, v in prev.items() if k != "quiet_minutes"},
                   "may_be_open": pick.json().get("may_be_open")},
        "asks": [a for a in asks.json()["asks"] if a["session"] == sid],
        "ask": [posted.status_code, posted.json().get("session"), recorded],
    }


def check_whole(case, got):
    """The owner reads all of it: what the reads after the delete are compared with."""
    sid = f"hid-{case}"
    asked = [404, None, False] if case == "station" else [200, sid, True]
    ck("same", f"before the delete: the owner reads all of {sid}",
       got["session"][0] == 200 and SECRET in json.dumps(got["session"][1])
       and SECRET in (got["said"]["last_said"] or "") and got["pickup"].get("session") == sid
       and got["asks"] and got["ask"] == asked, got)


async def check_deleted(app, world):
    """Once the private project is deleted nobody is on it: the station's owner reads its sessions
    as before, everyone else gets none of them."""
    before = {}
    for case in ("station", "reply"):
        before[case] = await owner_reads(app, world, "kim", case)
        check_whole(case, before[case])
    async with client(app, world["who"]["lee"]) as cx:
        r = await cx.delete(f"/api/projects/{world['vault']}")
    ck("same", "the private project is deleted through the API",
       r.status_code < 300 and app.state.store.get_project(world["vault"]) is None,
       (r.status_code, r.text))
    for case, want in before.items():
        got = await owner_reads(app, world, "kim", case)
        for read in READS[:-1]:
            ck(read, f"after the delete: the owner reads hid-{case} as before",
               got[read] == want[read], (want[read], got[read]))
    for case in ("station", "reply", "plain"):
        sid = f"hid-{case}"
        for who in ("mo", "root"):
            await check_hidden(app, world, who, case, f"{who}, after the delete")
            if case != "plain":     # no station, so never among the asks
                got = await asked_stations(app, world, who)
                ck("asks", f"{who}, after the delete: the ask of {sid} is not listed",
                   sid not in got, got)
    r, recorded = await ask(app, world, "root", "app-build-reply", "hid-reply")
    ck("ask", "root, after the delete: asking for hid-reply answers no session id",
       r.status_code == 200 and r.json()["session"] is None and recorded,
       (r.status_code, r.text))


async def keys(app) -> dict:
    """The cell's credential, plain and narrowed to the public, and a key for each person and
    for nobody."""
    root = {"Authorization": f"Bearer {base.TOKEN}"}
    who = {"root": root, "public": {**root, "x-tares-viewer": "public"}}
    pid = {}
    async with client(app, root) as cx:
        for name in ("kim", "lee", "mo", "ann", "bot"):
            body = {"name": f"{name}-laptop", "scopes": ["admin", "ingest", "read"]}
            if name != "bot":   # the bot's key has no person
                body["person"] = {"email": f"{name}-psr@example.com", "name": name.capitalize()}
            r = await cx.post("/api/keys", json=body)
            assert r.status_code < 300, r.text
            who[name] = {"Authorization": f"Bearer {r.json()['secret']}"}
            if name != "bot":
                pid[name] = r.json()["person"]["id"]
    return {"who": who, "pid": pid}


async def main():
    from tares import session_ingest
    from tares.daemon import make_app
    app = make_app()
    store = app.state.store
    async with app.router.lifespan_context(app):
        people = await keys(app)
        world = await seed(app, people["who"], "lee", "kim")
        store.add_project_member(world["vault"], people["pid"]["mo"])
        for case in (*TIES, "open"):
            async with client(app, world["who"]["kim"]) as cx:
                r = await cx.post(f"/api/projects/{world[case]}/keys", json={"name": "agent"})
                assert r.status_code < 300, r.text
                world["who"][f"key-{case}"] = {"Authorization": f"Bearer {r.json()['secret']}"}
        await seed_local(app, world)
        ck("same", "the sessions table has caught up", session_ingest.caught_up(store))
        every = [*HIDDEN, "open-local", *(f"open-{c}" for c in (*TIES, "open"))]
        tied = store.sessions_in(every, {world["vault"]})
        ck("same", "the sessions tied to the vault are the hidden ones", tied == set(HIDDEN),
           sorted(tied ^ set(HIDDEN)))

        real = session_ingest.caught_up
        for path_name, caught in (("caught up", real), ("events scan", lambda s: False)):
            session_ingest.caught_up = caught
            try:
                for case in TIES:
                    for who in OUTSIDERS:
                        await check_hidden(app, world, who, case, f"{who}, {path_name}")
                        await check_shown_open(app, world, who, case, f"{who}, {path_name}")
                    for who in ("kim", "lee", "mo", "root"):
                        label = f"{who}, {path_name}"
                        # Kim recorded the plain session, which is no station's: no owner to be
                        if who == "kim" and case == "plain":
                            await check_hidden(app, world, who, case, label)
                        else:
                            await check_shown(app, world, who, case, f"hid-{case}", label)
                for who in (*OUTSIDERS, "kim", "mo", "root"):
                    await check_shown(app, world, who, "open", "open-open",
                                      f"{who}, {path_name}", read="same")
                await check_asks(app, world, path_name)
                await check_ask(app, world, path_name)
            finally:
                session_ingest.caught_up = real
        await check_take_over(app, world)
        await check_deleted(app, world)
    return report()


async def check_shown_open(app, world, who, case, label):
    """Beside a hidden session, the open one in the same project still reads."""
    sid = f"open-{case}"
    one = await get(app, world, as_caller(who, case),
                    f"/api/projects/{world[case]}/sessions/{sid}")
    ck("same", f"{label}: {sid} still reads", one.status_code == 200
       and f"open-work-{sid}" in one.text, (one.status_code, one.text))


if __name__ == "__main__":
    sys.exit(1 if asyncio.run(main()) else 0)
