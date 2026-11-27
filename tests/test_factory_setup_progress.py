"""GET /api/factory/setup/progress and GET /api/factory/setup work out the crew's choices, GitHub and
the terminal steps without building the home (TR-1634), and answer what the home answers.

For each state of a cell's first run this asserts the exact `progress`, `onboarding` and `terminal`
of both setup routes and the exact `start` and `onboarding` blocks of GET /api/home, for each kind
of caller: the root token, a person's admin key, an admin key with no person, and auth off (a second
process). The states run with the sessions table caught up and with it not caught up.

Run: .venv/bin/python tests/test_factory_setup_progress.py
Postgres: TARES_TEST_PG=postgresql://... PYTHONPATH=tests/pg_harness:. .venv/bin/python tests/test_factory_setup_progress.py
"""
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))
from _env import scrub_os_environ  # noqa: E402

AUTH_OFF = "--auth-off" in sys.argv
scrub_os_environ()
os.environ["TZ"] = "UTC"      # DuckDB answers times in the machine's zone
time.tzset() if hasattr(time, "tzset") else None

_TMP = tempfile.mkdtemp(prefix="tares-setup-progress-")
os.environ["TARES_DB"] = os.path.join(_TMP, "api.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(_TMP, "none.yaml")
TOKEN = "root-token-setup-progress"
if AUTH_OFF:
    os.environ.pop("TARES_AUTH_TOKEN", None)
else:
    os.environ["TARES_AUTH_TOKEN"] = TOKEN
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
os.environ["TARES_EDITION"] = "factory"
for _k in ("TARES_PROJECT", "TARES_PROJECT_NAME", "TARES_WORKSPACE_API_URL", "TARES_LOGIN_URL",
           "TARES_GITHUB_CONNECT_URL", "TARES_LINEAR_CONNECT_URL"):
    os.environ.pop(_k, None)

import httpx

from tares import crew_onboarding as co
from tares import factory_home as fh
from tares import session_ingest
from tares.envelope import Envelope
from tares.store import Store  # noqa: E402

# The "sessions table behind" states read the events, and a cell that has not caught up writes every
# line there too. So the lines go to both stores here, as they did before the write gate opened.
Store._lines_skip_duckdb = lambda self: False

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


NOW = datetime.now(timezone.utc).replace(microsecond=0)
AT = "2026-11-20T09:00:00+00:00"          # when a machine reported
TOTAL = 7                                 # the integration steps Setup shows, and crew, GitHub, terminal
BASE = 2                                  # set on a new cell: the steps with nothing to choose


def ago(**kw) -> datetime:
    return NOW - timedelta(**kw)


def iso(t) -> str | None:
    return t.isoformat() if t else None


# ── the expected answers ──────────────────────────────────────────────────────────────────────
def onboarding(chosen=None, github="", machine=("", "", None), orch=("none", None),
               rev=("none", None), live=0, up=False) -> dict:
    """`chosen` is (autonomy, releaser, builders_max) or None; `github` the line of a connected
    GitHub; `machine` (name, version, at); `orch` and `rev` (state, last heard from)."""
    a, r, b = chosen or (None, None, None)
    d = co.defaults()
    return {"choices": {"chosen": bool(chosen), "autonomy": a, "releaser": r, "builders_max": b,
                        "defaults": d},
            "github": {"connected": bool(github), "text": github},
            "machine": {"name": machine[0], "version": machine[1], "at": machine[2]},
            "crew": {"machine": machine[0],
                     "orchestrator": {"state": orch[0], "last_at": iso(orch[1])},
                     "reviewer": {"state": rev[0], "last_at": iso(rev[1])},
                     "builders": {"live": live, "max": b or d["builders_max"]}, "up": up}}


def _step(key, title, gets, done, action, detail, where, command=None) -> dict:
    out = {"key": key, "title": title, "gets": gets, "done": done, "actions": [] if done else [action]}
    if detail:
        out["detail"] = detail
    out["where"] = where
    if command:
        out["command"] = command
    return out


def steps(o: dict, setup_text="") -> list[dict]:
    """The four Start steps for the onboarding block `o`; `setup_text` is step 1's line, written
    out by the caller."""
    m, crew, c = o["machine"], o["crew"], o["choices"]
    install = bool(m["version"] or m["name"] or crew["up"])
    login = bool(m["name"] or crew["up"])
    running = (f"Running on {m['name']}" if m["name"] else "Running") if crew["up"] else None

    def cmd(key, title, gets, done, command, detail):
        return _step(key, title, gets, done, {"label": "Copy the command", "copy": command}, detail,
                     "terminal", command)
    return [
        _step("setup", "Set up your factory",
              "How your crew works, your GitHub, and where tickets, reviews and checks live.",
              c["chosen"] and o["github"]["connected"],
              {"label": "Set up your factory", "to": "/setup"}, setup_text or None, "workspace"),
        cmd("install", "Install Tares", "The command that starts and watches the crew.", install,
            "uv tool install tares", f"Installed: tares {m['version']}." if m["version"] else None),
        cmd("login", "Sign your machine in", "So the crew on your machine reports here.", login,
            "tares login", f"Signed in from {m['name']}." if m["name"] else None),
        cmd("crew", "Start the crew",
            "It reads your choices from this workspace and asks only what belongs to your machine.",
            crew["up"], "tares factory crew up", running),
    ]


# ── one state, as the three routes answer it ──────────────────────────────────────────────────
BUILDS = [0]
_real_build = fh.build


def _counted_build(*a, **kw):
    BUILDS[0] += 1
    return _real_build(*a, **kw)


fh.build = _counted_build
SETUP_BUILDS = [0]        # home builds seen while a setup route answered, over the whole run
SETUP_READS = [0]         # setup route calls made


async def state(label, cx, want_o, setup_text="", projects=0):
    """Both setup routes and the home, as the caller of `cx` reads them."""
    want = steps(want_o, setup_text)
    terminal = want[1:]
    n = (BASE + want_o["choices"]["chosen"] + want_o["github"]["connected"]
         + all(s["done"] for s in terminal))
    progress = {"set": n, "total": TOTAL, "text": f"{n} of {TOTAL} done"}
    done = sum(1 for s in want if s["done"])
    start = {"steps": want, "done": done, "total": 4, "show": done < 4, "hidden": False}
    real_caught = session_ingest.caught_up
    for caught in (True, False):
        if not caught:
            session_ingest.caught_up = lambda store: False
        tag = f"{label}{'' if caught else ' (sessions table behind)'}"
        try:
            fh.clear_cache()
            BUILDS[0] = 0
            rp = await cx.get("/api/factory/setup/progress")
            rs = await cx.get("/api/factory/setup")
            SETUP_BUILDS[0] += BUILDS[0]
            SETUP_READS[0] += 2
            fh.clear_cache()
            rh = await cx.get("/api/home")
        finally:
            session_ingest.caught_up = real_caught
        if (rp.status_code, rs.status_code, rh.status_code) != (200, 200, 200):
            ck(f"{tag}: the three routes answer 200", False,
               [(r.status_code, r.text[:200]) for r in (rp, rs, rh)])
            continue
        p, s, h = rp.json(), rs.json(), rh.json()
        ck(f"{tag}: progress", p == {**progress, "projects": projects}, p)
        ck(f"{tag}: setup's progress", s["progress"] == progress, s["progress"])
        ck(f"{tag}: setup's onboarding", s["onboarding"] == want_o, _diff(s["onboarding"], want_o))
        ck(f"{tag}: setup's terminal", s["terminal"] == terminal, _diff(s["terminal"], terminal))
        ck(f"{tag}: setup's other fields", sorted(s) == ["edition", "exit_label", "licence", "onboarding",
           "progress", "steps", "terminal", "title", "tools"]
           and (s["title"], s["exit_label"]) == ("Set up your factory", "Exit setup")
           and s["licence"] == {"state": "none", "ended_on": None, "text": "This cell has no licence."}
           and sum(1 for st in s["steps"] if st["set"]) == BASE
           and s["tools"]["github"]["connected"] == want_o["github"]["connected"], sorted(s))
        ck(f"{tag}: home's onboarding", h["onboarding"] == want_o, _diff(h["onboarding"], want_o))
        ck(f"{tag}: home's start", h["start"] == start, _diff(h["start"], start))


def _diff(got, want) -> str:
    return f"got {json.dumps(got, sort_keys=True)} want {json.dumps(want, sort_keys=True)}"


# ── what puts a cell in a state ───────────────────────────────────────────────────────────────
def station(store, sid, name, role, crew, seen, said="working", project=None, said_at=None):
    """A session that plays `name` in `crew`, last seen at `seen`, whose state is `said`."""
    parent = "" if role == "orchestrator" else "crew-orchestrator"
    store.set_session_station(sid, name, role, parent, project, seen, crew)
    if said:
        store.set_session_state(sid, said, None, said_at or seen)


def line(store, sid, at):
    store.append([Envelope("claude_code", "claude_code", sid, "assistant", "a line", at,
                           {"session_id": sid}, {"session_id": sid}, at)])


def report(store, name, version, person=None):
    store.set_setting(co.machine_key(person), json.dumps({"machine": name, "version": version, "at": AT}))


async def lifespan_app():
    from tares.daemon import make_app
    app = make_app()
    return app, app.state.store


def client(app, token=None):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t", timeout=120,
                             headers={"Authorization": f"Bearer {token}"} if token else {})


