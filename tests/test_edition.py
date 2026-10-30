"""The cell's edition (TR-724): factory, sre or platform, stored in the cell.

TARES_EDITION seeds it on the first start, a cell that already holds data becomes platform, the
stored value wins on every later start, and PUT /api/settings/edition (admin) is the only switch.
GET /api/home answers per edition, and the screen registry starts empty.
"""
import asyncio
import os
import signal
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _env import clean_env

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tares import edition as ed
from tares import screens
from tares.store import Store

PASS = FAIL = 0
PORT, MCP_PORT = "8852", "8853"
ROOT = "root-edition"
B = f"http://127.0.0.1:{PORT}"
# whoami on an open instance today: nothing but these, plus the three edition keys
TODAY_KEYS = {"id", "name", "scopes"}


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1; print(f"  ok   {label}")
    else:
        FAIL += 1; print(f"  FAIL {label}  {detail}")


class Fake:
    """What settle() and edition() touch."""
    DEFAULT_PROJECT_NAME = "Default"

    def __init__(self, projects=(), sources=(), **kv):
        self.kv, self.projects, self.sources = dict(kv), list(projects), list(sources)

    def get_setting(self, k):
        return self.kv.get(k)

    def set_setting(self, k, v):
        if v is None:
            self.kv.pop(k, None)
        else:
            self.kv[k] = v

    def list_projects(self):
        return [{"name": n} for n in self.projects]

    def list_catalog_sources(self):
        return self.sources


def unit():
    print("== where the edition comes from ==")
    os.environ.pop(ed.EDITION_ENV, None)
    s = Fake()
    check("empty store, no env: unset", ed.settle(s) is None and ed.edition(s) == ("platform", True))
    s = Fake()
    os.environ[ed.EDITION_ENV] = "factory"
    check("empty store, env factory: stored", ed.settle(s) == "factory"
          and s.kv["edition"] == "factory" and ed.edition(s) == ("factory", False))
    s = Fake()
    os.environ[ed.EDITION_ENV] = "SRE "
    check("env is trimmed and lowercased", ed.settle(s) == "sre" and s.kv["edition"] == "sre")
    s = Fake()
    os.environ[ed.EDITION_ENV] = "bogus"
    check("an unknown env value leaves it unset", ed.settle(s) is None and "edition" not in s.kv)
    os.environ.pop(ed.EDITION_ENV)
    s = Fake(projects=["Default"])
    check("only the Default project is not data", ed.settle(s) is None)
    s = Fake(projects=["Default", "project-x"])
    check("a real project makes it platform", ed.settle(s) == "platform" and s.kv["edition"] == "platform")
    s = Fake(sources=[{"name": "svc"}])
    check("a catalog source makes it platform", ed.settle(s) == "platform")
    s = Fake(projects=["project-x"])
    os.environ[ed.EDITION_ENV] = "sre"
    check("env wins over the has-data default", ed.settle(s) == "sre")
    s = Fake(edition="sre")
    os.environ[ed.EDITION_ENV] = "factory"
    check("the stored value wins over env", ed.settle(s) == "sre" and s.kv["edition"] == "sre"
          and ed.edition(s) == ("sre", False))
    s = Fake(edition="nonsense")
    os.environ.pop(ed.EDITION_ENV)
    check("a bad stored value counts as unset", ed.edition(s) == ("platform", True))

    print("== the real store ==")
    store = Store(os.path.join(tempfile.mkdtemp(), "t.duckdb"))
    check("a fresh store has no data", not ed.store_has_data(store))
    check("a fresh store settles to nothing", ed.settle(store) is None)
    store.create_project("p1", "custom", "project-x", {})
    check("a project in the real store counts as data", ed.store_has_data(store))
    check("and settles to platform", ed.settle(store) == "platform" and ed.edition(store) == ("platform", False))

    print("== the screen registry ==")
    check("no screens yet", screens.SCREENS == [])
    check("register_all with none does nothing", screens.register_all(None, None, None, None) is None)


