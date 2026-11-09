#!/usr/bin/env python3
"""The parity harness: a factory scenario runs against the bash engine and against the Python engine,
and what they print and write must be the same.

    python tests/factory/parity.py <scenario>            run one scenario on both engines (a name with * or ?, such as tidy_*, runs every match)
    python tests/factory/parity.py --list                the scenarios defined here
    python tests/factory/parity.py --coverage            every scenario of every bash suite, and how
                                                         many of them pass against Python
    python tests/factory/parity.py --coverage --require-full   the same, exit 1 below 100%

A scenario is defined here, in Python (steps to run, checks on the result), so it runs on Windows too,
where the bash suites cannot. It is run twice, each time in a throwaway crew with the Python fake
Claude Code (tests/factory/fake_claude.py): with TARES_FACTORY_ENGINE=bash and with =python, through
`python -m tares.crew`. The bash suites in this folder stay the reference; a scenario here is one of
their checks moved over, with the same assertions.

What is compared, after normalising: the exit code, stdout and stderr of every step; every file in the
crew home; the calls made to Claude Code and the sessions it lists. Normalised away: the temp folder,
timestamps, pids, session ids and the key order of JSON. Ids and epoch times are only replaced where they
appear as such (after `backgrounded ·`, `job`, `at`, `since`, under an id or time key in JSON): a plain
eight-digit or ten-digit number is data and is compared as it is.

Result, per side: PASS, FAIL, or SKIP. The Python engine answers exit 2 with "is not yet ported" for a
command that has not moved over; that side prints `SKIP python: not ported`. Without bash (Windows)
the bash side prints `SKIP bash: ...`. Exit 0 when the bash side passes and the Python side passes
(same normalised output as bash) or is skipped. Exit 1 when the bash side fails, or when Python runs
and fails a check or differs from bash. On Windows a skipped bash side is expected and the Python side
stands alone; anywhere else a skipped bash side (no bash or jq) is an error: exit 1, and the scenario
does not count in --coverage.

Coverage. A "scenario" of a bash suite (the suites named in tests/factory/ci_list.txt) is one `check
"<label>"` call: a source line that starts, after any indentation, with `check "`. A check in a loop
counts once. A suite with no such line (guard, heavy, parse) counts as one scenario, named after the
suite. A scenario here lists the labels it covers (`covers`), and counts as passing against Python
when its run passes on the Python side (a SKIP does not count). Percentage = passing / all scenarios.

Test hooks (for tests/test_parity.py): PARITY_CMD_BASH and PARITY_CMD_PYTHON replace the command a side
runs (default `python -m tares.crew`); PARITY_FAIL=bash|python|both adds a failing check to that side.

Standard library only.
"""
from __future__ import annotations

import difflib
import fnmatch
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
FAKE_CLAUDE = HERE / "fake_claude.py"
CI_LIST = HERE / "ci_list.txt"
ENGINES = ("bash", "python")
STEP_TIMEOUT = 180
NOT_PORTED = "not yet ported"


# ---------------------------------------------------------------------------------------------
# Scenarios

@dataclass
class Step:
    argv: list           # everything after `tares factory`; "{tmp}" in an argument or env value is the scenario's folder,
                         # "{tares}" in an env value is the address of the scenario's fake Tares
    env: dict = field(default_factory=dict)
    stdin: str = ""
    cwd: str = ""        # the folder the command runs in ("{tmp}" works); default the repo root
    before: object = None   # fn(tmp) run before the step: change files between two commands
    after: object = None    # fn(tmp) -> anything, run after the step; Run.snap(i) gives it back to a check
    drop: tuple = ()        # regexes: printed lines that match one are left out of the comparison (not of the checks)


@dataclass
class Scenario:
    name: str
    suite: str           # the bash suite it comes from (tests/factory/<suite>_test.sh)
    steps: list
    checks: list         # (label, fn(Run) -> bool), the assertions of the bash suite, kept as they are
    covers: list         # labels of the suite's `check` calls this scenario stands for
    about: str = ""
    setup: object = None    # fn(tmp, env) run once before the first step: build files, a repo, a link
    tares: dict | None = None   # routes of a fake Tares to run for the scenario (see FakeTares); None: no Tares
    driver: str = ""     # a script of this folder that runs a function of the engine instead of a command
                         # (`<driver> <engine> <argv...>`), for code that has no command yet
    ignore_calls: tuple = ()  # argument prefixes of calls to Claude Code that are left out of the comparison


class Run:
    """What a scenario's checks can look at: the steps' output and the crew's state on disk."""

    def __init__(self, tmp: Path, results: list, tares=None):
        self.tmp = tmp
        self.tares = tares
        self.crew = tmp / "crew"
        self.state = tmp / "claude"
        self.results = results
        self.ignore_calls: tuple = ()

    def out(self, i: int = -1) -> str:
        """stdout and stderr of step i together, as the bash suites capture them (`>out 2>&1`)."""
        r = self.results[i]
        return r["stdout"] + r["stderr"]

    def code(self, i: int = -1) -> int:
        return self.results[i]["code"]

    def snap(self, i: int = -1):
        """What step i's `after` function returned (None when it has none)."""
        return self.results[i].get("snap")

    def calls(self) -> list:
        try:
            lines = (self.state / "calls.jsonl").read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        return [json.loads(x) for x in lines if x.strip()]

    def launches(self) -> int:
        """Sessions started with `claude --bg` (the suites' `launches`)."""
        return sum(1 for c in self.calls() if "--bg" in c["args"])


SCENARIOS: dict[str, Scenario] = {}


def scenario(s: Scenario) -> Scenario:
    SCENARIOS[s.name] = s
    return s


# crew_test.sh, its first scenario: "No Tares: every command says so plainly and starts nothing."
# Same assertions as the bash check: the message names the address and `tares up`, nothing is launched.
scenario(Scenario(
    name="crew_basic",
    suite="crew",
    about="without a Tares, `crew up --yes` says so plainly and starts nothing",
    steps=[Step(["crew", "up", "--yes"], env={"TARESD_URL": "http://127.0.0.1:9"})],
    checks=[("without Tares crew up stops plainly",
             lambda r: "no Tares answers at http://127.0.0.1:9" in r.out(0)
             and "tares up" in r.out(0) and r.launches() == 0)],
    covers=["without Tares crew up stops plainly"],
))


# ---------------------------------------------------------------------------------------------
# tidy_test.sh: tidy removes inside the crew's own folders and nowhere else. The bash suite hands a
# project over to a running Tares so its repo's .factory/worktrees is a root; here FACTORY_REPO names the
# same root, and TARESD_URL points at a closed port so a Tares on the host cannot add roots.

NO_TARES = {"TARESD_URL": "http://127.0.0.1:9"}


def _git_env(tmp: Path) -> dict:
    return {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_GLOBAL": str(tmp / "gitconfig"), "GIT_CONFIG_NOSYSTEM": "1"}


def _git(tmp: Path, *args, cwd=None) -> None:
    subprocess.run(["git", *args], cwd=str(cwd) if cwd else None, env=_git_env(tmp), check=True,
                   capture_output=True)


def _link_dir(target: Path, link: Path) -> None:
    """A link to a folder: a symlink, or on Windows a junction (no privilege needed)."""
    if sys.platform == "win32":
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], check=True, capture_output=True)
    else:
        os.symlink(target, link)


def _tidy_base(tmp: Path) -> Path:
    """The crew's worktrees and tmp, an `outside` folder that must survive, and a repo with a remote."""
    for d in ("crew/worktrees", "crew/tmp", "outside/node_modules"):
        (tmp / d).mkdir(parents=True, exist_ok=True)
    (tmp / "outside" / "node_modules" / "keep").write_text("keep\n")
    (tmp / "outside" / "file").write_text("keep\n")
    r = tmp / "repo"
    _git(tmp, "init", "-q", "--bare", str(tmp / "origin.git"))
    _git(tmp, "clone", "-q", str(tmp / "origin.git"), str(r))
    (r / "tracked.txt").write_text("a\n")
    _git(tmp, "add", ".", cwd=r)
    _git(tmp, "commit", "-q", "-m", "init", cwd=r)
    _git(tmp, "push", "-q", "origin", "HEAD", cwd=r)
    return r


def _worktree(tmp: Path, name: str) -> Path:
    w = tmp / name
    _git(tmp, "worktree", "add", "-q", "--detach", str(w), "HEAD", cwd=tmp / "repo")
    return w


def _setup_worktrees(tmp: Path, env: dict) -> None:
    _tidy_base(tmp)
    w = _worktree(tmp, "crew/worktrees/p-pr1")
    _link_dir(tmp / "outside" / "node_modules", w / "node_modules")
    (w / "scratch.txt").write_text("x\n")
    (_worktree(tmp, "repo/.factory/worktrees/T2") / "tracked.txt").write_text("changed\n")
    (_worktree(tmp, "repo/.factory/worktrees/T3") / "new-file.txt").write_text("new\n")


def _wip_commit(tmp: Path) -> None:
    b = tmp / "repo" / ".factory" / "worktrees" / "T2"
    _git(tmp, "checkout", "-q", "--", "tracked.txt", cwd=b)
    _git(tmp, "commit", "-q", "--allow-empty", "-m", "wip", cwd=b)


def _listed(tmp: Path, name: str) -> bool:
    out = subprocess.run(["git", "worktree", "list"], cwd=str(tmp / "repo"), env=_git_env(tmp),
                         capture_output=True, text=True).stdout
    return name in out


def _exists(*rel):
    return lambda tmp: all((tmp.joinpath(*r.split("/"))).exists() for r in rel)


_REPO = {"FACTORY_REPO": "{tmp}/repo", **NO_TARES}

scenario(Scenario(
    name="tidy_worktrees",
    suite="tidy",
    about="tidy removes a worktree through git, and refuses one with work in it unless --force",
    setup=_setup_worktrees,
    steps=[
        Step(["tidy", "{tmp}/crew/worktrees/p-pr1"], env=_REPO, after=_exists("crew/worktrees/p-pr1")),
        Step(["tidy", "--force", "{tmp}/crew/worktrees/p-pr1"], env=_REPO,
             after=lambda tmp: (_exists("crew/worktrees/p-pr1")(tmp), _listed(tmp, "p-pr1"),
                                _exists("outside/node_modules/keep")(tmp))),
        Step(["tidy", "{tmp}/repo/.factory/worktrees/T2"], env=_REPO, after=_exists("repo/.factory/worktrees/T2")),
        Step(["tidy", "{tmp}/repo/.factory/worktrees/T2"], env=_REPO, before=_wip_commit,
             after=_exists("repo/.factory/worktrees/T2")),
        Step(["tidy", "--force", "{tmp}/repo/.factory/worktrees/T2"], env=_REPO,
             after=_exists("repo/.factory/worktrees/T2")),
        Step(["tidy", "{tmp}/repo/.factory/worktrees/T3"], env=_REPO,
             after=_exists("repo/.factory/worktrees/T3/new-file.txt")),
        Step(["tidy", "--force", "{tmp}/repo/.factory/worktrees/T3"], env=_REPO),
    ],
    checks=[
        ("a review worktree with scratch files needs --force", lambda r: r.code(0) != 0 and r.snap(0)),
        ("a review worktree is removed with --force",
         lambda r: r.code(1) == 0 and not r.snap(1)[0] and not r.snap(1)[1]),
        ("its node_modules link was not followed", lambda r: r.snap(1)[2]),
        ("a worktree with uncommitted changes is refused",
         lambda r: r.code(2) != 0 and r.snap(2) and "--force" in r.out(2)),
        ("a worktree with unpushed commits is refused", lambda r: r.code(3) != 0 and r.snap(3)),
        ("--force removes it", lambda r: r.code(4) == 0 and not r.snap(4)),
        ("a worktree with new, never-added files is refused", lambda r: r.code(5) != 0 and r.snap(5)),
    ],
    covers=["a review worktree with scratch files needs --force", "a review worktree is removed with --force",
            "its node_modules link was not followed", "a worktree with uncommitted changes is refused",
            "a worktree with unpushed commits is refused", "--force removes it",
            "a worktree with new, never-added files is refused"],
))


def _setup_scratch(tmp: Path, env: dict) -> None:
    _tidy_base(tmp)
    crew_tmp = tmp / "crew" / "tmp"
    _link_dir(tmp / "outside", crew_tmp / "link")
    (crew_tmp / "inner").mkdir()
    (crew_tmp / "inner" / "keep").write_text("keep\n")
    _link_dir(crew_tmp / "inner", crew_tmp / "lg")
    (crew_tmp / "s" / "deep").mkdir(parents=True)
    (crew_tmp / "f.txt").write_text("x\n")
    (tmp / "repo" / ".factory" / "worktrees" / "T1" / "x").mkdir(parents=True)
    for v in ("v1", "v2"):
        (tmp / "job" / "tmp" / v).mkdir(parents=True)


def _gone(*rel):
    return lambda r: all(not r.tmp.joinpath(*x.split("/")).exists() for x in rel)


def _kept(*rel):
    return lambda r: all(r.tmp.joinpath(*x.split("/")).exists() for x in rel)


