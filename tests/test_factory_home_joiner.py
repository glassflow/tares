"""The `joiner` block of GET /api/home (TR-1719): is the caller joining a factory someone else
set up?

{joining, team_settings, team_crews, own_crew_up}. A person other than the owner is joining when
the cell's own crew settings name an autonomy and another person's crew has reported. The owner and
a key with no person get nothing about a team. A person joining also gets `team` (TR-1646),
{people, projects}; nobody else's block has that key. Everything goes through the real GET /api/home
with real keys, but for the shapes no recording makes, which call joiner_block and joiner_team
directly.

Run: .venv/bin/python tests/test_factory_home_joiner.py
"""
import asyncio
import hashlib
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _env import scrub_os_environ
scrub_os_environ()

_TMP = tempfile.mkdtemp(prefix="tares-factory-joiner-")
os.environ["TARES_DB"] = os.path.join(_TMP, "api.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(_TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-joiner"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
os.environ["TARES_EDITION"] = "factory"

import httpx

from tares import factory_home as fh

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def ts(minutes_ago: float) -> str:
    t = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return t.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def station(crew, role, machine, ago=2, state="idle"):
    """The two lines one station of `crew` records: who it is, and the state it is in."""
    sid, name = f"s-{crew}-{role}", f"{crew}-{role}"
    return [{"type": "session_station", "sessionId": sid, "cwd": "/w/crew", "timestamp": ts(ago),
             "station": name, "role": role, "parent": "", "factory_station": name,
             "factory_crew": crew, "crew": crew, "factory_machine": machine, "machine": machine},
            {"type": "session_state", "sessionId": sid, "cwd": "/w/crew", "timestamp": ts(ago),
             "state": state, "reason": ""}]


NOTHING = {"joining": False, "team_settings": None, "team_crews": 0}
TEAM = {"autonomy": "L3-review", "releaser": False, "builders_max": 3, "challenger": True}


def about_team(j: dict) -> dict:
    return {k: j.get(k) for k in NOTHING}


def people_of(team: dict) -> dict:
    return {r["id"]: r for r in team.get("people") or []}


def projects_of(team: dict) -> dict:
    return {r["name"]: r for r in team.get("projects") or []}


def when(v: str | None):
    return datetime.fromisoformat(v.replace("Z", "+00:00")) if v else None


def team_rows_no_recording_makes():
    """joiner_team on rows the daemon does not make: every station state, stations of no person,
    empty names and times, and equal names."""
    print("== team rows no recording makes ==")
    reads = {"people": 0, "members": []}

    def team(people, stations=(), projects=(), members=None):
        def list_people():
            reads["people"] += 1
            return list(people)

        def members_by_project(ids):
            reads["members"].append(list(ids))
            return members or {}

        store = SimpleNamespace(list_people=list_people, members_by_project=members_by_project)
        return fh.joiner_team(store, list(stations), list(projects), "p_me")

    def who(pid, name="Same Name", email=None, admin=False):
        return {"id": pid, "name": name, "email": email, "is_admin": admin}

    seen = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)

    def at_station(person, state="idle", last_at=seen):
        return {"person": person, "state": state, "last_at": last_at}

    def running(state):
        return team([who("p_a")], [at_station({"id": "p_a"}, state)])["people"][0]["crew_running"]

    got = {s: running(s) for s in ("working", "idle", "waiting")}
    ck("a working, idle or waiting station is a running crew", all(v is True for v in got.values()), got)
    got = {s: running(s) for s in ("quiet", "ended", "replaced", "", None)}
    ck("no other station state is a running crew", all(v is False for v in got.values()), got)
    got = team([who("p_a"), who("p_me")], [at_station(None), at_station({"id": None}), at_station({}),
                                           {"state": "idle", "last_at": seen}])
    ck("a station of no person adds no row and counts for nobody, the caller included",
       got["people"] == [{"id": pid, "name": "Same Name", "is_admin": False, "you": pid == "p_me",
                          "crew_running": False, "last_heard": None} for pid in ("p_a", "p_me")], got)
    got = team([who("p_a")], [at_station({"id": "p_a"}, last_at=None),
                              at_station({"id": "p_a"}, last_at="")])
    ck("stations with no time give no last_heard", got["people"][0]["last_heard"] is None, got)
    got = team([who("p_a")], [at_station({"id": "p_a"}, last_at=None),
                              at_station({"id": "p_a"}, last_at=seen - timedelta(hours=1)),
                              at_station({"id": "p_a"}, last_at=seen.isoformat()),
                              at_station({"id": "p_a"}, last_at=seen - timedelta(hours=2))])
    ck("last_heard is the newest time among the person's stations",
       got["people"][0]["last_heard"] == seen.isoformat(), got)
    got = team([who("p_a", None, "a@example.test"), who("p_b", "", None), who("p_c", "Cee", "c@example.test")])
    names = {i: r["name"] for i, r in people_of(got).items()}
    ck("a person is named by name, else Someone, never by email",
       names == {"p_a": "Someone", "p_b": "Someone", "p_c": "Cee"}, names)
    local = who("local", "You", None, admin=True)
    got = team([local], [at_station({"id": "local"}, last_at=None)])
    ck("the unclaimed local is not listed while no station of theirs has reported", got["people"] == [], got)
    got = team([local], [at_station({"id": "local"}, "ended")])
    ck("the unclaimed local is listed as Local once a station has reported, ended or not",
       [(r["id"], r["name"]) for r in got["people"]] == [("local", "Local")], got)
    reads.update(people=0, members=[])
    got = team([who("p_b"), who("p_a"), who("p_me")],
               projects=[{"id": "uc_b", "name": "Same", "lead_person_id": ""},
                         {"id": "uc_a", "name": "same", "lead_person_id": None}],
               members={"uc_a": [{"person_id": "p_b"}, {"person_id": "p_nobody"}, {"person_id": "p_a"}]})
    ck("equal names are ordered by id: people, projects and members",
       [r["id"] for r in got["people"]] == ["p_a", "p_b", "p_me"]
       and [r["id"] for r in got["projects"]] == ["uc_a", "uc_b"]
       and [m["id"] for m in got["projects"][0]["members"]] == ["p_a", "p_b"], got)
    ck("an empty lead is no lead, and a member who is no person here is left out",
       [r["lead"] for r in got["projects"]] == [None, None] and got["projects"][1]["members"] == [], got)
    ck("one read of the people and one of the members, for all the projects",
       reads["people"] == 1 and len(reads["members"]) == 1
       and sorted(reads["members"][0]) == ["uc_a", "uc_b"], reads)


