"""The team interface (TR-914): tares.team.Team, its single-person default, the ee loader and the
order routes are registered in.

  Contract   every public method of Team is overridden by SingleTeam and by a fake ee team, with the
             same signature; METHODS names them all, so a ticket that adds a method adds it here too.
  Defaults   what SingleTeam answers: nobody to add, no areas, assignees "not synced", one crew per
             name with the machine dropped, grants that never settle, team-home hooks that pass rows on.
  Loader     get() is SingleTeam without a licence or without an ee team, the ee team with a valid or
             an expired licence, and again SingleTeam once the key is removed. A team that fails to
             load gives None and one log line; the entry point is read once per process.
  Routes     through make_app: an ee route answers 402 licence_required without a key and 200 after
             PUT /api/licence, with no restart; a fallback row answers 402 when no ee team is loaded;
             an ee GET route answers JSON, not the console page; nothing under tares/ imports tares_ee.

Run: python tests/test_team_interface.py
"""
import asyncio, importlib.metadata, inspect, logging, os, re, sys, tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from _env import scrub_os_environ
scrub_os_environ()

TMP = tempfile.mkdtemp(prefix="tares-team-")
os.environ["TARES_DB"] = os.path.join(TMP, "team.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = AUTH = "root-token-team"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"

import httpx
import licence_testkit as kit
import tares.licence as L
from tares import team as team_mod
from tares.team import SingleTeam, Team

P = F = 0
def ck(l, c, d=""):
    global P, F; P += 1 if c else 0; F += 0 if c else 1
    print(("  ok   " if c else "  FAIL ") + l + ("" if c else f"  {d}"))


METHODS = [
    "can_add_person", "areas", "area_owner",
    "assignee_from_linear", "assignee_from_github", "assignee_warn_nameless", "assignee_sync_join",
    "assignee_join", "assignee_resolve",
    "grant_waiting_for", "grants_waiting_for_me", "settle_grant",
    "handover_blockers", "open_questions_for", "resolve_crew",
    "team_home", "own_stations", "own_desk", "own_tickets", "team_projects",
    "register_routes", "mcp_tools",
]

TODAY = datetime.now(timezone.utc).date()
root_priv, root_pub = kit.keypair(kit.ROOT_PHRASE)
iss_priv, iss_pub = kit.keypair(kit.ISSUER_PHRASE)
CERT = kit.issuer_cert(root_priv, iss_pub, (TODAY + timedelta(days=3000)).isoformat())


def make(org, kid, expires):
    return kit.sign(iss_priv, CERT, org, 5, ["enterprise"], "2020-01-01T00:00:00Z", expires, kid)


LIC = make("Acme", "lic_ok", (TODAY + timedelta(days=100)).isoformat())
LIC_OLD = make("Old Co", "lic_old", (TODAY - timedelta(days=3)).isoformat())

PING = ("POST", "/api/ee-ping", "Ping needs an enterprise licence.")
GONE = ("GET", "/api/ee-gone", "Gone needs an enterprise licence.")


class FakeEeTeam(SingleTeam):
    """What tares_ee.team.Team will look like: every method overridden, one route of its own."""
    def can_add_person(self, store): return True, None
    def areas(self, store): return [{"repo": "o/r"}]
    def area_owner(self, store, repo): return {"id": "p_1"}
    def assignee_from_linear(self, store, assignee, cache=None): return ("p_1", "ana")
    def assignee_from_github(self, store, issue): return ("p_1", "ana")
    def assignee_warn_nameless(self, project, count): return None
    def assignee_sync_join(self, store, project, person_id, status): return None
    def assignee_join(self, store, project, person_id, added_by=None): return None
    def assignee_resolve(self, store, ref): return ("p_1", ref)
    def grant_waiting_for(self, c, grant): return [{"id": "p_1"}]
    def grants_waiting_for_me(self, c, grants, request): return list(grants)
    def settle_grant(self, c, grant, request): return {**grant, "settled": True}
    def handover_blockers(self, c, project): return ["a sign-off is missing"]
    def open_questions_for(self, c, person_id, hidden, want): return [{"id": "q"}]
    def resolve_crew(self, name, person_id, machine): return (f"{name}@{machine}", machine or "")
    def team_home(self, c, request, now): return {"team": True}
    def own_stations(self, c, request, rows): return rows[:1]
    def own_desk(self, c, request, rows): return rows[:1]
    def own_tickets(self, c, request, tickets, projects): return {}
    def team_projects(self, c, request, open_ps, tickets, rows, which, now, waiting): return {"team": True}
    def mcp_tools(self): return [print]

    def register_routes(self, app, host):
        @app.post(PING[1])
        def ping():
            if not L.ee_allowed():
                host.licence_required("Ping needs an enterprise licence (ee route).")
            return {"pong": True}

        @app.get("/api/ee-info")
        def info():
            return {"ee": True}


def public(cls):
    return sorted(n for n, v in vars(cls).items() if not n.startswith("_") and callable(v))


def sig(fn):
    """Parameter names, kinds and defaults; the return annotation is documentation, not contract."""
    return [(p.name, p.kind, p.default) for p in inspect.signature(fn).parameters.values()]


def contract():
    print("== the contract ==")
    ck("METHODS names every public method of Team", public(Team) == sorted(METHODS),
       sorted(set(public(Team)) ^ set(METHODS)))
    ck("SingleTeam overrides every one", public(SingleTeam) == sorted(METHODS),
       sorted(set(METHODS) - set(public(SingleTeam))))
    ck("the fake ee team overrides every one", all(m in vars(FakeEeTeam) for m in METHODS),
       [m for m in METHODS if m not in vars(FakeEeTeam)])
    for cls in (SingleTeam, FakeEeTeam):
        bad = [m for m in METHODS if sig(getattr(cls, m)) != sig(getattr(Team, m))]
        ck(f"{cls.__name__}: every signature matches Team's", not bad,
           [(m, sig(getattr(cls, m)), sig(getattr(Team, m))) for m in bad][:2])
    ck("Team itself answers nothing (a plain contract)", all(
        _raises_not_implemented(getattr(Team(), m)) for m in ("areas", "resolve_crew", "mcp_tools")))
    names = {n for n in dir(Team) if not n.startswith("_")}
    ck("the module keeps the old factory_team.Team out of its namespace",
       "Team" in vars(team_mod) and vars(team_mod)["Team"] is Team and "factory_team" not in vars(team_mod))
    _ = names


def _raises_not_implemented(fn):
    n = len(inspect.signature(fn).parameters)
    try:
        fn(*([None] * n))
    except NotImplementedError:
        return True
    except Exception:
        return False
    return False


def defaults(store):
    print("== SingleTeam defaults ==")
    t = SingleTeam()
    ok, why = t.can_add_person(store)
    ck("can_add_person: False with the plain-words reason", ok is False and why == team_mod.ADD_PERSON_REASON
       and "enterprise licence" in why and "—" not in why, (ok, why))
    ck("areas is [] and area_owner is None", t.areas(store) == [] and t.area_owner(store, "o/r") is None)
    ck("assignee_from_linear and _from_github are None (not synced)",
       t.assignee_from_linear(store, {"name": "x"}) is None and t.assignee_from_linear(store, {}, cache={}) is None
       and t.assignee_from_github(store, {"assignees": []}) is None)
    ck("warn_nameless, sync_join and join do nothing",
       t.assignee_warn_nameless({"id": "p"}, 2) is None and t.assignee_sync_join(store, {"id": "p"}, "p_1", "in") is None
       and t.assignee_join(store, {"id": "p"}, "p_1") is None and t.assignee_join(store, {"id": "p"}, "p_1", "p_2") is None)
    ck("assignee_resolve: an empty ref gives (None, None)",
       t.assignee_resolve(store, "") == (None, None) and t.assignee_resolve(store, None) == (None, None))
    try:
        t.assignee_resolve(store, "a@b.c")
        raised = None
    except L.LicenceRequired as e:
        raised = e.message
    ck("assignee_resolve: a person raises LicenceRequired in plain words",
       raised == "Assigning tickets to people needs an enterprise licence.", raised)
    grant = {"id": "g_1", "kind": "team"}
    ck("grants: nobody waits, none waits for me, settle returns the grant itself",
       t.grant_waiting_for(None, grant) == [] and t.grants_waiting_for_me(None, [grant], None) == []
       and t.settle_grant(None, grant, None) is grant)
    ck("hand-over and questions: no blockers, no open questions",
       t.handover_blockers(None, {"id": "p"}) == [] and t.open_questions_for(None, "local", set(), None) == [])
    ck("resolve_crew keeps the name and drops the machine",
       t.resolve_crew("crew", "local", "mbp") == ("crew", "") and t.resolve_crew(None, "local", "desk") == ("crew", "")
       and t.resolve_crew("ana-laptop", "p_1", "x") == ("ana-laptop", "") and t.resolve_crew("  ", None, None) == ("crew", "")
       and t.resolve_crew(" ops ", "local", "m") == ("ops", ""))
    rows = [1, 2]
    tickets = {"t": 1}
    from types import SimpleNamespace as NS
    one = NS(store=NS(only_local_person=lambda: True),
             h=NS(current_person=lambda r: {"id": "local"}, is_system=lambda r: False),
             work_person=lambda r: {"id": "local"})
    ck("team home: {} and the own_* hooks pass their rows on unchanged on a one-person cell",
       t.team_home(one, None, None) == {} and t.own_stations(one, None, rows) is rows
       and t.own_desk(one, None, rows) is rows and t.own_tickets(one, None, tickets, []) is tickets
       and t.team_projects(one, None, [], [], [], "all", None, []) == {})
    many = NS(store=NS(only_local_person=lambda: False),
              h=NS(current_person=lambda r: {"id": "ann"}, is_system=lambda r: False),
              work_person=lambda r: {"id": "ann"},
              desk_is_mine=lambda d, me: d["to"] == me, is_mine=lambda tk, me, p: tk["who"] == me)
    stations = [{"person": {"id": "ann"}}, {"person": {"id": "bob"}}, {"person": None}]
    desk = [{"to": "ann"}, {"to": "bob"}]
    tks = {"p": [{"who": "ann"}, {"who": "bob"}]}
    ck("without a team, a person caller of a many-person cell keeps only their own rows (no licence)",
       t.own_stations(many, None, stations) == [stations[0]] and t.own_desk(many, None, desk) == [desk[0]]
       and t.own_tickets(many, None, tks, [{"id": "p"}]) == {"p": [{"who": "ann"}]})
    anon = NS(store=many.store, h=NS(current_person=lambda r: None, is_system=lambda r: False),
              work_person=many.work_person)
    system = NS(store=many.store, h=NS(current_person=lambda r: None, is_system=lambda r: True),
                work_person=many.work_person, desk_is_mine=many.desk_is_mine, is_mine=many.is_mine)
    ck("a caller with no person is not cut; the system credential is cut to the person it acts for",
       t.own_stations(anon, None, stations) is stations
       and t.own_stations(system, None, stations) == [stations[0]])
    ck("register_routes does nothing and mcp_tools is []",
       t.register_routes(None, None) is None and t.mcp_tools() == [])


class _EP:
    """A stand-in entry point."""
    def __init__(self, load): self.load = load


class _Log(logging.Handler):
    def __init__(self):
        super().__init__(); self.lines = []
    def emit(self, r): self.lines.append(r.getMessage())


def loader(store):
    print("== the loader ==")
    real = importlib.metadata.entry_points
    log = _Log()
    logging.getLogger("tares.team").addHandler(log)
    try:
        calls = []
        importlib.metadata.entry_points = lambda **kw: (calls.append(kw), [])[1]
        team_mod._reset_ee_for_tests()
        ck("no entry point: _ee() is None and nothing is logged", team_mod._ee() is None and log.lines == [])
        ck("... the group and name asked for are tares.ee / team", calls == [{"group": "tares.ee", "name": "team"}], calls)
        team_mod._ee()
        ck("... and it asked once per process", len(calls) == 1, calls)

        def boom():
            raise ImportError("no module named tares_ee")
        importlib.metadata.entry_points = lambda **kw: [_EP(boom)]
        team_mod._reset_ee_for_tests()
        ck("an entry point that fails to import gives None", team_mod._ee() is None)
        ck("... with one log line naming the type and message", log.lines == [
            "tares.team: ee team did not load: ImportError: no module named tares_ee"], log.lines)
        team_mod._ee(); team_mod._ee()
        ck("... and no more lines on later calls", len(log.lines) == 1, log.lines)
        store.set_setting(L.LICENCE_SETTING, LIC)
        L.current(store, force=True)
        ck("... with a valid licence get() is still SingleTeam", type(team_mod.get()) is SingleTeam)

        importlib.metadata.entry_points = lambda **kw: [_EP(lambda: FakeEeTeam)]
        team_mod._reset_ee_for_tests()
        ck("an entry point that loads is instantiated", isinstance(team_mod._ee(), FakeEeTeam))
    finally:
        importlib.metadata.entry_points = real
        logging.getLogger("tares.team").removeHandler(log)

    fake = FakeEeTeam()
    team_mod._set_ee_for_tests(fake)
    store.set_setting(L.LICENCE_SETTING, "")
    L.current(store, force=True)
    ck("no licence: get() is SingleTeam even with an ee team", type(team_mod.get()) is SingleTeam)
    ck("... and it is one shared object", team_mod.get() is team_mod.get())
    store.set_setting(L.LICENCE_SETTING, LIC)
    L.current(store, force=True)
    ck("valid licence: get() is the ee team", team_mod.get() is fake)
    store.set_setting(L.LICENCE_SETTING, LIC_OLD)
    st = L.current(store, force=True)
    ck("expired licence: get() is still the ee team", st["expired"] is True and team_mod.get() is fake, st)
    store.set_setting(L.LICENCE_SETTING, "")
    L.current(store, force=True)
    ck("key removed: back to SingleTeam, no restart", type(team_mod.get()) is SingleTeam)
    team_mod._set_ee_for_tests(None)
    store.set_setting(L.LICENCE_SETTING, LIC)
    L.current(store, force=True)
    ck("valid licence but no ee team: SingleTeam", type(team_mod.get()) is SingleTeam)
    store.set_setting(L.LICENCE_SETTING, "")
    L.current(store, force=True)


def client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                             headers={"Authorization": f"Bearer {AUTH}"})