scenario(Scenario(
    name="tidy_links_scratch",
    suite="tidy",
    about="tidy removes a link and not what it points at, and scratch folders of the crew, a repo and a job",
    setup=_setup_scratch,
    steps=[
        Step(["tidy", "{tmp}/crew/tmp/link"], env=_REPO),
        Step(["tidy", "{tmp}/crew/tmp/lg/"], env=_REPO),
        Step(["tidy", "{tmp}/crew/tmp/s", "{tmp}/crew/tmp/f.txt"], env=_REPO),
        Step(["tidy", "{tmp}/repo/.factory/worktrees/T1"], env=_REPO),
        Step(["tidy", "{tmp}/job/tmp/v1"], env={**NO_TARES, "CLAUDE_JOB_DIR": "{tmp}/job"}),
        Step(["tidy", "{tmp}/job/tmp/v2"], env=NO_TARES),
    ],
    checks=[
        ("a link is removed, not followed",
         lambda r: not os.path.lexists(r.tmp / "crew" / "tmp" / "link") and _kept("outside/file")(r)),
        ("a link given with a trailing slash goes, its target stays",
         lambda r: not os.path.lexists(r.tmp / "crew" / "tmp" / "lg") and _kept("crew/tmp/inner/keep")(r)),
        ("a scratch folder and a file are removed", _gone("crew/tmp/s", "crew/tmp/f.txt")),
        ("a project's .factory/worktrees is inside", _gone("repo/.factory/worktrees/T1")),
        ("the job's own scratch folder is inside", _gone("job/tmp/v1")),
        ("another job's scratch folder is not", _kept("job/tmp/v2")),
    ],
    covers=["a link is removed, not followed", "a link given with a trailing slash goes, its target stays",
            "a scratch folder and a file are removed", "a project's .factory/worktrees is inside",
            "the job's own scratch folder is inside", "another job's scratch folder is not"],
))


def _setup_refusals(tmp: Path, env: dict) -> None:
    _tidy_base(tmp)
    (tmp / "crew" / "worktrees" / "a").mkdir()


def _link_root(tmp: Path) -> None:
    _link_dir(tmp / "outside", tmp / "linkroot")


def _home_x(tmp: Path) -> None:
    (tmp / "home" / "x").mkdir()


_HOME_ROOT = {**NO_TARES, "FACTORY_WORKTREES": "{tmp}/home"}

scenario(Scenario(
    name="tidy_refusals",
    suite="tidy",
    about="tidy refuses what is outside the roots, a root itself, .., the home folder and a root that is a link",
    setup=_setup_refusals,
    steps=[
        Step(["tidy", "{tmp}/outside"], env=NO_TARES),
        Step(["tidy", "{tmp}/crew/worktrees"], env=NO_TARES),
        Step(["tidy", "{tmp}/crew/worktrees/a/..", "{tmp}/crew/worktrees/a/../../crew-x"], env=NO_TARES),
        Step(["tidy", "{tmp}/home"], env=NO_TARES),
        Step(["tidy", "{tmp}/linkroot/file"], env={**NO_TARES, "FACTORY_WORKTREES": "{tmp}/linkroot"},
             before=_link_root),
        Step(["tidy", "{tmp}/home/.claude"], env=_HOME_ROOT),
        Step(["tidy", "{tmp}/home/x"], env=_HOME_ROOT, before=_home_x),
        Step(["tidy", "{tmp}/gone"], env=NO_TARES),
    ],
    checks=[
        ("outside the crew's folders is refused",
         lambda r: r.code(0) != 0 and "outside the crew" in r.out(0) and _kept("outside")(r)),
        ("a root itself is refused", _kept("crew/worktrees")),
        ("an escape through .. is refused", _kept("crew/worktrees/a")),
        ("the home folder is refused", _kept("home")),
        ("a root that is a link is not a root", _kept("outside/file")),
        ("the home folder is never a root", _kept("home/x")),
        ("something already gone is fine", lambda r: r.code(7) == 0 and "already gone" in r.out(7)),
    ],
    covers=["outside the crew's folders is refused", "a root itself is refused",
            "an escape through .. is refused", "the home folder is refused",
            "a root that is a link is not a root", "the home folder is never a root",
            "something already gone is fine"],
))


def _setup_escape(tmp: Path, env: dict) -> None:
    _tidy_base(tmp)
    _link_dir(tmp / "outside", tmp / "crew" / "tmp" / "door")
    objects = tmp / "crew" / "tmp" / "scratch" / ".git" / "objects" / "ab"
    objects.mkdir(parents=True)
    for n in ("cdef01", "cdef02"):
        f = objects / n
        f.write_text("object\n")
        f.chmod(0o444)  # git writes its objects read-only; Windows will not delete such a file


scenario(Scenario(
    name="tidy_link_escape",
    suite="tidy",
    about="a path that reaches outside the roots through a link or junction is refused; read-only git objects go",
    setup=_setup_escape,
    steps=[
        Step(["tidy", "{tmp}/crew/tmp/door/node_modules"], env=NO_TARES),
        Step(["tidy", "{tmp}/crew/tmp/scratch"], env=NO_TARES),
    ],
    checks=[
        ("a path through a link out of the roots is refused",
         lambda r: r.code(0) != 0 and "outside the crew" in r.out(0) and _kept("outside/node_modules/keep")(r)),
        ("a folder of read-only git objects is removed",
         lambda r: r.code(1) == 0 and _gone("crew/tmp/scratch")(r)),
    ],
    covers=[],
))

# launch_*: building and starting a station (write_settings, launch, boot_prompt, grant_rules,
# compact_window). Not a command yet, so each runs through tests/factory/launch_support.py, which calls
# the function of the bash engine (sourced from a copy of the kit) or of tares.crew.launch, on a fake
# Tares of its own. The assertions are those of crew_test.sh, read off the files the station is given.

LAUNCH_DRIVER = "launch_support.py"
LAUNCH_GRANT = '- "the releaser may restart the staging database" (2026-01-02)\n'


def lstep(op: str, *args: str, world: dict | None = None, setup: dict | None = None, stdin: str = "",
          env: dict | None = None) -> Step:
    """One call of the driver: the function, its arguments, the fake Tares and the crew home it runs on."""
    e = dict(env or {})
    if world is not None:
        e["LAUNCH_WORLD"] = json.dumps(world)
    if setup is not None:
        e["LAUNCH_SETUP"] = json.dumps(setup)
    return Step([op, *args], env=e, stdin=stdin)


def lworld(autonomy: str = "L4-ship", projects: list | None = None, grants: str = "", project_grants: dict | None = None,
           **settings) -> dict:
    return {"settings": {"autonomy": autonomy, "releaser": True, "release_profile": "kind", "challenger": False,
                         "builders_max": 3, **settings},
            "projects": projects or [], "grants": {"all": grants, "projects": project_grants or {}}}


def station_json(r: "Run", station: str) -> dict:
    return json.loads((r.crew / "stations" / f"{station}.settings.json").read_text(encoding="utf-8"))


def allow_of(r: "Run", station: str) -> list:
    return station_json(r, station)["autoMode"]["allow"]


def fwd(path) -> str:
    """A path with forward slashes: how the Python engine writes it into a settings file on Windows."""
    return str(path).replace("\\", "/")


def tmp_path(r: "Run", *parts: str) -> str:
    return fwd(r.tmp.joinpath(*parts))


def relayed(allow: list) -> bool:
    return any(re.search(r"relayed .*yes", a) for a in allow)


def per_role(label: str, fn) -> list:
    """The checks of a crew_test.sh loop over `for r in orchestrator reviewer releaser`: one label (with its
    literal `$r`), true when fn(run, role) holds for every role."""
    return [(label, lambda r, fn=fn: all(fn(r, role) for role in ("orchestrator", "reviewer", "releaser")))]


_ROLE_STEPS = [lstep("write_settings", f"crew-{role}", role, "", "", "",
                     world=lworld("L3-review", grants=LAUNCH_GRANT)) for role in ("orchestrator", "reviewer", "releaser")]

scenario(Scenario(
    name="launch_settings_crew",
    suite="crew",
    about="the settings file of the orchestrator, reviewer and releaser (env, plugin, permissions, grants)",
    driver=LAUNCH_DRIVER,
    steps=_ROLE_STEPS,
    checks=[
        *per_role("crew-$r env FACTORY_STATION", lambda r, role: station_json(r, f"crew-{role}")["env"]["FACTORY_STATION"] == f"crew-{role}"),
        *per_role("crew-$r env FACTORY_ROLE", lambda r, role: station_json(r, f"crew-{role}")["env"]["FACTORY_ROLE"] == role),
        *per_role("crew-$r env FACTORY_PARENT empty", lambda r, role: station_json(r, f"crew-{role}")["env"]["FACTORY_PARENT"] == ""),
        *per_role("crew-$r has no TARES_PROJECT", lambda r, role: not station_json(r, f"crew-{role}")["env"].get("TARES_PROJECT")),
        *per_role("crew-$r is told the shared MCP endpoint",
                  lambda r, role: station_json(r, f"crew-{role}")["env"]["TARES_FACTORY_MCP_URL"] == "http://127.0.0.1:8788/mcp"),
        ("no Rius, no comms", lambda r: not re.search("rius|comms", json.dumps(station_json(r, "crew-reviewer")), re.I)),
        ("stations turn the plugin on, whatever the person's settings say",
         lambda r: station_json(r, "crew-reviewer")["enabledPlugins"]["tares@inline"] is True),
        ("orchestrator may spawn",
         lambda r: "Bash(factory spawn *)" in station_json(r, "crew-orchestrator")["permissions"]["allow"]),
        ("nothing goes to production without the person",
         lambda r: any("needs" in x and "prod" in x for x in station_json(r, "crew-releaser")["autoMode"]["soft_deny"])
         and "L5" not in json.dumps(station_json(r, "crew-releaser"))),
        ("grant quoted in the allow rules",
         lambda r: any('granted this on 2026-01-02 for all projects, in their own words: '
                       '"the releaser may restart the staging database". Acting' in x for x in allow_of(r, "crew-releaser"))),
    ],
    covers=["crew-$r env FACTORY_STATION", "crew-$r env FACTORY_ROLE", "crew-$r env FACTORY_PARENT empty",
            "crew-$r has no TARES_PROJECT", "crew-$r is told the shared MCP endpoint", "no Rius, no comms",
            "stations turn the plugin on, whatever the person's settings say", "orchestrator may spawn",
            "nothing goes to production without the person", "grant quoted in the allow rules"],
))

_LINKS = {"id": "uc_links", "name": "Links App", "repos": [{"path": "{TMP}/links"}]}
scenario(Scenario(
    name="launch_settings_builder",
    suite="crew",
    about="a builder in a repo of its project, at L3 (settings and the project's id in stations.json)",
    driver=LAUNCH_DRIVER,
    steps=[lstep("write_settings", "links-app-build-m1", "builder", "crew-orchestrator", "Links App", "{TMP}/links",
                 world=lworld("L3-review", projects=[_LINKS]))],
    checks=[
        ("builder TARES_PROJECT is the project id, the name beside it",
         lambda r: station_json(r, "links-app-build-m1")["env"]["TARES_PROJECT"] == "uc_links"
         and station_json(r, "links-app-build-m1")["env"]["TARES_PROJECT_NAME"] == "Links App"),
        ("builder FACTORY_PARENT", lambda r: station_json(r, "links-app-build-m1")["env"]["FACTORY_PARENT"] == "crew-orchestrator"),
        ("builder FACTORY_REPO", lambda r: station_json(r, "links-app-build-m1")["env"]["FACTORY_REPO"] == tmp_path(r, "links")),
        ("builder worktrees in its repo",
         lambda r: station_json(r, "links-app-build-m1")["env"]["FACTORY_WORKTREES"] == tmp_path(r, "links") + "/.factory/worktrees"),
        ("L3: builder waits for the relayed yes", lambda r: relayed(allow_of(r, "links-app-build-m1"))),
        ("L3: the no-CI clause holds at L3 too",
         lambda r: any(re.search(r"relayed .*yes", a) and "no CI checks configured" in a for a in allow_of(r, "links-app-build-m1"))),
        ("L3: no pre-authorized merge",
         lambda r: not any("pre-authorized at level" in a for a in allow_of(r, "links-app-build-m1"))),
        ("stations leave cmux hooks on, so its permission alerts stay",
         lambda r: "CMUX_CLAUDE_HOOKS_DISABLED" not in station_json(r, "links-app-build-m1")["env"]),
    ],
    covers=["builder TARES_PROJECT is the project id, the name beside it", "builder FACTORY_PARENT", "builder FACTORY_REPO",
            "builder worktrees in its repo", "L3: builder waits for the relayed yes",
            "L3: the no-CI clause holds at L3 too", "L3: no pre-authorized merge",
            "stations leave cmux hooks on, so its permission alerts stay"],
))

_CORE = {"id": "uc_core", "name": "strict", "repos": [{"path": "{TMP}/core"}]}
_LOOSE = {"id": "uc_loose", "name": "loose", "repos": [{"path": "{TMP}/core"}]}
scenario(Scenario(
    name="launch_settings_levels",
    suite="crew",
    about="a project handed over stricter keeps its level, one at the crew's level merges on the gate",
    driver=LAUNCH_DRIVER,
    steps=[lstep("write_settings", "strict-build-m1", "builder", "crew-orchestrator", "strict", "{TMP}/core",
                 world=lworld("L4-ship", projects=[_CORE, _LOOSE]),
                 setup={"stations": {"strict-build-m1": {"autonomy": "L3-review", "project_id": "uc_core"}}}),
           lstep("write_settings", "loose-build-m1", "builder", "crew-orchestrator", "loose", "{TMP}/core",
                 world=lworld("L4-ship", projects=[_CORE, _LOOSE]))],
    checks=[
        ("a project handed over stricter keeps it", lambda r: relayed(allow_of(r, "strict-build-m1"))),
        ("L4: the builder merges on the gate",
         lambda r: any("pre-authorized at level L4-ship" in a for a in allow_of(r, "loose-build-m1"))),
    ],
    covers=["a project handed over stricter keeps it", "L4: the builder merges on the gate"],
))

scenario(Scenario(
    name="launch_settings_lowered",
    suite="crew",
    about="a crew lowered after the hand-over takes the project's builder down with it",
    driver=LAUNCH_DRIVER,
    steps=[lstep("write_settings", "loose-build-m1", "builder", "crew-orchestrator", "loose", "{TMP}/core",
                 world=lworld("L3-review", projects=[_LOOSE]),
                 setup={"stations": {"loose-build-m1": {"autonomy": "L4-ship", "project_id": "uc_loose"}}})],
    checks=[("a crew lowered later takes the project down with it", lambda r: relayed(allow_of(r, "loose-build-m1")))],
    covers=["a crew lowered later takes the project down with it"],
))