CHOICES = {"autonomy": "L3-review", "builders_max": 3, "releaser": True}
CHOSEN = ("L3-review", True, 3)
GH = "GitHub connected as example-org"
LINE_CHOSEN = "You approve each merge, 3 builders, releases on"
LINE_BOTH = f"{LINE_CHOSEN}. {GH}"


async def one_person():
    """A cell of one person, read with the root token: every state of the first run."""
    app, store = await lifespan_app()
    async with app.router.lifespan_context(app), client(app, TOKEN) as cx:
        print("== a new cell: no projects, no crew, no GitHub, nothing chosen ==")
        o = onboarding()
        await state("new cell", cx, o)

        print("== GitHub alone ==")
        store.upsert_github_credential("main", "ghp_example", "token", "", "example-org")
        await state("GitHub alone", cx, onboarding(github=GH), GH)
        store.delete_github_credential("main")

        print("== the crew's choices ==")
        r = await cx.put("/api/crew/settings", json=CHOICES)
        ck("the choices are saved", r.status_code == 200, r.text)
        await state("choices alone", cx, onboarding(chosen=CHOSEN), LINE_CHOSEN)

        print("== GitHub connected ==")
        store.upsert_github_credential("main", "ghp_example", "token", "", "example-org")
        set_up = dict(chosen=CHOSEN, github=GH)
        await state("choices and GitHub", cx, onboarding(**set_up), LINE_BOTH)

        print("== the machine reports ==")
        report(store, "", "1.60.0")
        await state("a version only", cx, onboarding(**set_up, machine=("", "1.60.0", AT)), LINE_BOTH)
        report(store, "cell-box", "")
        await state("a name only", cx, onboarding(**set_up, machine=("cell-box", "", AT)), LINE_BOTH)
        report(store, "cell-box", "1.60.0")
        m = ("cell-box", "1.60.0", AT)
        await state("signed in", cx, onboarding(**set_up, machine=m), LINE_BOTH)

        print("== the crew comes up ==")
        crew = store.resolve_crew("crew", "local", "cell-box", ago(hours=5))
        o1 = ago(minutes=2)
        station(store, "o-1", "crew-orchestrator", "orchestrator", crew, o1)
        await state("the orchestrator alone", cx, onboarding(
            **set_up, machine=m, orch=("running", o1), rev=("starting", None)), LINE_BOTH)
        r1 = ago(hours=3)
        station(store, "r-1", "crew-reviewer", "reviewer", crew, r1)
        b1 = ago(minutes=1)
        station(store, "b-1", "one-build-m1", "builder", crew, b1)
        station(store, "b-2", "two-build-m1", "builder", crew, b1, said="ended")
        station(store, "b-3", "three-build-m1", "builder", crew, ago(hours=3))      # quiet: not live
        station(store, "b-4", "four-build-m1", "builder", crew, b1, said="idle")
        station(store, "b-5", "five-build-m1", "builder", crew, b1, said=None)      # no state: working
        up = dict(**set_up, machine=m, orch=("running", o1), rev=("quiet", r1), live=3, up=True)
        await state("terminal done, a quiet reviewer", cx, onboarding(**up), LINE_BOTH)

        print("== the newest session of a station is the one that counts ==")
        r2 = ago(minutes=3)
        station(store, "r-2", "crew-reviewer", "reviewer", crew, r2, said="ended")
        await state("a newer session that ended", cx, onboarding(
            **{**up, "rev": ("stopped", r2), "up": False}), LINE_BOTH)
        r3 = ago(minutes=2)
        station(store, "r-3", "crew-reviewer", "reviewer", crew, r3, said="replaced")
        await state("a replaced one reads as stopped", cx, onboarding(
            **{**up, "rev": ("stopped", r3), "up": False}), LINE_BOTH)
        r4 = ago(seconds=30)
        line(store, "r-1", r4)           # the oldest session writes again: it is the newest now
        await state("the session with the newest line", cx, onboarding(
            **{**up, "rev": ("running", r4)}), LINE_BOTH)
        up["rev"] = ("running", r4)

        print("== a second crew with the same station names ==")
        other = store.resolve_crew("second", "local", "spare-box", ago(hours=1))
        station(store, "o-x", "crew-orchestrator", "orchestrator", other, ago(minutes=1), said="ended")
        station(store, "r-x", "crew-reviewer", "reviewer", other, ago(minutes=1), said="ended")
        await state("a live row beats an ended one", cx, onboarding(**up), LINE_BOTH)

        print("== no machine report, the root token: no person, so no crew names the machine ==")
        store.set_setting(co.machine_key(), None)
        await state("no report", cx, onboarding(**{**up, "machine": ("", "", None)}), LINE_BOTH)
        report(store, "cell-box", "1.60.0")

        print("== projects ==")
        store.create_project("uc_one", "custom", "One", {}, goal="g")
        store.set_project_kind("uc_one", "software_factory")
        store.create_project("uc_other", "custom", "Not a factory project", {}, goal="g")
        await state("a project", cx, onboarding(**up), LINE_BOTH, projects=1)


