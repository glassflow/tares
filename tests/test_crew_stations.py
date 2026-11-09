"""`factory spawn` in Python (tares/crew/stations.py, TR-954): what it decides before it starts a station.

The call to launch is replaced by a recorder, so these checks need no Claude Code and no bash: each spawn gets a
fake Tares (tests/factory/launch_support.py: health, crew settings, projects and an MCP initialize) and a
throwaway crew, and the test looks at what spawn hands to launch, what it records and what it refuses. The same
spawns run against bash, scenario by scenario, in the parity harness (`parity.py spawn_*`), which also starts the
fake Claude Code.

The test runs in a fresh HOME with no keyring: nothing of the person's is read or written.

Run: .venv/bin/python tests/test_crew_stations.py
"""
import io
import json
import os
import subprocess
import sys
import tempfile
from contextlib import redirect_stderr
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HERE = Path(__file__).resolve().parent

SCRATCH = Path(tempfile.mkdtemp(prefix="tares-crew-stations-"))
for _k in [k for k in os.environ if k.startswith(("TARES", "FACTORY", "FAKE_CLAUDE", "CLAUDE_PLUGIN_OPTION_"))]:
    del os.environ[_k]
os.environ.pop("TARESD_URL", None)
os.environ.update(
    HOME=str(SCRATCH / "home"), USERPROFILE=str(SCRATCH / "home"),
    PYTHON_KEYRING_BACKEND="keyring.backends.fail.Keyring", TARES_HOME=str(SCRATCH / "tares-home"),
    FACTORY_PAUSE="0", FACTORY_NO_WATCH="1", FACTORY_CREW_HOME=str(SCRATCH / "crew"),
)
(SCRATCH / "home").mkdir()

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE / "factory"))

from tares.crew import claude, dispatch, launch as L, stations as S  # noqa: E402
from tares.crew.client import TaresClient  # noqa: E402
from tares.crew.errors import CrewError  # noqa: E402
from tares.crew.paths import CrewPaths  # noqa: E402
import launch_support as LS  # noqa: E402

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def repo(name: str) -> str:
    d = SCRATCH / name
    d.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(d)], check=True)
    return subprocess.run(["git", "-C", str(d), "rev-parse", "--show-toplevel"], check=True, stdout=subprocess.PIPE,
                          text=True).stdout.strip()


LINKS, OTHER, SHOP = repo("links"), repo("other"), repo("shop")
PROJECTS = [
    {"id": "uc_links", "name": "Links App", "repos": [{"path": LINKS}]},
    {"id": "uc_strict", "name": "Strict", "autonomy": "L3-review", "repos": [{"path": SHOP}]},
    {"id": "uc_crew", "name": "Crew Tools", "repos": [{"path": SHOP}]},
    {"id": "uc_named", "name": "No Path", "repos": [{"name": "glassflow/nopath"}]},
]
SETUP = {"stations": {"crew-reviewer": {"role": "reviewer", "status": "active", "project": "", "repo": "", "cwd": str(SCRATCH / "crew"),
                                        "model": "sonnet", "effort": "high"}}}
LS.make_crew(SCRATCH, SETUP)
CREW = SCRATCH / "crew"

launched: list = []
real_launch = L.launch
L.launch = lambda client, *a, **kw: launched.append(a)  # what spawn hands over, not a session


def run(args, **world):
    """Spawn on a fake Tares; (the CrewError message or None, the launches it made)."""
    n = len(launched)
    w = {"settings": {"autonomy": "L4-ship", "releaser": True, "release_profile": "kind", "challenger": False, "builders_max": 3},
         "projects": PROJECTS, **world}
    with LS.FakeTares(w) as t:
        os.environ["TARESD_URL"] = t.url
        os.environ["TARES_FACTORY_MCP_URL"] = t.url + "/mcp"
        paths = CrewPaths.from_env()
        client = TaresClient(paths)
        try:
            client.prime()
            S.spawn(client, paths, list(args))
            return None, launched[n:]
        except CrewError as e:
            return str(e), launched[n:]
        finally:
            client.close()


def stations() -> dict:
    return json.loads((CREW / "stations.json").read_text(encoding="utf-8"))


print("names")
ck("station_name: a role word is this crew's station", S.station_name("reviewer") == "crew-reviewer" and S.station_name("releaser") == "crew-releaser"
   and S.station_name("orchestrator") == "crew-orchestrator")