def exact_402(r, message=None):
    b = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    return (r.status_code == 402 and set(b) == {"code", "message", "url"} and b["code"] == "licence_required"
            and b["url"] == "https://docs.glassflow.ai/tares/enterprise" and (message is None or b["message"] == message))


async def routes_without_ee():
    print("== routes: no ee team loaded ==")
    from tares.daemon import make_app
    team_mod._set_ee_for_tests(None)
    team_mod.FALLBACKS[:] = [PING, GONE]
    app = make_app()
    async with client(app) as cx:
        r = await cx.post("/api/ee-ping", json={})
        ck("fallback POST row: 402 with exactly {code, message, url}", exact_402(r, PING[2]), f"{r.status_code} {r.text}")
        r = await cx.get("/api/ee-gone")
        ck("fallback GET row: 402, not the console page and not a 404", exact_402(r, GONE[2]), f"{r.status_code} {r.text[:80]}")
        r = await cx.get("/api/ee-info")
        ck("a path nobody registered still gets the catch-all's 404", r.status_code == 404, r.status_code)
        r = await cx.get("/api/ee-ping")
        ck("a fallback answers only its own method", r.status_code in (404, 405), r.status_code)
        ck("fallback routes are left out of the API schema", "/api/ee-ping" not in app.openapi()["paths"])
        r = await cx.put("/api/licence", json={"key": LIC})
        ck("a licence alone does not make a fallback work (no ee team)", r.status_code == 200, r.text)
        r = await cx.post("/api/ee-ping", json={})
        ck("... the fallback still answers 402", exact_402(r, PING[2]), f"{r.status_code} {r.text}")
        await cx.delete("/api/licence")