async def team():
    """A cell of several people: each caller reads their own crew, choices and machine."""
    app, store = await lifespan_app()
    async with app.router.lifespan_context(app):
        ana = store.create_person("Ana Example", email="ana@example.test", via="admin")
        ben = store.create_person("Ben Example", email="ben@example.test", via="admin")
        async with client(app, TOKEN) as root:
            keys = {}
            for key, extra in (("ana", {"person_id": ana["id"]}), ("ben", {"person_id": ben["id"]}),
                               ("nobody", {})):
                r = await root.post("/api/keys", json={"name": key, "scopes": ["admin"], **extra})
                assert r.status_code in (200, 201), r.text
                keys[key] = r.json()["secret"]
            async with client(app, keys["ana"]) as ax, client(app, keys["ben"]) as bx, \
                    client(app, keys["nobody"]) as nx:
                print("== several people, nothing set up ==")
                for who, cx in (("root", root), ("ana", ax), ("ben", bx), ("no person", nx)):
                    await state(f"{who}, new", cx, onboarding())

                print("== Ana's own choices, machine and crew ==")
                r = await ax.put("/api/crew/settings", json=CHOICES)
                ck("Ana saves her choices", r.status_code == 200, r.text)
                report(store, "ana-box", "1.60.1", ana["id"])
                ac = store.resolve_crew("crew", ana["id"], "ana-box", ago(hours=5))
                t = ago(minutes=2)
                station(store, "a-o", "crew-orchestrator", "orchestrator", ac, t)
                station(store, "a-r", "crew-reviewer", "reviewer", ac, t, said="waiting")
                station(store, "a-b", "ana-build-m1", "builder", ac, t, project="uc_open")
                station(store, "a-s", "secret-build-m1", "builder", ac, t, project="uc_secret")
                store.create_project("uc_secret", "custom", "Secret", {}, goal="g")
                store.set_project_kind("uc_secret", "software_factory")
                store.set_project_scope("uc_secret", "private")
                am = ("ana-box", "1.60.1", AT)
                mine = dict(chosen=CHOSEN, machine=am, orch=("running", t), rev=("running", t), up=True)
                # the builder in the private project Ana is not on is not counted
                await state("Ana, her crew up", ax, onboarding(**mine, live=1), LINE_CHOSEN, projects=1)
                await state("Ben sees none of it", bx, onboarding(), projects=1)
                await state("root reads the cell's own, not Ana's", root, onboarding(), projects=1)
                # a key with no person is cut to no person's crew: it sees every station it may see
                await state("a key with no person", nx, onboarding(
                    orch=("running", t), rev=("running", t), live=1, up=True), projects=1)

                print("== Ana joins the private project ==")
                store.add_project_member("uc_secret", ana["id"])
                await state("Ana, a member", ax, onboarding(**mine, live=2), LINE_CHOSEN, projects=1)

                print("== a station that also worked in a project the caller may see ==")
                store.remove_project_member("uc_secret", ana["id"])
                station(store, "a-s0", "secret-build-m1", "builder", ac, ago(hours=4), project="uc_open",
                        said="ended")
                await state("kept for the other project", ax, onboarding(**mine, live=2), LINE_CHOSEN,
                            projects=1)

                print("== Ana has no report: her own crew names the machine ==")
                store.set_setting(co.machine_key(ana["id"]), None)
                await state("Ana, no report", ax, onboarding(
                    **{**mine, "machine": ("ana-box", "", None)}, live=2), LINE_CHOSEN, projects=1)

                print("== the root token's own: the person local ==")
                lc = store.resolve_crew("crew", "local", "cell-box", ago(hours=5))
                station(store, "l-o", "crew-orchestrator", "orchestrator", lc, t, said="idle")
                report(store, "cell-box", "1.60.0")
                await state("root, local's orchestrator", root, onboarding(
                    machine=("cell-box", "1.60.0", AT), orch=("running", t), rev=("starting", None)),
                    projects=1)


