"""The crew command line starts a station from the cell's launch spec (TR-870, on the cell side of TR-867).

`launch.launch_plan` asks Tares for `GET /api/factory/stations/<station>/launch` and starts from the answer: no
launch of its own is left on the command line. Covered, each against a fake Tares that builds its answer with the
real runtime (tests/factory/fake_launch.py), in a throwaway crew home:
 a. Tares up: the spec is fetched, the plan is the spec with this machine's values put in, and the raw spec is
    kept at <crew home>/launch/<station>.json;
 b. Tares down, a copy kept: the plan is the same, built from the copy, and a line says so;
 c. Tares down, no copy: the start stops with "Tares is unreachable and this station has never started; start
    Tares first.", and writes nothing;
 d. the plan is the route's answer filled, for a builder with a project and a repo and for a reviewer with
    neither, argument for argument, key for key;
 e. a project Tares does not list is left out of the request with a line saying so, and the station starts with
    no project variable;
 f. a name that is not a station name (a slash, a backslash, two dots, a space, a leading dot or hyphen, upper
    case, a length over 120) is refused with the rule before Tares is asked; ordinary names pass;
 g. `crew status` and `crew down` never ask for a capability or a launch spec.
Also: the refusals and the older cell (no launch_spec), and what the command line adds (FACTORY_TARES only when
a `tares` is on the PATH).

The route's side of (f) is in tests/test_runtime_launch.py.

Run: python tests/test_crew_launch_spec.py
"""
import io
import json
import os
import shutil
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HERE = Path(__file__).resolve().parent

SCRATCH = Path(tempfile.mkdtemp(prefix="tares-crew-launch-spec-"))
for _k in [k for k in os.environ if k.startswith(("TARES", "FACTORY", "FAKE_CLAUDE", "CLAUDE_PLUGIN_OPTION_"))]:
    del os.environ[_k]
os.environ.pop("TARESD_URL", None)
os.environ.update(HOME=str(SCRATCH / "home"), USERPROFILE=str(SCRATCH / "home"), TARES_HOME=str(SCRATCH / "tares-home"),
                  PYTHON_KEYRING_BACKEND="keyring.backends.fail.Keyring", FACTORY_PAUSE="0", FACTORY_NO_WATCH="1")
(SCRATCH / "home").mkdir()
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE / "factory"))

from tares.crew import launch as L  # noqa: E402
from tares.crew import lifecycle, rules, state, views  # noqa: E402
from tares.crew.client import TaresClient  # noqa: E402
from tares.crew.errors import CrewError  # noqa: E402
from tares.crew.paths import CrewPaths  # noqa: E402
from tares.integrations.runtime import fill_placeholders  # noqa: E402
import launch_support as LS  # noqa: E402
import parity  # noqa: E402

P = F = 0
FAKE_TOKEN = "tok-fake-launch-spec-0123456789"
NEVER = "Tares is unreachable and this station has never started; start Tares first."
_n = [0]


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def crew(world=None, setup=None, stations=None):
    """A fresh crew home and a fake Tares with `world`; returns (tares, paths, said, make_client). `said` is the
    list of lines the client says."""
    _n[0] += 1
    tmp = SCRATCH / f"t{_n[0]}"
    tmp.mkdir()
    LS.make_crew(tmp, {"token": FAKE_TOKEN, "stations": stations or {}, **(setup or {})})
    tares = LS.FakeTares(world or {})
    tares.__enter__()
    paths = CrewPaths.at(tmp / "crew")
    said = []

    def make_client():
        return TaresClient(paths, env={**os.environ, "TARESD_URL": tares.url}, say=said.append)
    return tares, paths, said, make_client


def plan_of(client, station="crew-reviewer", role="reviewer", parent="", project="", repo=""):
    try:
        return L.launch_plan(client, station, role, parent, project, repo), None
    except CrewError as e:
        return None, str(e)


def project_world(tmp_repo):
    return {"projects": [{"id": "uc_links", "name": "Links App", "repos": [{"path": str(tmp_repo)}]}],
            "settings": {"autonomy": "L3-review", "releaser": True, "release_profile": "kind", "challenger": False,
                         "builders_max": 3}}