async def routes_with_ee():
    print("== routes: an ee team is loaded ==")
    from tares.daemon import make_app
    team_mod._set_ee_for_tests(FakeEeTeam())
    team_mod.FALLBACKS[:] = [PING, GONE]
    app = make_app()
    paths = [getattr(r, "path", "") for r in app.routes]
    ping_at = paths.index("/api/ee-ping")
    gone_at = paths.index("/api/ee-gone")
    catch_at = paths.index("/{path:path}")
    ck("the ee route sits before its fallback, both before the catch-all (last route)",
       ping_at < len(paths) - 1 and ping_at < paths.index("/api/ee-ping", ping_at + 1) < gone_at < catch_at
       and catch_at == len(paths) - 1, (ping_at, gone_at, catch_at, len(paths)))
    async with client(app) as cx:
        r = await cx.post("/api/ee-ping", json={})
        ck("ee route without a key: 402 licence_required from the route itself",
           exact_402(r, "Ping needs an enterprise licence (ee route)."), f"{r.status_code} {r.text}")
        r = await cx.get("/api/ee-info")
        ck("ee GET route: JSON, not the console page", r.status_code == 200 and r.json() == {"ee": True}
           and r.headers["content-type"].startswith("application/json"), f"{r.status_code} {r.text[:80]}")
        r = await cx.put("/api/licence", json={"key": LIC})
        ck("PUT /api/licence with a test licence: 200", r.status_code == 200 and r.json()["ee_allowed"] is True, r.text)
        r = await cx.post("/api/ee-ping", json={})
        ck("the same route now works, without a restart", r.status_code == 200 and r.json() == {"pong": True},
           f"{r.status_code} {r.text}")
        r = await cx.get("/api/ee-gone")
        ck("a fallback row with no ee route behind it still answers 402", exact_402(r, GONE[2]), f"{r.status_code} {r.text[:80]}")
        r = await cx.delete("/api/licence")
        r = await cx.post("/api/ee-ping", json={})
        ck("DELETE /api/licence: 402 again", exact_402(r), f"{r.status_code} {r.text}")
    store = app.state.store
    ck("team.get() follows the same licence (SingleTeam after the DELETE)", type(team_mod.get()) is SingleTeam)
    store.set_setting(L.LICENCE_SETTING, LIC)
    L.current(store, force=True)
    ck("... and the ee team after a key is stored", isinstance(team_mod.get(), FakeEeTeam))
    store.set_setting(L.LICENCE_SETTING, "")
    L.current(store, force=True)