async def auth_off():
    app, store = await lifespan_app()
    async with app.router.lifespan_context(app), client(app) as cx:
        print("== auth off ==")
        await state("auth off, new", cx, onboarding())
        r = await cx.put("/api/crew/settings", json=CHOICES)
        ck("auth off: the choices are saved", r.status_code == 200, r.text)
        store.upsert_github_credential("main", "ghp_example", "token", "", "example-org")
        report(store, "cell-box", "1.60.0")
        crew = store.resolve_crew("crew", "local", "cell-box", ago(hours=5))
        t = ago(minutes=2)
        station(store, "o-1", "crew-orchestrator", "orchestrator", crew, t)
        station(store, "r-1", "crew-reviewer", "reviewer", crew, t)
        await state("auth off, all done", cx, onboarding(
            chosen=CHOSEN, github=GH, machine=("cell-box", "1.60.0", AT), orch=("running", t),
            rev=("running", t), up=True), LINE_BOTH)

        print("== auth off, no machine report: the person local's crew names the machine ==")
        store.set_setting(co.machine_key(), None)
        await state("auth off, no report", cx, onboarding(
            chosen=CHOSEN, github=GH, machine=("cell-box", "", None), orch=("running", t),
            rev=("running", t), up=True), LINE_BOTH)


def finish():
    print("== no home build ==")
    ck(f"neither setup route built the home ({SETUP_READS[0]} calls)",
       SETUP_READS[0] > 0 and SETUP_BUILDS[0] == 0, f"{SETUP_BUILDS[0]} builds")
    print(f"\n{P} passed, {F} failed")
    sys.exit(1 if F else 0)


def run_again(*args) -> None:
    """One part in a process of its own: its own database, and the auth setting it needs."""
    global P, F
    env = {k: v for k, v in os.environ.items() if k not in ("TARES_DB", "TARES_AUTH_TOKEN")}
    r = subprocess.run([sys.executable, os.path.abspath(__file__), *args], env=env,
                       capture_output=True, text=True, timeout=600)
    print(r.stdout, end="")
    last = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else ""
    words = last.split()
    if len(words) == 4 and words[1] == "passed,":
        P += int(words[0])
        F += int(words[2])
    else:
        ck(f"the part {' '.join(args)} finished", False, r.stderr[-1500:])


if __name__ == "__main__":
    if AUTH_OFF:
        asyncio.run(auth_off())
    elif "--team" in sys.argv:
        asyncio.run(team())
    else:
        asyncio.run(one_person())
        run_again("--team")
        run_again("--auth-off")
    finish()