ck("station_name: anything else is already a name", S.station_name("shop-build-m1") == "shop-build-m1" and S.station_name("") == "")
ck("the dispatcher has spawn", dispatch.find(["spawn", "builder", "x"]) is not None)
ck("pace_read is a stub: nothing is paced until TR-962", S.pace_read_stub() is None)

print("a builder")
err, ls = run(["builder", "Links App", "m1", LINKS, "work T1 and T2"])
ck("a builder starts", err is None and len(ls) == 1, str(err))
a = ls[0] if ls else ()
ck("launch gets the station, role, parent, project, repo and cwd",
   a[:6] == ("links-app-build-m1", "builder", "crew-orchestrator", "Links App", LINKS, LINKS), str(a[:6]))
ck("...the crew's builder model and effort", len(a) > 7 and bool(a[6]) and bool(a[7]), str(a[6:8]))
ck("...the boot prompt carries the task", "work T1 and T2" in a[8] and a[8].startswith("Boot as the builder station"), a[8][:80])
rec = stations().get("links-app-build-m1", {})
ck("it is recorded with the project id and its autonomy", rec.get("project_id") == "uc_links" and "autonomy" in rec, str(rec))
exclude = Path(subprocess.run(["git", "-C", LINKS, "rev-parse", "--path-format=absolute", "--git-common-dir"], check=True,
                              stdout=subprocess.PIPE, text=True).stdout.strip()) / "info" / "exclude"
ck("the repo hides .factory and has a worktrees folder",
   ".factory/" in exclude.read_text(encoding="utf-8").split("\n") and (Path(LINKS) / ".factory" / "worktrees").is_dir())
run(["builder", "Links App", "m2", LINKS, "x"])
ck(".factory/ is added to the exclude file once", exclude.read_text(encoding="utf-8").split("\n").count(".factory/") == 1)
(CREW / "stations" / "links-app-build-m3.done").write_text("", encoding="utf-8")
run(["builder", "Links App", "m3", LINKS, "x"])
ck("a builder's .done file is removed when it starts again", not (CREW / "stations" / "links-app-build-m3.done").exists())
err, ls = run(["builder", "--autonomy", "L3-review", "Links App", "m4", LINKS, "x"])
ck("--autonomy L3-review under an L4 crew is recorded", err is None and stations()["links-app-build-m4"]["autonomy"] == "L3-review", str(err))
err, ls = run(["builder", "Strict", "m1", SHOP, "x"])
ck("a project handed over stricter keeps it", err is None and stations()["strict-build-m1"]["autonomy"] == "L3-review", str(err))

print("refusals")
def refused(label, args, text, **world):
    err, ls = run(args, **world)
    ck(label, err is not None and text in err and not ls, str(err))


refused("too few arguments", ["builder", "Links App", "m9"], "usage: factory spawn builder [--autonomy L3-review]")
refused("an unknown kind", ["wizard"], "spawn takes builder or helper")
refused("a level that does not exist", ["builder", "--autonomy", "L5-dark", "Links App", "m9", LINKS, "x"], "autonomy must be L3-review or L4-ship")
refused("a level looser than the crew", ["builder", "--autonomy", "L4-ship", "Links App", "m9", LINKS, "x"],
        "a project can only be stricter than the crew (crew autonomy is L3-review)",
        settings={"autonomy": "L3-review", "releaser": True, "release_profile": "kind", "challenger": False, "builders_max": 3})
refused("a project that is not handed over", ["builder", "Nobody", "m9", LINKS, "x"], "Nobody is not handed to the crew on Tares")
refused("a folder that is no repository", ["builder", "Links App", "m9", str(SCRATCH / "nowhere"), "x"], "is not a git repository")
refused("a repo of another project", ["builder", "Links App", "m9", OTHER, "x"], "is not one of Links App's repos on Tares")
refused("a repo listed by name only has no path to match", ["builder", "No Path", "m9", LINKS, "x"], "glassflow/nopath")
refused("a project named like the crew", ["builder", "Crew Tools", "m9", SHOP, "x"], "a project name can't start with \"crew\"")
refused("a slug with no letters", ["builder", "Links App", "!!!", LINKS, "x"], "need letters or digits a station name can use")
refused("no MCP server", ["builder", "Links App", "m9", LINKS, "x"], "no MCP server answers at", mcp=False)
# launch is replaced here, so no spawn above became an active builder: record one the way launch does
_saved = (CREW / "stations.json").read_text(encoding="utf-8")
_all = json.loads(_saved)
_all["links-app-build-m1"] = {**_all.get("links-app-build-m1", {}), "role": "builder", "status": "active"}
(CREW / "stations.json").write_text(json.dumps(_all), encoding="utf-8")
refused("builders_max reached", ["builder", "Links App", "m9", LINKS, "x"],
        "already 1 active builders (crew setting builders_max is 1); retire one first",
        settings={"autonomy": "L4-ship", "releaser": True, "release_profile": "kind", "challenger": False, "builders_max": 1})
