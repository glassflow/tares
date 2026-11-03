"""The Start checklist of the factory home (TR-756): tares/start_steps.py, its place in
GET /api/home, and hiding it per credential (POST and DELETE /api/home/start/hide).

Five steps in a fixed order with fixed words; each is done from one read of the store; the block
hides when everything is done or when the caller hid it, for that caller only; the words pass
tares/words.py; the cloud cell and the open source cell differ in two rows.

Run: .venv/bin/python tests/test_factory_start.py
"""
import asyncio
import os
import sys
import tempfile
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_TMP = tempfile.mkdtemp(prefix="tares-factory-start-")
os.environ["TARES_DB"] = os.path.join(_TMP, "api.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(_TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-start"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
os.environ["TARES_EDITION"] = "factory"
for _k in ("TARES_PROJECT", "TARES_PROJECT_NAME", "TARES_FACTORY_URL", "TARES_FACTORY_MCP_URL",
           "TARES_FACTORY_TOKEN_FILE", "TARES_FACTORY_CRED_FILE", "FACTORY_PROJECT",
           "TARES_LOGIN_URL", "TARES_GITHUB_CONNECT_URL", "TARES_LINEAR_CONNECT_URL"):
    os.environ.pop(_k, None)

import httpx
from fastapi import FastAPI

from tares import factory_api
from tares import factory_home as fh
from tares import start_steps as ss
from tares import words

fh.WAITING_BUILDERS.clear()

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


KEYS = ["claude_code", "github", "tickets", "crew", "spec"]
ALL = dict(key_used=True, github_connected=True, linear_connected=True, stations=2,
           spec_with_tickets=True)


def done_of(ctx):
    return {s["key"]: s["done"] for s in ss.factory_steps(ctx)}


def step(ctx, key):
    return next(s for s in ss.factory_steps(ctx) if s["key"] == key)


def strings(steps):
    out = []
    for s in steps:
        out += [s["title"], s["gets"], s.get("detail") or ""]
        for a in s["actions"]:
            out += [a["label"], a.get("copy") or ""]
    return out


class Store:
    """The two settings calls the block uses."""
    def __init__(self):
        self.d = {}

    def get_setting(self, k):
        return self.d.get(k)

    def set_setting(self, k, v):
        if v is None:
            self.d.pop(k, None)
        else:
            self.d[k] = v


def units():
    print("== the steps ==")
    steps = ss.factory_steps({})
    ck("five steps in order", [s["key"] for s in steps] == KEYS, [s["key"] for s in steps])
    ck("the titles and what each gives, word for word",
       [(s["title"], s["gets"]) for s in steps] == [
           ("Sign in from Claude Code", "Your Claude Code sessions and the crew report here."),
           ("Connect GitHub", "The crew opens pull requests and Tares follows their reviews and checks."),
           ("Connect Linear or GitHub Issues", "Tickets stay where your team already tracks work."),
           ("Start the crew", "Claude Code sessions that build, review and merge your tickets."),
           ("Write your first spec", "A plan with milestones and tickets the crew can build.")],
       [(s["title"], s["gets"]) for s in steps])
    ck("every row has done and actions, nothing done on an empty cell",
       all(set(s) >= {"key", "title", "gets", "done", "actions"} and isinstance(s["actions"], list)
           for s in steps) and not any(s["done"] for s in steps))
    ck("a ctx as a dict or as the dataclass is the same",
       ss.factory_steps(ss.StartCtx(**ALL)) == ss.factory_steps(ALL))
    ck("a dict with a name it does not know is fine", ss.factory_steps({"nope": 1}) == steps)

    print("== each rule of done ==")
    ck("everything true: everything done, no action anywhere",
       all(done_of(ALL).values()) and all(s["actions"] == [] for s in ss.factory_steps(ALL)))
    ck("everything false: nothing done", not any(done_of({}).values()))
    for label, name, key, bad in (
            ("a key was used", "key_used", "claude_code", True),
            ("the claude_code connector got events", "claude_code_events", "claude_code", True),
            ("a GitHub credential is saved", "github_connected", "github", True),
            ("Linear is connected", "linear_connected", "tickets", True),
            ("a project keeps its tickets in GitHub Issues", "github_issues_linked", "tickets", True),
            ("a station reported", "stations", "crew", 1),
            ("a factory project has tickets", "spec_with_tickets", "spec", True)):
        d = done_of({name: bad})
        ck(f"{label}: only {key} is done", d[key] is True and sum(d.values()) == 1, d)
    ck("no station is not done", done_of({"stations": 0})["crew"] is False)
    ck("Linear and GitHub Issues are either one", done_of({"linear_connected": True})["tickets"]
       and done_of({"github_issues_linked": True})["tickets"]
       and not done_of({"github_connected": True})["tickets"])

    print("== what a person can do on each row ==")
    o = {}
    ck("sign in on an open source cell copies tares mcp",
       step(o, "claude_code")["actions"] == [{"label": "Copy the command", "copy": "tares mcp"}],
       step(o, "claude_code")["actions"])
    ck("sign in on a cloud cell copies tares login",
       step({"cloud": True}, "claude_code")["actions"] == [{"label": "Copy the command", "copy": "tares login"}])
    ck("GitHub on an open source cell goes to the tools in Settings",
       step(o, "github")["actions"] == [{"label": "Connect GitHub", "to": "/settings#tools"}],
       step(o, "github")["actions"])
    cloud = {"cloud": True, "github_connect_url": "https://cp.example.test/connect/github",
             "linear_connect_url": "https://cp.example.test/connect/linear"}
    ck("GitHub on a cloud cell is the link out",
       step(cloud, "github")["actions"] == [{"label": "Connect GitHub",
                                              "href": "https://cp.example.test/connect/github"}],
       step(cloud, "github")["actions"])
    ck("a cloud cell with no GitHub link goes to Settings like the open source one",
       step({"cloud": True}, "github")["actions"] == step(o, "github")["actions"])
    t = step(o, "tickets")["actions"]
    ck("tickets: Linear in Settings, then GitHub Issues in the docs",
       [a["label"] for a in t] == ["Connect Linear", "Use GitHub Issues"] and t[0] == {
           "label": "Connect Linear", "to": "/settings#tools"}
       and t[1]["href"].startswith("https://docs.glassflow.ai/tares/"), t)
    ck("tickets on a cloud cell: Linear is the link out",
       step(cloud, "tickets")["actions"][0] == {"label": "Connect Linear",
                                                 "href": "https://cp.example.test/connect/linear"})
    ck("the crew and the spec each copy their command",
       step(o, "crew")["actions"] == [{"label": "Copy the command", "copy": "tares factory crew up"}]
       and step(o, "spec")["actions"] == [{"label": "Copy the command",
                                            "copy": 'tares factory spec "what you want to build"'}])
    ck("a button is one of to, href or copy",
       all(sum(k in a for k in ("to", "href", "copy")) == 1
           for c in (o, cloud) for s in ss.factory_steps(c) for a in s["actions"]))

    print("== the words ==")
    for label, c in (("open source", o), ("cloud", cloud), ("all done", ALL)):
        found = {w for x in strings(ss.factory_steps(c)) for w in words.offenders(x)}
        ck(f"{label}: no forbidden word and no em dash", not found, found)
        ck(f"{label}: no em dash in any string", not any(chr(0x2014) in x for x in strings(ss.factory_steps(c))))

    print("== checklist ==")
    st = Store()
    c = ss.checklist(st, ss.factory_steps({"github_connected": True, "key_used": True}), "ana")
    ck("the counts and show", c["done"] == 2 and c["total"] == 5 and c["show"] is True
       and len(c["steps"]) == 5, {k: v for k, v in c.items() if k != "steps"})
    c = ss.checklist(st, ss.factory_steps(ALL), "ana")
    ck("show is false when everything is done", c["show"] is False and c["done"] == 5, c["show"])
    ck("show is false when everything is done, for whoever", ss.checklist(Store(), ss.factory_steps(ALL), "bo")["show"] is False)
    ss.hide(st, "ana")
    some = ss.factory_steps({"key_used": True})
    ck("hidden by one person: not shown to that one", ss.checklist(st, some, "ana")["show"] is False)
    ck("hidden by one person: shown to another", ss.checklist(st, some, "bo")["show"] is True)
    ck("the setting is start_hidden:<who>", st.d == {"start_hidden:ana": "1"}, st.d)
    ss.unhide(st, "ana")
    ck("unhide shows it again and leaves no setting", ss.checklist(st, some, "ana")["show"] is True and st.d == {})


async def main():
    from tares.daemon import make_app
    app = make_app()
    store = app.state.store
    fx = factory_api.register(FastAPI(), store, SimpleNamespace(cc_source=lambda: "claude_code", hidden_ids=lambda: set()))
    mk = lambda token=TOKEN: httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t",
        headers={"Authorization": f"Bearer {token}"} if token else {})

    def start_of():
        fh._cache.clear()
        return fh.home(store, fx, fh.now_utc())["start"]

    def done_now():
        return {s["key"]: s["done"] for s in start_of()["steps"]}

    async with app.router.lifespan_context(app):
        print("== the block in the home of a new cell ==")
        s0 = start_of()
        ck("five steps, none done, shown", [s["key"] for s in s0["steps"]] == KEYS and s0["done"] == 0
           and s0["total"] == 5 and s0["show"] is True, s0["done"])
        ck("open source: tares mcp", s0["steps"][0]["actions"][0]["copy"] == "tares mcp")

        print("== each step reads the store ==")
        store.upsert_github_credential("main", "ghp_example", "token", "", "example-org")
        ck("a GitHub credential finishes Connect GitHub", done_now()["github"] is True)
        async with mk() as cx:
            made = (await cx.post("/api/keys", json={"name": "ana", "scopes": ["read"]})).json()
            await cx.post("/api/keys", json={"name": "bo", "scopes": ["read"]})
            ck("a key never used does not finish sign in", done_now()["claude_code"] is False)
            async with mk(made["secret"]) as ax:
                await ax.get("/api/whoami")
            ck("a key that was used finishes sign in", done_now()["claude_code"] is True)
            await cx.post("/api/projects", json={"template": "custom", "name": "billing-export",
                                                 "goal": "Invoices as a CSV file", "objects": [],
                                                 "kind": "software_factory"})
            pid = store.list_projects()[0]["id"]
            ck("a project with no ticket does not finish the spec", done_now()["spec"] is False)
            await cx.post(f"/api/projects/{pid}/tickets", json={"title": "Write the export query"})
            ck("a ticket finishes the spec", done_now()["spec"] is True)
            ck("tickets are not done yet", done_now()["tickets"] is False)
            store.set_project_github(pid, {"repo": "example-org/billing", "label": "tares"})
            ck("a GitHub Issues link finishes the tickets step", done_now()["tickets"] is True)
            store.set_project_github(pid, None)
            ck("and unlinking undoes it", done_now()["tickets"] is False)
            real = fx.stations
            fx.stations = lambda project=None: [{"name": "crew-orchestrator", "role": "orchestrator",
                                                 "state": "idle", "project": None, "last_at": None}]
            try:
                ck("a station finishes the crew step", done_now()["crew"] is True)
            finally:
                fx.stations = real
            ck("no station: not done again", done_now()["crew"] is False)

        print("== a cloud cell ==")
        os.environ["TARES_LOGIN_URL"] = "https://cp.example.test/login"
        os.environ["TARES_LINEAR_CONNECT_URL"] = "https://cp.example.test/connect/linear"
        try:
            s1 = start_of()
            ck("Connect Linear is the link out from the environment",
               s1["steps"][2]["actions"][0] == {"label": "Connect Linear",
                                                "href": "https://cp.example.test/connect/linear"},
               s1["steps"][2]["actions"])
        finally:
            os.environ.pop("TARES_LOGIN_URL")
            os.environ.pop("TARES_LINEAR_CONNECT_URL")
        ck("and back to Settings without it", start_of()["steps"][2]["actions"][0]["to"] == "/settings#tools")

        print("== hiding, per credential ==")
        async with mk() as cx:
            ka = (await cx.post("/api/keys", json={"name": "key-a", "scopes": ["read"]})).json()["secret"]
            kb = (await cx.post("/api/keys", json={"name": "key-b", "scopes": ["admin"]})).json()["secret"]
        async with mk(ka) as ax, mk(kb) as bx, mk(None) as nx:
            fh._cache.clear()
            a0 = (await ax.get("/api/home")).json()["start"]
            b0 = (await bx.get("/api/home")).json()["start"]
            ck("both see the block", a0["show"] is True and b0["show"] is True)
            r = await ax.post("/api/home/start/hide")
            ck("a read key may hide its own block", r.status_code == 200, r.text)
            ck("and gets its new start block back", r.json()["show"] is False and r.json()["total"] == 5
               and len(r.json()["steps"]) == 5, r.text[:200])
            ck("key-a sees it hidden at once, not after the cache",
               (await ax.get("/api/home")).json()["start"]["show"] is False)
            ck("key-b still sees it", (await bx.get("/api/home")).json()["start"]["show"] is True)
            ck("the setting is key-a's own", store.get_setting("start_hidden:key-a") == "1"
               and store.get_setting("start_hidden:key-b") is None)
            r = await ax.delete("/api/home/start/hide")
            ck("delete shows it again and returns the block", r.status_code == 200
               and r.json()["show"] is True, r.text[:200])
            ck("and the home agrees", (await ax.get("/api/home")).json()["start"]["show"] is True)
            ck("the setting is gone", store.get_setting("start_hidden:key-a") is None)
            await bx.post("/api/home/start/hide")
            ck("key-b hides its own", (await bx.get("/api/home")).json()["start"]["show"] is False
               and (await ax.get("/api/home")).json()["start"]["show"] is True)
            await bx.delete("/api/home/start/hide")
            ck("a call with no credential is refused",
               (await nx.post("/api/home/start/hide")).status_code == 401
               and (await nx.delete("/api/home/start/hide")).status_code == 401)
            async with mk() as rx:
                r = await rx.post("/api/home/start/hide")
                ck("the root token may", r.status_code == 200 and r.json()["show"] is False, r.text[:200])
                ck("and key-a is not touched", (await ax.get("/api/home")).json()["start"]["show"] is True)
                await rx.delete("/api/home/start/hide")

        print("== other editions ==")
        async with mk() as cx:
            body = (await cx.get("/api/home")).json()
            ck("the factory home carries start", "start" in body and body["edition"] == "factory")
            await cx.put("/api/settings/edition", json={"edition": "sre"})
            sre = (await cx.get("/api/home")).json()
            ck("the AI SRE home carries its own start block (TR-765, tests/test_sre_start.py)",
               sre["edition"] == "sre" and "headline" in sre and sre["start"]["total"] == 5, list(sre))
            r = await cx.post("/api/home/start/hide")
            ck("hide on the AI SRE edition answers with its block",
               r.status_code == 200 and r.json()["show"] is False, r.text)
            await cx.delete("/api/home/start/hide")
            await cx.put("/api/settings/edition", json={"edition": "platform"})
            r = await cx.post("/api/home/start/hide")
            ck("hide on the platform edition answers ok", r.status_code == 200 and r.json() == {"ok": True}, r.text)
            await cx.delete("/api/home/start/hide")
            r = await cx.get("/api/home")
            ck("platform still has no home", r.status_code == 404)
            await cx.put("/api/settings/edition", json={"edition": "factory"})


units()
asyncio.run(main())
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