# ---------------------------------------------------------------------------------------------
print("a. Tares up: the spec is fetched and kept")
tares, paths, said, mk = crew()
client = mk()
try:
    plan, err = plan_of(client)
    ck("launch_plan answers", plan is not None, err)
    copy_file = paths.launch_file("crew-reviewer")
    ck("the raw spec is kept at <crew home>/launch/<station>.json", copy_file.is_file(), str(copy_file))
    raw = json.loads(copy_file.read_text(encoding="utf-8")) if copy_file.is_file() else {}
    ck("...it is the cell's spec, with the placeholders still in",
       raw.get("runtime") == "claude_code" and raw.get("station") == "crew-reviewer" and "{SETTINGS_FILE}" in raw.get("command", [])
       and raw.get("env", {}).get("FACTORY_HOME") == "{CREW_HOME}", str(raw)[:300])
    ck("the plan has the placeholders filled", plan and "{" not in json.dumps(plan["args"])
       and plan["env"]["FACTORY_HOME"] == L._forward(paths.raw), str(plan)[:300] if plan else "")
    ck("the key is not in the copy and not in the plan",
       FAKE_TOKEN not in json.dumps(raw) and FAKE_TOKEN not in json.dumps(plan), "")
    ck("no rules copy is written any more (the rules are in the spec)", not (paths.home / "rules").exists())
    ck("a start with Tares up says nothing about a copy", not any("kept from its last start" in s for s in said), str(said))
finally:
    client.close()
    tares.__exit__(None, None, None)

# ---------------------------------------------------------------------------------------------
print("b. Tares down, with a copy: the start uses it and says so")
saved_plan = plan
dead = SCRATCH / "t1" / "crew"
client = TaresClient(paths, env={**os.environ, "TARESD_URL": tares.url}, say=said.append)   # the fake is stopped
try:
    said.clear()
    plan_b, err = plan_of(client)
    ck("the plan is built from the copy", plan_b is not None and plan_b == saved_plan, err or "")
    ck("a line says the launch is the one kept from the last start",
       any("kept from its last start" in s and "crew-reviewer" in s for s in said), str(said))
    ck("no line says it for a start with Tares up (see a) and none has an em dash", all("—" not in s for s in said), str(said))
finally:
    client.close()

# ---------------------------------------------------------------------------------------------
print("c. Tares down, no copy: the start stops")
tares, paths2, said2, mk2 = crew()
tares.__exit__(None, None, None)
client = mk2()
try:
    plan_c, err = plan_of(client)
    ck("the start stops with the one message", plan_c is None and err == NEVER, str(err))
    ck("nothing is written for the station", not paths2.launch_file("crew-reviewer").exists()
       and not paths2.station_file("crew-reviewer", "settings.json").exists())
finally:
    client.close()

# ---------------------------------------------------------------------------------------------
print("d. the plan is the route's answer filled")
repo = SCRATCH / "links"
repo.mkdir()
tares, paths, said, mk = crew(project_world(repo), stations={"links-app-build-m1": {"autonomy": "L3-review"}})
client = mk()
try:
    for station, role, parent, project, rp in (("links-app-build-m1", "builder", "crew-orchestrator", "Links App", str(repo)),
                                                ("crew-reviewer", "reviewer", "", "", "")):
        plan, err = plan_of(client, station, role, parent, project, rp)
        raw = json.loads(paths.launch_file(station).read_text(encoding="utf-8")) if paths.launch_file(station).is_file() else {}
        label = f"{role} {'with' if project else 'without'} a project"
        ck(f"{label}: the plan was made", plan is not None and bool(raw), err)
        if not plan or not raw:
            continue
        home = L._forward(paths.raw)
        kit = L.kit_dir(client)
        repos, _, wt = L._repo_words(client, project, rp)
        values = {"CREW_HOME": home, "PLUGIN_DIR": kit, "PLUGIN_DIR_SHELL": L.shell_word(L._forward(kit)), "WT_REPOS": wt,
                  "REPOS": repos, "TOKEN_FILE": f"{home}/{paths.token_file.name}",
                  "SETTINGS_FILE": L.settings_path(client, station)[1]}
        want = fill_placeholders({"args": raw["command"], "env": raw["env"], "settings": raw["settings"]}, values)
        env = {k: v for k, v in plan["env"].items() if k != "FACTORY_TARES"}
        ck(f"{label}: the arguments equal the filled command, one for one", plan["args"] == want["args"], str(plan["args"])[:300])
        ck(f"{label}: the environment equals the filled env, but for FACTORY_TARES", env == want["env"],
           {k: (env.get(k), want["env"].get(k)) for k in set(env) | set(want["env"]) if env.get(k) != want["env"].get(k)})
        ck(f"{label}: the settings equal the filled settings, deep", plan["settings"] == want["settings"])
        ck(f"{label}: the plan names the station, role and the project the cell resolved",
           plan["env"]["FACTORY_STATION"] == station and plan["env"]["FACTORY_ROLE"] == role
           and plan["env"].get("TARES_PROJECT") == ("uc_links" if project else None), str(plan["env"])[:300])
    ck("the builder's repo is in its environment and its worktrees live there",
       plan_of(client, "links-app-build-m1", "builder", "crew-orchestrator", "Links App", str(repo))[0]["env"]["FACTORY_WORKTREES"]
       == f"{L._forward(str(repo))}/.factory/worktrees")
    # write_settings keeps the project id the cell resolved
    L.write_settings(client, "links-app-build-m1", "builder", "crew-orchestrator", "Links App", str(repo))
    ck("write_settings records the project id from the spec's TARES_PROJECT",
       state.stations_get("links-app-build-m1", "project_id", paths=paths) == "uc_links")
    file = json.loads(paths.station_file("links-app-build-m1", "settings.json").read_text(encoding="utf-8"))
    ck("...and writes {env, **settings} as the settings file", list(file)[0] == "env" and file["env"]["TARES_PROJECT"] == "uc_links"
       and "autoMode" in file)
    ck("a start does not need a rules copy, a default project or any other launch of its own",
       not hasattr(L, "_settings_parts") and not hasattr(L, "_project_id") and not hasattr(rules, "for_station"))