def no_import():
    print("== the boundary ==")
    bad = []
    for dp, _, files in os.walk(os.path.join(ROOT, "tares")):
        for f in files:
            if f.endswith(".py"):
                p = os.path.join(dp, f)
                for i, line in enumerate(open(p, encoding="utf-8"), 1):
                    if re.match(r"\s*(import|from)\s+tares_ee\b", line):
                        bad.append(f"{os.path.relpath(p, ROOT)}:{i}")
    ck("no file under tares/ imports tares_ee", not bad, bad)
    import ast   # the live list is emptied by the cases, so read the shipped one from the source
    tree = ast.parse(open(os.path.join(ROOT, "tares", "team.py"), encoding="utf-8").read())
    rows = [sorted((e.elts[0].value, e.elts[1].value) for e in n.value.elts) for n in tree.body
            if isinstance(n, ast.AnnAssign) and getattr(n.target, "id", "") == "FALLBACKS"]
    ck("FALLBACKS in the shipped module holds the four area routes, the three project access routes, the two grant consent routes and nothing else",
       rows == [[("DELETE", "/api/areas/{owner}/{name}"), ("DELETE", "/api/projects/{uid}/members/{person_id}"),
                 ("GET", "/api/areas"), ("PATCH", "/api/projects/{uid}"),
                 ("POST", "/api/grants/{gid}/consent"), ("POST", "/api/grants/{gid}/decline"),
                 ("POST", "/api/projects/{uid}/members"),
                 ("PUT", "/api/areas"), ("PUT", "/api/areas/{owner}/{name}")]], str(rows))