async def main():
    from tares.daemon import make_app
    app = make_app()
    root = {"Authorization": f"Bearer {TOKEN}"}
    async with app.router.lifespan_context(app):
        store = app.state.store
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                                     timeout=30) as cx:
            async def home(who=root, fresh=True):
                if fresh:
                    fh.clear_cache()
                r = await cx.get("/api/home", headers=who)
                assert r.status_code == 200, r.text
                return r.json()

            async def joiner(who=root):
                return (await home(who)).get("joiner") or {}

            async def push(who, *stations):
                body = "\n".join(json.dumps(x) for lines in stations for x in lines) + "\n"
                r = await cx.post("/ingest/claude_code", content=body,
                                  headers={**who, "Content-Type": "application/x-ndjson"})
                assert r.status_code < 300, r.text

            async def settings(who, body):
                r = await cx.put("/api/crew/settings", json=body, headers=who)
                assert r.status_code == 200, r.text
                return r.json()

            def key(kid, person=None, scopes=("read", "ingest")):
                token = f"nvf_{kid}_{kid}-secret-for-the-test"
                store.insert_api_key(kid, kid, f"nvf_{kid}", hashlib.sha256(token.encode()).hexdigest(),
                                     list(scopes), person_id=person)
                return {"Authorization": f"Bearer {token}"}

            r = await cx.post("/api/sources", headers=root, json={
                "name": "claude_code", "connector": "claude_code", "poll": "10s", "config": {"push": True}})
            assert r.status_code < 300, r.text

            print("== a cell of one person ==")
            h = await home()
            ck("a cell of one: the block is there, with its four keys",
               set(h.get("joiner") or {}) == {"joining", "team_settings", "team_crews", "own_crew_up"},
               h.get("joiner"))
            ck("a cell of one: nobody is joining",
               h.get("joiner") == {**NOTHING, "own_crew_up": False}, h.get("joiner"))
            ck("a cell of one: no team key", "team" not in h["joiner"], h["joiner"])
            alone = fh.home(store, None, datetime.now(timezone.utc)).get("joiner")
            ck("a call with no request: the block is there, with nothing about a team",
               alone == {**NOTHING, "own_crew_up": False}, alone)

            print("== a second person, and their own crew only ==")
            ana = store.create_person("Ana Example", "ana@example.test")["id"]
            ben = store.create_person("Ben Example", "ben@example.test")["id"]
            ana_key, ben_key = key("kana", ana), key("kben", ben)
            owner_key, no_person = key("kowner", "local"), key("knobody")
            await settings(root, TEAM)
            ck("own_crew_up: false before the member's crew reports",
               (await joiner(ana_key)).get("own_crew_up") is False, await joiner(ana_key))
            await push(ana_key, station("ana", "orchestrator", "m-ana"))
            ck("own_crew_up: false with the orchestrator alone",
               (await joiner(ana_key)).get("own_crew_up") is False, await joiner(ana_key))
            await push(ana_key, station("ana", "reviewer", "m-ana"))
            j = await joiner(ana_key)
            ck("own_crew_up: true once the orchestrator and the reviewer have reported",
               j.get("own_crew_up") is True, j)
            ck("a member's own crew is not another's: nobody to join",
               j.get("joining") is False and j.get("team_crews") == 0, j)
            ck("the team's settings are there before anyone else's crew is",
               j.get("team_settings") == TEAM, j)

            print("== stations no recording makes ==")
            fx = SimpleNamespace(caller_person=lambda r: {"id": ana}, is_system=lambda r: False,
                                 is_admin=lambda r: False)
            seen = datetime.now(timezone.utc)

            def of(person, last_at):
                row = {"role": "builder", "state": "idle", "person": person, "last_at": last_at}
                return fh.joiner_block(store, fx, object(), [row], {})

            got = of({"id": ben}, seen)
            ck("(the direct call sees another person's station that has reported)",
               got["team_crews"] == 1 and got["joining"] is True, got)
            got = of({"id": ben}, None)
            ck("another person's station that never reported does not count",
               got["team_crews"] == 0 and got["joining"] is False, got)
            got = of(None, seen)
            ck("a station of no person does not count",
               got["team_crews"] == 0 and got["joining"] is False, got)
            fx.own_stations = lambda r, rows: [s for s in rows if s["person"]["id"] == ana]

            def up_for(person):
                rows = [{"role": role, "state": "idle", "person": {"id": person}, "last_at": seen}
                        for role in ("orchestrator", "reviewer")]
                return fh.joiner_block(store, fx, object(), rows, {})["own_crew_up"]

            ck("called alone, it cuts the stations to the caller's own",
               (up_for(ana), up_for(ben)) == (True, False), (up_for(ana), up_for(ben)))

            ck("a member who is not joining gets no team key", "team" not in await joiner(ana_key),
               await joiner(ana_key))
            j = await joiner(ben_key)
            ck("(a third person is joining before the owner has a crew)", j.get("joining") is True, j)
            ck("the unclaimed local is not listed without a crew",
               set(people_of(j.get("team") or {})) == {ana, ben}, j.get("team"))

            print("== the owner has two crews ==")
            await push(root, station("first", "orchestrator", "m-1"), station("first", "reviewer", "m-1"),
                       station("second", "orchestrator", "m-2"))
            owners = [c for c in store.list_crews() if c["person_id"] == "local"]
            ck("(the owner's two crews are two crews)", len(owners) == 2, owners)
            j = await joiner(ana_key)
            ck("the second person is joining", j.get("joining") is True, j)
            ck("with the four values the team stored", j.get("team_settings") == TEAM, j)
            ck("two crews of one other person count once", j.get("team_crews") == 1, j)
            j = await joiner(ben_key)
            ck("a third person counts both of the others", j.get("team_crews") == 2, j)
            ck("another person's crew is never the member's own", j.get("own_crew_up") is False, j)

            for who, headers in (("the cell's token", root), ("the owner's key", owner_key)):
                j = await joiner(headers)
                ck(f"{who}: not joining, and nothing about a team", about_team(j) == NOTHING, j)
                ck(f"{who}: their own crew is up", j.get("own_crew_up") is True, j)
                ck(f"{who}: no team key", "team" not in j, j)
            j = await joiner(no_person)
            ck("a key with no person: not joining, and nothing about a team", about_team(j) == NOTHING, j)
            ck("a key with no person: no other person's crew is its own", j.get("own_crew_up") is False, j)
            ck("a key with no person: no team key", "team" not in j, j)

            print("== one kept home per caller ==")
            fh.clear_cache()
            first = await home(ana_key, fresh=False)
            other = await home(ben_key, fresh=False)
            again = await home(ana_key, fresh=False)
            ck("(the second read of one caller is the kept one)", again["now"] == first["now"],
               (first["now"], again["now"]))
            ck("two people reading inside the cache window get their own block",
               (first["joiner"]["team_crews"], first["joiner"]["own_crew_up"]) == (1, True)
               and (other["joiner"]["team_crews"], other["joiner"]["own_crew_up"]) == (2, False),
               (first["joiner"], other["joiner"]))

            print("== no stored team settings ==")
            await settings(root, {"clear": list(TEAM)})
            j = await joiner(ana_key)
            ck("no team settings: not joining", j.get("joining") is False, j)
            ck("no team settings: no team key", "team" not in j, j)
            ck("no team settings: none are given", "team_settings" in j and j["team_settings"] is None, j)
            ck("no team settings: the other crews are still counted", j.get("team_crews") == 1, j)
            own = await settings(ana_key, {"autonomy": "L3-review", "releaser": True})
            ck("(the member's own settings are saved)",
               own["autonomy"] == "L3-review" and own["releaser"] is True, own)
            j = await joiner(ana_key)
            ck("a member's own settings are never the team's",
               j.get("joining") is False and "team_settings" in j and j["team_settings"] is None, j)
            await settings(root, {"autonomy": "L4-ship"})
            j = await joiner(ana_key)
            ck("a value the team never set stays null, whatever the member saved",
               j.get("team_settings") == {"autonomy": "L4-ship", "releaser": None, "builders_max": None,
                                          "challenger": None} and j.get("joining") is True, j)

            print("== the team a joiner sees ==")
            await settings(root, TEAM)
            ada = store.create_person("Ada Admin", "ada@example.test", is_admin=True)["id"]
            abe = store.create_person("abe lower", "abe@example.test")["id"]
            cy = store.create_person("Cy Ended", "cy@example.test")["id"]
            dee = store.create_person("Dee Quiet", "dee@example.test")["id"]
            gus = store.create_person("Gus Gone", "gus@example.test")["id"]
            cy_key, dee_key, gus_key = key("kcy", cy), key("kdee", dee), key("kgus", gus)
            ben_new = station("ben", "reviewer", "m-ben", ago=4)
            await push(ben_key, station("ben", "orchestrator", "m-ben", ago=50, state="ended"), ben_new)
            cy_new = station("cy", "orchestrator", "m-cy", ago=30, state="ended")
            await push(cy_key, cy_new, station("cy", "reviewer", "m-cy", ago=300, state="ended"))
            dee_old = station("dee", "orchestrator", "m-dee", ago=600, state="working")
            await push(dee_key, dee_old)
            await push(gus_key, station("gus", "orchestrator", "m-gus"))

            def project(uid, name, lead=None, members=(), scope="team", status="active"):
                store.create_project(uid, "custom", name, {}, status=status, lead_person_id=lead)
                store.set_project_kind(uid, "software_factory")
                store.set_project_scope(uid, scope)
                for m in members:
                    store.add_project_member(uid, m)

            project("uc_delta", "delta")
            project("uc_cellar", "Cellar", lead=gus, members=(abe,))
            project("uc_beta", "beta site", lead=ben, members=(cy, ben, "local", gus, abe))
            project("uc_attic", "Attic", lead="local", members=(ana,))
            project("uc_secret", "secret-plan", lead=ben, members=(dee,), scope="private")
            project("uc_done", "old-work", lead=ben, members=(ana,), status="done")
            project("uc_later", "someday-work", lead=ben, members=(ana,), status="later")
            ck("(a person is removed from the cell)", store.remove_person(gus) is True)

            j = await joiner(ana_key)
            t = j.get("team") or {}
            people, projects = people_of(t), projects_of(t)
            ck("a joiner gets the team beside the other four keys",
               set(j) == {"joining", "team_settings", "team_crews", "own_crew_up", "team"}
               and set(t) == {"people", "projects"}, j)
            ck("each person has these six fields",
               bool(people) and all(set(r) == {"id", "name", "is_admin", "you", "crew_running", "last_heard"}
                                    for r in people.values()), t)
            ck("each project has these four fields",
               bool(projects) and all(set(r) == {"id", "name", "lead", "members"} for r in projects.values()), t)
            ck("everyone on the cell is listed, a removed person is not",
               set(people) == {"local", ada, ana, ben, abe, cy, dee}, t)
            ck("the unclaimed owner with a crew is listed as Local, an admin",
               (people["local"]["name"], people["local"]["is_admin"], people["local"]["crew_running"])
               == ("Local", True, True), people["local"])
            ck("the caller's own row says you, no other does",
               [i for i, r in people.items() if r["you"] is True] == [ana]
               and all(r["you"] is False for i, r in people.items() if i != ana), t)
            ck("an admin is marked, a member is not",
               people[ada]["is_admin"] is True and people[ana]["is_admin"] is False, t)
            ck("a crew with one station running is running, heard at its newest time",
               people[ben]["crew_running"] is True
               and when(people[ben]["last_heard"]) == when(ben_new[1]["timestamp"]),
               (people[ben], ben_new[1]["timestamp"]))
            ck("a crew whose stations all ended is not running, and says when it was last heard",
               people[cy]["crew_running"] is False
               and when(people[cy]["last_heard"]) == when(cy_new[1]["timestamp"]),
               (people[cy], cy_new[1]["timestamp"]))
            ck("a crew that went quiet is not running, and says when it was last heard",
               people[dee]["crew_running"] is False
               and when(people[dee]["last_heard"]) == when(dee_old[1]["timestamp"]),
               (people[dee], dee_old[1]["timestamp"]))
            ck("a person with no crew: not running and never heard",
               (people[abe]["crew_running"], people[abe]["last_heard"]) == (False, None), people[abe])
            order = [r["id"] for r in t["people"]]
            ck("people: admins first, running crews first, then by name whatever its case",
               order == ["local", ada, ana, ben, abe, cy, dee], [r["name"] for r in t["people"]])
            ck("no email address is in the team block", "@" not in json.dumps(t), t)
            ck("open projects the caller may see, by name whatever its case",
               [r["name"] for r in t["projects"]] == ["Attic", "beta site", "Cellar", "delta"], t["projects"])
            ck("done and later projects are absent",
               not any(x in json.dumps(j) for x in ("old-work", "uc_done", "someday-work", "uc_later")), j)
            ck("a private project the joiner is not on is absent from the whole block",
               "secret-plan" not in json.dumps(j) and "uc_secret" not in json.dumps(j), j)
            ck("a team project shows its lead",
               projects["beta site"]["id"] == "uc_beta"
               and projects["beta site"]["lead"] == {"id": ben, "name": "Ben Example"}, projects["beta site"])
            stored = {m["person_id"] for m in store.members_by_project(["uc_beta"])["uc_beta"]}
            ck("(the stored members hold the lead, the unclaimed local and the removed person)",
               stored == {cy, ben, "local", gus, abe}, stored)
            ck("its members by name, without the lead, the unclaimed local or a removed person",
               projects["beta site"]["members"] == [{"id": abe, "name": "abe lower"},
                                                    {"id": cy, "name": "Cy Ended"}], projects["beta site"])
            ck("a project with no lead and no members",
               (projects["delta"]["lead"], projects["delta"]["members"]) == (None, []), projects["delta"])
            ck("a project the unclaimed local leads has no lead",
               projects["Attic"]["lead"] is None
               and projects["Attic"]["members"] == [{"id": ana, "name": "Ana Example"}], projects["Attic"])
            ck("a project a removed person led has no lead", projects["Cellar"]["lead"] is None,
               projects["Cellar"])
            store.set_project_lead("uc_cellar", gus)
            got = projects_of((await joiner(ana_key)).get("team") or {}).get("Cellar") or {}
            ck("a stored lead who left the cell is no lead",
               store.get_project("uc_cellar")["lead_person_id"] == gus and got.get("lead", "missing") is None, got)

            j = await joiner(dee_key)
            t = j.get("team") or {}
            ck("a person on the private project, also joining, sees it",
               j.get("joining") is True and projects_of(t).get("secret-plan") ==
               {"id": "uc_secret", "name": "secret-plan", "lead": {"id": ben, "name": "Ben Example"},
                "members": [{"id": dee, "name": "Dee Quiet"}]}, t)
            ck("and their own row is the one that says you",
               [i for i, r in people_of(t).items() if r["you"]] == [dee], t)

            eve = store.create_person("Eve Elsewhere", "eve@example.test")["id"]
            store.add_project_member("uc_secret", eve)
            eve_crew = [{**line, "tares_project": "uc_secret"}
                        for line in station("eve", "builder", "m-eve")]
            await push(key("keve", eve), eve_crew)
            seen_by_dee = people_of((await joiner(dee_key)).get("team") or {}).get(eve) or {}
            ck("(someone on the private project reads a crew that works in it as running)",
               seen_by_dee.get("crew_running") is True
               and when(seen_by_dee.get("last_heard")) == when(eve_crew[1]["timestamp"]), seen_by_dee)
            seen_by_ana = people_of((await joiner(ana_key)).get("team") or {}).get(eve) or {}
            ck("a crew only on a project the joiner cannot see counts as no crew",
               (seen_by_ana.get("crew_running"), seen_by_ana.get("last_heard", "missing")) == (False, None),
               seen_by_ana)

            ck("(the owner claims the cell)",
               store.claim_local("olive@example.test", "Olive Owner") is not None)
            t = (await joiner(ana_key)).get("team") or {}
            owner = people_of(t).get("local") or {}
            ck("a joiner sees the owner named and marked admin",
               (owner.get("name"), owner.get("is_admin"), owner.get("you")) == ("Olive Owner", True, False), owner)
            ck("a named owner leads their project and is among the members",
               projects_of(t)["Attic"]["lead"] == {"id": "local", "name": "Olive Owner"}
               and {"id": "local", "name": "Olive Owner"} in projects_of(t)["beta site"]["members"], t["projects"])
            ck("no email address is in the team block once the owner is named", "@" not in json.dumps(t), t)
            for who, headers in (("the cell's token", root), ("the named owner's key", owner_key),
                                 ("a key with no person", no_person)):
                j = await joiner(headers)
                ck(f"{who}, with a whole team on the cell: no team key",
                   j.get("joining") is False and "team" not in j, j)


asyncio.run(main())
team_rows_no_recording_makes()
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
