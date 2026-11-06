"""The daemon always boots, whatever the licence check or the ee package does (TR-1057).

Three failures, each in its own process (one make_app per database file), each run with a valid
test licence on the cell so a fall back to "licensed" would show:

  licence   licence.state raises. make_app still returns, /health answers, GET /api/licence says
            ee_allowed false and why, team.get() is the single-person team, an ee route answers 402,
            and the failure is logged once.
  type      the entry point builds something that is not a tares.team.Team. The loader gives None
            with one log line; the cell boots, get() is single-person, a fallback answers 402.
  routes    the ee team's register_routes raises after adding one route. The cell boots, the half
            registered route is gone, get() is single-person even with the licence, one log line.

Run: python tests/test_boot_guard.py
"""
import asyncio, importlib.metadata, logging, os, re, subprocess, sys, tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from _env import scrub_os_environ
scrub_os_environ()

TMP = tempfile.mkdtemp(prefix="tares-boot-guard-")
os.environ["TARES_DB"] = os.path.join(TMP, "boot.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = AUTH = "root-token-boot"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"

import httpx
import licence_testkit as kit
import tares.licence as L
from tares import team as team_mod
from tares.team import SingleTeam

P = F = 0
def ck(l, c, d=""):
    global P, F; P += 1 if c else 0; F += 0 if c else 1
    print(("  ok   " if c else "  FAIL ") + l + ("" if c else f"  {d}"))


TODAY = datetime.now(timezone.utc).date()
root_priv, root_pub = kit.keypair(kit.ROOT_PHRASE)
iss_priv, iss_pub = kit.keypair(kit.ISSUER_PHRASE)
CERT = kit.issuer_cert(root_priv, iss_pub, (TODAY + timedelta(days=3000)).isoformat())
LIC = kit.sign(iss_priv, CERT, "Acme", 5, ["enterprise"], "2020-01-01T00:00:00Z",
               (TODAY + timedelta(days=100)).isoformat(), "lic_ok")

PING = ("POST", "/api/ee-ping", "Ping needs an enterprise licence.")
HALF = "/api/ee-half"


class _Log(logging.Handler):
    def __init__(self):
        super().__init__(); self.lines = []
    def emit(self, r): self.lines.append(r.getMessage())


class _EP:
    def __init__(self, load): self.load = load


class EeTeam(SingleTeam):
    """An ee team with one route that wants the licence."""
    def register_routes(self, app, host):
        @app.post(PING[1])
        def ping():
            if not L.ee_allowed():
                host.licence_required("Ping needs an enterprise licence (ee route).")
            return {"pong": True}


class HalfTeam(SingleTeam):
    """Adds one route, then raises."""
    def register_routes(self, app, host):
        @app.get(HALF)
        def half():
            return {"half": True}
        raise RuntimeError("register_routes broke half way")


def exact_402(r):
    b = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    return r.status_code == 402 and set(b) == {"code", "message", "url"} and b["code"] == "licence_required"


def client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                             headers={"Authorization": f"Bearer {AUTH}"})


def watch(name):
    log = _Log()
    logging.getLogger(name).addHandler(log)
    return log


async def case_licence():
    print("== licence.state raises ==")
    log = watch("tares.licence")
    os.environ["TARES_LICENSE"] = LIC   # a valid key is on the cell; the check never gets to read it
    real = L.state
    def boom(*a, **k):
        raise RuntimeError("licence check crashed on purpose")
    L.state = boom
    team_mod._set_ee_for_tests(EeTeam())
    team_mod.FALLBACKS[:] = [PING]
    from tares.daemon import make_app
    try:
        app = make_app()
    except Exception as e:
        ck("make_app returns", False, f"{type(e).__name__}: {e}")
        return
    ck("make_app returns", True)
    async with client(app) as cx:
        r = await cx.get("/health")
        ck("/health answers", r.status_code == 200, r.status_code)
        r = await cx.get("/api/licence")
        b = r.json() if r.status_code == 200 else {}
        ck("GET /api/licence: not valid, ee not allowed, the reason says the check failed",
           r.status_code == 200 and b.get("valid") is False and b.get("ee_allowed") is False
           and "failed" in (b.get("reason") or "") and b.get("env_set") is True, f"{r.status_code} {r.text[:200]}")
        ck("... and the body never carries the key text", LIC not in r.text)
        r = await cx.post("/api/ee-ping", json={})
        ck("an ee route answers 402 licence_required", exact_402(r), f"{r.status_code} {r.text[:100]}")
    ck("team.get() is the single-person team", type(team_mod.get()) is SingleTeam)
    ck("licence.ee_allowed() is false", L.ee_allowed() is False)
    for _ in range(3):
        L.current(app.state.store, force=True)
    ck("the failure is logged once, with the error type and not the key",
       len(log.lines) == 1 and "RuntimeError" in log.lines[0] and LIC not in log.lines[0], log.lines)
    L.state = real