def case(name):
    """One app per process: a second make_app on the same database file cannot checkpoint."""
    L._set_root_keys_for_tests({kit.kid_of(root_pub): root_pub})
    asyncio.run(routes_without_ee() if name == "no-ee" else routes_with_ee())
    print(f"\n{P} passed, {F} failed")
    sys.exit(1 if F else 0)


def main():
    if len(sys.argv) > 1:
        case(sys.argv[1])
    L._set_root_keys_for_tests({kit.kid_of(root_pub): root_pub})
    team_mod.FALLBACKS[:] = []
    contract()
    from tares.daemon import make_app
    store = make_app().state.store
    defaults(store)
    loader(store)
    no_import()
    import subprocess
    global P, F
    for name in ("no-ee", "ee"):
        env = dict(os.environ, TARES_DB=os.path.join(tempfile.mkdtemp(prefix="tares-team-"), "team.duckdb"))
        r = subprocess.run([sys.executable, os.path.abspath(__file__), name], env=env,
                           capture_output=True, text=True)
        out = r.stdout.strip().split("\n")
        print("\n".join(out[:-1]))
        m = re.search(r"(\d+) passed, (\d+) failed", out[-1] if out else "")
        if r.returncode and not (m and m.group(2) != "0"):
            ck(f"case {name} ran", False, (r.stderr or r.stdout)[-600:])
        elif m:
            P += int(m.group(1)); F += int(m.group(2))
        else:
            ck(f"case {name} ran", False, r.stdout[-300:])
    print(f"\n{P} passed, {F} failed")
    sys.exit(1 if F else 0)


main()
