"""The Start checklist of a person joining a factory (TR-1314): GET /api/home gives them their own
five steps in `start`, and what the team already set as `start.team`.

Everything goes through the real routes with real keys: the crew settings, the machine report, the
crew's own lines, hiding the list. Only memberships are written to the store directly. The steps and
their words are checked in tests/test_factory_start.py.

Run: .venv/bin/python tests/test_factory_start_joiner.py
"""
import asyncio
import hashlib
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _env import scrub_os_environ
scrub_os_environ()

_TMP = tempfile.mkdtemp(prefix="tares-factory-start-joiner-")
os.environ["TARES_DB"] = os.path.join(_TMP, "api.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(_TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-start-joiner"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
os.environ["TARES_EDITION"] = "factory"

import httpx

from tares import factory_home as fh
from tares import words

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def ts(minutes_ago: float) -> str:
    t = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return t.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def station(crew, role, machine, ago=2):
    """The two lines one member of `crew` records: who it is, and that it is idle."""
    sid, name = f"s-{crew}-{role}", f"{crew}-{role}"
    return [{"type": "session_station", "sessionId": sid, "cwd": "/w/crew", "timestamp": ts(ago),
             "station": name, "role": role, "parent": "", "factory_station": name,
             "factory_crew": crew, "crew": crew, "factory_machine": machine, "machine": machine},
            {"type": "session_state", "sessionId": sid, "cwd": "/w/crew", "timestamp": ts(ago),
             "state": "idle", "reason": ""}]


OWNER_KEYS = ["setup", "install", "login", "crew"]
JOIN_KEYS = ["settings", "install", "login", "crew", "project"]
TEAM = {"autonomy": "L3-review", "releaser": False, "builders_max": 3, "challenger": True}


def done_of(block):
    return {s["key"]: s["done"] for s in block["steps"]}


def ticked(block):
    return [s["key"] for s in block["steps"] if s["done"]]


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

            async def start(who=root, fresh=True):
                return (await home(who, fresh))["start"]

            async def push(who, *members):
                body = "\n".join(json.dumps(x) for lines in members for x in lines) + "\n"
                r = await cx.post("/ingest/claude_code", content=body,
                                  headers={**who, "Content-Type": "application/x-ndjson"})
                assert r.status_code < 300, r.text

            async def crew(who, name):
                await push(who, station(name, "orchestrator", f"m-{name}"),
                           station(name, "reviewer", f"m-{name}"))

            async def settings(who, body):
                r = await cx.put("/api/crew/settings", json=body, headers=who)
                assert r.status_code == 200, r.text
                return r.json()

            async def mine(who):
                r = await cx.get("/api/crew/settings", headers=who)
                assert r.status_code == 200, r.text
                return {k: r.json()[k] for k in TEAM}

            async def machine(who, **body):
                r = await cx.post("/api/crew/machine", json=body, headers=who)
                assert r.status_code == 200, r.text

            async def project(name, kind="software_factory"):
                body = {"template": "custom", "name": name, "goal": "A goal", "objects": []}
                if kind:
                    body["kind"] = kind
                r = await cx.post("/api/projects", headers=root, json=body)
                assert r.status_code < 300, r.text
                return r.json()["id"]

            def key(kid, person=None, scopes=("read", "ingest")):
                token = f"nvf_{kid}_{kid}-secret-for-the-test"
                store.insert_api_key(kid, kid, f"nvf_{kid}", hashlib.sha256(token.encode()).hexdigest(),
                                     list(scopes), person_id=person)
                return {"Authorization": f"Bearer {token}"}

            def person(name):
                return store.create_person(f"{name.title()} Example", f"{name}@example.test")["id"]

            r = await cx.post("/api/sources", headers=root, json={
                "name": "claude_code", "connector": "claude_code", "poll": "10s", "config": {"push": True}})
            assert r.status_code < 300, r.text

            print("== not joining: the owner's four steps ==")
            ana, ben, cat, dee, eve = (person(n) for n in ("ana", "ben", "cat", "dee", "eve"))
            ana_key, ben_key = key("kana", ana), key("kben", ben)
            cat_key = key("kcat", cat, ("read",))
            dee_key = key("kdee", dee, ("admin",))
            eve_key = key("keve", eve)
            owner_key, no_person = key("kowner", "local"), key("knobody")
            await crew(root, "owner")
            await machine(root, machine="m-owner", version="1.0.0")
            s = await start(ana_key)
            ck("another crew but no team settings: the four steps, no team key",
               list(done_of(s)) == OWNER_KEYS and "team" not in s, s)

            print("== a team that saved only who merges ==")
            await settings(root, {"autonomy": "L3-review"})
            s = await start(ana_key)
            ck("a joiner gets the five steps", list(done_of(s)) == JOIN_KEYS and s["total"] == 5, s)
            ck("none done, shown", ticked(s) == [] and s["done"] == 0 and s["show"] is True
               and s["hidden"] is False, s)
            ck("only the saved value is worded, and no GitHub is an empty text",
               s["team"] == {"settings_text": "You approve each merge", "github": ""}, s.get("team"))
            await settings(root, {"autonomy": "L4-ship", "releaser": True})
            s = await start(ana_key)
            ck("two saved values are both worded, the unset ones are not",
               s["team"]["settings_text"] == "The crew merges on its own, releases on", s["team"])

            print("== what the team set ==")
            await settings(root, TEAM)
            store.upsert_github_credential("main", "ghp_example", "token", "", "example-org")
            s = await start(ana_key)
            ck("every saved value and the GitHub connection",
               s["team"] == {"settings_text": "You approve each merge, 3 builders, no releases, second check on",
                             "github": "GitHub connected as example-org"}, s["team"])
            ck("the team's saved choices and GitHub tick nothing of the joiner's", ticked(s) == [], s)
            found = words.offenders_in(s)
            ck("no forbidden word and no em dash in the block", found == [], found)
            for who, headers in (("the cell's token", root), ("the owner's key", owner_key),
                                 ("a key with no person", no_person)):
                s = await start(headers)
                ck(f"{who}: four steps and no team key",
                   list(done_of(s)) == OWNER_KEYS and s["total"] == 4 and "team" not in s, s)
            s = await start()
            ck("the owner's finished setup line is the same as before",
               s["steps"][0].get("detail")
               == "You approve each merge, 3 builders, no releases. GitHub connected as example-org",
               s["steps"][0])

            print("== another person's crew, machine and settings tick nothing ==")
            await settings(ben_key, {})
            await machine(ben_key, machine="m-ben", version="2.0.0")
            await crew(ben_key, "ben")
            s = await start(ana_key)
            ck("the owner's and a teammate's crew, machine and settings leave every step open",
               ticked(s) == [], s)
            ck("and no step of theirs names another machine",
               not any(m in json.dumps(s) for m in ("m-owner", "m-ben", "1.0.0", "2.0.0")), s)
            s = await start(ben_key)
            ck("the teammate's own are ticked for the teammate",
               ticked(s) == ["settings", "install", "login", "crew"], s)

            print("== the settings step ==")
            r = await cx.put("/api/crew/settings", json={}, headers=cat_key)
            ck("a read key may not save crew settings", r.status_code == 403, (r.status_code, r.text))
            ck("and its step stays open", done_of(await start(cat_key))["settings"] is False)
            r = await cx.put("/api/crew/settings", json={}, headers=no_person)
            ck("a key with no person may not either", r.status_code == 403, (r.status_code, r.text))
            await settings(dee_key, {})
            ck("a key with the admin scope may, for its own person",
               ticked(await start(dee_key)) == ["settings"])
            await start(ana_key, fresh=False)
            await settings(ana_key, {})
            s = await start(ana_key, fresh=False)
            ck("saving no change ticks the step at once, and only it", ticked(s) == ["settings"], s)
            ck("a done step has no action", s["steps"][0]["actions"] == [], s["steps"][0])
            ck("the member still reads the team's values", await mine(ana_key) == TEAM, await mine(ana_key))
            await settings(root, {"autonomy": "L4-ship", "builders_max": 5})
            ck("and follows a later change by the owner",
               await mine(ana_key) == {**TEAM, "autonomy": "L4-ship", "builders_max": 5}, await mine(ana_key))
            await settings(ana_key, {"autonomy": "L3-review", "builders_max": 1})
            s = await start(ana_key)
            ck("a member's own values never change what the team strip says",
               s["team"]["settings_text"]
               == "The crew merges on its own, 5 builders, no releases, second check on", s["team"])
            await settings(root, TEAM)

            print("== install, sign in, crew: the joiner's own ==")
            await machine(ana_key, version="3.0.0")
            s = await start(ana_key)
            ck("their version ticks install only", ticked(s) == ["settings", "install"], s)
            await machine(ana_key, machine="m-ana")
            s = await start(ana_key)
            ck("their machine ticks sign in", ticked(s) == ["settings", "install", "login"], s)
            await push(ana_key, station("ana", "orchestrator", "m-ana"))
            ck("the orchestrator alone leaves the crew step open",
               done_of(await start(ana_key))["crew"] is False)
            await push(ana_key, station("ana", "reviewer", "m-ana"))
            s = await start(ana_key)
            ck("their orchestrator and reviewer tick the crew step",
               ticked(s) == ["settings", "install", "login", "crew"], s)
            ck("with their own machine on the rows", [x.get("detail") for x in s["steps"][1:4]]
               == ["Installed: tares 3.0.0.", "Signed in from m-ana.", "Running on m-ana"], s["steps"])
            ck("(this member has saved no settings)", store.get_setting(f"crew_settings:{eve}") is None)
            ck("(their step is open before their crew)", done_of(await start(eve_key))["settings"] is False)
            await crew(eve_key, "eve")
            s = await start(eve_key)
            ck("a crew of their own that is up ticks the settings step too",
               ticked(s) == ["settings", "install", "login", "crew"], s)

            print("== the project step ==")
            ck("no project at all: open", done_of(await start(ana_key))["project"] is False)
            one, two = await project("billing-export"), await project("invoice-search")
            store.add_project_member(one, ben)
            ck("another person's membership does not tick it",
               done_of(await start(ana_key))["project"] is False)
            ck("and ticks it for that person", done_of(await start(ben_key))["project"] is True)
            plain = await project("plain-notes", kind=None)
            store.add_project_member(plain, ana)
            ck("a project that is not a factory project does not count",
               done_of(await start(ana_key))["project"] is False)
            store.add_project_member(one, ana)
            s = await start(ana_key)
            ck("a member of an open project: done, and with every other step the list closes",
               done_of(s)["project"] is True and s["done"] == 5 and s["show"] is False, s)
            for status in ("done", "later"):
                store.update_project(one, status=status)
                s = await start(ana_key)
                ck(f"a project that is {status} does not count",
                   done_of(s)["project"] is False and s["show"] is True, s)
            store.update_project(two, status="done")
            ck("no open project: open", done_of(await start(ana_key))["project"] is False)
            store.update_project(two, status="active")
            store.set_project_lead(two, ana)
            ck("the lead of an open project: done", done_of(await start(ana_key))["project"] is True)
            store.set_project_lead(two, ben)
            ck("no longer the lead: open", done_of(await start(ana_key))["project"] is False)
            store.set_project_scope(two, "private")
            ck("a private project they are not on does not count",
               done_of(await start(ana_key))["project"] is False)
            store.add_project_member(two, ana)
            ck("a private project they are on does", done_of(await start(ana_key))["project"] is True)

            print("== what it costs ==")
            real, asked = store.members_by_project, []
            store.members_by_project = lambda ids: asked.append(list(ids)) or real(ids)
            try:
                await project("third")
                await home(root)
                ck("the owner's home asks for no membership", asked == [], asked)
                await home(ana_key)
                ck("a joiner's asks once, for all the open projects together",
                   len(asked) == 1 and len(asked[0]) == 2, asked)
            finally:
                store.members_by_project = real

            print("== hiding ==")
            await settings(root, TEAM)
            store.update_project(two, status="done")      # a step is open again, so the list shows
            ck("(both joiners see the list)", (await start(ana_key))["show"] is True
               and (await start(cat_key))["show"] is True)
            r = await cx.post("/api/home/start/hide", headers=ana_key)
            got = r.json()
            ck("hide answers with the joiner's own block",
               r.status_code == 200 and list(done_of(got)) == JOIN_KEYS and "team" in got
               and got["show"] is False and got["hidden"] is True, r.text[:300])
            ck("the home agrees at once", (await start(ana_key, fresh=False))["show"] is False)
            ck("another joiner still sees theirs", (await start(cat_key))["show"] is True)
            r = await cx.delete("/api/home/start/hide", headers=ana_key)
            got = r.json()
            ck("show again answers with the joiner's block", r.status_code == 200
               and list(done_of(got)) == JOIN_KEYS and got["show"] is True and got["hidden"] is False,
               r.text[:300])
            r = await cx.post("/api/home/start/hide", headers=cat_key)
            ck("a joiner with a read key may hide it", r.status_code == 200
               and list(done_of(r.json())) == JOIN_KEYS and r.json()["show"] is False, r.text[:300])

            print("== the team clears its settings ==")
            await settings(root, {"clear": list(TEAM)})
            s = await start(ana_key)
            ck("back to the four steps, no team key",
               list(done_of(s)) == OWNER_KEYS and "team" not in s, s)


asyncio.run(main())
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