def start(tmp, **env):
    e = {**clean_env(), "TARES_DB": os.path.join(tmp, "t.duckdb"), "TARES_HOME": tmp,
         "TARES_PORT": PORT, "TARES_MCP_PORT": MCP_PORT, "TARES_OTLP_GRPC_PORT": "off", **env}
    for k in ("TARES_SLACK_BOT_TOKEN", "TARES_SLACK_SIGNING_SECRET", "TARES_CATALOG", "TARESD_URL",
              "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        e.pop(k, None)
    for k in [k for k, v in env.items() if v is None]:
        e.pop(k, None)
    return subprocess.Popen([sys.executable, "-c", "from tares.cli import run_daemon; run_daemon()"],
                            env=e, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


async def wait_up(cx):
    for _ in range(80):
        try:
            if (await cx.get(f"{B}/health")).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        await asyncio.sleep(0.5)


def stop(proc):
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()


async def seeding_order():
    print("== a seeded empty cell is not settled as platform ==")
    proc = start(tempfile.mkdtemp(), TARES_SEED_PROJECT="ai_sre_demo")
    try:
        async with httpx.AsyncClient(timeout=10) as cx:
            await wait_up(cx)
            w = (await cx.get(f"{B}/api/whoami")).json()
            check("still needs an edition", w["edition"] == "platform" and w["edition_needed"] is True, str(w))
            names = [p["name"] for p in (await cx.get(f"{B}/api/projects")).json()["projects"]]
            check("and the seeded project exists", any(n != "Default" for n in names), str(names))
    finally:
        stop(proc)


async def api():
    print("== the API on a running daemon ==")
    tmp = tempfile.mkdtemp()
    root = {"Authorization": f"Bearer {ROOT}"}
    proc = start(tmp, TARES_AUTH_TOKEN=ROOT, TARES_EDITION="factory")
    try:
        async with httpx.AsyncClient(timeout=10) as cx:
            await wait_up(cx)
            w = (await cx.get(f"{B}/api/whoami", headers=root)).json()
            check("root sees factory", w.get("edition") == "factory" and w.get("edition_name") == "Software Factory"
                  and w.get("edition_needed") is False, str(w))
            k = (await cx.post(f"{B}/api/keys", headers=root, json={"name": "r", "scopes": ["read"]})).json()
            rd = {"Authorization": f"Bearer {k['secret']}"}
            w = (await cx.get(f"{B}/api/whoami", headers=rd)).json()
            check("a read key sees it too", w.get("edition") == "factory", str(w))
            r = await cx.put(f"{B}/api/settings/edition", headers=rd, json={"edition": "sre"})
            check("a read key cannot switch it", r.status_code == 403, r.text)
            w = (await cx.get(f"{B}/api/whoami", headers=rd)).json()
            check("and nothing changed", w.get("edition") == "factory")
            r = await cx.put(f"{B}/api/settings/edition", headers=root, json={"edition": "sre"})
            check("root switches it", r.status_code == 200 and r.json()["edition"] == "sre"
                  and r.json()["ok"] is True, r.text)
            w = (await cx.get(f"{B}/api/whoami", headers=rd)).json()
            check("the read key sees the switch", w.get("edition") == "sre", str(w))
            g = (await cx.get(f"{B}/api/settings/edition", headers=root)).json()
            check("settings lists the choices and the env value", g["env_value"] == "factory"
                  and [c["id"] for c in g["choices"]] == ["factory", "sre", "platform"], str(g))
            for bad in ("factory2", ""):
                r = await cx.put(f"{B}/api/settings/edition", headers=root, json={"edition": bad})
                check(f"{bad!r} is refused", r.status_code == 400 and "must be one of" in r.text, r.text)

            print("== /api/home ==")
            r = await cx.get(f"{B}/api/home", headers=root)
            check("sre home", r.status_code == 200 and r.json() == {"edition": "sre"}, r.text)
            await cx.put(f"{B}/api/settings/edition", headers=root, json={"edition": "platform"})
            r = await cx.get(f"{B}/api/home", headers=root)
            check("platform has no home", r.status_code == 404 and r.json() == {"error": "no home on this edition"}, r.text)
            await cx.put(f"{B}/api/settings/edition", headers=root, json={"edition": "factory"})
            r = await cx.get(f"{B}/api/home", headers=root)
            check("factory home", r.status_code == 200 and r.json().get("edition") == "factory"
                  and "headline" in r.json() and "projects" in r.json(), r.text)
            await cx.put(f"{B}/api/settings/edition", headers=root, json={"edition": "sre"})
    finally:
        stop(proc)

    print("== a restart keeps the stored value ==")
    proc = start(tmp, TARES_AUTH_TOKEN=ROOT, TARES_EDITION="factory")
    try:
        async with httpx.AsyncClient(timeout=10) as cx:
            await wait_up(cx)
            w = (await cx.get(f"{B}/api/whoami", headers=root)).json()
            check("still sre with TARES_EDITION=factory", w.get("edition") == "sre", str(w))
    finally:
        stop(proc)


async def platform_regression():
    print("== a cell with no edition looks like today's ==")
    proc = start(tempfile.mkdtemp())
    try:
        async with httpx.AsyncClient(timeout=10) as cx:
            await wait_up(cx)
            w = (await cx.get(f"{B}/api/whoami")).json()
            check("open whoami: platform, needed", w["edition"] == "platform" and w["edition_name"] == "Tares"
                  and w["edition_needed"] is True, str(w))
            check("its keys are today's plus the three", set(w) == TODAY_KEYS | {"edition", "edition_name", "edition_needed"}, str(sorted(w)))
            r = await cx.get(f"{B}/api/home")
            check("no home", r.status_code == 404, r.text)
    finally:
        stop(proc)


if __name__ == "__main__":
    unit()
    asyncio.run(seeding_order())
    asyncio.run(api())
    asyncio.run(platform_regression())
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
