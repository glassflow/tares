"""The factory home without a licence (TR-1048): GET /api/home never shows another person's rows.

First a cell of one person (only `local`): the body has no `summary` and only the single-person
kinds of waiting items. Then a cell with two people and no licence: Ann and Bob are seeded before
the daemon starts, each gets a key, a crew station line, a desk item addressed to them and a
ticket assigned to them. The home is still the single-person home (none of the team keys), and
Ann's key lists Ann's rows and none of Bob's, and the reverse. `?projects=team` changes nothing.
The team home itself, with a licence, is ee/tests/test_factory_team_home.py.

Run: .venv/bin/python tests/test_factory_home_single.py
"""
import asyncio
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _env import scrub_os_environ
scrub_os_environ()
import _people

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_TMP = tempfile.mkdtemp(prefix="tares-home-single-")
os.environ["TARES_DB"] = os.path.join(_TMP, "api.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(_TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-home-single"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
os.environ["TARES_EDITION"] = "factory"

import httpx

from tares import factory_home as fh
from tares import team as team_mod

P = F = 0

TEAM_KEYS = ("summary", "you_are_waiting_on", "crews", "crews_quiet", "crew_limit", "areas", "counts",
             "hidden_summary")


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def ts(minutes_ago: float) -> str:
    t = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return t.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def station_lines(sid, station, project):
    return [
        {"type": "session_station", "sessionId": sid, "cwd": "/w/shop", "timestamp": ts(10),
         "station": station, "role": "builder", "parent": "", "factory_station": station,
         "tares_project": project},
        {"type": "session_project", "sessionId": sid, "cwd": "/w/shop", "timestamp": ts(10),
         "tares_project": project, "from_env": True},
        {"type": "session_state", "sessionId": sid, "cwd": "/w/shop", "timestamp": ts(1),
         "state": "working", "reason": ""},
    ]


def blob(body) -> str:
    return json.dumps(body)


async def one_person():
    """A cell whose only person is `local`."""
    from tares import daemon
    app = daemon.make_app()
    store = app.state.store
    root = {"Authorization": f"Bearer {TOKEN}"}
    async with app.router.lifespan_context(app):
        ck("only local is on the cell", store.only_local_person())
        fh._cache.clear()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                                     headers=root) as cx:
            r = await cx.post("/api/projects", json={"template": "custom", "name": "Solo",
                                                     "objects": [], "kind": "software_factory"})
            solo = r.json()["id"]
            store.add_desk_item(solo, "solo was refused a command. Allow it?", "", [],
                                "solo may read a pull request", "it needs it", "", [], False,
                                "crew-orchestrator", [], datetime.now(timezone.utc) - timedelta(hours=2),
                                station="solo-build-m1", command="gh pr view 1")
            store.add_desk_item(solo, "Which colour for the links?", "", ["blue"], "blue",
                                "it reads best", "", [], False, "crew-orchestrator", [],
                                datetime.now(timezone.utc) - timedelta(hours=1))
            one = (await cx.get("/api/home")).json()
        ck("no summary key", "summary" not in one, sorted(one))
        ck("none of the team keys", not [k for k in TEAM_KEYS if k in one], sorted(one))
        ck("the single-person kinds, not the team ones", {i["kind"] for i in one["waiting_for_you"]}
           == {"permission", "question"}, [i["kind"] for i in one["waiting_for_you"]])
        ck("both desk items are listed", len(one["waiting_for_you"]) == 2)
        ck("team_home itself answers {} without a licence",
           type(team_mod.get()) is team_mod.SingleTeam and team_mod.get().team_home(None, None, None) == {})


