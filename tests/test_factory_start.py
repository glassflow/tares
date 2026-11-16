"""The Start checklist of the factory home (TR-756): tares/start_steps.py, its place in
GET /api/home, and hiding it per credential (POST and DELETE /api/home/start/hide).

Four steps in a fixed order with fixed words (TR-1312): Set up your factory, done on /setup, then three
terminal commands (planning the first project comes after setup, on the home); each is done from one read of the store; the block hides when everything is done or
when the caller hid it, for that caller only; the words pass tares/words.py. A terminal step keeps its
command once done, so /setup can always show it.

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

# a regression in the TARES_KEYRING gate must fail this test, never reach a person's OS keychain
os.environ["PYTHON_KEYRING_BACKEND"] = "keyring.backends.fail.Keyring"
try:
    import keyring  # noqa: E402
    import keyring.backends.fail  # noqa: E402
    keyring.set_keyring(keyring.backends.fail.Keyring())
except ImportError:
    pass

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


KEYS = ["setup", "install", "login", "crew"]
ALL = dict(choices_made=True, github_connected=True, cli_version="1.0.0", machine_name="example-laptop",
           crew_up=True)


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
    ck("four steps in order: planning the first project is not a step of setup", [s["key"] for s in steps] == KEYS, [s["key"] for s in steps])
    ck("the titles and what each gives, word for word",
       [(s["title"], s["gets"]) for s in steps] == [
           ("Set up your factory",
            "How your crew works, your GitHub, and where tickets, reviews and checks live."),
           ("Install Tares", "The command that starts and watches the crew."),
           ("Sign your machine in", "So the crew on your machine reports here."),
           ("Start the crew",
            "It reads your choices from this workspace and asks only what belongs to your machine.")],
       [(s["title"], s["gets"]) for s in steps])
    ck("the first is done in the console, the last three in a terminal",
       [s["where"] for s in steps] == ["workspace", "terminal", "terminal", "terminal"])
    ck("every row has done and actions, nothing done on an empty cell",
       all(set(s) >= {"key", "title", "gets", "done", "actions", "where"} and isinstance(s["actions"], list)
           for s in steps) and not any(s["done"] for s in steps))
    ck("a ctx as a dict or as the dataclass is the same",
       ss.factory_steps(ss.StartCtx(**ALL)) == ss.factory_steps(ALL))
    ck("a dict with a name it does not know is fine", ss.factory_steps({"nope": 1}) == steps)

    print("== each rule of done ==")
    ck("everything true: everything done, no action anywhere",
       all(done_of(ALL).values()) and all(s["actions"] == [] for s in ss.factory_steps(ALL)))
    ck("everything false: nothing done", not any(done_of({}).values()))
    ck("the choices alone do not finish setup", done_of({"choices_made": True})["setup"] is False)
    ck("GitHub alone does not finish setup", done_of({"github_connected": True})["setup"] is False)
    d = done_of({"choices_made": True, "github_connected": True})
    ck("the choices and GitHub finish setup, and only setup", d["setup"] is True and sum(d.values()) == 1, d)
    d = done_of({"cli_version": "1.2.3"})
    ck("a reported version finishes install only", d["install"] and not d["login"] and sum(d.values()) == 1, d)
    d = done_of({"machine_name": "example-laptop"})
    ck("a machine that signed in finishes install and sign in", d["install"] and d["login"]
       and not d["crew"] and sum(d.values()) == 2, d)
    d = done_of({"crew_up": True})
    ck("a crew that is up finishes install, sign in and the crew", d["install"] and d["login"] and d["crew"]
       and sum(d.values()) == 3, d)
    ck("done rows say what was chosen",
       step({"choices_made": True, "github_connected": True,
             "setup_text": "You approve each merge, 2 builders, no releases. GitHub connected as example-org"},
            "setup")["detail"] == "You approve each merge, 2 builders, no releases. GitHub connected as example-org"
       and step({"cli_version": "1.2.3"}, "install")["detail"] == "Installed: tares 1.2.3."
       and step({"machine_name": "example-laptop"}, "login")["detail"] == "Signed in from example-laptop.")

    print("== what a person can do on each row ==")
    o = {}
    cloud = {"cloud": True}
    ck("Set up your factory opens /setup",
       step(o, "setup")["actions"] == [{"label": "Set up your factory", "to": "/setup"}], step(o, "setup")["actions"])
    ck("the three commands are copied word for word",
       [step(o, k)["actions"] for k in ("install", "login", "crew")] == [
           [{"label": "Copy the command", "copy": "uv tool install tares"}],
           [{"label": "Copy the command", "copy": "tares login"}],
           [{"label": "Copy the command", "copy": "tares factory crew up"}]])
    ck("a terminal step keeps its command, done or not",
       [step(c, k)["command"] for c in (o, ALL) for k in ("install", "login", "crew")]
       == ["uv tool install tares", "tares login", "tares factory crew up"] * 2)
    ck("the command that plans a project is kept for the home", ss.SPEC_COMMAND == "tares factory spec")
    ck("Set up your factory has no command", "command" not in step(o, "setup"))
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
    c = ss.checklist(st, ss.factory_steps({"choices_made": True, "github_connected": True}), "ana")
    ck("the counts and show", c["done"] == 1 and c["total"] == 4 and c["show"] is True
       and len(c["steps"]) == 4, {k: v for k, v in c.items() if k != "steps"})
    c = ss.checklist(st, ss.factory_steps(ALL), "ana")
    ck("show is false when everything is done", c["show"] is False and c["done"] == 4, c["show"])
    ck("show is false when everything is done, for whoever", ss.checklist(Store(), ss.factory_steps(ALL), "bo")["show"] is False)
    ss.hide(st, "ana")
    some = ss.factory_steps({"choices_made": True})
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
        ck("four steps, none done, shown", [s["key"] for s in s0["steps"]] == KEYS and s0["done"] == 0
           and s0["total"] == 4 and s0["show"] is True, s0["done"])
        ck("Set up your factory opens /setup",
           s0["steps"][0]["actions"] == [{"label": "Set up your factory", "to": "/setup"}])

        print("== each step reads the store ==")
        async with mk() as cx:
            await cx.put("/api/crew/settings", json={"autonomy": "L3-review", "builders_max": 2,
                                                     "releaser": False})
        ck("the three choices alone do not finish setup (no GitHub yet)", done_now()["setup"] is False)
        store.upsert_github_credential("main", "ghp_example", "token", "", "example-org")
        ck("the choices and GitHub finish setup", done_now()["setup"] is True)
        ck("and the row says what was chosen",
           next(s for s in start_of()["steps"] if s["key"] == "setup")["detail"]
           == "You approve each merge, 2 builders, no releases. GitHub connected as example-org")
        async with mk() as cx:
            await cx.post("/api/projects", json={"template": "custom", "name": "billing-export",
                                                 "goal": "Invoices as a CSV file", "objects": [],
                                                 "kind": "software_factory"})
            ck("a project is not a step: the steps stay the four", list(done_now()) == KEYS)
            real = fx.stations
            now = fh.now_utc()
            row = lambda role, state: {"name": "crew-" + role, "role": role, "state": state,
                                       "project": None, "last_at": now}
            fx.stations = lambda project=None: [row("orchestrator", "idle")]
            try:
                ck("an orchestrator alone does not finish the crew step", done_now()["crew"] is False)
                fx.stations = lambda project=None: [row("orchestrator", "idle"), row("reviewer", "working")]
                d = done_now()
                ck("the orchestrator and the reviewer finish the crew step, and install and sign in with it",
                   d["crew"] and d["install"] and d["login"], d)
            finally:
                fx.stations = real
            ck("no station: not done again", done_now()["crew"] is False)

        print("== a cloud cell ==")
        before = start_of()
        os.environ["TARES_LOGIN_URL"] = "https://cp.example.test/login"
        os.environ["TARES_GITHUB_CONNECT_URL"] = "https://cp.example.test/connect/github"
        try:
            ck("the connect links live on /setup: the cloud's environment changes no step here",
               start_of() == before)
        finally:
            os.environ.pop("TARES_LOGIN_URL")
            os.environ.pop("TARES_GITHUB_CONNECT_URL")

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
            ck("and gets its new start block back", r.json()["show"] is False and r.json()["total"] == 4
               and len(r.json()["steps"]) == 4, r.text[:200])
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