_ARGUS = {"id": "uc_argus", "name": "argus", "repos": [{"path": "{TMP}/argus/core"}, {"path": "{TMP}/argus/charts"}]}
scenario(Scenario(
    name="launch_settings_multi",
    suite="crew",
    about="one project with two repos: each builder is scoped to its repo, the reviewer sees both",
    driver=LAUNCH_DRIVER,
    steps=[lstep("write_settings", "argus-build-m1-charts", "builder", "crew-orchestrator", "argus", "{TMP}/argus/charts",
                 world=lworld("L4-ship", projects=[_LINKS, _ARGUS])),
           lstep("write_settings", "crew-reviewer", "reviewer", "", "", "", world=lworld("L4-ship", projects=[_LINKS, _ARGUS]))],
    checks=[
        ("multi: each builder runs in its repo",
         lambda r: station_json(r, "argus-build-m1-charts")["env"]["FACTORY_REPO"] == tmp_path(r, "argus", "charts")),
        ("multi: a builder may push and merge only in its own repo",
         lambda r: (lambda rules: len(rules) == 2 and all(tmp_path(r, "argus", "charts") in x and tmp_path(r, "argus", "core") not in x
                                                           for x in rules))(
             [x for x in allow_of(r, "argus-build-m1-charts") if re.match(r"^(Pushing|Merging)", x)])),
        ("multi: a builder's worktrees stay within its project",
         lambda r: "repositories of project argus: " in allow_of(r, "argus-build-m1-charts")[1]
         and tmp_path(r, "argus", "core") in allow_of(r, "argus-build-m1-charts")[1]
         and tmp_path(r, "links") not in allow_of(r, "argus-build-m1-charts")[1]),
        ("multi: the reviewer's grant names every repo",
         lambda r: any(tmp_path(r, "argus", "core") in x and tmp_path(r, "argus", "charts") in x and "factory/review" in x
                       for x in allow_of(r, "crew-reviewer"))),
    ],
    covers=["multi: each builder runs in its repo", "multi: a builder may push and merge only in its own repo",
            "multi: a builder's worktrees stay within its project", "multi: the reviewer's grant names every repo"],
))


def bg_calls(r: "Run") -> list:
    return [c for c in r.calls() if c["args"] and c["args"][0] == "--bg"]


def arg_after(args: list, flag: str):
    return args[args.index(flag) + 1] if flag in args else None


_UP_WORLD = lworld("L3-review", grants=LAUNCH_GRANT)
_UP = [lstep("launch", f"crew-{role}", role, "", "", "", "{TMP}/crew", model, "high", f"Boot as the {role} station", world=_UP_WORLD)
       for role, model in (("orchestrator", "opus"), ("reviewer", "sonnet"), ("releaser", "sonnet"))]
scenario(Scenario(
    name="launch_argv",
    suite="crew",
    about="the claude command line of the three crew stations, and what stations.json records for them",
    driver=LAUNCH_DRIVER,
    steps=_UP,
    checks=[
        *per_role("crew-$r recorded", lambda r, role: json.loads((r.crew / "stations.json").read_text(encoding="utf-8"))[f"crew-{role}"]["status"] == "active"),
        *per_role("crew-$r runs in the crew home",
                  lambda r, role: json.loads((r.crew / "stations.json").read_text(encoding="utf-8"))[f"crew-{role}"]["cwd"].replace("\\", "/") == fwd(r.crew)),
        ("orchestrator started first", lambda r: bg_calls(r)[0]["args"][2] == "crew-orchestrator"),
        ("stations load this plugin",
         lambda r: {arg_after(c["args"], "--plugin-dir") for c in bg_calls(r)} == {str((r.tmp / "kit").resolve())}),
        ("stations launch with a 200k autocompact window",
         lambda r: arg_after(next(c for c in bg_calls(r) if c["args"][2] == "crew-orchestrator")["args"], "--autocompact") == "200000"),
        ("launched in the folder asked for, prompt last, model and effort given",
         lambda r: all(c["cwd"] == str(r.crew) and c["args"][-1].startswith("Boot as the ") and arg_after(c["args"], "--effort") == "high"
                       for c in bg_calls(r)) and arg_after(bg_calls(r)[1]["args"], "--model") == "sonnet"),
        ("the grants it started with are kept for the watch",
         lambda r: (r.crew / "stations" / "crew-releaser.grants").is_file()),
    ],
    covers=["crew-$r recorded", "crew-$r runs in the crew home", "orchestrator started first", "stations load this plugin",
            "stations launch with a 200k autocompact window"],
))

scenario(Scenario(
    name="launch_resume",
    suite="crew",
    about="a station restarted from its session: --resume with the id, the prompt still last",
    driver=LAUNCH_DRIVER,
    steps=[lstep("launch", "crew-reviewer", "reviewer", "", "", "", "{TMP}/crew", "sonnet", "high", "carry on", "sess-123",
                 world=lworld("L3-review"))],
    checks=[("--resume carries the session id and the prompt follows it",
             lambda r: arg_after(bg_calls(r)[0]["args"], "--resume") == "sess-123" and bg_calls(r)[0]["args"][-1] == "carry on")],
    covers=[],
))

scenario(Scenario(
    name="launch_fail",
    suite="crew",
    about="a station is refused (Haiku, an unreadable level) or does not start: said plainly, nothing recorded",
    driver=LAUNCH_DRIVER,
    steps=[lstep("launch", "crew-reviewer", "reviewer", "", "", "", "{TMP}/crew", "haiku", "high", "x", world=lworld("L3-review")),
           lstep("launch", "crew-reviewer", "reviewer", "", "", "", "{TMP}/crew", "sonnet", "high", "x", world=lworld("L9-mad")),
           lstep("launch", "crew-reviewer", "reviewer", "", "", "", "{TMP}/crew", "sonnet", "high", "x", world=lworld("L3-review"),
                 env={"FAKE_CLAUDE_FAIL_FRESH": "1"})],
    checks=[("the three refusals each exit 1 and say why",
             lambda r: [r.code(i) for i in range(3)] == [1, 1, 1] and "Auto mode does not run on Haiku" in r.out(0)
             and "no valid crew autonomy" in r.out(1) and "could not start crew-reviewer" in r.out(2)),
            ("nothing is recorded for a station that did not start",
             lambda r: "crew-reviewer" not in json.loads((r.crew / "stations.json").read_text(encoding="utf-8")))],
    covers=[],
))

scenario(Scenario(
    name="launch_boot",
    suite="crew",
    about="the boot prompt of a station, with and without the idle wake note and a task",
    driver=LAUNCH_DRIVER,
    steps=[lstep("boot_prompt", "orchestrator", world=lworld("L3-review")),
           lstep("boot_prompt", "reviewer", world=lworld("L3-review", idle_wake=True)),
           lstep("boot_prompt", "builder", "work T1 and T2", world=lworld("L3-review", idle_wake=False)),
           lstep("idle_wake_note", world=lworld("L3-review"))],
    checks=[("the prompts name the role, the READY message and the task",
             lambda r: "follow the Boot section" in r.out(0) and "send crew-orchestrator READY" in r.out(1)
             and "Your task:\nwork T1 and T2" in r.out(2))],
    covers=[],
))

scenario(Scenario(
    name="launch_grants_compact",
    suite="crew",
    about="grant lines into allow rules (all-project and project, dated, undated, noise), and the autocompact window",
    driver=LAUNCH_DRIVER,
    steps=[lstep("grant_rules", "Links App", "Pat", stdin=(
               '- "restart staging" (2026-01-02)\n@project - "push to main on Fridays" (2026-02-03)\n'
               '* 2026-03-04: deploy the docs\n2026-03-05 - tidy the cache\n- no date at all\nnot a grant\n- ""\n'
               '- "dropped, the last line has no newline" (2026-04-05)'),
                 world=lworld()),
           lstep("compact_window", world=lworld(), setup={"crew": {"limits": {"autocompact_tokens": 100000}}}),
           lstep("compact_window", world=lworld(), setup={"crew": {"limits": {"autocompact_tokens": "auto"}}}),
           lstep("compact_window", world=lworld(), setup={"crew": {"limits": {"autocompact_tokens": "oops"}}})],
    checks=[("grants read as bash reads them", lambda r: r.out(0).count("granted this") == 6 and "(undated)" in r.out(0)
             and "for project Links App" in r.out(0) and "dropped" not in r.out(0)),
            ("a window under 150k is raised and said", lambda r: "stations use 150000" in r.out(1) and r.results[1]["stdout"].strip() == "150000"),
            ("auto stays auto, junk gives 200000", lambda r: r.out(2).strip() == "auto" and r.out(3).strip() == "200000")],
    covers=[],
))

scenario(Scenario(
    name="launch_window",
    suite="crew",
    about="a window under 150k in crew.json: the station starts with --autocompact 150000, and it is said",
    driver=LAUNCH_DRIVER,
    steps=[lstep("launch", "crew-orchestrator", "orchestrator", "", "", "", "{TMP}/crew", "opus", "high", "go",
                 world=lworld("L3-review"), setup={"crew": {"limits": {"autocompact_tokens": 100000}}})],
    checks=[("window under 150k raised",
             lambda r: arg_after(bg_calls(r)[0]["args"], "--autocompact") == "150000" and "stations use 150000" in r.out(0))],
    covers=["window under 150k raised"],
))


# ---------------------------------------------------------------------------------------------
# The views (TR-957): roster, status, attach and logs read the crew's stations and the sessions the fake
# Claude Code lists. The seeds are written straight into the throwaway crew, so the scenarios run on Windows.
# crewname_test.sh (the roster names its crew) and crew_test.sh are the bash suites they come from.

def _row(state: str, sid: str, name: str, **extra) -> dict:
    live = state != "stopped"
    row = {"id": sid, "name": name, "cwd": "/x", "state": {"working": "working", "idle": "working", "waiting": "blocked",
                                                             "stopped": "stopped"}[state]}
    if state in ("working", "idle"):
        row["status"] = "busy" if state == "working" else "idle"
    if state == "waiting":
        row["status"] = "waiting"
    if live:
        row["pid"] = 40000 + int(sid[-4:], 16)
    row.update(extra)
    return row


def _setup_views(tmp: Path, env: dict) -> None:
    crew = tmp / "crew"
    crew.mkdir(parents=True, exist_ok=True)
    shutil.copy(ROOT / "claude-plugin" / "templates" / "crew.json", crew / "crew.json")
    base = {"status": "active", "repo": "", "cwd": "", "model": "opus", "effort": "high"}
    stations = {
        "crew-orchestrator": {**base, "role": "orchestrator", "project": "", "id": "aaaa0001"},
        "crew-reviewer": {**base, "role": "reviewer", "project": "", "id": "bbbb0002"},
        "shop-build-m1": {**base, "role": "builder", "project": "shop", "id": "cccc0003"},
        "shop-build-m2": {**base, "role": "builder", "project": "shop", "id": "dddd0004"},
        "old-helper": {**base, "role": "helper", "project": "", "id": "eeee0005", "status": "stopped"},
    }
    (crew / "stations.json").write_text(json.dumps(stations, indent=2) + "\n", encoding="utf-8")
    agents = [_row("working", "aaaa0001", "crew-orchestrator", version="2.1.310"),
              _row("idle", "bbbb0002", "crew-reviewer", version="2.1.310"),
              _row("waiting", "cccc0003", "shop-build-m1", version="2.1.100")]  # shop-build-m2: no session
    state = tmp / "claude"
    state.mkdir(parents=True, exist_ok=True)
    (state / "agents.json").write_text(json.dumps(agents, indent=2) + "\n", encoding="utf-8")
    (state / "version").write_text("2.1.310\n", encoding="utf-8")


def _attach_calls(r: Run) -> list:
    return [c["args"] for c in r.calls() if c["args"][:1] in (["attach"], ["logs"])]


scenario(Scenario(
    name="views_roster",
    suite="crewname",
    about="roster names the crew, lists the active stations with their turn and Claude Code, and flags an older one",
    setup=_setup_views,
    steps=[Step(["roster"], env=NO_TARES), Step(["roster"], env={**NO_TARES, "FACTORY_CREW_NAME": "ops"})],
    checks=[
        ("default crew: roster names it", lambda r: r.out(0).splitlines()[0] == "crew: crew (orchestrator: crew-orchestrator)"),
        ("roster lists a working orchestrator", lambda r: "crew-orchestrator\torchestrator\t\tworking\t2.1.310" in r.out(0)),
        ("a station with no session is gone", lambda r: "shop-build-m2\tbuilder\tshop\tgone\t" in r.out(0)),
        ("a stopped station is not listed", lambda r: "old-helper" not in r.out(0)),
        ("an older Claude Code is flagged", lambda r: "blocked\t2.1.100 (older than 2.1.310)" in r.out(0)),
        ("FACTORY_CREW_NAME=ops: roster names ops-orchestrator",
         lambda r: r.out(1).splitlines()[0] == "crew: ops (orchestrator: ops-orchestrator)"),
    ],
    covers=["default crew: roster names it"],
))

scenario(Scenario(
    name="views_status",
    suite="crew",
    about="status without a Tares says so plainly; the desk too",
    setup=_setup_views,
    steps=[Step(["status"], env=NO_TARES), Step(["desk"], env=NO_TARES), Step(["crew", "status"], env=NO_TARES)],
    checks=[
        ("status without Tares stops plainly", lambda r: r.code(0) == 1 and "no Tares answers at http://127.0.0.1:9" in r.out(0)),
        ("desk without Tares stops plainly", lambda r: r.code(1) == 1 and "no Tares answers at http://127.0.0.1:9" in r.out(1)),
        ("crew status is status", lambda r: r.code(2) == 1 and r.out(2) == r.out(0)),
    ],
    covers=[],
))