finally:
    client.close()
    tares.__exit__(None, None, None)

# ---------------------------------------------------------------------------------------------
print("e. a project Tares does not list")
tares, paths, said, mk = crew(project_world(repo), stations={"links-app-build-m1": {"autonomy": "L3-review"}})
client = mk()
try:
    plan, err = plan_of(client, "links-app-build-m1", "builder", "crew-orchestrator", "Ghost", str(repo))
    ck("the plan is made", plan is not None, err)
    ck("a line says the project was not found and the station starts without a default project",
       any("could not find Ghost on Tares" in s and "without a default project" in s for s in said), str(said))
    ck("the plan has no project variable", plan and not any(k in plan["env"] for k in ("TARES_PROJECT", "TARES_PROJECT_NAME", "FACTORY_PROJECT")),
       str(plan["env"])[:300] if plan else "")
    said.clear()
    plan2, _ = plan_of(client, "links-app-build-m1", "builder", "crew-orchestrator", "Links App", str(repo))
    ck("a listed project is named, with no line", plan2 and plan2["env"].get("TARES_PROJECT") == "uc_links" and not said, str(said))
finally:
    client.close()
    tares.__exit__(None, None, None)

# ---------------------------------------------------------------------------------------------
print("f. a name that is not a station name")
tares, paths, said, mk = crew()
client = mk()
asked = []
real_ask = L._ask
L._ask = lambda *a, **k: (asked.append(a[1]), real_ask(*a, **k))[1]
try:
    bad = ["a/b", "a\\b", "..", "a..b", "../x", "a b", ".hidden", "-lead", "Shop", "a" * 121, "", " ", "a:b", "x/../y"]
    for name in bad:
        plan, err = plan_of(client, name)
        ck(f"{name!r} is refused, with the rule", plan is None and err and state.STATION_NAME_RULE in err and name in err or
           (plan is None and err and state.STATION_NAME_RULE in err and name.strip() == ""), str(err))
    ck("Tares is not asked for any of them", asked == [], str(asked))
    for name in ("a", "x_y-1", "a" * 120, "crew-reviewer", "ashish-crew-factory-is-modular-build-m5-tares", "0abc"):
        plan, err = plan_of(client, name)
        ck(f"{name[:30]!r} passes", plan is not None, str(err))
    ck("the rule is one sentence a person can follow",
       state.station_name_ok("a-b_c1") and not state.station_name_ok("") and not state.station_name_ok(None)
       and "1 to 120 characters" in state.STATION_NAME_RULE)
finally:
    L._ask = real_ask
    client.close()
    tares.__exit__(None, None, None)