async def team_database():
    from tares import daemon
    # two people on the cell before it starts; no licence anywhere
    ann_row, bob_row = _people.seed(daemon.DB_PATH, [("Ann", "ann@example.com"),
                                                      ("Bob", "bob@example.com")])
    app = daemon.make_app()
    store = app.state.store
    root = {"Authorization": f"Bearer {TOKEN}"}

    async def call(hdr, method, path, body=None, content=None, **params):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                                     headers=hdr) as cx:
            return await cx.request(method, path, json=body, content=content, params=params or None)

    async def home(hdr, **params):
        fh._cache.clear()
        r = await call(hdr, "GET", "/api/home", **params)
        assert r.status_code == 200, r.text
        return r.json()

    async with app.router.lifespan_context(app):
        ck("no licence on this cell", app.state.host.licence_state()["valid"] is False)
        ck("the team interface is the single-person one", type(team_mod.get()) is team_mod.SingleTeam)
        ck("two people besides local: this is a team database", not store.only_local_person())

        print("== two people, no licence ==")
        keys, pid = {}, {"ann": ann_row["id"], "bob": bob_row["id"]}
        for who in ("ann", "bob"):
            r = await call(root, "POST", "/api/keys", {"name": f"{who}-key", "scopes": ["admin"],
                                                       "person": {"email": f"{who}@example.com"}})
            ck(f"{who} gets a key on the person that exists", r.status_code == 201
               and r.json()["person"]["id"] == pid[who], r.text[:200])
            keys[who] = {"Authorization": f"Bearer {r.json()['secret']}"}
        ck("no person row was added", len(store.list_people()) == 3, store.list_people())

        r = await call(root, "POST", "/api/projects", {"template": "custom", "name": "Shop",
                                                       "objects": [], "kind": "software_factory"})
        assert r.status_code in (200, 201), r.text
        shop = r.json()["id"]
        r = await call(root, "POST", "/api/sources", {"name": "claude_code", "connector": "claude_code",
                                                      "poll": "10s", "config": {"push": True},
                                                      "project": shop})
        ck("the claude_code source is made", r.status_code < 300, r.text)
        for who in ("ann", "bob"):
            lines = station_lines(f"s-{who}", f"{who}-build-m1", "Shop")
            r = await call(keys[who], "POST", "/ingest/claude_code",
                           content="\n".join(json.dumps(x) for x in lines) + "\n",
                           **{})
            ck(f"{who}'s station line is taken in", r.status_code < 300, r.text[:200])
        for who, label in (("ann", "Ann"), ("bob", "Bob")):
            store.add_desk_item(shop, f"{label}: which colour for the links?", "", ["blue"], "blue",
                                "it reads best", "", [], False, "crew-orchestrator", [],
                                datetime.now(timezone.utc) - timedelta(hours=1),
                                to_person_id=pid[who])
            r = await call(root, "POST", f"/api/projects/{shop}/tickets",
                           {"title": f"{label}'s ticket", "repo": f"{who}/app"})
            assert r.status_code == 201, r.text
            store.update_ticket(r.json()["id"], assignee_person_id=pid[who], stage="doing",
                                holder=f"{who}-build-m1")

        stations = {s["name"]: s for s in (await call(root, "GET", "/api/crew")).json()["stations"]}
        ck("each station belongs to the person whose key sent it",
           (stations.get("ann-build-m1", {}).get("person") or {}).get("id") == pid["ann"]
           and (stations.get("bob-build-m1", {}).get("person") or {}).get("id") == pid["bob"],
           {k: v.get("person") for k, v in stations.items()})

        homes = {"ann": await home(keys["ann"]), "bob": await home(keys["bob"])}
        for who, other in (("ann", "bob"), ("bob", "ann")):
            h, text = homes[who], blob(homes[who])
            me, you = who.capitalize(), other.capitalize()
            ck(f"{me}'s home has none of the team keys", not [k for k in TEAM_KEYS if k in h],
               [k for k in TEAM_KEYS if k in h])
            ck(f"{me}'s home keeps the single-person kinds only",
               {i["kind"] for i in h["waiting_for_you"]} <= {"permission", "question", "prs_to_review",
                                                             "ready_to_start", "calls_to_confirm",
                                                             "milestone", "close"}
               and not any(i["kind"] in ("grant", "signoff", "desk") for i in h["waiting_for_you"]),
               [i["kind"] for i in h["waiting_for_you"]])
            ck(f"{me}'s own desk item is listed", f"{me}: which colour" in text)
            ck(f"none of {you}'s desk items is", f"{you}: which colour" not in text)
            members = h["crew"]["members"]
            ck(f"{me}'s crew is one station, their own (not {you}'s)", len(members) == 1
               and f"{me}'s ticket" in blob(members[0]), members)
            ck(f"{me}'s ticket is listed and {you}'s is not", f"{me}'s ticket" in text
               and f"{you}'s ticket" not in text)
            ck(f"the crew's open questions count only {me}'s", h["crew"]["desk_open"] == 1,
               h["crew"]["desk_open"])
        same = await home(keys["ann"], projects="team")
        drop = ("now", "start")
        ck("?projects=team gives the same body as without it", (
            {k: v for k, v in same.items() if k not in drop}
            == {k: v for k, v in homes["ann"].items() if k not in drop}))


async def main():
    print("== a cell of one person ==")
    await one_person()
    # the daemon reads its database path once, at import: point it at a second file for the team cell
    from tares import daemon
    daemon.DB_PATH = os.path.join(_TMP, "team.duckdb")
    await team_database()


asyncio.run(main())
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
