"""The first run of a new factory cell (TR-1312): tares/crew_onboarding.py, the two calls it adds
(POST /api/crew/machine, PUT /api/crew/tracker), and the `onboarding` block of GET /api/home.

The panel ticks from what the cell knows: the crew's settings, the GitHub and Linear connections, the
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
    ck("the form starts on the crew's own defaults, unchanged",
       (d["autonomy"], d["releaser"], d["builders_max"]) == (want["autonomy"], want["releaser"], want["builders_max"]), d)
    ck("and recommends L3-review", d["recommended"] == "L3-review")
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

    print("== where tickets live ==")
    s = Store()
    ck("nothing known: no place", co.tracker(s, False, False, False) is None)
    ck("Linear connected", co.tracker(s, True, False, False) == "linear")
    ck("a project in GitHub Issues", co.tracker(s, False, True, False) == "github_issues")
    ck("a crew that is up with nothing else: Tares itself", co.tracker(s, False, False, True) == "tares")
    ck("a saved choice wins over what is connected", co.save_tracker(s, "github_issues") == "github_issues"
       and co.tracker(s, True, False, True) == "github_issues")
    bad = False
    try:
        co.save_tracker(s, "jira")
    except ValueError:
        bad = True
    ck("a place that is not one of the three is refused and not saved", bad and co.saved_tracker(s) == "github_issues")
    s.set_setting(co.TRACKER_SETTING, "jira")
    ck("a saved value that is not one of the three reads as no choice", co.saved_tracker(s) is None)
    ck("done needs GitHub", co.tracker_done("tares", False, False) is False)
    ck("done needs a place", co.tracker_done(None, True, False) is False)
    ck("Tares and GitHub Issues need only GitHub", co.tracker_done("tares", True, False)
       and co.tracker_done("github_issues", True, False))
    ck("Linear needs Linear connected", co.tracker_done("linear", True, False) is False
       and co.tracker_done("linear", True, True) is True)

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
    r = co.save_machine(s, "exämple\x00-pc\n" + "x" * 200, "1.0", NOW)
    ck("only printable ASCII, and short", r["machine"].startswith("exmple-pc") and len(r["machine"]) <= 80
       and "\n" not in r["machine"], r)
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
    fx = factory_api.register(FastAPI(), store, SimpleNamespace(cc_source=lambda: "claude_code", hidden_ids=lambda: set()))
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
           b["choices"]["chosen"] is False and b["tracker"] is None and b["github"]["connected"] is False
           and b["machine"]["name"] == "" and b["crew"]["up"] is False, b)
        ck("the form is told the defaults and the recommendation", b["choices"]["defaults"] == co.defaults())
        ck("loading the home saved nothing (the form saves only on confirm)",
           store.get_setting("crew_settings") is None and store.get_setting(co.TRACKER_SETTING) is None
           and store.get_setting(co.MACHINE_SETTING) is None)

        print("== the machine report call ==")
        async with mk() as cx:
            ka = (await cx.post("/api/keys", json={"name": "ana", "scopes": ["read", "ingest"]})).json()["secret"]
            kr = (await cx.post("/api/keys", json={"name": "rea", "scopes": ["read"]})).json()["secret"]
            r = await cx.post("/api/crew/machine", json={"machine": "example-laptop", "version": "1.58.0"})
            ck("the root token reports a machine", r.status_code == 200 and r.json() == {
                "machine": "example-laptop", "version": "1.58.0"}, r.text)
        async with mk(ka) as ax, mk(kr) as rx, mk(None) as nx:
            await ax.get("/api/home")      # the home is cached for a few seconds: the report must clear it
            r = await ax.post("/api/crew/machine", json={"machine": "example-laptop", "version": "1.58.1"})
            ck("a key that may ingest reports, and the last report wins", r.status_code == 200
               and r.json()["version"] == "1.58.1", r.text)
            ck("a read key is refused", (await rx.post("/api/crew/machine", json={"machine": "x"})).status_code == 403)
            ck("no credential is refused", (await nx.post("/api/crew/machine", json={"machine": "x"})).status_code == 401)
            ck("the report holds no key", TOKEN not in json.dumps(store.get_setting(co.MACHINE_SETTING))
               and ka not in json.dumps(store.get_setting(co.MACHINE_SETTING)))
            ck("the home ticks install and sign in within the same call, no cache wait",
               {s["key"]: s["done"] for s in (await ax.get("/api/home")).json()["start"]["steps"]}
               == {"choices": False, "connections": False, "install": True, "login": True, "crew": False,
                   "spec": False})
            h = (await ax.get("/api/home")).json()
            ck("and says which machine and version", h["onboarding"]["machine"]["name"] == "example-laptop"
               and h["onboarding"]["machine"]["version"] == "1.58.1", h["onboarding"]["machine"])
            ck("a body without a name or version saves what is there",
               (await ax.post("/api/crew/machine", json={})).json() == {"machine": "example-laptop",
                                                                         "version": "1.58.1"})

        print("== where tickets live, over the API ==")
        async with mk() as cx, mk(ka) as ax:
            r = await cx.put("/api/crew/tracker", json={"tracker": "jira"})
            ck("a place that is not one of the three is a 400", r.status_code == 400, r.text)
            ck("and saved nothing", co.saved_tracker(store) is None)
            r = await ax.put("/api/crew/tracker", json={"tracker": "tares"})
            ck("an ingest key may not choose it for the cell", r.status_code == 403, (r.status_code, r.text))
            await cx.get("/api/home")      # cached for a few seconds: saving must clear it
            r = await cx.put("/api/crew/tracker", json={"tracker": "tares"})
            ck("the root token saves it", r.status_code == 200 and r.json() == {"tracker": "tares"}, r.text)
            ck("the home reads it at once", (await cx.get("/api/home")).json()["onboarding"]["tracker"] == "tares")

        print("== the choices, saved on confirm ==")
        async with mk() as cx:
            h = (await cx.get("/api/home")).json()
            ck("GitHub not connected: step 2 waits", h["start"]["steps"][1]["done"] is False)
            store.upsert_github_credential("main", "ghp_example", "token", "", "example-org")
            await cx.put("/api/crew/tracker", json={"tracker": "tares"})
            h = (await cx.get("/api/home")).json()
            ck("GitHub and a place: step 2 done, with its line", h["start"]["steps"][1]["done"] is True
               and h["start"]["steps"][1]["detail"] == "GitHub connected as example-org, tickets in Tares",
               h["start"]["steps"][1])
            ck("the token never reaches the home", "ghp_example" not in json.dumps(h))
            r = await cx.put("/api/crew/settings", json={"autonomy": "L3-review", "builders_max": 3, "releaser": True})
            ck("saving the three choices", r.status_code == 200, r.text)
            h = (await cx.get("/api/home")).json()
            c = h["onboarding"]["choices"]
            ck("the home says chosen, with the values", c["chosen"] is True and c["autonomy"] == "L3-review"
               and c["builders_max"] == 3 and c["releaser"] is True, c)
            ck("step 1 is done with its line", h["start"]["steps"][0]["done"] is True
               and h["start"]["steps"][0]["detail"] == "You approve each merge, 3 builders, releases on",
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
               and h["start"]["steps"][4]["done"] is False, h["onboarding"]["crew"])
            rows.append(st("reviewer", "idle"))
            h = home()
            ck("then the reviewer: the crew is up, with the machine named",
               h["onboarding"]["crew"]["up"] is True and h["start"]["steps"][4]["done"] is True
               and h["start"]["steps"][4]["detail"] == "Running on example-laptop", h["start"]["steps"][4])

            print("== the old cell: a crew that was running before this panel existed ==")
            store.set_setting(co.MACHINE_SETTING, None)
            store.set_setting(co.TRACKER_SETTING, None)
            h = home()
            ck("it is up, ticks install and sign in, and tickets default to Tares",
               h["onboarding"]["crew"]["up"] is True and h["onboarding"]["tracker"] == "tares"
               and h["start"]["steps"][2]["done"] and h["start"]["steps"][3]["done"], h["start"]["steps"])
            ck("with no machine name the crew line has none", h["start"]["steps"][4]["detail"] == "Running")
        finally:
            fx.stations = real


units()
asyncio.run(main())
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