scenario(Scenario(
    name="views_attach_logs",
    suite="crew",
    about="attach and logs open a station's session through Claude Code; an unknown station or none is refused",
    setup=_setup_views,
    steps=[Step(["attach", "reviewer"], env=NO_TARES), Step(["logs", "shop-build-m1"], env=NO_TARES),
           Step(["attach", "nobody"], env=NO_TARES), Step(["crew", "logs", "nobody"], env=NO_TARES)],
    checks=[
        ("attach reviewer opens its session", lambda r: ["attach", "bbbb0002"] in _attach_calls(r)),
        ("logs shop-build-m1 follows its session", lambda r: ["logs", "cccc0003"] in _attach_calls(r)),
        ("an unknown station is refused", lambda r: r.code(2) == 1 and "unknown station nobody" in r.out(2)),
        ("crew logs refuses it the same way", lambda r: r.code(3) == 1 and "unknown station nobody" in r.out(3)),
    ],
    covers=[],
))

# doctor: crew_test.sh, tares_test.sh (the lines that read "doctor"), freeze_test.sh, idle_wake_test.sh and
# claude_update_test.sh each ask doctor a question. Here doctor runs against a fake Tares (fake_tares.py), a
# fake `gh` and a fake `tares` on PATH. The bash doctor does not make the checks that are new in Python
# (NEW_LABELS in tares/crew/doctor.py), and TR-970 ports its cmux and language-server checks: those lines
# are set aside from the comparison, on both sides, with `drop`.

sys.path.insert(0, str(HERE))
import fake_tares  # noqa: E402

NEW_CHECKS = (r"^  (ok|warn|FIX)\s+(engine|tares on PATH|tares version|login key|crew key file|git|Git for Windows)\b",)
TR970 = (r"cmux", r"language server")
DOCTOR_DROP = NEW_CHECKS + TR970
LSP_CALLS = (["plugin", "list"],)   # the language-server check asks Claude Code for its plugins (TR-970)
# When Tares refuses the key in the environment, the bash doctor stops silently at the crew settings (its
# `crew_settings` dies inside a pipe). The Python doctor says the settings are unanswered and goes on to the
# checks after them. That is the one place the two differ on purpose; a scenario sets those lines aside.
BASH_STOPS_EARLY = (r"^  warn  crew settings not answered yet", r"^  ok    gh signed in", r"^fix the items marked FIX$")


def _fake_tool(tmp: Path, name: str) -> None:
    make_launcher(tmp / "bin" / name, [name], script=HERE / "fake_tares.py")


def _doctor_world(world: "fake_tares.World", repos: list = (), trusted: list = ()):
    """A setup hook: start the fake Tares, put a fake gh and tares on PATH, hand the crew a key file, and
    have Claude Code trust the crew home (and the folders named in `trusted`)."""
    def setup(tmp: Path, env: dict) -> None:
        _fake_tool(tmp, "gh")
        _fake_tool(tmp, "tares")
        for r in repos:
            (tmp / r).mkdir(parents=True, exist_ok=True)
        folders = [str(tmp / "crew")] + [str(tmp / t) for t in trusted]
        (tmp / "home").mkdir(parents=True, exist_ok=True)
        (tmp / "home" / ".claude.json").write_text(
            json.dumps({"projects": {f: {"hasTrustDialogAccepted": True} for f in folders}}), encoding="utf-8")
        _srv, base = fake_tares.serve(world)
        env["TARESD_URL"] = base
        env["TARES_FACTORY_MCP_URL"] = base + "/mcp"
        crew = tmp / "crew"
        crew.mkdir(parents=True, exist_ok=True)
        if world.token:
            f = crew / ".tares-token"
            f.write_text(world.token, encoding="utf-8")
            f.chmod(0o600)
        env["PYTHON_KEYRING_BACKEND"] = "keyring.backends.fail.Keyring"  # never the person's keychain
    return setup


scenario(Scenario(
    name="doctor_notares",
    suite="crew",
    about="doctor without a Tares says how to start one",
    setup=lambda tmp, env: (_fake_tool(tmp, "gh"), _fake_tool(tmp, "tares"),
                            env.update(PYTHON_KEYRING_BACKEND="keyring.backends.fail.Keyring")),
    steps=[Step(["doctor"], env={"TARESD_URL": "http://127.0.0.1:9"}, drop=DOCTOR_DROP)],
    checks=[("doctor without Tares says how to start one",
             lambda r: "FIX   no Tares answers at http://127.0.0.1:9 (from the environment): start one with `tares up`, "
                       "connect to your Tares Cloud workspace with `tares login`" in r.out(0) and r.code(0) == 1)],
    covers=["doctor without Tares says how to start one"],
))

_ok_world = fake_tares.World()
scenario(Scenario(
    name="doctor_tares",
    suite="crew",
    about="doctor with a Tares: it answers, lets the crew in, and the MCP endpoint answers",
    setup=_doctor_world(_ok_world),
    steps=[Step(["doctor"], drop=DOCTOR_DROP)],
    checks=[("doctor: Tares answers",
             lambda r: re.search(r"  ok    Tares answers at http://127\.0\.0\.1:\d+", r.out(0)) is not None
             and "ok    Tares lets this crew in" in r.out(0)),
            ("doctor: the MCP endpoint answers",
             lambda r: re.search(r"^  ok    MCP endpoint answers at http://127\.0\.0\.1:\d+/mcp$", r.out(0), re.M) is not None),
            ("doctor: nothing about tares-mcp on PATH", lambda r: "tares-mcp" not in r.out(0)),
            ("doctor: no Rius", lambda r: "rius" not in r.out(0).lower())],
    covers=["doctor: Tares answers", "doctor: the MCP endpoint answers",
            "doctor: nothing about tares-mcp on PATH", "doctor: no Rius"],
))


def _write(tmp: Path, rel: str, text: str) -> None:
    f = tmp / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text, encoding="utf-8", newline="\n")


def _crew_json(tmp: Path) -> None:
    _write(tmp, "crew/crew.json", json.dumps({"stations": {r: {"model": "sonnet"} for r in
                                                           ("orchestrator", "reviewer", "releaser", "builder")}}))


def _kit_version() -> str:
    return json.loads((ROOT / "claude-plugin" / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))["version"]


def _installed(tmp: Path, version: str) -> None:
    _write(tmp, "home/.claude/plugins/installed_plugins.json", json.dumps(
        {"version": 2, "plugins": {"tares@tares": [{"scope": "project", "projectPath": "/w/main", "version": version}]}}))


def _remove_installed(tmp: Path) -> None:
    (tmp / "home" / ".claude" / "plugins" / "installed_plugins.json").unlink()


scenario(Scenario(
    name="doctor_plugin_clash",
    suite="crew",
    about="doctor names an installed tares plugin of another version, and is quiet about the same version",
    setup=_doctor_world(fake_tares.World()),
    steps=[Step(["doctor"], before=lambda tmp: _installed(tmp, "0.2.1"), drop=DOCTOR_DROP),
           Step(["doctor"], before=lambda tmp: _installed(tmp, _kit_version()), drop=DOCTOR_DROP)],
    checks=[("doctor says the same",
             lambda r: "warn  another version of the tares plugin is installed from a marketplace "
                       "(tares@tares 0.2.1 project (/w/main))" in r.out(0)),
            ("the same version installed is fine", lambda r: "another version of the tares plugin" not in r.out(1))],
    covers=["doctor says the same", "the same version installed is fine"],
))


def _multi(tmp: Path, env: dict) -> None:
    core, charts = tmp / "core", tmp / "ro-charts"
    projects = [{"name": "argus", "repos": [{"path": str(core)}, {"path": str(charts)}]}]
    _doctor_world(fake_tares.World(projects=projects), repos=["core", "ro-charts"], trusted=["core"])(tmp, env)
    _crew_json(tmp)


scenario(Scenario(
    name="doctor_projects",
    suite="crew",
    ignore_calls=LSP_CALLS,
    about="doctor checks each repo of a project: Claude Code's trust and GitHub write access",
    setup=_multi,
    steps=[Step(["doctor"], drop=DOCTOR_DROP)],
    checks=[("multi doctor: trust per repo",
             lambda r: re.search(r"ok .*project argus: .+[/\\]core trusted", r.out(0)) is not None
             and re.search(r"FIX .*project argus: Claude Code has not trusted .+[/\\]ro-charts", r.out(0)) is not None),
            ("multi doctor: GitHub write access per repo",
             lambda r: re.search(r"ok .*[/\\]core is o/core on GitHub \(WRITE\)", r.out(0)) is not None
             and "FIX   project argus: gh has READ on o/ro-charts" in r.out(0))],
    covers=["multi doctor: trust per repo", "multi doctor: GitHub write access per repo"],
))


def _no_mcp(tmp: Path, env: dict) -> None:
    w = fake_tares.World()
    w.mcp = False
    _doctor_world(w)(tmp, env)


scenario(Scenario(
    name="doctor_nomcp",
    suite="crew",
    about="doctor without an MCP server says how to start one; taresd itself is not an MCP server",
    setup=_no_mcp,
    steps=[Step(["doctor"], drop=DOCTOR_DROP)],
    checks=[("doctor without an MCP server says how to start one",
             lambda r: re.search(r"FIX   no MCP server answers at http://127\.0\.0\.1:\d+/mcp; start one with "
                                 r"tares mcp --transport streamable-http", r.out(0)) is not None)],
    covers=["doctor without an MCP server says how to start one"],
))


def _keys(tmp: Path, env: dict) -> None:
    _doctor_world(fake_tares.World(token="stub-token"))(tmp, env)
    _crew_json(tmp)
    env["TARES_AUTH_TOKEN"] = "nvf_dead0000_revoked"


def _drop_key_file(tmp: Path) -> None:
    f = tmp / "crew" / ".tares-token"
    f.unlink()
    f.write_text("", encoding="utf-8")


scenario(Scenario(
    name="doctor_keys",
    suite="tares",
    about="doctor: a key Tares refuses never replaces the crew's; a crew without a key is flagged; no key is printed",
    setup=_keys,
    steps=[Step(["doctor"], after=lambda tmp: (tmp / "crew" / ".tares-token").read_text(encoding="utf-8"),
                drop=DOCTOR_DROP + BASH_STOPS_EARLY),
           Step(["doctor"], before=_drop_key_file, env={"TARES_AUTH_TOKEN": ""}, drop=DOCTOR_DROP + BASH_STOPS_EARLY)],
    checks=[("doctor names the stations' key and finds it good",
             lambda r: "ok    stations use a token with no key id from " in r.out(0)
             and ", and Tares accepts it" in r.out(0)),
            ("a key Tares refuses never replaces the crew's",
             lambda r: "refuses key dead0000 from the environment; the crew keeps the key it has" in r.out(0)
             and r.snap(0) == "stub-token"),
            ("no key value in doctor's output",
             lambda r: "nvf_dead0000_revoked" not in r.out(0) and "stub-token" not in r.out(0)),
            ("doctor flags a crew without a key",
             lambda r: "FIX   the stations have no key in " in r.out(1) and ": run tares login" in r.out(1))],
    covers=["doctor names the stations' key and finds it good", "a key Tares refuses never replaces the crew's",
            "no key value in doctor's output", "doctor flags a crew without a key"],
))


def _frozen(tmp: Path, env: dict) -> None:
    w = fake_tares.World(settings={"kit": {"frozen": {"at": "2026-01-02T03:04:05Z", "by": "ada", "version": "0.0.1"}}})
    _doctor_world(w)(tmp, env)
    _crew_json(tmp)


scenario(Scenario(
    name="doctor_kit",
    suite="freeze",
    about="doctor: the kit follows the checkout, or is frozen (copy missing, or older than the checkout)",
    setup=lambda tmp, env: (_doctor_world(fake_tares.World())(tmp, env), _crew_json(tmp)),
    steps=[Step(["doctor"], drop=DOCTOR_DROP)],
    checks=[("doctor says the kit follows the checkout",
             lambda r: "ok    the kit follows the plugin checkout (not frozen)" in r.out(0))],
    covers=["doctor says the kit follows the checkout"],
))

scenario(Scenario(
    name="doctor_frozen",
    suite="freeze",
    about="doctor: a frozen kit whose copy is gone, then one whose copy is older than the checkout",
    setup=_frozen,
    steps=[Step(["doctor"], drop=DOCTOR_DROP),
           Step(["doctor"], before=lambda tmp: (tmp / "crew" / "frozen-kit").mkdir(parents=True), drop=DOCTOR_DROP)],
    checks=[("the freeze with no copy is a FIX", lambda r: "FIX   the kit is frozen since 2026-01-02T03:04:05Z by ada, but its copy" in r.out(0)),
            ("doctor warns that a newer kit is not followed",
             lambda r: "warn  the kit is frozen since 2026-01-02T03:04:05Z by ada, and the checkout has a newer kit "
                       "that no station moves onto" in r.out(1))],
    covers=["doctor warns that a newer kit is not followed"],
))


def _settings(**kw):
    def setup(tmp: Path, env: dict) -> None:
        _doctor_world(fake_tares.World(settings=kw))(tmp, env)
        _crew_json(tmp)
    return setup


scenario(Scenario(
    name="doctor_idle_wake",
    suite="idle_wake",
    about="doctor: idle_wake off says nothing, on without a watch warns",
    setup=_settings(),
    steps=[Step(["doctor"], drop=DOCTOR_DROP)],
    checks=[("off: status and doctor have no wake line", lambda r: "idle_wake is on" not in r.out(0))],
    covers=["off: status and doctor have no wake line"],
))

scenario(Scenario(
    name="doctor_idle_wake_on",
    suite="idle_wake",
    about="doctor: idle_wake on without a running watch warns",
    setup=_settings(idle_wake=True),
    steps=[Step(["doctor"], drop=DOCTOR_DROP)],
    checks=[("on: doctor warns that no watch is running",
             lambda r: "warn  idle_wake is on but the watch is not running" in r.out(0)
             and "ok    idle_wake is on" not in r.out(0))],
    covers=[],
))

scenario(Scenario(
    name="doctor_claude_update",
    suite="claude_update",
    about="doctor tells when the watch does not move stations onto a new Claude Code",
    setup=_settings(claude_update=False),
    steps=[Step(["doctor"], drop=DOCTOR_DROP)],
    checks=[("doctor tells the watch is off", lambda r: "claude_update" in r.out(0).lower())],
    covers=["doctor tells the watch is off"],
))