# ---------------------------------------------------------------------------------------------
print("g. status and down never ask for a launch")
repo2 = SCRATCH / "g-repo"
repo2.mkdir()
tmp = SCRATCH / "g"
tmp.mkdir()
LS.make_crew(tmp, {"token": FAKE_TOKEN, "stations": {"crew-reviewer": {"role": "reviewer", "status": "ended"}}})
tares = LS.FakeTares({})
tares.__enter__()
calls = {"cap": [], "ask": []}
real_need, real_ask = TaresClient.need_capability, L._ask
TaresClient.need_capability = lambda self, name, *a, **k: (calls["cap"].append(name), real_need(self, name, *a, **k))[1]
L._ask = lambda *a, **k: (calls["ask"].append(a[1]), real_ask(*a, **k))[1]
try:
    env = {**parity.crew_env(tmp), "TARESD_URL": tares.url}
    saved_env = dict(os.environ)
    os.environ.update(env)   # the view commands find the crew home in the process environment
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = views.cmd_status(["status"], env)
    ck("crew status runs", code == 0, buf.getvalue()[-200:])
    ck("...and asks for no capability and no launch", calls == {"cap": [], "ask": []}, str(calls))
    paths_g = CrewPaths.at(tmp / "crew")
    client = TaresClient(paths_g, env=env, say=lambda s: None)
    try:
        lifecycle.down(client, paths_g, [], claude_bin=env["CLAUDE_BIN"])
    finally:
        client.close()
    ck("crew down runs and asks for no capability and no launch", calls == {"cap": [], "ask": []}, str(calls))
    ck("...and a start of the same crew does ask (the check is live)",
       plan_of(TaresClient(paths_g, env=env, say=lambda s: None))[0] is not None and calls["cap"] == ["launch_spec"]
       and calls["ask"] == ["crew-reviewer"], str(calls))
finally:
    TaresClient.need_capability, L._ask = real_need, real_ask
    os.environ.clear()
    os.environ.update(saved_env)
    tares.__exit__(None, None, None)

# ---------------------------------------------------------------------------------------------
print("the refusals, and an older cell")
for status, detail, want in ((401, "no key", "Tares refused to give the launch of crew-reviewer (HTTP 401)"),
                             (403, "no", "Tares refused to give the launch of crew-reviewer (HTTP 403)"),
                             (404, "unknown project", "Tares would not give the launch of crew-reviewer: unknown project"),
                             (422, "no rules for crew-reviewer", "Tares would not give the launch of crew-reviewer: no rules for crew-reviewer")):
    tares, paths, said, mk = crew({"launch_status": status, "launch_detail": detail})
    client = mk()
    try:
        plan, err = plan_of(client)
        ck(f"HTTP {status} stops the start: {want[:50]}", plan is None and err and want in err, str(err))
        ck(f"...and no copy stands in for it", not paths.launch_file("crew-reviewer").exists())
    finally:
        client.close()
        tares.__exit__(None, None, None)
tares, paths, said, mk = crew()
client = mk()
try:
    plan_of(client)   # a copy exists now
    tares.world["launch_status"] = 500
    plan, err = plan_of(client)
    ck("a 500 from the cell falls back to the copy, with a line", plan is not None and any("kept from its last start" in s for s in said), str(err or said))
    tares.world["launch_status"] = 200
    tares.world["launch"] = {"nonsense": True}
    plan, err = plan_of(client)
    ck("an answer that is not a spec falls back to the copy too", plan is not None, str(err))
    tares.world["launch"] = None
    tares.world["capabilities"] = ["integrations", "merge_queue", "guardrail_rules"]
    client._caps = None
    plan, err = plan_of(client)
    ck("a cell without launch_spec is refused with the upgrade message, copy or not",
       plan is None and err and "launch_spec" in err and "Upgrade Tares" in err, str(err))
finally:
    client.close()
    tares.__exit__(None, None, None)

print("what the command line adds")
tares, paths, said, mk = crew()
client = mk()
real_which = shutil.which
try:
    shutil.which = lambda name, *a, **k: "/opt/bin/tares" if name == "tares" else real_which(name, *a, **k)
    ck("FACTORY_TARES is the `tares` on the PATH", plan_of(client)[0]["env"].get("FACTORY_TARES") == "/opt/bin/tares")
    shutil.which = lambda name, *a, **k: None if name == "tares" else real_which(name, *a, **k)
    got = plan_of(client)[0]
    ck("...and is left out when there is none", "FACTORY_TARES" not in got["env"])
finally:
    shutil.which = real_which
    client.close()
    tares.__exit__(None, None, None)

shutil.rmtree(SCRATCH, ignore_errors=True)
print(f"{P} passed, {F} failed")
sys.exit(1 if F else 0)