async def case_type():
    print("== the ee team is not a Team ==")
    log = watch("tares.team")
    os.environ["TARES_LICENSE"] = LIC
    class NotATeam:
        def register_routes(self, app, host):
            raise AssertionError("a wrong type must never get as far as registering")
    real_eps = importlib.metadata.entry_points
    # only the ee group is faked: pydantic and others ask for their own groups while the app builds
    importlib.metadata.entry_points = (
        lambda **kw: [_EP(lambda: NotATeam)] if kw.get("group") == team_mod.ENTRY_GROUP else real_eps(**kw))
    team_mod._reset_ee_for_tests()
    team_mod.FALLBACKS[:] = [PING]
    from tares.daemon import make_app
    try:
        app = make_app()
    except Exception as e:
        ck("make_app returns", False, f"{type(e).__name__}: {e}")
        return
    ck("make_app returns", True)
    ck("the loader gives None and one log line says it is not a Team",
       team_mod._ee() is None and len(log.lines) == 1 and "not a tares.team.Team" in log.lines[0], log.lines)
    async with client(app) as cx:
        r = await cx.get("/health")
        ck("/health answers", r.status_code == 200, r.status_code)
        r = await cx.get("/api/licence")
        ck("the licence itself is valid and still reported", r.status_code == 200 and r.json()["ee_allowed"] is True, r.text[:200])
        r = await cx.post("/api/ee-ping", json={})
        ck("a fallback answers 402: team actions are refused", exact_402(r), f"{r.status_code} {r.text[:100]}")
    ck("team.get() is the single-person team even with a valid licence", type(team_mod.get()) is SingleTeam)


async def case_routes():
    print("== ee register_routes raises ==")
    log = watch("tares.team")
    os.environ["TARES_LICENSE"] = LIC
    team_mod._set_ee_for_tests(HalfTeam())
    team_mod.FALLBACKS[:] = [PING]
    from tares.daemon import make_app
    try:
        app = make_app()
    except Exception as e:
        ck("make_app returns", False, f"{type(e).__name__}: {e}")
        return
    ck("make_app returns", True)
    paths = [getattr(r, "path", "") for r in app.routes]
    ck("the half registered route is gone", HALF not in paths, paths[-6:])
    ck("... the fallback and the console catch-all are still there, catch-all last",
       PING[1] in paths and paths[-1] == "/{path:path}", paths[-4:])
    ck("one log line says the routes did not register",
       len(log.lines) == 1 and "ee routes did not register" in log.lines[0] and "half way" in log.lines[0], log.lines)
    async with client(app) as cx:
        r = await cx.get("/health")
        ck("/health answers", r.status_code == 200, r.status_code)
        r = await cx.get(HALF)
        ck("the half registered route does not answer (not 200)", r.status_code != 200, r.status_code)
        r = await cx.post("/api/ee-ping", json={})
        ck("a fallback answers 402: team actions are refused", exact_402(r), f"{r.status_code} {r.text[:100]}")
    ck("team.get() is the single-person team even with a valid licence", type(team_mod.get()) is SingleTeam)


CASES = {"licence": case_licence, "type": case_type, "routes": case_routes}


def case(name):
    """One app per process: a second make_app on the same database file cannot checkpoint."""
    L._set_root_keys_for_tests({kit.kid_of(root_pub): root_pub})
    asyncio.run(CASES[name]())
    print(f"\n{P} passed, {F} failed")
    sys.exit(1 if F else 0)


def main():
    if len(sys.argv) > 1:
        case(sys.argv[1])
    global P, F
    for name in CASES:
        env = dict(os.environ, TARES_DB=os.path.join(tempfile.mkdtemp(prefix="tares-boot-guard-"), "boot.duckdb"))
        r = subprocess.run([sys.executable, os.path.abspath(__file__), name], env=env,
                           capture_output=True, text=True)
        out = r.stdout.strip().split("\n")
        print("\n".join(out[:-1]))
        m = re.search(r"(\d+) passed, (\d+) failed", out[-1] if out else "")
        if m:
            P += int(m.group(1)); F += int(m.group(2))
        if not m or r.returncode and m.group(2) == "0":
            ck(f"case {name} ran", False, (r.stderr or r.stdout)[-600:])
    print(f"\n{P} passed, {F} failed")
    sys.exit(1 if F else 0)


main()