# ---------------------------------------------------------------------------------------------
# A fake Tares. `projects`, `finish`, `context` and `plan` talk to Tares, and parity has none: a scenario
# with `tares=` gets a small local server that answers the few routes these commands use, with the
# answers the scenario names, and writes down what it was asked (the route and whether a key came with it,
# never the key). TARESD_URL names it for both engines.

class FakeTares:
    """routes: {"GET /api/crew/projects": (status, json document or text)}; unnamed routes answer 404."""

    def __init__(self, routes: dict):
        import http.server
        import threading
        self.routes = {"GET /health": (200, {"ok": True}), "GET /api/crew/settings": (200, {"settings": {}})}
        self.routes.update(routes)
        self.requests: list = []
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _answer(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n).decode("utf-8", "replace") if n else ""
                key = f"{self.command} {self.path}"
                fake.requests.append({"route": key, "auth": bool(self.headers.get("Authorization")), "body": body})
                status, doc = fake.routes.get(key, (404, {"detail": "not found"}))
                data = (doc if isinstance(doc, str) else json.dumps(doc)).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = do_PUT = _answer

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def asked(self, route: str) -> list:
        return [r for r in self.requests if r["route"] == route]


def routes_in(routes: dict, tmp: Path) -> dict:
    """The routes with "{tmp}" in an answer replaced by the scenario's folder (a path inside a document)."""
    folder = json.dumps(str(tmp))[1:-1]  # as it reads inside a JSON string
    return {k: (st, doc.replace("{tmp}", str(tmp)) if isinstance(doc, str)
                else json.loads(json.dumps(doc).replace("{tmp}", folder)))
            for k, (st, doc) in routes.items()}


def _crew_files(stations: dict):
    """A setup that gives the crew a crew.json and the stations.json named."""
    def setup(tmp: Path, env: dict) -> None:
        crew = tmp / "crew"
        crew.mkdir(parents=True, exist_ok=True)
        (crew / "crew.json").write_text('{"name": "crew"}\n', encoding="utf-8")
        (crew / "stations.json").write_text(json.dumps(stations), encoding="utf-8")
        (tmp / "work").mkdir(exist_ok=True)
    return setup


# ---------------------------------------------------------------------------------------------
# usage_test.sh: what the crew's machine reports. `factory context` reads a station's /context through
# a copy of its session that is never saved, `factory plan` reads /usage; both send the reading to Tares.
# The station and its session come from stations.json; Claude Code is the Python fake.

def _usage_stations(tmp: Path) -> dict:
    return {"crew-orchestrator": {"role": "orchestrator", "id": "deadbeef", "sessionId": "sess-orch-1",
                                  "cwd": str(tmp / "work")}}


def _setup_usage(tmp: Path, env: dict) -> None:
    _crew_files(_usage_stations(tmp))(tmp, env)


def _only_call(r: Run, needle: str) -> dict:
    calls = [c for c in r.calls() if needle in c["args"]]
    return calls[-1] if calls else {}


scenario(Scenario(
    name="usage_context",
    suite="usage",
    about="`context` reads a station through an unsaved copy of its session and sends the reading to Tares",
    tares={"POST /ingest/claude_code": (200, {"ok": True})},
    setup=_setup_usage,
    steps=[Step(["context", "orchestrator"]), Step(["context", "nobody"])],
    checks=[
        ("factory context reads the station",
         lambda r: r.code(0) == 0 and "crew-orchestrator: 95300 of 200000 tokens (48%), sent to Tares" in r.out(0)),
        ("through a copy that is never saved",
         lambda r: (lambda a: a[a.index("--resume") + 1] == "sess-orch-1" and "--fork-session" in a
                    and "--no-session-persistence" in a and "-p" in a)(_only_call(r, "/context").get("args", ["--resume", ""]))),
        ("in the station's folder, with its plugin",
         lambda r: _only_call(r, "/context").get("cwd") == str(r.tmp / "work")
         and "--plugin-dir" in _only_call(r, "/context").get("args", [])),
        ("with its settings, hooks and the status line off",
         lambda r: json.loads((r.state / "p-settings.json").read_text(encoding="utf-8")).get("disableAllHooks") is True),
        ("Tares has the reading",
         lambda r: len(r.tares.asked("POST /ingest/claude_code")) == 1
         and json.loads(r.tares.asked("POST /ingest/claude_code")[0]["body"]).get("type") == "session_context"),
        ("an unknown station: said plainly", lambda r: r.code(1) != 0 and "unknown station nobody" in r.out(1)),
    ],
    covers=["factory context reads the station", "through a copy that is never saved",
            "in the station's folder, with its plugin", "with its settings, hooks and the status line off",
            "Tares has the reading", "an unknown station: said plainly"],
))

scenario(Scenario(
    name="usage_plan",
    suite="usage",
    about="`plan` reads /usage with hooks off and nothing saved, sends the limits, and says plainly when there is no plan",
    tares={"POST /ingest/claude_code": (200, {"ok": True})},
    setup=_setup_usage,
    steps=[Step(["plan"]), Step(["plan"], env={"FAKE_CLAUDE_NO_PLAN": "1"})],
    checks=[
        ("factory plan reads and sends the limits", lambda r: r.code(0) == 0 and "session: 27% used" in r.out(0)),
        ("with hooks off, nothing saved",
         lambda r: "--no-session-persistence" in _only_call(r, "/usage").get("args", [])
         and json.loads((r.state / "p-settings.json").read_text(encoding="utf-8")).get("disableAllHooks") is True),
        ("Tares has /usage's limits",
         lambda r: json.loads(r.tares.asked("POST /ingest/claude_code")[0]["body"]).get("type") == "claude_plan"),
        ("no plan to read (an API key): said plainly", lambda r: r.code(1) != 0 and "Pro or Max" in r.out(1)),
    ],
    covers=["factory plan reads and sends the limits", "with hooks off, nothing saved",
            "Tares has /usage's limits", "no plan to read (an API key): said plainly"],
))


# ---------------------------------------------------------------------------------------------
# crew_test.sh and tares_test.sh: `projects` lists what was handed to the crew, `finish` is the person's.

PROJECTS = [
    {"id": "p1", "name": "Links App", "tickets_done": 1, "tickets_total": 2,
     "milestone": {"name": "Links", "done": 1, "total": 2},
     "repos": [{"name": "acme/links", "path": "/work/links"}]},
    {"id": "p2", "name": "shop", "tickets_done": 0, "tickets_total": 0, "milestone": None,
     "repos": [{"path": "/work/shop"}, {"name": "acme/shop-api", "path": "/work/shop-api"}]},
    {"id": "p3", "name": "A very long project name that is cut", "tickets_done": 3, "tickets_total": 3,
     "milestone": None, "repos": []},
]
BUILDERS = {
    "links-app-build-m1": {"role": "builder", "project": "Links App", "status": "active"},
    "links-app-build-m2": {"role": "builder", "project": "Links App", "status": "retired"},
    "shop-build-m1": {"role": "builder", "project": "shop", "status": "active"},
    "shop-build-m2": {"role": "builder", "project": "shop", "status": "active"},
    "crew-reviewer": {"role": "reviewer", "status": "active"},
}


def _setup_projects(tmp: Path, env: dict) -> None:
    _crew_files(BUILDERS)(tmp, env)


scenario(Scenario(
    name="projects_list",
    suite="crew",
    about="`projects` lists each handed-over project with its progress, builders and repos",
    tares={"GET /api/crew/projects": (200, {"projects": PROJECTS})},
    setup=_setup_projects,
    steps=[Step(["projects"])],
    checks=[
        ("projects lists each handed-over project with its builders and repos",
         lambda r: r.code(0) == 0
         and re.search(r"^Links App +1/2 +Links 1/2 +1 +acme/links$", r.out(0), re.M) is not None
         and re.search(r"^shop +0/0 +- +2 +/work/shop, acme/shop-api$", r.out(0), re.M) is not None
         and re.search(r"^A very long project name tha 3/3 +all done +0 *$", r.out(0), re.M) is not None),
    ],
    covers=["projects lists each handed-over project with its builders and repos"],
))

scenario(Scenario(
    name="projects_none",
    suite="tares",
    about="`projects` with none handed over says how to start one",
    tares={"GET /api/crew/projects": (200, {"projects": []})},
    setup=_setup_projects,
    steps=[Step(["projects"])],
    checks=[("no project: said plainly", lambda r: r.code(0) == 0 and "no projects handed to the crew yet" in r.out(0))],
    covers=[],
))

FINISH_OPEN = {"detail": "Links App still has open tickets: T1"}


def _finish_scenario(name: str, about: str, routes: dict, steps: list, checks: list, covers: list) -> None:
    scenario(Scenario(name=name, suite="crew", about=about, tares=routes, setup=_setup_projects,
                      steps=steps, checks=checks, covers=covers))


_finish_scenario(
    "projects_finish_open", "`finish` refuses a project with an open ticket and says which",
    {"GET /api/crew/projects": (200, {"projects": PROJECTS}), "POST /api/projects/p1/finish": (409, FINISH_OPEN)},
    [Step(["finish", "Links App", "all shipped"])],
    [("finish refuses a project with an open ticket",
      lambda r: r.code(0) != 0 and "still has open tickets: T1" in r.out(0)
      and json.loads(r.tares.asked("POST /api/projects/p1/finish")[0]["body"]) == {"words": "all shipped"})],
    ["finish refuses a project with an open ticket"])

_finish_scenario(
    "projects_finish_station", "a station cannot run `finish`",
    {"GET /api/crew/projects": (200, {"projects": PROJECTS})},
    [Step(["finish", "Links App"], env={"FACTORY_STATION": "crew-orchestrator"})],
    [("a station can't run finish",
      lambda r: r.code(0) != 0 and "only the person runs this" in r.out(0)
      and not r.tares.asked("POST /api/projects/p1/finish"))],
    ["a station can't run finish"])

_finish_scenario(
    "projects_finish_done", "the person finishes a project; the last one tells how to stop the crew",
    {"GET /api/crew/projects": (200, {"projects": PROJECTS}),
     "POST /api/projects/p1/finish": (200, {"open_projects": 0}),
     "POST /api/projects/p2/finish": (200, {"open_projects": 1})},
    [Step(["finish", "Links App", "all", "shipped"]), Step(["finish", "shop"]), Step(["finish", "nothing"])],
    [("the person finishes it",
      lambda r: r.code(0) == 0 and "✓ Links App is done." in r.out(0) and "crew down" in r.out(0)
      and r.code(1) == 0 and "crew down" not in r.out(1)),
     ("an unknown project lists the open ones",
      lambda r: r.code(2) != 0 and "nothing is not a project the crew holds (open projects: Links App, shop" in r.out(2)),
     ],
    ["the person finishes it"])

_finish_scenario(
    "projects_finish_down", "`finish` says plainly when Tares cannot be reached for the request",
    {"GET /api/crew/projects": (200, {"projects": PROJECTS}), "POST /api/projects/p1/finish": (500, "boom")},
    [Step(["finish", "Links App"])],
    [("a refusal without a detail shows the body", lambda r: r.code(0) != 0 and "Tares did not finish Links App: boom" in r.out(0))],
    [])


# ---------------------------------------------------------------------------------------------
# spec_test.sh and session_test.sh: the person's own Claude Code session. `spec` and `session` open Claude in
# the foreground in the folder they choose, so the scenarios read what the fake Claude Code was called with
# (its arguments, folder and environment) and what the commands asked of a fake Tares. The suites' checks
# that need a real Tares (fixtures, the session-start hook, the skill text) stay in the bash suites.

PLUGIN = str((ROOT / "claude-plugin").resolve())
MCP_ROUTE = {"POST /mcp": (200, {"result": {"serverInfo": {"name": "tares"}}})}
WITH_MCP = {"TARES_FACTORY_MCP_URL": "{tares}/mcp"}
SEEN_ENV = ("TARES_PROJECT", "TARES_PROJECT_NAME", "TARES_SESSION_NAME", "TARES_SESSION_TAKEOVER",
            "TARES_SESSION_KIND", "TARESD_URL", "TARES_FACTORY_URL", "TARES_FACTORY_MCP_URL",
            "TARES_FACTORY_CRED_FILE", "TARES_FACTORY_TOKEN_FILE", "FACTORY_ROLE", "PWD")


def _mkrepo(tmp: Path, rel: str) -> Path:
    p = tmp / rel
    p.mkdir(parents=True, exist_ok=True)
    _git(tmp, "init", "-q", cwd=p)
    (p / "README.md").write_text("x\n")
    _git(tmp, "add", ".", cwd=p)
    _git(tmp, "commit", "-q", "-m", "init", cwd=p)
    return p