refused("a builders_max that is no number refuses too", ["builder", "Links App", "m9", LINKS, "x"],
        "(crew setting builders_max is lots); retire one first",
        settings={"autonomy": "L4-ship", "releaser": True, "release_profile": "kind", "challenger": False, "builders_max": "lots"})
(CREW / "stations.json").write_text(_saved, encoding="utf-8")
S.pace_read_stub, real_pace = (lambda: {"level": 1}), S.pace_read_stub
refused("a plan that is nearly full holds new builders", ["builder", "Links App", "m9", LINKS, "x"], "no new builder starts now")
err, ls = run(["helper", "crew-reviewer", "probe", "look"])
ck("...but not helpers", err is None and len(ls) == 1, str(err))
S.pace_read_stub = real_pace

print("a helper")
err, ls = run(["helper", "reviewer", "probe", "look at PR 9"])
a = ls[0] if ls else ()
ck("a helper is named after its parent and keeps its parent's place",
   err is None and a[:6] == ("crew-reviewer-probe", "helper", "crew-reviewer", "", "", str(CREW)), f"{err} {a[:6]}")
ck("its prompt names the parent's manual, the parent and the task",
   a[8] == "Boot as a helper of the station crew-reviewer: load the skills factory-protocol and station-reviewer, do only the task below, "
           "write results to your report, and send crew-reviewer DONE when it is finished.\n\nYour task:\nlook at PR 9", a[8:9])
err, ls = run(["releaser-helper", "deploy", "ship it"])
ck("a releaser-helper belongs to the releaser", err is not None and "crew-releaser is not a recorded station" in err, str(err))
refused("a parent that is not recorded", ["helper", "nobody", "probe", "x"], "nobody is not a recorded station")
refused("a helper without a task", ["helper", "crew-reviewer", "probe"], "usage: factory spawn helper <parent-station> <slug> \"<task>\"")

print("refusals before anything starts")
(CREW / "HALT").write_text('{"at":"2026-01-01T00:00:00Z","reason":"testing"}\n', encoding="utf-8")
refused("a halted crew says what is held and how to go on", ["builder", "Links App", "m9", LINKS, "x"],
        'the crew is halted ({"at":"2026-01-01T00:00:00Z","reason":"testing"}); tares factory crew resume first')
(CREW / "HALT").unlink()
claude_alive, claude.is_alive = claude.is_alive, (lambda *a, **k: True)
refused("a station that is running is not started twice", ["builder", "Links App", "m5", LINKS, "x"],
        "links-app-build-m5 is already running; message it instead")
claude.is_alive = claude_alive
ck("...and a refused spawn did not record a builder", "links-app-build-m5" not in stations())

print("the command")
buf = io.StringIO()
with redirect_stderr(buf):
    with LS.FakeTares({"settings": {"autonomy": "L4-ship", "releaser": True, "release_profile": "kind", "challenger": False,
                                    "builders_max": 3}, "projects": PROJECTS}) as t:
        env = {**os.environ, "TARESD_URL": t.url, "TARES_FACTORY_MCP_URL": t.url + "/mcp"}
        rc_bad = S.cmd_spawn(["spawn", "wizard"], env)
        rc_ok = S.cmd_spawn(["spawn", "helper", "crew-reviewer", "second", "look"], env)
ck("a refusal prints its line on stderr and exits 1", rc_bad == 1 and buf.getvalue().strip() == "tares factory: spawn takes builder or helper", buf.getvalue())
ck("a spawn that works exits 0", rc_ok == 0 and "crew-reviewer-second" in [x[0] for x in launched])

L.launch = real_launch
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
