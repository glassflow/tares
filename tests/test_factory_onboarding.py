"""The first run of a new factory cell (TR-1312): tares/crew_onboarding.py, the two calls it adds
(POST /api/crew/machine), and the `onboarding` block of GET /api/home that /setup draws.

The steps tick from what the cell knows: the crew's settings, the GitHub connection, the
machine report the command line posts, and the crew's live sessions. The form starts on the crew's own
defaults and saves nothing until the person confirms. The report is a name and a version, never a key.

Run: .venv/bin/python tests/test_factory_onboarding.py
"""
import asyncio
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_TMP = tempfile.mkdtemp(prefix="tares-crew-onboarding-")
os.environ["TARES_DB"] = os.path.join(_TMP, "api.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(_TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-onboarding"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
os.environ["TARES_EDITION"] = "factory"
for _k in ("TARES_PROJECT", "TARES_PROJECT_NAME", "TARES_FACTORY_URL", "TARES_FACTORY_MCP_URL",
           "TARES_FACTORY_TOKEN_FILE", "TARES_FACTORY_CRED_FILE", "FACTORY_PROJECT",
           "TARES_LOGIN_URL", "TARES_GITHUB_CONNECT_URL", "TARES_LINEAR_CONNECT_URL"):
    os.environ.pop(_k, None)

import httpx
from fastapi import FastAPI

from tares import crew_onboarding as co
from tares import factory_api
from tares import factory_home as fh
from tares import words
from tares.crew.client import settings_defaults

fh.WAITING_BUILDERS.clear()

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


NOW = datetime(2026, 11, 15, 10, 0, tzinfo=timezone.utc)


class Store:
    def __init__(self):
        self.d = {}
        self.github = []

    def get_setting(self, k):
        return self.d.get(k)

    def set_setting(self, k, v):
        if v is None:
            self.d.pop(k, None)
        else:
            self.d[k] = v

    def list_github_credentials(self):
        return self.github

    def list_crews(self):
        return []


def st(role, state, name=None):
    return {"name": name or f"crew-{role}", "role": role, "state": state, "last_at": NOW}


def units():
    print("== the choices ==")
    d = co.defaults()
    want = settings_defaults()
    ck("the form starts on your approval of each merge, with the crew's own releases and builders",
       (d["autonomy"], d["releaser"], d["builders_max"]) == ("L3-review", want["releaser"], want["builders_max"]), d)
    ck("the crew's own default is not touched (the terminal's recommendation stays)",
       want["autonomy"] == "L4-ship" and set(d) == {"autonomy", "releaser", "builders_max"}, (want, d))
    c = co.choices({})
    ck("nothing saved: not chosen, nothing preselected as a saved value",
       c["chosen"] is False and c["autonomy"] is None and c["releaser"] is None and c["builders_max"] is None, c)
    for label, s in (("only autonomy", {"autonomy": "L3-review"}),
                     ("no builders", {"autonomy": "L4-ship", "releaser": False}),
                     ("a text for builders", {"autonomy": "L4-ship", "releaser": False, "builders_max": "3"}),
                     ("zero builders", {"autonomy": "L4-ship", "releaser": False, "builders_max": 0}),
                     ("an unknown autonomy", {"autonomy": "L9", "releaser": False, "builders_max": 2}),
                     ("a true as the builders", {"autonomy": "L4-ship", "releaser": False, "builders_max": True})):
        ck(f"{label}: not chosen", co.choices(s)["chosen"] is False, co.choices(s))
    full = {"autonomy": "L3-review", "releaser": False, "builders_max": 3}
    ck("all three saved: chosen", co.choices(full)["chosen"] is True)
    ck("the line for a finished step 1",
       co.choices_text(co.choices(full)) == "You approve each merge, 3 builders, no releases"
       and co.choices_text(co.choices({"autonomy": "L4-ship", "releaser": True, "builders_max": 1}))
       == "The crew merges on its own, 1 builder, releases on")

    print("== GitHub, in words ==")
    s = Store()
    ck("none: nothing", co.github_text(s) == "")
    s.github = [{"account": "example-org"}, {"account": "example-two"}, {"account": "example-org"}]
    ck("accounts, once each", co.github_text(s) == "GitHub connected as example-org, example-two", co.github_text(s))
    s.github = [{"account": ""}]
    ck("a credential with no account name", co.github_text(s) == "GitHub connected")
    s.github = [{"account": f"o{i}"} for i in range(5)]
    ck("more than three are counted", co.github_text(s) == "GitHub connected as o0, o1, o2 and 2 more", co.github_text(s))

    print("== the machine report ==")
    s = Store()
    ck("none yet", co.machine_report(s) is None)
    r = co.save_machine(s, "example-laptop", "1.58.0", NOW)
    ck("saved as a name, a version and a time", r == {"machine": "example-laptop", "version": "1.58.0",
                                                      "at": NOW.isoformat()} and co.machine_report(s) == r, r)
    r = co.save_machine(s, "", "1.59.0", NOW)
    ck("an empty name keeps the one reported before", r["machine"] == "example-laptop" and r["version"] == "1.59.0", r)
    r = co.save_machine(s, "Ana\u2019s exämple\x00-pc\n" + "x" * 200, "1.0", NOW)
    ck("printable characters stay (a curly apostrophe, an umlaut), control ones go, and it is short",
       r["machine"].startswith("Ana\u2019s exämple-pc") and len(r["machine"]) <= 80
       and "\n" not in r["machine"] and "\x00" not in r["machine"], r)
    later = datetime(2026, 11, 15, 11, 0, tzinfo=timezone.utc)
    before = co.machine_report(s)
    r = co.save_machine(s, "\x00 \n", "", later)
    ck("a report with both fields empty changes nothing, not even the time",
       co.machine_report(s) == before and r["at"] == before["at"] and before["at"] != later.isoformat(), (r, before))
    e = Store()
    r = co.save_machine(e, "", "", later)
    ck("and on a cell with no report it saves none", co.machine_report(e) is None and r["at"] is None, r)
    k = co.machine_key("p1")
    co.save_machine(s, "second-pc", "2.0", NOW, k)
    ck("a person's report is kept apart from the cell's", co.machine_key() == "crew_machine" and k == "crew_machine:p1"
       and co.machine_report(s, k)["machine"] == "second-pc" and co.machine_report(s)["machine"].startswith("Ana"))
    s.set_setting(co.MACHINE_SETTING, "{not json")
    ck("a damaged setting reads as no report", co.machine_report(s) is None)

    print("== your crew ==")
    v = co.crew_view([], {}, "")
    ck("no session: both are none, builders 0 of the default", v["orchestrator"]["state"] == "none"
       and v["reviewer"]["state"] == "none" and v["up"] is False
       and v["builders"] == {"live": 0, "max": settings_defaults()["builders_max"]}, v)
    v = co.crew_view([st("orchestrator", "working")], {"builders_max": 4}, "example-laptop")
    ck("the orchestrator alone: running, and the reviewer is starting", v["orchestrator"]["state"] == "running"
       and v["reviewer"]["state"] == "starting" and v["up"] is False and v["machine"] == "example-laptop"
       and v["builders"]["max"] == 4, v)
    v = co.crew_view([st("orchestrator", "idle"), st("reviewer", "waiting")], {}, "m")
    ck("both reporting: up", v["up"] is True and v["reviewer"]["state"] == "running")
    v = co.crew_view([st("orchestrator", "quiet"), st("reviewer", "idle")], {}, "m")
    ck("a quiet one has not ended: up, shown as quiet", v["up"] is True and v["orchestrator"]["state"] == "quiet", v)
    v = co.crew_view([st("orchestrator", "ended"), st("reviewer", "idle")], {}, "m")
    ck("an ended one is stopped and the crew is not up", v["orchestrator"]["state"] == "stopped"
       and v["up"] is False, v)
    v = co.crew_view([st("orchestrator", "idle"), st("reviewer", "idle"), st("builder", "working", "b1"),
                      st("builder", "ended", "b2"), st("builder", "idle", "b3")], {"builders_max": 2}, "m")
    ck("builders counted live, not ended ones", v["builders"] == {"live": 2, "max": 2}, v["builders"])
    ck("last heard from is a time", v["orchestrator"]["last_at"] == NOW.isoformat(), v["orchestrator"])

    print("== a crew that ran before the report existed ==")
    old = Store()
    old.list_crews = lambda: [{"machine": "old-box", "person_id": "p1"}]
    name = lambda pid: co.payload(old, [], {}, co.machine_key(pid), pid)["machine"]["name"]
    ck("its person's home names the machine of their own crew", name("p1") == "old-box")
    ck("another person's home does not take it", name("p2") == "" and name(None) == "")

    print("== the words ==")
    texts = [co.choices_text(co.choices(full)), co.github_text(type("S", (), {
        "list_github_credentials": staticmethod(lambda: [{"account": "example-org"}])})())]
    found = {w for t in texts for w in words.offenders(t)}
    ck("no forbidden word and no em dash in what the cell words", not found
       and not any(chr(0x2014) in t for t in texts), found)


async def main():
    from tares.daemon import make_app
    app = make_app()
    store = app.state.store
    fx = factory_api.register(FastAPI(), SimpleNamespace(store=store, cc_source=lambda: "claude_code", hidden_ids=lambda: set()))
    mk = lambda token=TOKEN: httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t",
        headers={"Authorization": f"Bearer {token}"} if token else {})

    def home():
        fh._cache.clear()
        return fh.home(store, fx, fh.now_utc())

    async with app.router.lifespan_context(app):
        print("== a new cell ==")
        b = home()["onboarding"]
        ck("nothing chosen, nothing connected, no machine, no crew",
           b["choices"]["chosen"] is False and "tracker" not in b and b["github"]["connected"] is False
           and b["machine"]["name"] == "" and b["crew"]["up"] is False, b)
        ck("the form is told the defaults and the recommendation", b["choices"]["defaults"] == co.defaults())
        ck("loading the home saved nothing (the form saves only on confirm)",
           store.get_setting("crew_settings") is None and store.get_setting(co.MACHINE_SETTING) is None)

        print("== the machine report call ==")
        ana = store.create_person("Ana Example", email="ana@example.test", via="admin")
        ben = store.create_person("Ben Example", email="ben@example.test", via="admin")
        cy = store.create_person("Cy Example", email="cy@example.test", via="admin")
        async with mk() as cx:
            ka = (await cx.post("/api/keys", json={"name": "ana", "scopes": ["read", "ingest"], "person_id": ana["id"]})).json()["secret"]
            kb = (await cx.post("/api/keys", json={"name": "ben", "scopes": ["read", "ingest"], "person_id": ben["id"]})).json()["secret"]
            kc = (await cx.post("/api/keys", json={"name": "cy", "scopes": ["read", "ingest"], "person_id": cy["id"]})).json()["secret"]
            kn = (await cx.post("/api/keys", json={"name": "nobody", "scopes": ["read", "ingest"]})).json()["secret"]
            kr = (await cx.post("/api/keys", json={"name": "rea", "scopes": ["read"]})).json()["secret"]
            r = await cx.post("/api/crew/machine", json={"machine": "cell-machine", "version": "1.57.0"})
            ck("the root token reports the cell's own machine", r.status_code == 200 and r.json() == {
                "machine": "cell-machine", "version": "1.57.0"}, r.text)
            ck("kept under the cell's key", json.loads(store.get_setting(co.MACHINE_SETTING))["machine"] == "cell-machine")
        async with mk(ka) as ax, mk(kb) as bx, mk(kn) as nn, mk(kr) as rx, mk(None) as nx:
            await ax.get("/api/home")      # the home is cached for a few seconds: the report must clear it
            r = await ax.post("/api/crew/machine", json={"machine": "ana-laptop", "version": "1.58.1"})
            ck("a key with a person that may ingest reports, and the last report wins", r.status_code == 200
               and r.json() == {"machine": "ana-laptop", "version": "1.58.1"}, r.text)
            r = await nn.post("/api/crew/machine", json={"machine": "x", "version": "1"})
            ck("an ingest key with no person is refused, like the crew's settings", r.status_code == 403
               and "no person" in r.text, (r.status_code, r.text))
            ck("and saved nothing", json.loads(store.get_setting(co.MACHINE_SETTING))["machine"] == "cell-machine"
               and store.get_setting("crew_machine:None") is None)
            ck("a read key is refused", (await rx.post("/api/crew/machine", json={"machine": "x"})).status_code == 403)
            ck("no credential is refused", (await nx.post("/api/crew/machine", json={"machine": "x"})).status_code == 401)
            r = await bx.post("/api/crew/machine", json={"machine": "ben-desktop", "version": "1.57.2"})
            ck("a second person reports their own", r.status_code == 200, r.text)
            mine = lambda pid: json.loads(store.get_setting(co.machine_key(pid)))
            ck("each person keeps their own machine, and the cell's is untouched",
               mine(ana["id"])["machine"] == "ana-laptop" and mine(ben["id"])["machine"] == "ben-desktop"
               and json.loads(store.get_setting(co.MACHINE_SETTING))["machine"] == "cell-machine",
               [store.get_setting(k) for k in (co.machine_key(ana["id"]), co.machine_key(ben["id"]), co.MACHINE_SETTING)])
            cell_report = json.loads(store.get_setting(co.MACHINE_SETTING))
            ha = (await ax.get("/api/home")).json()["onboarding"]["machine"]
            hb = (await bx.get("/api/home")).json()["onboarding"]["machine"]
            ck("each home says its own machine and version, never the other's",
               (ha["name"], ha["version"]) == ("ana-laptop", "1.58.1") and (hb["name"], hb["version"]) == ("ben-desktop", "1.57.2"),
               (ha, hb))
            ck("a person who never reported has none (the cell's is not theirs)",
               (await mk(kc).get("/api/home")).json()["onboarding"]["machine"]["name"] == "")
            nh = (await nn.get("/api/home")).json()["onboarding"]["machine"]
            ck("a key with no person sees no machine report, not the cell's",
               nh["name"] == "" and nh["version"] == "" and cell_report["machine"] == "cell-machine", (nh, cell_report))
            cw = factory_api.cell_wide
            ck("one rule for who speaks for the cell: the system credential and `local`, `local` needs admin for a write",
               cw(True, None, lambda: False) and cw(False, {"id": "local"}, lambda: True)
               and not cw(False, {"id": "local"}, lambda: False)
               and cw(False, {"id": "local"}, lambda: False, admin_only=False)
               and not cw(False, {"id": ana["id"]}, lambda: True, admin_only=False)
               and not cw(False, None, lambda: True, admin_only=False))
            ck("the report holds no key", all(t not in json.dumps([store.get_setting(co.MACHINE_SETTING),
                                                                    store.get_setting(co.machine_key(ana["id"]))])
                                              for t in (TOKEN, ka, kb)))
            ck("the home ticks install and sign in within the same call, no cache wait",
               {s["key"]: s["done"] for s in (await ax.get("/api/home")).json()["start"]["steps"]}
               == {"setup": False, "install": True, "login": True, "crew": False})
            ck("a body without a name or version saves what is there",
               (await ax.post("/api/crew/machine", json={})).json() == {"machine": "ana-laptop",
                                                                         "version": "1.58.1"})
            at = mine(ana["id"])["at"]
            await ax.post("/api/crew/machine", json={"machine": " ", "version": "\u0007"})
            ck("an empty report does not move the time", mine(ana["id"])["at"] == at, mine(ana["id"]))

        print("== where tickets live is not asked here any more ==")
        async with mk() as cx:
            r = await cx.put("/api/crew/tracker", json={"tracker": "tares"})
            ck("the old route is gone: tickets are chosen on /setup's Tickets step", r.status_code in (404, 405), r.status_code)

        print("== the choices, saved on confirm ==")
        async with mk() as cx:
            h = (await cx.get("/api/home")).json()
            ck("nothing chosen, no GitHub: setup waits", h["start"]["steps"][0]["done"] is False)
            store.upsert_github_credential("main", "ghp_example", "token", "", "example-org")
            fh._cache.clear()      # a credential written straight to the store: the API would clear it
            h = (await cx.get("/api/home")).json()
            ck("GitHub alone: setup still waits for the choices", h["start"]["steps"][0]["done"] is False
               and h["onboarding"]["github"] == {"connected": True, "text": "GitHub connected as example-org"},
               h["onboarding"]["github"])
            ck("the token never reaches the home", "ghp_example" not in json.dumps(h))
            r = await cx.put("/api/crew/settings", json={"autonomy": "L3-review", "builders_max": 3, "releaser": True})
            ck("saving the three choices", r.status_code == 200, r.text)
            h = (await cx.get("/api/home")).json()
            c = h["onboarding"]["choices"]
            ck("the home says chosen, with the values", c["chosen"] is True and c["autonomy"] == "L3-review"
               and c["builders_max"] == 3 and c["releaser"] is True, c)
            ck("the choices and GitHub: setup is done with its line", h["start"]["steps"][0]["done"] is True
               and h["start"]["steps"][0]["detail"]
               == "You approve each merge, 3 builders, releases on. GitHub connected as example-org",
               h["start"]["steps"][0])

        print("== the crew coming up ==")
        real = fx.stations
        rows = [st("orchestrator", "working")]
        fx.stations = lambda project=None: rows
        try:
            h = home()
            ck("the orchestrator first: the reviewer is starting and the crew is not up",
               h["onboarding"]["crew"]["orchestrator"]["state"] == "running"
               and h["onboarding"]["crew"]["reviewer"]["state"] == "starting"
               and h["start"]["steps"][3]["done"] is False, h["onboarding"]["crew"])
            rows.append(st("reviewer", "idle"))
            h = home()
            ck("then the reviewer: the crew is up, with the machine named",
               h["onboarding"]["crew"]["up"] is True and h["start"]["steps"][3]["done"] is True
               and h["start"]["steps"][3]["detail"] == "Running on cell-machine", h["start"]["steps"][3])

            print("== the old cell: a crew that was running before this panel existed ==")
            store.set_setting(co.MACHINE_SETTING, None)
            h = home()
            ck("it is up and ticks install and sign in",
               h["onboarding"]["crew"]["up"] is True
               and h["start"]["steps"][1]["done"] and h["start"]["steps"][2]["done"], h["start"]["steps"])
            ck("with no machine name the crew line has none", h["start"]["steps"][3]["detail"] == "Running")
        finally:
            fx.stations = real


units()
asyncio.run(main())
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