def _claude_after(tmp: Path) -> dict:
    """What the fake Claude Code saw on its last call: how many calls so far, the settings file it was
    given, and the environment variables the commands set (a token is only noted as present)."""
    state = tmp / "claude"
    calls = []
    try:
        calls = [json.loads(x) for x in (state / "calls.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
    except OSError:
        pass
    seen: dict = {}
    keys: set = set()
    try:
        for line in (state / "last.env").read_text(encoding="utf-8").splitlines():
            k, _, v = line.partition("=")
            keys.add(k)
            if k in SEEN_ENV:
                seen[k] = v
    except OSError:
        pass
    settings = None
    if calls:
        a = calls[-1]["args"]
        if "--settings" in a:
            try:
                settings = json.loads(Path(a[a.index("--settings") + 1]).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                settings = "<unreadable>"
    return {"n": len(calls), "env": seen, "has_auth_token": "TARES_AUTH_TOKEN" in keys, "settings": settings}


def _the_call(r: Run, i: int = -1) -> dict:
    calls = r.calls()
    return calls[i] if calls else {"cwd": "", "args": [""] * 3}


def _flag_value(call: dict, flag: str):
    a = call["args"]
    return a[a.index(flag) + 1] if flag in a else None


def _plain_session(call: dict) -> bool:
    return "--bg" not in call["args"]


def _plugin_on(r: Run, i: int) -> bool:
    return (_flag_value(_the_call(r, i), "--plugin-dir") == PLUGIN
            and (r.snap(i) or {}).get("settings") == {"enabledPlugins": {"tares@inline": True}})


def _env_of(r: Run, i: int) -> dict:
    return (r.snap(i) or {}).get("env", {})


def _repo_clean(r: Run, rel: str) -> bool:
    p = subprocess.run(["git", "status", "--porcelain"], cwd=str(r.tmp / rel), capture_output=True, text=True)
    return p.stdout.strip() == "" and not (r.tmp / rel / ".factory").exists()


def _setup_spec(tmp: Path, env: dict) -> None:
    (_mkrepo(tmp, "shop") / "src").mkdir()
    (tmp / "plain").mkdir()
    for name in ("api", "web"):
        _mkrepo(tmp, f"work/{name}")
    (tmp / "work" / "notes").mkdir()
    (tmp / "work" / "zz-not-a-repo").mkdir()
    (tmp / "empty").mkdir()
    (tmp / "fresh").mkdir()
    _git(tmp, "init", "-q", cwd=tmp / "fresh")


def _spec_scenario(name: str, about: str, routes: dict, steps: list, checks: list, covers: list) -> None:
    scenario(Scenario(name=name, suite="spec", about=about, tares={**MCP_ROUTE, **routes}, setup=_setup_spec,
                      steps=steps, checks=checks, covers=covers))


def _sstep(*argv, cwd: str = "{tmp}/shop/src", env: dict | None = None, stdin: str = "") -> Step:
    return Step(list(argv), env={**WITH_MCP, **(env or {})}, cwd=cwd, stdin=stdin, after=_claude_after)


NO_PROJECTS = {"GET /api/projects": (200, {"projects": []}), "GET /api/whoami": (200, {"person": None})}

_spec_scenario(
    "spec_repo", "`spec` opens the person's own Claude in the repo root, named after the idea, with this plugin",
    NO_PROJECTS,
    [_sstep("spec", "an order form for the bakery shop"),
     _sstep("spec", "--project", "Bakery orders", cwd="{tmp}/shop"),
     _sstep("spec", "with a token", cwd="{tmp}/shop", env={"TARES_AUTH_TOKEN": "spec-tok"})],
    [("the spec runs in the repo root", lambda r: _the_call(r, 0)["cwd"] == str(r.tmp / "shop")),
     ("the session is named after the idea", lambda r: _the_call(r, 0)["args"][1] == "an-order-form-for-spec"),
     ("it loads this plugin", lambda r: _flag_value(_the_call(r, 0), "--plugin-dir") == PLUGIN),
     ("it runs /tares:spec with the idea",
      lambda r: _the_call(r, 0)["args"][-1] == "/tares:spec an order form for the bakery shop"),
     ("the spec session turns the plugin on too", lambda r: _plugin_on(r, 0)),
     ("it is the person's own session, not a background one", lambda r: _plain_session(_the_call(r, 0))),
     ("it says the plan goes to Tares", lambda r: f"into a project on Tares ({r.tares.url})" in r.out(0)),
     ("the spec session is told the shared MCP endpoint",
      lambda r: _env_of(r, 0).get("TARES_FACTORY_MCP_URL") == r.tares.url + "/mcp"),
     ("without a token there is no token file to name", lambda r: "TARES_FACTORY_CRED_FILE" not in _env_of(r, 0)),
     ("nothing is written into the repo", lambda r: _repo_clean(r, "shop")),
     ("a project name names the session and goes to /tares:spec",
      lambda r: _the_call(r, 1)["args"][1] == "bakery-orders-spec"
      and _the_call(r, 1)["args"][-1] == "/tares:spec (project name: Bakery orders)"),
     ("with a token the spec session gets the token file under both names",
      lambda r: _env_of(r, 2).get("TARES_FACTORY_CRED_FILE") == str(r.crew / ".tares-token")
      and _env_of(r, 2).get("TARES_FACTORY_TOKEN_FILE") == str(r.crew / ".tares-token"))],
    ["the spec runs in the repo root", "the session is named after the idea", "it loads this plugin",
     "it runs /tares:spec with the idea", "the spec session turns the plugin on too",
     "it is the person's own session, not a background one", "it says the plan goes to Tares",
     "the spec session is told the shared MCP endpoint", "without a token there is no token file to name",
     "nothing is written into the repo", "a project name names the session and goes to /tares:spec",
     "with a token the spec session gets the token file under both names"])

_spec_scenario(
    "spec_down", "`spec` says how to start Tares, or an MCP server, and opens nothing",
    NO_PROJECTS,
    [_sstep("spec", "an order form", env={"TARESD_URL": "http://127.0.0.1:9"}),
     _sstep("spec", "an order form", env={"TARES_FACTORY_MCP_URL": "http://127.0.0.1:9/mcp"})],
    [("without Tares the spec says how to start one", lambda r: "no Tares answers" in r.out(0) and not r.calls()),
     ("without an MCP server the spec says how to start one",
      lambda r: "no MCP server answers at http://127.0.0.1:9/mcp" in r.out(1)
      and "tares mcp --transport streamable-http" in r.out(1) and not r.calls())],
    ["without Tares the spec says how to start one", "without an MCP server the spec says how to start one"])

_spec_scenario(
    "spec_folder", "`spec` runs in a folder that holds the repos, and refuses a folder with none or a repo with no commit",
    NO_PROJECTS,
    [_sstep("spec", "orders across api and web", cwd="{tmp}/work"),
     _sstep("spec", "nothing here", cwd="{tmp}/empty"),
     _sstep("spec", "x", cwd="{tmp}/fresh")],
    [("a folder of repos: the spec runs in the folder", lambda r: _the_call(r, 0)["cwd"] == str(r.tmp / "work")),
     ("and names the repos it holds", lambda r: "holds 2 repo(s): api, web" in r.out(0)),
     ("a folder with no repo is refused plainly",
      lambda r: r.code(1) != 0 and "inside the git repo the project builds on, or in a folder that holds its repos" in r.out(1)),
     ("a repo with no commit is refused plainly", lambda r: r.code(2) != 0 and "has no commit yet" in r.out(2))],
    ["a folder of repos: the spec runs in the folder", "and names the repos it holds",
     "a folder with no repo is refused plainly", "a repo with no commit is refused plainly"])

ASHISH = {"id": "u_ash", "name": "Ashish"}
SPEC_PROJECTS = {"projects": [
    {"id": "p_acme", "name": "Acme web", "kind": "software_factory", "lead": ASHISH},
    {"id": "p_nobody", "name": "Nobody leads", "kind": "software_factory", "lead": None},
    {"id": "p_notes", "name": "Plain notes", "lead": ASHISH},
    {"id": "p_secret", "name": "Secret plan", "hidden": True, "lead": ASHISH},
    {"id": "p_lead", "name": "Lead case", "kind": "software_factory", "lead": ASHISH}]}
WANT = "You are contributing to Acme web led by Ashish. Load the spec-project skill, section Contributor."


def _lead_flow(r: Run, i: int, name: str) -> bool:
    return (_the_call(r, min(i, len(r.calls()) - 1))["args"][-1] == f"/tares:spec (project name: {name})"
            and "TARES_PROJECT" not in _env_of(r, i) and "TARES_SESSION_KIND" not in _env_of(r, i)
            and "as a contributor" not in r.out(i))


_spec_scenario(
    "spec_contributor", "`spec` for a project someone else leads opens a contributor session; nothing else does",
    {"GET /api/projects": (200, SPEC_PROJECTS), "GET /api/whoami": (200, {"person": {"id": "u_kir", "name": "Kiran"}})},
    [_sstep("spec", "--project", "Acme web", cwd="{tmp}/plain"),
     _sstep("spec", "acme web"),
     _sstep("spec", "--project", "No such project"),
     _sstep("spec", "--project", "Nobody leads"),
     _sstep("spec", "--project", "Plain notes"),
     _sstep("spec", "--project", "Secret plan")],
    [("a contributor gets the contributor prompt, naming the project and its lead",
      lambda r: r.code(0) == 0 and _the_call(r, 0)["args"][-1] == WANT),
     ("the contributor session is bound to the project", lambda r: _env_of(r, 0).get("TARES_PROJECT") == "Acme web"),
     ("a contributor needs no git repo: it opens in the current folder, named for the project",
      lambda r: _the_call(r, 0)["cwd"] == str(r.tmp / "plain") and _the_call(r, 0)["args"][1] == "acme-web-spec"),
     ("the contributor session carries the contributor marker, so its hook does not say to build",
      lambda r: _env_of(r, 0).get("TARES_SESSION_KIND") == "contributor" and "FACTORY_ROLE" not in _env_of(r, 0)),
     ("a contributor is told it is a contributor session",
      lambda r: "as a contributor to Acme web, which Ashish leads" in r.out(0)),
     ("a contributor session does not run /tares:spec", lambda r: "/tares:spec" not in _the_call(r, 0)["args"][-1]),
     ("it loads this plugin and turns it on", lambda r: _plugin_on(r, 0)),
     ("the idea text finds the project too, and a contributor inside a repo opens at its root",
      lambda r: _the_call(r, 1)["args"][-1] == WANT and _the_call(r, 1)["cwd"] == str(r.tmp / "shop")),
     ("a contributor session writes nothing into the repo", lambda r: _repo_clean(r, "shop")),
     ("an unknown project name stays today's flow", lambda r: _lead_flow(r, 2, "No such project")),
     ("a project with no lead stays today's flow", lambda r: _lead_flow(r, 3, "Nobody leads")),
     ("a project that is not a software factory one, led by someone else, keeps the lead's flow",
      lambda r: _lead_flow(r, 4, "Plain notes")),
     ("a private project the caller is not on keeps the lead's flow", lambda r: _lead_flow(r, 5, "Secret plan"))],
    ["a contributor gets the contributor prompt, naming the project and its lead",
     "the contributor session is bound to the project",
     "a contributor needs no git repo: it opens in the current folder, named for the project",
     "the contributor session carries the contributor marker, so its hook does not say to build",
     "a contributor is told it is a contributor session", "a contributor session does not run /tares:spec",
     "it loads this plugin and turns it on",
     "the idea text finds the project too, and a contributor inside a repo opens at its root",
     "a contributor session writes nothing into the repo", "an unknown project name stays today's flow",
     "a project with no lead stays today's flow",
     "a project that is not a software factory one, led by someone else, keeps the lead's flow",
     "a private project the caller is not on keeps the lead's flow"])

_spec_scenario(
    "spec_lead", "the lead of the project, and a caller with no person, get the usual flow",
    {"GET /api/projects": (200, SPEC_PROJECTS), "GET /api/whoami": (200, {"person": ASHISH})},
    [_sstep("spec", "--project", "Acme web"),
     _sstep("spec", "--project", "Acme web", cwd="{tmp}/plain"),
     _sstep("spec", "--project", "Lead case")],
    [("the lead's session has no contributor marker", lambda r: "TARES_SESSION_KIND" not in _env_of(r, 0)),
     ("the lead keeps today's prompt",
      lambda r: _lead_flow(r, 0, "Acme web") and _the_call(r, 0)["cwd"] == str(r.tmp / "shop")),
     ("the lead outside a repo is still refused as today",
      lambda r: r.code(1) != 0 and "inside the git repo the project builds on" in r.out(1)),
     ("the lead of an existing project gets the usual prompt",
      lambda r: _lead_flow(r, 2, "Lead case") and _the_call(r, -1)["cwd"] == str(r.tmp / "shop"))],
    ["the lead's session has no contributor marker", "the lead keeps today's prompt",
     "the lead outside a repo is still refused as today", "the lead of an existing project gets the usual prompt"])

_spec_scenario(
    "spec_no_person", "a caller with no person (the root token) stays today's flow",
    {"GET /api/projects": (200, SPEC_PROJECTS), "GET /api/whoami": (200, {"person": None})},
    [_sstep("spec", "--project", "Acme web")],
    [("a caller with no person (the root token) stays today's flow", lambda r: _lead_flow(r, 0, "Acme web"))],
    ["a caller with no person (the root token) stays today's flow"])

for _route in ("/api/whoami", "/api/projects"):
    _routes = {"GET /api/projects": (200, SPEC_PROJECTS), "GET /api/whoami": (200, {"person": {"id": "u_kir"}})}
    _routes[f"GET {_route}"] = (500, "boom")
    _spec_scenario(
        "spec_failed_" + _route.rsplit("/", 1)[1], f"a failed {_route} lookup stays today's flow",
        _routes,
        [_sstep("spec", "--project", "Acme web"), _sstep("spec", "--project", "Acme web", cwd="{tmp}/plain")],
        [(f"a failed {_route} lookup stays today's flow", lambda r: _lead_flow(r, 0, "Acme web"))]
        + ([("and a failed lookup outside a repo is refused as today, not opened as a contributor",
             lambda r: r.code(1) != 0 and "inside the git repo the project builds on" in r.out(1))]
           if _route == "/api/whoami" else []),
        ["a failed $route lookup stays today's flow"]
        + (["and a failed lookup outside a repo is refused as today, not opened as a contributor"]
           if _route == "/api/whoami" else []))


SHOP = {"id": "p_shop", "name": "Shop", "kind": "software_factory", "status": "active"}
NOTES = {"id": "p_notes", "name": "Notes app", "kind": "software_factory", "status": "active"}
NOT_FACTORY = {"id": "p_nf", "name": "Not factory"}
LISTED = {"GET /api/projects": (200, {"projects": [SHOP, NOTES, NOT_FACTORY]})}
CREW_NONE = {"GET /api/crew/projects": (200, {"projects": [], "releasing": []})}
SHOP_REPO = {"GET /api/projects/p_shop/crew": (200, {"handover": {"repos": [{"path": "{tmp}/shop", "name": "acme/shop"}]}})}
HELD = {"GET /api/crew/projects": (200, {"projects": [{"id": "p_shop", "name": "Shop", "tickets_done": 1,
                                                       "tickets_total": 2, "milestone": None}], "releasing": []})}
RELEASING = {"GET /api/crew/projects": (200, {"projects": [], "releasing": [{"id": "p_shop", "name": "Shop"}]})}


def _setup_session(tmp: Path, env: dict) -> None:
    _mkrepo(tmp, "shop")
    (tmp / "elsewhere").mkdir()


def _session_scenario(name: str, about: str, routes: dict, steps: list, checks: list, covers: list) -> None:
    scenario(Scenario(name=name, suite="session", about=about, tares={**MCP_ROUTE, **routes}, setup=_setup_session,
                      steps=steps, checks=checks, covers=covers))


def _ss(*argv, cwd: str = "{tmp}/elsewhere", env: dict | None = None, stdin: str = "") -> Step:
    return _sstep(*argv, cwd=cwd, env=env, stdin=stdin)


def _last_arg(r: Run, i: int) -> str:
    return _the_call(r, i)["args"][-1]


_session_scenario(
    "session_start", "`session` opens the person's own Claude on a project, or on an idea, and names the choices",
    {**LISTED, **CREW_NONE},
    [_ss("session", "Notes app"),
     _ss("session", "notes-app"),
     _ss("session", "a tiny url shortener", cwd="{tmp}/shop"),
     _ss("session", "--new", "Shop", cwd="{tmp}/shop"),
     _ss("session", "Widgets", stdin="2\n"),
     _ss("session", "Widgets", stdin="1\n")],
    [("a planned project: the person's own session",
      lambda r: (r.snap(0) or {}).get("n") == 1 and _plain_session(_the_call(r, 0))),
     ("named after the project", lambda r: _the_call(r, 0)["args"][1] == "notes-app-session"),
     ("it loads this plugin and turns it on", lambda r: _plugin_on(r, 0)),
     ("it runs /tares:session with the project", lambda r: _last_arg(r, 0) == "/tares:session Notes app"),
     ("the session is bound to the project",
      lambda r: _env_of(r, 0).get("TARES_PROJECT") == "p_notes" and _env_of(r, 0).get("TARES_PROJECT_NAME") == "Notes app"
      and _env_of(r, 0).get("TARES_SESSION_NAME") == "notes-app-session"
      and _env_of(r, 0).get("TARES_SESSION_TAKEOVER", "") == ""),
     ("and told the Tares and its MCP endpoint",
      lambda r: _env_of(r, 0).get("TARESD_URL") == r.tares.url
      and _env_of(r, 0).get("TARES_FACTORY_MCP_URL") == r.tares.url + "/mcp"),
     ("with no repo of its own it opens where you are", lambda r: _the_call(r, 0)["cwd"] == str(r.tmp / "elsewhere")),
     ("the project's slug finds it too", lambda r: _last_arg(r, 1) == "/tares:session Notes app"),
     ("an idea: plan lightly and build here",
      lambda r: _last_arg(r, 2) == "/tares:session new: a tiny url shortener"
      and _the_call(r, 2)["args"][1] == "a-tiny-url-shortener-session"),
     ("not bound to a project yet", lambda r: _env_of(r, 2).get("TARES_PROJECT", "") == ""),
     ("it opens in the repo you are in", lambda r: _the_call(r, 2)["cwd"] == str(r.tmp / "shop")),
     ("it says the plan goes to Tares and the build happens here", lambda r: "builds it in the same" in r.out(2)),
     ("--new makes even a project name an idea", lambda r: _last_arg(r, 3) == "/tares:session new: Shop"),
     ("one unknown word: no, I meant a project, lists the open ones",
      lambda r: r.code(4) != 0 and "the open projects: " in r.out(4) and "Shop" in r.out(4)
      and "Not factory" not in r.out(4) and (r.snap(4) or {}).get("n") == (r.snap(3) or {}).get("n")),
     ("one unknown word: yes, a new project", lambda r: _last_arg(r, -1) == "/tares:session new: Widgets"),
     ("nothing is written into the repo", lambda r: _repo_clean(r, "shop"))],
    ["a planned project: the person's own session", "named after the project", "it loads this plugin and turns it on",
     "it runs /tares:session with the project", "the session is bound to the project",
     "and told the Tares and its MCP endpoint", "with no repo of its own it opens where you are",
     "the project's slug finds it too", "an idea: plan lightly and build here", "not bound to a project yet",
     "it opens in the repo you are in", "it says the plan goes to Tares and the build happens here",
     "--new makes even a project name an idea", "one unknown word: no, I meant a project, lists the open ones",
     "one unknown word: yes, a new project", "nothing is written into the repo"])

_session_scenario(
    "session_refuse", "`session` says how to start Tares, and a station may not open one",
    {**LISTED, **CREW_NONE},
    [_ss("session", "Shop", env={"TARESD_URL": "http://127.0.0.1:9"}),
     _ss("session", "Shop", env={"FACTORY_STATION": "crew-orchestrator"})],
    [("without Tares it says how to start one", lambda r: "no Tares answers" in r.out(0) and not r.calls()),
     ("a station may not open one",
      lambda r: r.code(1) != 0 and "only the person runs this" in r.out(1) and not r.calls())],
    ["without Tares it says how to start one", "a station may not open one"])

_session_scenario(
    "session_crew_keep", "a project the crew holds is left with it unless the person says yes",
    {**LISTED, **HELD, **SHOP_REPO, "POST /api/projects/p_shop/takeover": (200, {"ok": True})},
    [_ss("session", "Shop", stdin="\n")],
    [("the crew holds it: Enter leaves it with the crew",
      lambda r: "Left with the crew" in r.out(0) and "crew attach orchestrator" in r.out(0) and not r.calls()
      and not r.tares.asked("POST /api/projects/p_shop/takeover"))],
    ["the crew holds it: Enter leaves it with the crew"])

_session_scenario(
    "session_crew_take", "yes takes a project over from the crew, in the person's words, and opens its repo",
    {**LISTED, **HELD, **SHOP_REPO, "POST /api/projects/p_shop/takeover": (200, {"ok": True})},
    [_ss("session", "Shop", stdin="1\nsmall, I will finish it myself\n")],
    [("yes: taken over, in the person's words",
      lambda r: len(r.tares.asked("POST /api/projects/p_shop/takeover")) == 1
      and json.loads(r.tares.asked("POST /api/projects/p_shop/takeover")[0]["body"]) == {"words": "small, I will finish it myself"}),
     ("the session knows it took it over",
      lambda r: _last_arg(r, 0) == "/tares:session Shop (taken over from the crew)"
      and _env_of(r, 0).get("TARES_SESSION_TAKEOVER") == "1" and _env_of(r, 0).get("TARES_PROJECT") == "p_shop"),
     ("it opens in the project's repo", lambda r: _the_call(r, 0)["cwd"] == str(r.tmp / "shop"))],
    ["yes: taken over, in the person's words", "the session knows it took it over", "it opens in the project's repo"])

_session_scenario(
    "session_released", "a project being released carries on its takeover, and the menu marks it",
    {**LISTED, **RELEASING, **SHOP_REPO},
    [_ss("session", "Shop"),
     _ss("session", stdin="\n"),
     _ss("session", stdin="2\n"),
     _ss("session", stdin="3\na habit tracker\n")],
    [("while it is being released, a new session carries on the takeover without asking",
      lambda r: _last_arg(r, 0) == "/tares:session Shop (taken over from the crew)"
      and "was taken over from the crew" in r.out(0)),
     ("no argument lists the open factory projects",
      lambda r: "Shop (taken over, being released)" in r.out(1) and "Notes app" in r.out(1)
      and "something new" in r.out(1) and "Not factory" not in r.out(1)),
     ("picking a project opens it", lambda r: _last_arg(r, 2) == "/tares:session Notes app"),
     ("something new asks what to build", lambda r: _last_arg(r, 3) == "/tares:session new: a habit tracker")],
    ["while it is being released, a new session carries on the takeover without asking",
     "no argument lists the open factory projects", "picking a project opens it", "something new asks what to build"])


# ---------------------------------------------------------------------------------------------
# Normalising

ISO = re.compile(r"\d{4}-\d\d-\d\d[T ]\d\d:\d\d:\d\d(?:[.,]\d+)?(?:Z|[+-]\d\d:?\d\d)?")
# An epoch time is replaced only where it is a time: after a word that says so (`at`, `since`, `until`,
# `ts`, `time`, `epoch`, or a name ending in _at, _ts, _time, At), with a `=`, `:` or space between. A
# plain ten-digit number elsewhere (a port, a count, an id) is kept.
EPOCH = re.compile(r"(?P<pre>(?<![\w.])(?:at|since|until|epoch|ts|time|\w*_at|\w*_ts|\w*_time|\w*At)[\"']?\s*[=:]?\s*[\"']?)"
                   r"1[5-9]\d{8}(?:\d{3})?(?![\d.])")
EPOCH_VALUE = re.compile(r"1[5-9]\d{8}(?:\d{3})?")
# A fake Tares listens on a free port, a different one on each side
PORT = re.compile(r"127\.0\.0\.1:\d{4,5}\b")
UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")
SESSION = re.compile(r"\bsession-\d+\b")
# A job id (eight hex digits) is replaced only where a job id is printed: after `backgrounded ·`, or after
# job, session, id, stop or rm. In JSON, a string under an id key is replaced whole (see ID_KEYS).
JOB_ID = re.compile(r"(?P<pre>\bbackgrounded[^0-9a-f\n]+|\b(?:job|session|id|stop|rm)[ =:]+[\"']?)[0-9a-f]{8}\b")
HEX8 = re.compile(r"[0-9a-f]{8}")
PID_TEXT = re.compile(r"\b(pid|PID)([ =:]+)\d+")
# The port of a throwaway fake Tares (an ephemeral port: five digits); 8787, 8788 and the like stay.
PORT = re.compile(r"(?<=127\.0\.0\.1:)\d{5}\b")
PID_KEYS = {"pid", "ppid", "watch_pid", "watchPid"}
ID_KEYS = {"sessionId", "session_id", "id"}
TIME_KEY = re.compile(r"(^(ts|time|at|since|until)$)|(_at$)|(At$)|(_ts$)|(_time$)", re.I)


def norm_text(s: str, tmp: Path | None = None) -> str:
    """Text with what changes from run to run replaced: the temp folder, timestamps, pids, ids."""
    s = s.replace("\r\n", "\n")
    if tmp is not None:
        forms = {str(tmp), os.path.realpath(tmp), tmp.as_posix(), Path(os.path.realpath(tmp)).as_posix()}
        # a Windows path inside JSON text has its backslashes doubled; and a drive letter may change case
        forms |= {f.replace("\\", "\\\\") for f in forms if "\\" in f}
        flags = re.IGNORECASE if is_windows() else 0
        for p in sorted(forms, key=len, reverse=True):
            s = re.sub(re.escape(p), "<TMP>", s, flags=flags)
    s = PORT.sub("127.0.0.1:<PORT>", s)
    s = UUID.sub("<ID>", s)
    s = ISO.sub("<TS>", s)
    s = EPOCH.sub(lambda m: m.group("pre") + "<TS>", s)
    s = PID_TEXT.sub(lambda m: f"{m.group(1)}{m.group(2)}<PID>", s)
    s = SESSION.sub("<ID>", s)
    s = PORT.sub("<PORT>", s)
    s = JOB_ID.sub(lambda m: m.group("pre") + "<ID>", s)
    return s


def norm_json(v, tmp: Path | None = None, key: str = ""):
    """A parsed JSON value with its strings normalised and numbers under pid or time keys replaced.
    Key order does not matter: it is sorted when rendered."""
    if isinstance(v, dict):
        return {k: norm_json(x, tmp, k) for k, x in v.items()}
    if isinstance(v, list):
        return [norm_json(x, tmp, key) for x in v]
    if isinstance(v, str):
        if key in ID_KEYS and HEX8.fullmatch(v):
            return "<ID>"
        if TIME_KEY.search(key) and EPOCH_VALUE.fullmatch(v):
            return "<TS>"
        return norm_text(v, tmp)
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        if key in PID_KEYS:
            return "<PID>"
        if TIME_KEY.search(key) or key == "startedAt":
            return "<TS>"
    return v


def _settings_arg(row):
    """A call's `--settings <file>`: the file is a temp name each engine makes its own way, so it is replaced.
    A value that is JSON text, not a file, is kept."""
    args = row.get("args") if isinstance(row, dict) else None
    if isinstance(args, list):
        for i, a in enumerate(args[:-1]):
            if a == "--settings" and isinstance(args[i + 1], str) and not args[i + 1].lstrip().startswith("{"):
                args[i + 1] = "<SETTINGS-FILE>"
    return row


def render_json(v) -> str:
    return json.dumps(v, indent=2, sort_keys=True, ensure_ascii=False)


def norm_file(text: str, name: str, tmp: Path | None = None) -> str:
    """A file's content, normalised: a .json file or .jsonl file as sorted-key JSON, anything else as text."""
    try:
        if name.endswith(".jsonl"):
            rows = [_settings_arg(norm_json(json.loads(x), tmp)) for x in text.splitlines() if x.strip()]
            return "\n".join(json.dumps(r, sort_keys=True, ensure_ascii=False) for r in rows)
        if name.endswith(".json"):
            return render_json(norm_json(json.loads(text), tmp))
    except ValueError:
        pass
    return norm_text(text, tmp).rstrip("\n")


def dropped(text: str, patterns: tuple) -> str:
    """text without the lines that match one of the regexes (a step's `drop`)."""
    if not patterns:
        return text
    res = [re.compile(p) for p in patterns]
    return "\n".join(ln for ln in text.split("\n") if not any(r.search(ln) for r in res))


def norm_output(text: str, tmp: Path | None = None) -> str:
    """printed output: a whole-output JSON document gets sorted keys, any other text is normalised as is."""
    stripped = text.strip()
    if stripped[:1] in "{[":
        try:
            return render_json(norm_json(json.loads(stripped), tmp))
        except ValueError:
            pass
    return norm_text(text, tmp).rstrip("\n")


# ---------------------------------------------------------------------------------------------
# Running one side

def command_for(engine: str, sc: "Scenario | None" = None) -> list:
    if sc is not None and sc.driver:
        return [sys.executable, str(HERE / sc.driver), engine]
    override = os.environ.get("PARITY_CMD_" + engine.upper())
    return shlex.split(override) if override else [sys.executable, "-m", "tares.crew"]


def is_windows() -> bool:
    """A function so a test can patch it."""
    return sys.platform == "win32"


def bash_available() -> str:
    """"" when the bash engine can run here, else the reason it cannot."""
    if is_windows():
        return "needs bash, which is not available on Windows"
    if not shutil.which("bash") or not shutil.which("jq"):
        return "needs bash and jq on PATH"
    return ""


def make_launcher(path: Path, extra: list, script: Path | None = None) -> Path:
    """An executable that runs the Python fake: a shell script, or a .cmd file on Windows (returns its path).

    Named `claude`, it is the Claude Code of the scenario: on PATH for a command that runs `claude` by
    name (the bash engine does for some commands, the Python engine will), and CLAUDE_BIN for the rest.
    The .cmd form passes the fake's exit code on, and is written with CRLF whatever the platform.
    The paths in it must be ASCII: cmd.exe reads a batch file in the console's code page."""
    if sys.platform == "win32":
        path = path.with_suffix(".cmd")
        lines = ['@"%s" "%s" %s %%*' % (sys.executable, script or FAKE_CLAUDE, " ".join(extra)), "@exit /b %ERRORLEVEL%"]
        path.write_bytes(("\r\n".join(lines) + "\r\n").encode("utf-8"))
    else:
        path.write_text('#!/bin/sh\nexec "%s" "%s" %s "$@"\n' % (sys.executable, script or FAKE_CLAUDE, " ".join(extra)), encoding="utf-8")
        path.chmod(0o755)
    return path


# What the person's own shell must not lend a scenario: the crew's and Tares' settings, Claude Code's
# config and job folders, the XDG folders and GitHub's CLI settings (all of them move state or a login).
STRIPPED_PREFIXES = ("FACTORY_", "TARES_", "CLAUDE_PLUGIN_OPTION_", "PARITY_", "FAKE_CLAUDE_",
                     "CLAUDE_CONFIG_DIR", "CLAUDE_JOB_DIR", "XDG_", "GH_")


def crew_env(tmp: Path, engine: str) -> dict:
    """The environment of a throwaway crew: everything under tmp, the Python fake for Claude Code, no
    station or Tares settings from the person's own shell."""
    home, crew, state, bindir = (tmp / n for n in ("home", "crew", "claude", "bin"))
    for d in (home, state, bindir):
        d.mkdir(parents=True, exist_ok=True)
    claude = make_launcher(bindir / "claude", [])  # claude.cmd on Windows, which is also what `claude` finds on PATH
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(STRIPPED_PREFIXES)
           and k not in ("TARESD_URL", "NAVFLOW_DB", "PWD", "OLDPWD")}
    env.update(
        HOME=str(home), USERPROFILE=str(home), FACTORY_CREW_HOME=str(crew), FAKE_CLAUDE_STATE=str(state),
        CLAUDE_BIN=str(claude), FACTORY_PAUSE="0", FACTORY_NO_WATCH="1",
        GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t",
        GIT_CONFIG_GLOBAL=str(tmp / "gitconfig"), GIT_CONFIG_NOSYSTEM="1",
        PYTHONPATH=str(ROOT), PYTHONIOENCODING="utf-8", TARES_FACTORY_ENGINE=engine,
        TARES_HOME=str(tmp / "tares-home"),
        PATH=str(bindir) + os.pathsep + os.environ.get("PATH", ""),  # `claude` by name finds the fake first
    )
    (tmp / "gitconfig").write_text("[user]\n\tname = Test Crew\n")
    return env


def _without_calls(text: str, prefixes: tuple) -> str:
    """calls.jsonl without the calls whose arguments start with one of the prefixes."""
    keep = []
    for line in text.splitlines():
        try:
            args = json.loads(line).get("args") or []
        except ValueError:
            args = []
        if not any(list(args[:len(pre)]) == list(pre) for pre in prefixes):
            keep.append(line)
    return "\n".join(keep) + ("\n" if keep else "")


def snapshot(run: Run) -> dict:
    """The files a scenario leaves: the crew home, and what the fake Claude Code recorded."""
    files = {}
    for base, prefix, only in ((run.crew, "crew/", None), (run.state, "claude/", {"calls.jsonl", "agents.json"})):
        if not base.is_dir():
            continue
        for p in sorted(base.rglob("*")):
            if p.is_file() and (only is None or p.name in only):
                rel = prefix + p.relative_to(base).as_posix()
                try:
                    text = p.read_text(encoding="utf-8", errors="replace")
                    if p.name == "calls.jsonl" and run.ignore_calls:
                        text = _without_calls(text, run.ignore_calls)
                    files[rel] = norm_file(text, p.name, run.tmp)
                except OSError:
                    files[rel] = "<unreadable>"
    return files


@dataclass
class Side:
    engine: str
    skip: str = ""                  # why it did not run ("" when it did)
    passed: list = field(default_factory=list)
    failed: list = field(default_factory=list)
    text: str = ""                  # the normalised record of the run, for the diff
    raw: str = ""                   # the first failing step's output, for the person reading


def run_side(sc: Scenario, engine: str) -> Side:
    side = Side(engine)
    if engine == "bash" and not os.environ.get("PARITY_CMD_BASH"):
        why = bash_available()
        if why:
            side.skip = why
            return side
    tmp = Path(tempfile.mkdtemp(prefix=f"parity-{sc.name}-{engine}-")).resolve()
    fake = None
    try:
        env = crew_env(tmp, engine)
        results = []
        if sc.tares is not None:
            fake = FakeTares(routes_in(sc.tares, tmp))
            env["TARESD_URL"] = fake.url
        if sc.setup:
            sc.setup(tmp, env)
        here = lambda text: (text.replace("{tmp}", str(tmp)).replace("{tares}", fake.url if fake else "")
                             if isinstance(text, str) else text)
        for step in sc.steps:
            if step.before:
                step.before(tmp)
            senv = {**env, **{k: here(v) for k, v in step.env.items()}}
            try:
                p = subprocess.run(command_for(engine, sc) + [here(a) for a in step.argv],
                                   cwd=here(step.cwd) or str(ROOT), env=senv,
                                   input=step.stdin, capture_output=True, text=True, encoding="utf-8",
                                   errors="replace", timeout=STEP_TIMEOUT)
                results.append({"code": p.returncode, "stdout": p.stdout, "stderr": p.stderr})
            except subprocess.TimeoutExpired:
                results.append({"code": 124, "stdout": "", "stderr": f"parity: step timed out after {STEP_TIMEOUT}s\n"})
            if step.after:
                results[-1]["snap"] = step.after(tmp)
            if engine == "python" and results[-1]["code"] == 2 and NOT_PORTED in results[-1]["stderr"]:
                side.skip = "not ported"
                return side
        run = Run(tmp, results, fake)
        run.ignore_calls = sc.ignore_calls
        for label, fn in sc.checks:
            try:
                ok = bool(fn(run))
            except Exception as e:  # a check that cannot look is a check that fails
                ok = False
                label = f"{label} ({type(e).__name__}: {e})"
            (side.passed if ok else side.failed).append(label)
        if os.environ.get("PARITY_FAIL") in (engine, "both"):
            side.failed.append(f"deliberate failure (PARITY_FAIL={os.environ['PARITY_FAIL']})")
        blocks = []
        for step, r in zip(sc.steps, results):
            blocks.append(f"== step: {' '.join(step.argv)} ==\nexit: {r['code']}\n"
                          f"-- stdout --\n{dropped(norm_output(r['stdout'], tmp), step.drop)}\n"
                          f"-- stderr --\n{dropped(norm_output(r['stderr'], tmp), step.drop)}")
            if r.get("snap") is not None:  # what the step's `after` read (compared too)
                blocks.append(f"-- after --\n{render_json(norm_json(r['snap'], tmp))}")
        for rel, content in snapshot(run).items():
            blocks.append(f"== file {rel} ==\n{content}")
        if fake is not None:  # what each engine asked Tares, and whether it sent a key (never the key)
            # A read asked twice in a row counts once: bash asks again from a subshell that lost its cache.
            asked = []
            for q in fake.requests:
                line = f"{q['route']} key={'yes' if q['auth'] else 'no'} {norm_output(q['body'], tmp)}"
                if not (asked and asked[-1] == line and q["route"].startswith("GET ")):
                    asked.append(line)
            blocks.append("== asked of Tares ==\n" + "\n".join(asked))
        side.text = "\n".join(blocks) + "\n"
        if fake is not None:  # the fake's port is different on each side
            side.text = side.text.replace(fake.url, "<TARES>")
        side.raw = "\n".join(f"$ tares factory {' '.join(s.argv)}  (exit {r['code']})\n{r['stdout']}{r['stderr']}"
                             for s, r in zip(sc.steps, results))
        return side
    finally:
        if fake is not None:
            fake.stop()
        shutil.rmtree(tmp, ignore_errors=True)


def run_scenario(sc: Scenario, out=print) -> int:
    """Run sc on both engines, print the result, return the exit code (0 or 1)."""
    return evaluate(sc, out)[0]


def evaluate(sc: Scenario, out=print) -> tuple:
    """Run sc on both engines and print the result: (exit code, whether it passes on the Python side)."""
    out(f"== {sc.name}: {sc.about or sc.suite} ==")
    sides = {e: run_side(sc, e) for e in ENGINES}
    code = 0
    for e in ENGINES:
        s = sides[e]
        if s.skip:
            out(f"SKIP {e}: {s.skip}")
        elif s.failed:
            code = 1
            out(f"FAIL {e}: {len(s.failed)} of {len(s.failed) + len(s.passed)} checks failed")
            for label in s.failed:
                out(f"  FAIL {label}")
            out(s.raw.rstrip("\n"))
        else:
            out(f"PASS {e}: {len(s.passed)} checks")
    b, p = sides["bash"], sides["python"]
    if b.skip and not is_windows():
        # Bash is the reference. Only Windows has none: elsewhere a missing bash or jq must not turn
        # "matches bash" into "the Python engine ran".
        code = 1
        out(f"ERROR bash: the bash side cannot run on this host ({b.skip}); there is nothing to compare "
            "the Python side with")
    if not b.skip and not p.skip and not b.failed and not p.failed and b.text != p.text:
        code = 1
        out("FAIL python: output differs from bash (normalised), bash first")
        out("".join(difflib.unified_diff(b.text.splitlines(True), p.text.splitlines(True), "bash", "python")).rstrip("\n"))
    elif not b.skip and not p.skip and not b.failed and not p.failed:
        out("MATCH: bash and python print and write the same")
    return code, (not p.skip and not p.failed and code == 0)


# ---------------------------------------------------------------------------------------------
# Coverage

CHECK_LINE = re.compile(r'^\s*check\s+"((?:[^"\\]|\\.)*)"')


def suites() -> list:
    return [ln.strip() for ln in CI_LIST.read_text().splitlines() if ln.strip() and not ln.lstrip().startswith("#")]


def suite_scenarios() -> dict:
    """suite -> its scenarios: the labels of its `check "<label>"` lines (see the module docstring)."""
    out = {}
    for suite in suites():
        labels = []
        for line in (HERE / f"{suite}_test.sh").read_text(encoding="utf-8").splitlines():
            m = CHECK_LINE.match(line)
            if m:
                labels.append(m.group(1))
        out[suite] = labels or [f"(the whole {suite} suite)"]
    return out


def coverage(require_full: bool, out=print) -> int:
    found = suite_scenarios()
    passing: dict = {}  # (suite, label) -> scenario name, for scenarios that pass against Python now
    bad = 0
    for sc in SCENARIOS.values():
        quiet: list = []
        ok = evaluate(sc, out=quiet.append)[1]
        for label in sc.covers:
            if label not in found.get(sc.suite, []):
                out(f"ERROR scenario {sc.name} covers a check that is not in {sc.suite}_test.sh: {label}")
                bad = 1
            elif ok:
                passing[(sc.suite, label)] = sc.name
    total = done = 0
    for suite, labels in found.items():
        n = sum(1 for label in labels if (suite, label) in passing)
        total += len(labels)
        done += n
        out(f"{suite}: {n}/{len(labels)}")
        for label in labels:
            mark = f"PASS python  ({passing[(suite, label)]})" if (suite, label) in passing else "todo"
            out(f"  {mark:<28} {label}")
    pct = 100.0 * done / total if total else 100.0
    out(f"coverage: {done} of {total} scenarios pass against Python ({pct:.1f}%) in {len(found)} suites")
    if require_full and done < total:
        out(f"FAIL: --require-full wants every scenario ported, {total - done} are not")
        return 1
    return bad


def main(argv: list) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass
    args = [a for a in argv if not a.startswith("--")]
    flags = {a for a in argv if a.startswith("--")}
    if flags - {"--coverage", "--require-full", "--list"}:
        print(__doc__)
        return 2
    if "--list" in flags:
        print("\n".join(SCENARIOS))
        return 0
    if "--coverage" in flags:
        return coverage("--require-full" in flags)
    if "--require-full" in flags:
        print("parity: --require-full goes with --coverage", file=sys.stderr)
        return 2
    if not args:
        print("usage: parity.py <scenario>... | --list | --coverage [--require-full]", file=sys.stderr)
        return 2
    names: list = []
    for pattern in args:  # `tidy_*` runs every tidy scenario; several patterns run the union, once each
        found = [n for n in SCENARIOS if fnmatch.fnmatchcase(n, pattern)]
        if not found:
            print(f"parity: no scenario {pattern!r}; known: {', '.join(SCENARIOS)}", file=sys.stderr)
            return 2
        names += [n for n in found if n not in names]
    return max(run_scenario(SCENARIOS[n]) for n in names)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
